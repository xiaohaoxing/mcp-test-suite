"""套件加载与 schema 校验。

这个模块的唯一职责：**在跑之前**把结构错误全部找出来，并且每条都指到
`cases[3].expect.foo` 这种可定位的路径上。

为什么校验要这么严（而不是"跑的时候再说"）：

· 一条缺少 `expect` 的用例**看起来是绿的**（没有断言 = 没有失败），
  它会把「忘了写指标」伪装成「行为符合预期」—— 这是验收文档最危险的失效模式；
· 未知断言键（比如把 `json_equals` 拼成 `json_equal`）如果不报错，
  同样会静默通过；
· 重复 id 会让报告里两条不同的用例互相覆盖，历史对比就断了。

所以：缺三件套 / 未知 expect 键 / 重复 id / 未知 layer / schema 漂移，一律在加载期报错，
CLI 退出码固定为 2（配置错），与「跑起来有失败」的 1 明确区分。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml

#: 覆盖维度的固定语义。顺序就是报告里的展示顺序（也是「从便宜到贵」的顺序）。
#: 只允许这六层：层的价值在于**每个服务用同一把尺子**，允许自定义层等于放弃对比。
LAYER_NOTES: Dict[str, str] = {
    "liveness": "进程活着 / 配置可读 / 就绪",
    "auth": "认证与越权",
    "guard": "SQL / 参数护栏（防的是「能查但不该查」）",
    "domain": "业务能力（能不能真的干活）",
    "governance": "限流 / 配额 / 审计 / 脱敏",
    "admin": "管理面（配置 / 组 / 令牌 / 观测）",
}

PRIORITIES: Tuple[str, ...] = ("P0", "P1", "P2")
CASE_STATUSES: Tuple[str, ...] = ("active", "todo", "skip")
ACTION_KINDS: Tuple[str, ...] = ("tool", "http")
HTTP_METHODS: Tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
HTTP_AUTH_MODES: Tuple[str, ...] = ("none", "business", "admin")

#: expect 支持的断言键 —— **这份清单是契约**。新增一个键等于新增一种判定方式，
#: 需要同时改 assertions / report / README / 模板，不允许某个服务私自扩。
EXPECT_KEYS: Tuple[str, ...] = (
    "status",
    "is_error",
    "error_type",
    "json_path",
    "json_equals",
    "json_contains",
    "json_len",
    "contains",
    "not_contains",
    "max_elapsed_ms",
    "rows_equals",
    "min_rows",
)

#: 依赖 `json_path` 定位目标的断言键
JSON_PATH_CONSUMERS: Tuple[str, ...] = ("json_equals", "json_contains", "json_len")

#: 报告与文档里的固定展示顺序
EXPECT_ORDER: Tuple[str, ...] = EXPECT_KEYS

TOP_LEVEL_KEYS: Tuple[str, ...] = (
    "suite", "version", "service", "updated", "owner", "defaults", "layers", "cases",
)
DEFAULTS_KEYS: Tuple[str, ...] = ("base_url", "timeout_s", "token_env", "admin_token_env")
CASE_KEYS: Tuple[str, ...] = (
    "id", "title", "layer", "priority", "goal", "given", "action", "expect",
    "metric", "regression", "status",
)
TOOL_ACTION_KEYS: Tuple[str, ...] = ("kind", "tool", "arguments")
HTTP_ACTION_KEYS: Tuple[str, ...] = ("kind", "method", "path", "body", "headers", "auth")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class SuiteConfigError(Exception):
    """套件结构/语义错误。CLI 捕获后打印全部错误并退出 2。"""

    def __init__(self, errors: Sequence[str], source: str = ""):
        self.errors: List[str] = list(errors)
        self.source = source
        head = f"套件配置错误（{source}）" if source else "套件配置错误"
        super().__init__(head + "：\n" + "\n".join(f"  - {e}" for e in self.errors))


@dataclass
class Defaults:
    base_url: str
    timeout_s: float
    token_env: str
    admin_token_env: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "timeout_s": self.timeout_s,
            "token_env": self.token_env,
            "admin_token_env": self.admin_token_env,
        }


@dataclass
class Action:
    kind: str
    # tool
    tool: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    # http
    method: Optional[str] = None
    path: Optional[str] = None
    body: Any = None
    headers: Dict[str, str] = field(default_factory=dict)
    auth: Optional[str] = None

    def resolved_auth(self) -> str:
        """http 动作用哪把令牌。显式 `auth` 优先，否则按路径前缀推断。

        为什么按前缀推断：/admin/v1/* 与 /mcp 用的是**两把完全不同的令牌**
        （管理令牌不授予任何 MCP 工具权限）。让用例作者每次手写 auth 容易写错，
        而写错的后果是「用管理令牌调工具」这种越权测试变成假绿。
        """
        if self.kind != "http":
            return "business"
        if self.auth:
            return self.auth
        p = (self.path or "").split("?", 1)[0]
        if p.startswith("/admin/"):
            return "admin"
        if p.startswith("/mcp"):
            return "business"
        return "none"

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"kind": self.kind}
        if self.kind == "tool":
            out["tool"] = self.tool
            out["arguments"] = self.arguments
        else:
            out["method"] = self.method
            out["path"] = self.path
            if self.body is not None:
                out["body"] = self.body
            if self.headers:
                out["headers"] = self.headers
            if self.auth:
                out["auth"] = self.auth
        return out


@dataclass
class Case:
    id: str
    title: str
    layer: str
    priority: str
    goal: str
    action: Action
    expect: Dict[str, Any]
    given: Dict[str, Any] = field(default_factory=dict)
    metric: Optional[str] = None
    regression: List[str] = field(default_factory=list)
    status: str = "active"
    index: int = 0

    @property
    def runnable(self) -> bool:
        return self.status == "active"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "layer": self.layer,
            "priority": self.priority,
            "goal": self.goal,
            "given": self.given,
            "action": self.action.to_dict(),
            "expect": self.expect,
            "metric": self.metric,
            "regression": self.regression,
            "status": self.status,
        }


@dataclass
class Suite:
    path: str
    suite: str
    version: int
    service: str
    updated: str
    owner: str
    defaults: Defaults
    layers: List[str]
    cases: List[Case]

    def select(
        self,
        layers: Optional[Iterable[str]] = None,
        priorities: Optional[Iterable[str]] = None,
    ) -> List[Case]:
        want_layers = {x for x in (layers or ()) if x}
        want_pri = {x for x in (priorities or ()) if x}
        out = []
        for c in self.cases:
            if want_layers and c.layer not in want_layers:
                continue
            if want_pri and c.priority not in want_pri:
                continue
            out.append(c)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suite": self.suite,
            "version": self.version,
            "service": self.service,
            "updated": self.updated,
            "owner": self.owner,
            "defaults": self.defaults.to_dict(),
            "layers": list(self.layers),
            "cases": [c.to_dict() for c in self.cases],
        }


# --------------------------------------------------------------------------- 读取
def load_file(path: str) -> Suite:
    """读并校验一个 `mcp-test-suite.yaml`。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise SuiteConfigError([f"无法读取文件：{exc}"], source=path) from exc
    return loads(text, source=path)


def loads(text: str, source: str = "<string>") -> Suite:
    """从字符串加载（测试用；也让 `--stdin` 之类的扩展不必再读一遍文件）。"""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"（第 {mark.line + 1} 行第 {mark.column + 1} 列）" if mark else ""
        raise SuiteConfigError([f"YAML 语法错误{where}：{exc}"], source=source) from exc

    errors: List[str] = []
    if not isinstance(doc, Mapping):
        raise SuiteConfigError(["根节点必须是 mapping（suite/version/service/...）"], source=source)

    _check_unknown_keys(doc, TOP_LEVEL_KEYS, "", errors)

    missing = [k for k in TOP_LEVEL_KEYS if k not in doc]
    if missing:
        errors.append("缺少顶层字段：" + ", ".join(missing))
        # 缺顶层就没法继续做语义校验，但仍然把已发现的错误一次性报出来
        raise SuiteConfigError(errors, source=source)

    suite_name = _req_str(doc, "suite", "", errors)
    service = _req_str(doc, "service", "", errors)
    owner = _req_str(doc, "owner", "", errors)

    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append(f"version 必须是 >=1 的整数，实际 {version!r}")

    updated = doc.get("updated")
    if not isinstance(updated, str) or not _DATE_RE.match(updated):
        errors.append(f"updated 必须是 YYYY-MM-DD 字符串，实际 {updated!r}")

    defaults = _parse_defaults(doc.get("defaults"), errors)
    layers = _parse_layers(doc.get("layers"), errors)
    cases = _parse_cases(doc.get("cases"), layers, errors)

    if errors:
        raise SuiteConfigError(errors, source=source)

    assert defaults is not None  # 上面没报错就一定有
    return Suite(
        path=source,
        suite=str(suite_name),
        version=int(version),
        service=str(service),
        updated=str(updated),
        owner=str(owner),
        defaults=defaults,
        layers=layers,
        cases=cases,
    )


# --------------------------------------------------------------------------- 各段校验
def _check_unknown_keys(obj: Mapping, allowed: Sequence[str], prefix: str, errors: List[str]) -> None:
    unknown = sorted(set(obj.keys()) - set(allowed))
    for key in unknown:
        where = prefix or "顶层"
        errors.append(f"{where}：未知字段 {key!r}；允许的字段：{', '.join(allowed)}")


def _req_str(obj: Mapping, key: str, prefix: str, errors: List[str]) -> str:
    val = obj.get(key)
    where = f"{prefix}.{key}" if prefix else key
    if not isinstance(val, str) or not val.strip():
        errors.append(f"{where} 必须是非空字符串，实际 {val!r}")
        return ""
    return val.strip()


def _parse_defaults(raw: Any, errors: List[str]) -> Optional[Defaults]:
    if not isinstance(raw, Mapping):
        errors.append("defaults 必须是 mapping")
        return None
    _check_unknown_keys(raw, DEFAULTS_KEYS, "defaults", errors)

    base_url = raw.get("base_url")
    if not isinstance(base_url, str) or not re.match(r"^https?://", base_url.strip()):
        errors.append(f"defaults.base_url 必须是 http(s):// 开头的字符串，实际 {base_url!r}")
        base_url = ""

    timeout_s = raw.get("timeout_s")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        errors.append(f"defaults.timeout_s 必须是正数（秒），实际 {timeout_s!r}")
        timeout_s = 0.0

    token_env = raw.get("token_env")
    if not isinstance(token_env, str) or not _ENV_RE.match(token_env):
        errors.append(f"defaults.token_env 必须是环境变量名（大写字母/数字/下划线），实际 {token_env!r}")
        token_env = ""

    admin_env = raw.get("admin_token_env")
    if admin_env is not None:
        if not isinstance(admin_env, str) or not _ENV_RE.match(admin_env):
            errors.append(
                f"defaults.admin_token_env 必须是环境变量名（大写字母/数字/下划线），实际 {admin_env!r}"
            )
            admin_env = None

    if any(not v for v in (base_url, token_env)) or not timeout_s:
        return None
    return Defaults(
        base_url=str(base_url).strip().rstrip("/"),
        timeout_s=float(timeout_s),
        token_env=str(token_env),
        admin_token_env=str(admin_env) if admin_env else None,
    )


def _parse_layers(raw: Any, errors: List[str]) -> List[str]:
    if not isinstance(raw, list) or not raw:
        errors.append("layers 必须是非空列表（六层：liveness/auth/guard/domain/governance/admin）")
        return []
    out: List[str] = []
    for i, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"layers[{i}] 必须是非空字符串，实际 {item!r}")
            continue
        name = item.strip()
        if name not in LAYER_NOTES:
            errors.append(
                f"layers[{i}] 未知层 {name!r}；只允许：{', '.join(LAYER_NOTES)}"
            )
            continue
        if name in out:
            errors.append(f"layers[{i}] 重复声明层 {name!r}")
            continue
        out.append(name)
    return out


def _parse_cases(raw: Any, layers: Sequence[str], errors: List[str]) -> List[Case]:
    if not isinstance(raw, list) or not raw:
        errors.append("cases 必须是非空列表")
        return []

    out: List[Case] = []
    seen: Dict[str, int] = {}
    for i, item in enumerate(raw):
        prefix = f"cases[{i}]"
        if not isinstance(item, Mapping):
            errors.append(f"{prefix} 必须是 mapping")
            continue
        _check_unknown_keys(item, CASE_KEYS, prefix, errors)

        cid = item.get("id")
        if not isinstance(cid, str) or not cid.strip():
            errors.append(f"{prefix}.id 必须是非空字符串")
            cid = ""
        else:
            cid = cid.strip()
            if not _ID_RE.match(cid):
                errors.append(f"{prefix}.id {cid!r} 非法：只允许小写字母/数字/连字符/下划线")
            if cid in seen:
                errors.append(f"{prefix}.id {cid!r} 重复（cases[{seen[cid]}] 已使用）")
            else:
                seen[cid] = i

        title = _req_str(item, "title", prefix, errors)
        layer = item.get("layer")
        if not isinstance(layer, str) or layer not in layers:
            errors.append(
                f"{prefix}.layer 必须是 layers 里声明过的层，实际 {layer!r}；已声明：{', '.join(layers) or '（空）'}"
            )
            layer = str(layer or "")

        priority = item.get("priority")
        if priority not in PRIORITIES:
            errors.append(f"{prefix}.priority 必须是 {'/'.join(PRIORITIES)}，实际 {priority!r}")

        goal = item.get("goal")
        if not isinstance(goal, str) or len(goal.strip()) < 10:
            # 10 个字符是最低门槛：真正的理由写不了这么短，通常是把操作复述了一遍
            errors.append(f"{prefix}.goal 必须写清「为什么值得测」（至少 10 个字符），实际 {goal!r}")
            goal = ""

        given = item.get("given", {})
        if given is None:
            given = {}
        if not isinstance(given, Mapping):
            errors.append(f"{prefix}.given 必须是 mapping，实际 {type(given).__name__}")
            given = {}

        action = _parse_action(item.get("action"), prefix, errors)
        expect = _parse_expect(item.get("expect"), prefix, errors)

        metric = item.get("metric")
        if metric is not None and (not isinstance(metric, str) or not metric.strip()):
            errors.append(f"{prefix}.metric 必须是非空字符串，实际 {metric!r}")
            metric = None

        regression = item.get("regression")
        if regression is not None:
            if isinstance(regression, str):
                regression = [regression]
            if not isinstance(regression, list) or not all(
                isinstance(x, str) and x.strip() for x in regression
            ):
                errors.append(f"{prefix}.regression 必须是字符串或字符串列表，实际 {regression!r}")
                regression = []

        status = item.get("status", "active")
        if status not in CASE_STATUSES:
            errors.append(f"{prefix}.status 必须是 {'/'.join(CASE_STATUSES)}，实际 {status!r}")
            status = "active"

        if action is None or expect is None or not cid or not title or not layer or not goal:
            continue
        if priority not in PRIORITIES:
            priority = "P2"

        out.append(
            Case(
                id=cid,
                title=title,
                layer=layer,
                priority=str(priority),
                goal=goal.strip(),
                given=dict(given),
                action=action,
                expect=expect,
                metric=metric.strip() if metric else None,
                regression=[str(x).strip() for x in (regression or [])],
                status=str(status),
                index=i,
            )
        )
    return out


def _parse_action(raw: Any, prefix: str, errors: List[str]) -> Optional[Action]:
    where = f"{prefix}.action"
    if not isinstance(raw, Mapping):
        errors.append(f"{where} 必须存在且为 mapping（kind: tool | http）")
        return None

    kind = raw.get("kind")
    if kind not in ACTION_KINDS:
        errors.append(f"{where}.kind 必须是 tool 或 http，实际 {kind!r}")
        return None

    if kind == "tool":
        _check_unknown_keys(raw, TOOL_ACTION_KEYS, where, errors)
        tool = raw.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            errors.append(f"{where}.tool 必须是工具名（非空字符串）")
            return None
        args = raw.get("arguments", {})
        if args is None:
            args = {}
        if not isinstance(args, Mapping):
            errors.append(f"{where}.arguments 必须是 mapping，实际 {type(args).__name__}")
            return None
        return Action(kind="tool", tool=tool.strip(), arguments=dict(args))

    _check_unknown_keys(raw, HTTP_ACTION_KEYS, where, errors)
    method = raw.get("method")
    if not isinstance(method, str) or method.upper() not in HTTP_METHODS:
        errors.append(f"{where}.method 必须是 {'/'.join(HTTP_METHODS)} 之一，实际 {method!r}")
        return None
    path = raw.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        errors.append(f"{where}.path 必须以 / 开头，实际 {path!r}")
        return None
    body = raw.get("body")
    if body is not None and not isinstance(body, (Mapping, str, list)):
        errors.append(f"{where}.body 必须是 mapping / 列表 / 字符串，实际 {type(body).__name__}")
        return None
    headers = raw.get("headers", {}) or {}
    if not isinstance(headers, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
    ):
        errors.append(f"{where}.headers 必须是 str→str 的 mapping")
        return None
    auth = raw.get("auth")
    if auth is not None and auth not in HTTP_AUTH_MODES:
        errors.append(f"{where}.auth 必须是 {'/'.join(HTTP_AUTH_MODES)}，实际 {auth!r}")
        return None
    return Action(
        kind="http",
        method=method.upper(),
        path=path,
        body=body,
        headers=dict(headers),
        auth=auth,
    )


def _parse_expect(raw: Any, prefix: str, errors: List[str]) -> Optional[Dict[str, Any]]:
    where = f"{prefix}.expect"
    if not isinstance(raw, Mapping):
        errors.append(f"{where} 必须存在且为 mapping（这是「预期指标」，缺了用例就没有判定依据）")
        return None
    if not raw:
        errors.append(f"{where} 不能为空：至少要有一个断言键")
        return None

    for key in raw:
        if key not in EXPECT_KEYS:
            errors.append(
                f"{where}：未知断言键 {key!r}；允许的键：{', '.join(EXPECT_KEYS)}"
            )

    ok = True
    for key, val in raw.items():
        if key not in EXPECT_KEYS:
            ok = False
            continue
        detail = f"{where}.{key}"
        if key == "status":
            if not isinstance(val, int) or isinstance(val, bool):
                errors.append(f"{detail} 必须是整数 HTTP 状态码，实际 {val!r}")
                ok = False
        elif key == "is_error":
            if not isinstance(val, bool):
                errors.append(f"{detail} 必须是布尔值，实际 {val!r}")
                ok = False
        elif key in ("error_type", "contains", "not_contains"):
            if not isinstance(val, str) or not val:
                errors.append(f"{detail} 必须是非空字符串，实际 {val!r}")
                ok = False
        elif key == "json_path":
            if not isinstance(val, str) or not val:
                errors.append(f"{detail} 必须是非空字符串（如 checks.b2b-qa.reachable）")
                ok = False
        elif key in ("json_len", "min_rows"):
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                errors.append(f"{detail} 必须是 >=0 的整数，实际 {val!r}")
                ok = False
        elif key == "max_elapsed_ms":
            if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
                errors.append(f"{detail} 必须是正数（毫秒），实际 {val!r}")
                ok = False
        elif key == "rows_equals":
            if not isinstance(val, list):
                errors.append(f"{detail} 必须是二维数组（rows 精确相等），实际 {type(val).__name__}")
                ok = False

    if "json_path" in raw:
        if not any(k in raw for k in JSON_PATH_CONSUMERS):
            errors.append(
                f"{where}.json_path 需要搭配 json_equals / json_contains / json_len 才有意义"
            )
            ok = False
    for key in JSON_PATH_CONSUMERS:
        if key in raw and "json_path" not in raw:
            errors.append(f"{where}.{key} 需要同时给出 json_path")
            ok = False

    return dict(raw) if ok else None


# --------------------------------------------------------------------------- 工具函数
def mask_token(token: Optional[str]) -> str:
    """报告里只显示前 8 位。空值显示 `<未设置>`。"""
    if not token:
        return "<未设置>"
    head = token[:8]
    return f"{head}…(len {len(token)})"


def parse_env_value(raw: Optional[str], consumer: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """把环境变量值解析成 (token, consumer)。

    兼容 mcp-kit 的 `token:消费者名, token2:消费者名2` 列表格式 —— ELK/Jenkins/
    SchemaWeaver 的 `.env` 都是这个形状，如果只认「裸令牌」，那每次跑基线都得手工
    从列表里抠一段出来（抠错就等于用错权限，报告会变成假绿/假红）。

    `consumer` 指定时按名字挑；找不到返回 (None, None)，由 CLI 报配置错。
    """
    if not raw or not raw.strip():
        return None, None
    entries = [e.strip() for e in raw.split(",") if e.strip()]
    parsed: List[Tuple[str, Optional[str]]] = []
    for entry in entries:
        if ":" in entry:
            tok, name = entry.split(":", 1)
            parsed.append((tok.strip(), name.strip() or None))
        else:
            parsed.append((entry, None))
    if not parsed:
        return None, None
    if consumer:
        for tok, name in parsed:
            if name == consumer:
                return tok, name
        return None, None
    return parsed[0]


def env_consumer_names(raw: Optional[str]) -> List[str]:
    """列出环境变量里登记的消费者名（只给名字，不碰令牌值）。用于报错提示。"""
    if not raw:
        return []
    names = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            names.append(entry.split(":", 1)[1].strip())
        else:
            names.append("(未命名)")
    return names


def load_env_file(path: str, env: Optional[Dict[str, str]] = None) -> List[str]:
    """把 dotenv 文件读进环境（**不覆盖已存在的变量**）。

    为什么不覆盖：`export MCP_TEST_TOKEN=...` 是操作者当次运行的显式意图，
    被 `.env` 里的旧值盖掉会非常难查。返回被跳过的键，便于 `--verbose` 说明。
    """
    target = env if env is not None else os.environ  # type: ignore[assignment]
    skipped: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        raise SuiteConfigError([f"无法读取 env 文件 {path}：{exc}"], source=path) from exc
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if not _ENV_RE.match(key):
            continue
        if key in target:
            skipped.append(key)
            continue
        target[key] = val
    return skipped
