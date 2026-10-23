"""断言：把 `expect` 的 12 个键变成可判定的 pass/fail，并保留「期望 vs 实际」。

## 为什么这里要单独一层

用例失败时最有价值的不是「FAIL」两个字，而是**期望值与实际值并排**。
只有 FAIL 的报告会逼着人重新跑一遍、再手工看响应 —— 那等于把自动化又退化成手工。
所以每个断言结果都同时带 `expected` / `actual`，报告层只做展示，不做解释。

## `json_path` 的语义（只此一套，别在别处再发明）

· tool 动作：根是 `structuredContent`；没有时退回「文本内容解析出的 JSON」。
· http 动作：根是响应体（SSE 帧会被拆开）。
· 点号分段，支持 `a.b[0].c` 与键名带连字符（`checks.b2b-qa.reachable`）。
· 中途遇到「长得像 JSON 的字符串」会自动解析一层 —— 某个服务 这类服务的工具把结果
  序列化成字符串放进 `{"result": "..."}`，不自动拆的话所有断言都只能写 `contains`。

## `json_contains` 的三套语义（按 target 类型区分）

· target 是 list：needle 是 mapping → 任一元素**包含这些键值**；needle 是 list →
  每个 needle 都能在 target 里找到；否则 → 任一元素等于 needle。
· target 是 dict：needle 是 mapping → 子集匹配（partial match）。
· target 是 str：needle 是 str → 子串。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .loader import EXPECT_ORDER

_MISSING = object()
_TEXT_PREVIEW = 400  # 断言失败时实际值最多展示这么多字符（够定位，又不至于把报告撑爆）


@dataclass
class Observation:
    """一次动作的可判定事实。runner 负责填，assertions 只读。"""

    kind: str                      # tool | http
    status: Optional[int] = None   # HTTP 状态码
    is_error: bool = False         # MCP result.isError
    error_type: Optional[str] = None
    structured: Any = None         # tool: structuredContent
    json_doc: Any = None           # 断言 json_path 的根
    text: str = ""
    rows: Any = None               # tool: structuredContent.rows
    elapsed_ms: int = 0
    transport_error: Optional[str] = None
    tool: Optional[str] = None
    method: Optional[str] = None
    path: Optional[str] = None
    consumer: Optional[str] = None
    session_id: Optional[str] = None

    def label(self) -> str:
        if self.kind == "tool":
            return f"tool {self.tool}"
        return f"{self.method} {self.path}"


@dataclass
class AssertionResult:
    key: str
    expected: Any
    actual: Any
    ok: bool
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "expected": _jsonable(self.expected),
            "actual": _jsonable(self.actual),
            "ok": self.ok,
            "detail": self.detail or None,
        }


# --------------------------------------------------------------------------- 判定
def check_expect(expect: Mapping[str, Any], obs: Observation) -> List[AssertionResult]:
    """按固定顺序判定，保证同一份套件每次输出的断言顺序一致（便于历史 diff）。"""
    results: List[AssertionResult] = []

    json_path = expect.get("json_path")
    target: Any = _MISSING
    path_error: Optional[str] = None
    if json_path is not None:
        try:
            target = resolve_json_path(obs.json_doc, str(json_path))
        except LookupError as exc:
            path_error = str(exc)

    for key in EXPECT_ORDER:
        if key not in expect:
            continue
        if key == "json_path":
            # json_path 是「定位参数」，不是独立断言；它的问题会在依赖它的键上体现
            continue
        expected = expect[key]

        if key == "status":
            actual = obs.status
            ok = actual == expected
            detail = "" if ok else _status_detail(obs)
            results.append(AssertionResult(key, expected, actual, ok, detail))

        elif key == "is_error":
            actual = bool(obs.is_error)
            ok = actual == expected
            detail = "" if ok else _error_detail(obs)
            results.append(AssertionResult(key, expected, actual, ok, detail))

        elif key == "error_type":
            actual = obs.error_type
            ok = actual == expected
            detail = "" if ok else _error_detail(obs)
            results.append(AssertionResult(key, expected, actual, ok, detail))

        elif key in ("json_equals", "json_contains", "json_len"):
            if path_error is not None:
                results.append(
                    AssertionResult(
                        key, expected, f"<路径不可达：{path_error}>", False,
                        detail=f"json_path={json_path} 在响应里取不到值",
                    )
                )
                continue
            if target is _MISSING:
                results.append(
                    AssertionResult(key, expected, "<未提供 json_path>", False,
                                    detail="该断言需要 json_path 定位目标")
                )
                continue
            if key == "json_equals":
                ok = _deep_eq(target, expected)
                results.append(AssertionResult(key, expected, target, ok))
            elif key == "json_contains":
                ok = _contains(target, expected)
                results.append(
                    AssertionResult(key, expected, target, ok,
                                    detail="" if ok else f"在 {type(target).__name__} 里没找到期望内容")
                )
            else:  # json_len
                try:
                    actual_len = len(target)
                except TypeError:
                    results.append(
                        AssertionResult(key, expected, f"<{type(target).__name__} 没有长度>", False,
                                        detail=f"json_path={json_path} 的值不可计长度")
                    )
                    continue
                results.append(AssertionResult(key, expected, actual_len, actual_len == expected))

        elif key == "contains":
            text = obs.text or ""
            ok = expected in text
            results.append(
                AssertionResult(key, expected, _preview(text), ok,
                                detail="" if ok else _text_detail(obs))
            )

        elif key == "not_contains":
            text = obs.text or ""
            ok = expected not in text
            results.append(
                AssertionResult(key, f"不含 {expected!r}", _preview(text), ok,
                                detail="" if ok else "响应里出现了本不该出现的内容")
            )

        elif key == "max_elapsed_ms":
            actual = obs.elapsed_ms
            ok = actual <= float(expected)
            results.append(
                AssertionResult(key, f"<= {expected} ms", f"{actual} ms", ok,
                                detail="" if ok else _slow_detail(obs))
            )

        elif key == "rows_equals":
            if obs.rows is None:
                results.append(AssertionResult(key, expected, "<响应里没有 rows>", False,
                                               detail="rows 只对返回 structuredContent.rows 的工具生效"))
            else:
                ok = _deep_eq(obs.rows, expected)
                results.append(AssertionResult(key, expected, obs.rows, ok))

        elif key == "min_rows":
            if obs.rows is None:
                results.append(AssertionResult(key, f">= {expected} 行", "<响应里没有 rows>", False,
                                               detail="rows 只对返回 structuredContent.rows 的工具生效"))
            else:
                try:
                    n = len(obs.rows)
                except TypeError:
                    n = -1
                results.append(AssertionResult(key, f">= {expected} 行", f"{n} 行", n >= int(expected)))

    return results


# --------------------------------------------------------------------------- 细节文案
def _status_detail(obs: Observation) -> str:
    if obs.transport_error:
        return f"请求没有拿到 HTTP 响应：{obs.transport_error}"
    return ""


def _error_detail(obs: Observation) -> str:
    if obs.transport_error:
        return f"请求失败：{obs.transport_error}"
    return ""


def _text_detail(obs: Observation) -> str:
    if obs.transport_error:
        return f"请求失败：{obs.transport_error}"
    return "响应文本里没有出现期望内容"


def _slow_detail(obs: Observation) -> str:
    if obs.transport_error:
        return f"请求失败：{obs.transport_error}"
    return "耗时超过预算"


def _preview(text: str) -> str:
    if len(text) <= _TEXT_PREVIEW:
        return text
    return text[:_TEXT_PREVIEW] + f"…（共 {len(text)} 字符）"


# --------------------------------------------------------------------------- JSON 路径
def resolve_json_path(doc: Any, path: str) -> Any:
    """解析 `a.b[0].c`。取不到就抛 LookupError（由调用方转成断言失败，而不是崩掉整轮）。"""
    if doc is None:
        raise LookupError("响应里没有可解析的 JSON")
    if path.strip() in ("", "."):
        return doc
    cur = doc
    for part in _split_path(path):
        cur = _step(cur, part, path)
    return cur


def _split_path(path: str) -> List[Any]:
    out: List[Any] = []
    for chunk in path.split("."):
        if not chunk:
            raise LookupError(f"路径 {path!r} 里有空片段")
        head = chunk
        indexes: List[int] = []
        while True:
            if head.endswith("]") and "[" in head:
                head, _, idx = head.rpartition("[")
                if not idx[:-1].isdigit():
                    raise LookupError(f"路径 {path!r} 里的下标 {idx!r} 不是数字")
                indexes.insert(0, int(idx[:-1]))
                continue
            break
        if not head:
            raise LookupError(f"路径 {path!r} 里的片段 {chunk!r} 没有键名")
        out.append(head)
        out.extend(indexes)
    return out


def _step(cur: Any, part: Any, path: str) -> Any:
    if isinstance(cur, str):
        parsed = _try_json(cur)
        if parsed is not _MISSING:
            cur = parsed
    if isinstance(part, int):
        if not isinstance(cur, (list, tuple)):
            raise LookupError(f"路径 {path!r}：在 {type(cur).__name__} 上取了下标 [{part}]")
        if part >= len(cur) or part < -len(cur):
            raise LookupError(f"路径 {path!r}：下标 [{part}] 越界（长度 {len(cur)}）")
        return cur[part]
    if isinstance(cur, Mapping):
        if part not in cur:
            keys = ", ".join(str(k) for k in list(cur)[:8])
            raise LookupError(f"路径 {path!r}：键 {part!r} 不存在（现有键：{keys}…）")
        return cur[part]
    raise LookupError(f"路径 {path!r}：无法在 {type(cur).__name__} 上取键 {part!r}")


def _try_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return _MISSING
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return _MISSING


# --------------------------------------------------------------------------- 比较
def _deep_eq(a: Any, b: Any) -> bool:
    """JSON 语义的相等：bool 不等于 1，int 与 float 按数值比。"""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return set(a.keys()) == set(b.keys()) and all(_deep_eq(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_deep_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b


def _contains(target: Any, needle: Any) -> bool:
    if isinstance(target, str):
        return isinstance(needle, str) and needle in target
    if isinstance(target, Mapping):
        if not isinstance(needle, Mapping):
            return False
        return all(k in target and _deep_eq(target[k], v) for k, v in needle.items())
    if isinstance(target, (list, tuple)):
        if isinstance(needle, Mapping):
            return any(
                isinstance(el, Mapping)
                and all(k in el and _deep_eq(el[k], v) for k, v in needle.items())
                for el in target
            )
        if isinstance(needle, (list, tuple)):
            return all(any(_deep_eq(el, n) for el in target) for n in needle)
        return any(_deep_eq(el, needle) for el in target)
    return False


def _jsonable(value: Any) -> Any:
    """报告要能 json.dumps：把不可序列化的对象降级成字符串。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return repr(value)


def summarize(results: Sequence[AssertionResult]) -> Tuple[int, int]:
    """(通过数, 总数)。报告与「P0 全过」判定都用它。"""
    return sum(1 for r in results if r.ok), len(results)
