"""执行器：跑 `tool` 与 `http` 两类动作。

## tool 动作走的是 MCP Streamable HTTP

一次调用 = `initialize` → (`notifications/initialized`) → `tools/call`，都走
同一个 `POST {base_url}/mcp`。为什么不用官方 SDK：

1. 验收要看的恰恰是**协议层事实**（HTTP 状态码、`isError`、`structuredContent`），
   SDK 会把这些包起来，反而不便于断言；
2. SDK 的版本与四个服务各自钉的版本不一定一致，验收工具不该引入这种耦合；
3. 这条链路很短（两次 POST），用 httpx 直接写比读 SDK 的会话状态机更可解释。

响应既可能是 `application/json`，也可能是 SSE（`event: message\\ndata: {...}`）——
两种都解析。SSE 里可能有多帧（进度通知 + 最终结果），取 id 匹配的那一帧。

## 失败不中断

单条用例的任何异常（超时、连接拒绝、响应不是 JSON）都收敛成一条 `fail`，
带 `transport_error` 说明，然后继续下一条。理由：故障现场往往同时有多处不对，
第一条就抛出来只会得到 1/N 的信息量。

## 计时口径

`elapsed_ms` 只计 **tools/call 那一次请求**（含为它做的 429 重试休眠），与 SLO 的口径一致；
`max_elapsed_ms` 断言的就是它。initialize 不计入。

## 为什么要处理 429

验收套件是**突发流量**：几十条用例在十几秒内打完，而服务端的限流是按分钟算的
（mcp-kit 默认 120/min、burst 30）。第一版实跑 某个服务 58 条时，第 27 条起全部
`rate_limited`，看起来像「23 条业务用例都不对」—— 其实是验收工具自己把配额打满了。

所以：

1. 一个 Runner 只做**一次** initialize（真实 MCP 客户端也是长会话），请求数从 3N 降到 N+1；
2. 遇到 429 按 `Retry-After` 重试（默认 5 次）；
3. **但**如果这条用例本身就在断言 429 / rate_limited，就不重试 —— 否则「限流生效」
   这条能力永远测不出来。

重试仍失败时记 `transport_error`（说明多半是验收工具的流量造成），不伪装成通过。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx

from . import __version__
from .assertions import AssertionResult, Observation, check_expect
from .loader import Case, Suite

MCP_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "mcp-test-suite", "version": __version__}
_RATE_LIMIT_HINTS = ("rate_limited", "too many requests")


@dataclass
class CaseResult:
    case: Case
    status: str                     # pass | fail | todo | skip
    elapsed_ms: int
    assertions: List[AssertionResult]
    observation: Optional[Observation] = None
    error: Optional[str] = None
    retries: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.case.id,
            "title": self.case.title,
            "layer": self.case.layer,
            "priority": self.case.priority,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "retries": self.retries,
            "goal": self.case.goal,
            "metric": self.case.metric,
            "regression": list(self.case.regression),
            "action": self.case.action.to_dict(),
            "assertions": [a.to_dict() for a in self.assertions],
            "error": self.error,
        }


class Runner:
    """一个 Runner 对应一次运行（base_url / 令牌 / 超时都固定）。"""

    def __init__(
        self,
        suite: Suite,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        admin_token: Optional[str] = None,
        consumer: Optional[str] = None,
        timeout_s: Optional[float] = None,
        retry_429: int = 5,
        pace_ms: float = 0.0,
    ):
        self.suite = suite
        self.base_url = (base_url or suite.defaults.base_url).rstrip("/")
        self.token = token
        self.admin_token = admin_token
        self.consumer = consumer
        self.timeout_s = float(timeout_s or suite.defaults.timeout_s)
        self.retry_429 = max(0, int(retry_429))
        self.pace_ms = max(0.0, float(pace_ms))
        self._client: Optional[httpx.Client] = None
        self._session_headers: Optional[Dict[str, str]] = None
        self._last_request_at = 0.0
        self._last_elapsed_ms = 0
        self.total_retries = 0

    # ------------------------------------------------------------------ 生命周期
    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.timeout_s), follow_redirects=False
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._session_headers = None

    # ------------------------------------------------------------------ 入口
    def run(self, cases: Sequence[Case], progress=None) -> List[CaseResult]:
        results: List[CaseResult] = []
        self._ensure_client()
        try:
            for case in cases:
                if case.status == "todo":
                    results.append(CaseResult(case=case, status="todo", elapsed_ms=0, assertions=[]))
                    if progress:
                        progress(results[-1])
                    continue
                if case.status == "skip":
                    results.append(CaseResult(case=case, status="skip", elapsed_ms=0, assertions=[]))
                    if progress:
                        progress(results[-1])
                    continue
                result = self.run_case(case)
                results.append(result)
                if progress:
                    progress(result)
        finally:
            self.close()
        return results

    def run_case(self, case: Case) -> CaseResult:
        started = time.monotonic()
        retries_before = self.total_retries
        try:
            obs = self._execute(case)
        except Exception as exc:  # noqa: BLE001 - 单条用例的任何异常都不能中断整轮
            return CaseResult(
                case=case,
                status="fail",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                assertions=[],
                error=f"{type(exc).__name__}: {exc}",
                retries=self.total_retries - retries_before,
            )

        assertions = check_expect(case.expect, obs)
        ok = bool(assertions) and all(a.ok for a in assertions) and not obs.transport_error
        return CaseResult(
            case=case,
            status="pass" if ok else "fail",
            elapsed_ms=obs.elapsed_ms,
            assertions=assertions,
            observation=obs,
            error=obs.transport_error,
            retries=self.total_retries - retries_before,
        )

    # ------------------------------------------------------------------ 动作分发
    def _execute(self, case: Case) -> Observation:
        if case.action.kind == "tool":
            return self._run_tool(case)
        return self._run_http(case)

    # ------------------------------------------------------------------ tool
    def _run_tool(self, case: Case) -> Observation:
        action = case.action
        obs = Observation(kind="tool", tool=action.tool, consumer=self.consumer)
        if not self.token:
            obs.transport_error = "没有可用的业务令牌（token_env 未设置）"
            obs.is_error = True
            return obs

        url = f"{self.base_url}/mcp"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }

        # ---- 只做一次 initialize（见模块文档：请求数 3N → N+1）----
        if self._session_headers is None:
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": _CLIENT_INFO,
                },
            }
            resp, retries, _, err = self._post(url, init, headers, case)
            obs.retries = retries
            if resp is None:
                obs.transport_error = err
                obs.is_error = True
                return obs
            if resp.status_code >= 400:
                payloads = _parse_payloads(resp)
                doc = payloads[-1] if payloads else None
                obs.status = resp.status_code
                obs.text = resp.text
                obs.is_error = True
                obs.json_doc = doc
                obs.error_type = _error_type_of(doc)
                return obs
            session_id = resp.headers.get("mcp-session-id")
            self._session_headers = dict(headers)
            if session_id:
                obs.session_id = session_id
                self._session_headers["Mcp-Session-Id"] = session_id
            # 协议要求 initialize 之后发 initialized 通知；服务端多为 stateless，best-effort。
            self._post(
                url,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                self._session_headers,
                case,
            )
        headers = self._session_headers

        # ---- tools/call ----
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": action.tool, "arguments": action.arguments},
        }
        resp, retries, _, err = self._post(url, call, headers, case, measure=True)
        obs.retries = retries
        obs.elapsed_ms = self._last_elapsed_ms
        if resp is None:
            obs.transport_error = err
            obs.is_error = True
            return obs
        obs.status = resp.status_code
        obs.text = resp.text
        payloads = _parse_payloads(resp)
        doc = _pick_payload(payloads, 2)
        obs.json_doc = doc
        if resp.status_code >= 400:
            obs.is_error = True
            obs.error_type = _error_type_of(doc)
            if resp.status_code == 429 and not _expects_rate_limit(case) and self.retry_429:
                obs.transport_error = (
                    f"被服务限流（429）且重试 {retries} 次后仍失败；"
                    f"这通常是验收工具自身的突发流量造成的，不是用例本身的问题"
                )
            return obs
        _apply_tool_result(obs, doc)
        return obs

    # ------------------------------------------------------------------ http
    def _run_http(self, case: Case) -> Observation:
        action = case.action
        assert action.method and action.path
        obs = Observation(kind="http", method=action.method, path=action.path, consumer=self.consumer)

        mode = action.resolved_auth()
        headers: Dict[str, str] = {"Accept": "application/json, text/event-stream"}
        headers.update(action.headers or {})
        token: Optional[str] = None
        if mode == "business":
            token = self.token
        elif mode == "admin":
            token = self.admin_token
        if mode != "none":
            if not token:
                obs.transport_error = (
                    f"该请求需要{'管理' if mode == 'admin' else '业务'}令牌，但环境变量里没有"
                )
                obs.is_error = True
                return obs
            headers.setdefault("Authorization", f"Bearer {token}")

        kwargs: Dict[str, Any] = {}
        if action.body is not None:
            headers.setdefault("Content-Type", "application/json")
            if isinstance(action.body, str):
                kwargs["content"] = action.body.encode("utf-8")
            else:
                kwargs["json"] = action.body

        url = f"{self.base_url}{action.path}"
        t0 = time.monotonic()
        resp, retries, _, err = self._post(
            url, None, headers, case, method=action.method, kwargs=kwargs
        )
        obs.retries = retries
        obs.elapsed_ms = int((time.monotonic() - t0) * 1000)
        if resp is None:
            obs.transport_error = err
            obs.is_error = True
            return obs

        obs.status = resp.status_code
        obs.text = resp.text
        payloads = _parse_payloads(resp)
        doc = payloads[-1] if payloads else _plain_json(resp.text)
        obs.json_doc = doc
        if isinstance(doc, Mapping):
            obs.is_error = bool(doc.get("isError", False))
            obs.error_type = _error_type_of(doc)
            rows = doc.get("rows")
            obs.rows = rows if isinstance(rows, list) else None
        if resp.status_code == 429 and not _expects_rate_limit(case) and self.retry_429:
            obs.transport_error = (
                f"被服务限流（429）且重试 {retries} 次后仍失败；这通常是验收工具的突发流量造成的"
            )
        return obs

    # ------------------------------------------------------------------ HTTP 底层
    def _post(
        self,
        url: str,
        body: Optional[Mapping[str, Any]],
        headers: Dict[str, str],
        case: Case,
        *,
        method: str = "POST",
        kwargs: Optional[Dict[str, Any]] = None,
        measure: bool = False,
    ) -> Tuple[Optional[httpx.Response], int, Optional[int], str]:
        """发一个请求，429 时按 Retry-After 重试。

        返回 `(response | None, retries_used, status, error_text)`。`response is None`
        表示传输层失败（超时/连接拒绝），`error_text` 就是原因。
        """
        client = self._ensure_client()
        retries = 0
        allow_retry = self.retry_429 > 0 and not _expects_rate_limit(case)
        # 计时从第一次尝试开始：429 重试的休眠也要算进这条用例的耗时，
        # 否则「重试了 3 次才成功」会被报告成「只花了 70ms」。
        t_start = time.monotonic()
        while True:
            self._pace()
            try:
                if kwargs is None:
                    resp = client.request(method, url, json=body, headers=headers)
                else:
                    resp = client.request(method, url, headers=headers, **kwargs)
            except httpx.TimeoutException:
                return None, retries, None, f"{method} {_path(url)} 超时（{self.timeout_s:g}s）"
            except httpx.HTTPError as exc:
                return None, retries, None, f"{method} {_path(url)} 请求失败：{type(exc).__name__}: {exc}"
            finally:
                self._last_request_at = time.monotonic()
                if measure:
                    self._last_elapsed_ms = int((time.monotonic() - t_start) * 1000)

            if resp.status_code == 429 and allow_retry and retries < self.retry_429:
                retries += 1
                self.total_retries += 1
                time.sleep(_retry_after(resp))
                continue
            return resp, retries, resp.status_code, ""

    def _pace(self) -> None:
        """可选的节流：两次请求之间至少间隔 pace_ms（默认 0，靠 429 重试兜底）。"""
        if self.pace_ms <= 0:
            return
        wait = self.pace_ms / 1000.0 - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)


# --------------------------------------------------------------------------- 解析
def _parse_payloads(resp: httpx.Response) -> List[Any]:
    raw = resp.text or ""
    ctype = (resp.headers.get("content-type") or "").lower()
    looks_sse = raw.lstrip().startswith(("event:", "data:", ":"))
    if "text/event-stream" in ctype or looks_sse:
        return _parse_sse(raw)
    doc = _plain_json(raw)
    return [doc] if doc is not None else []


def _parse_sse(raw: str) -> List[Any]:
    """把 SSE 拆成若干 JSON payload。

    只认 `data:` 行；一帧里的多行 data 按规范用 `\\n` 连接。解析不出来的帧
    （例如服务端的心跳/空注释）直接跳过 —— 它们不是错误。
    """
    out: List[Any] = []
    data_lines: List[str] = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line.strip() == "":
            if data_lines:
                _flush_sse(data_lines, out)
                data_lines = []
    if data_lines:
        _flush_sse(data_lines, out)
    return out


def _flush_sse(lines: List[str], out: List[Any]) -> None:
    body = "\n".join(lines).strip()
    if not body:
        return
    try:
        out.append(json.loads(body))
    except ValueError:
        return


def _plain_json(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _pick_payload(payloads: Sequence[Any], request_id: int) -> Any:
    """优先取 id 匹配的那一帧；没有 id 字段的（通知）跳过。"""
    for doc in reversed(payloads):
        if isinstance(doc, Mapping) and doc.get("id") == request_id:
            return doc
    for doc in reversed(payloads):
        if isinstance(doc, Mapping) and ("result" in doc or "error" in doc):
            return doc
    return payloads[-1] if payloads else None


def _error_type_of(doc: Any) -> Optional[str]:
    if not isinstance(doc, Mapping):
        return None
    if isinstance(doc.get("error_type"), str):
        return doc["error_type"]
    err = doc.get("error")
    if isinstance(err, Mapping) and isinstance(err.get("data"), Mapping):
        if isinstance(err["data"].get("error_type"), str):
            return err["data"]["error_type"]
    result = doc.get("result")
    if isinstance(result, Mapping) and isinstance(result.get("error_type"), str):
        return result["error_type"]
    return None


def _apply_tool_result(obs: Observation, doc: Any) -> None:
    """把 JSON-RPC 响应落成可判定事实。"""
    if not isinstance(doc, Mapping):
        obs.transport_error = "响应不是 JSON-RPC（既不是 JSON 也不是 SSE data 帧）"
        obs.is_error = True
        return

    if "error" in doc and doc.get("error"):
        err = doc["error"]
        obs.is_error = True
        obs.error_type = _error_type_of(doc)
        if isinstance(err, Mapping):
            obs.text = str(err.get("message") or json.dumps(err, ensure_ascii=False))
        else:
            obs.text = str(err)
        return

    result = doc.get("result")
    if not isinstance(result, Mapping):
        obs.transport_error = "JSON-RPC 响应里没有 result 对象"
        obs.is_error = True
        return

    structured = result.get("structuredContent")
    obs.structured = structured
    obs.rows = structured.get("rows") if isinstance(structured, Mapping) else None
    obs.is_error = bool(result.get("isError", False))

    texts = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, Mapping) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
    if texts:
        obs.text = "\n".join(texts)

    if isinstance(structured, Mapping):
        obs.json_doc = structured
        obs.error_type = structured.get("error_type") if isinstance(structured.get("error_type"), str) else None
    else:
        doc_from_text = _plain_json(obs.text)
        obs.json_doc = doc_from_text
        if isinstance(doc_from_text, Mapping) and isinstance(doc_from_text.get("error_type"), str):
            obs.error_type = doc_from_text["error_type"]


def _expects_rate_limit(case: Case) -> bool:
    """这条用例是不是**故意**在测限流？是的话不许重试，否则永远测不出来。"""
    expect = case.expect or {}
    if expect.get("status") == 429:
        return True
    error_type = str(expect.get("error_type") or "").lower()
    if error_type == "rate_limited":
        return True
    for key in ("contains", "not_contains"):
        value = str(expect.get(key) or "").lower()
        if any(hint in value for hint in _RATE_LIMIT_HINTS):
            return True
    return False


def _retry_after(resp: httpx.Response) -> float:
    """按 Retry-After 等待，但夹在 [0.2, 10] 秒：不信任服务端给的极端值。"""
    raw = resp.headers.get("retry-after")
    wait = 1.0
    if raw:
        try:
            wait = float(raw)
        except ValueError:
            wait = 1.0
    return max(0.2, min(wait, 10.0))


def _path(url: str) -> str:
    try:
        return "/" + url.split("://", 1)[1].split("/", 1)[1]
    except IndexError:
        return url
