"""把 `mcp-test-suite.yaml` 渲染成 `MCP-TEST-SUITE.md`（人读版）。

## 为什么是「渲染」而不是「写两份」

同一份验收资产有两种读者：

· **机器**：YAML（runner 执行、CI 归档、历史对比）；
· **人**：Markdown（评审、交接、客户对接时直接贴）。

如果两份都手写，它们**一定**会漂移 —— 通常发生在「临时改了一条断言想看结果」之后。
漂移的验收文档比没有文档更糟：它让人相信某条已被覆盖，而实际早就不是那样了。
所以这里只做单向渲染，并且在产物顶部明确写「不要手改」。

## 三个必须保留的部分

1. **三件套**（目标 / 操作 / 预期指标）—— 少了任何一栏，用例就退化成「跑了个请求」；
2. **覆盖矩阵** —— 用来看哪一层根本没测（0 条 ≠ 没问题）；
3. **回归项映射** —— 把 `regression:` 标签和真实事故编号对起来，
   这样「回归一次」有明确的范围，而不是凭记忆挑。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Mapping, Sequence

from .loader import EXPECT_KEYS, LAYER_NOTES, PRIORITIES, Suite, load_file


def render_doc(suite: Suite) -> str:
    lines: List[str] = []
    lines.append(f"# MCP 能力验收套件 · {suite.service}")
    lines.append("")
    lines.append(
        "> 本文件由 `mcp-test-suite.yaml` 渲染（`python -m mcp_suite doc`），**请勿手工编辑**。"
        "改用例只改 YAML，再重新渲染。"
    )
    lines.append("")

    # ---------------------------------------------------------------- 概览
    lines.append("## 概览")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 套件 | `{suite.suite}` v{suite.version} |")
    lines.append(f"| 被测服务 | `{suite.service}` |")
    lines.append(f"| 负责人 | `{suite.owner}` |")
    lines.append(f"| 用例更新 | {suite.updated} |")
    lines.append(f"| 默认入口 | `{suite.defaults.base_url}` |")
    lines.append(f"| 单请求超时 | {suite.defaults.timeout_s:g}s |")
    lines.append(f"| 业务令牌（环境变量） | `{suite.defaults.token_env}` |")
    admin_env = suite.defaults.admin_token_env or "（未声明）"
    lines.append(f"| 管理令牌（环境变量） | `{admin_env}` |")
    lines.append(f"| 用例总数 | {len(suite.cases)} |")
    lines.append("")
    lines.append(
        "令牌只从环境变量读取，**任何情况下都不写进 YAML**；报告里也只显示前 8 位。"
    )
    lines.append("")

    # ---------------------------------------------------------------- 覆盖矩阵
    lines.append("## 覆盖矩阵（layer × priority）")
    lines.append("")
    lines.append("| 层 | 语义 | P0 | P1 | P2 | 合计 |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for layer in suite.layers:
        cases = [c for c in suite.cases if c.layer == layer]
        counts = {p: sum(1 for c in cases if c.priority == p) for p in PRIORITIES}
        lines.append(
            f"| `{layer}` | {LAYER_NOTES.get(layer, '')} | {counts['P0']} | {counts['P1']} | "
            f"{counts['P2']} | {len(cases)} |"
        )
    lines.append("")
    lines.append("> 某层 0 条用例 = **没测**，不是「没问题」。")
    lines.append("")

    # ---------------------------------------------------------------- 用例
    lines.append("## 用例（目标 / 操作 / 预期指标）")
    lines.append("")
    for layer in suite.layers:
        cases = [c for c in suite.cases if c.layer == layer]
        if not cases:
            continue
        lines.append(f"### {layer} · {LAYER_NOTES.get(layer, '')}")
        lines.append("")
        lines.append("| ID | 优先级 | 目标（为什么测） | 操作（怎么触发） | 预期指标（怎么判定通过） |")
        lines.append("|---|---|---|---|---|")
        for c in cases:
            pri = c.priority + (f"（{c.status}）" if c.status != "active" else "")
            lines.append(
                f"| `{c.id}` | {pri} | {_cell(c.goal)} | {_cell(_render_action(c))} | "
                f"{_cell(_render_expect(c))} |"
            )
        lines.append("")

    # ---------------------------------------------------------------- 回归映射
    regressions: Dict[str, List[str]] = {}
    for c in suite.cases:
        for tag in c.regression:
            regressions.setdefault(tag, []).append(c.id)
    if regressions:
        lines.append("## 回归项映射")
        lines.append("")
        lines.append("| 回归项 | 用例 |")
        lines.append("|---|---|")
        for tag in sorted(regressions):
            ids = "、".join(f"`{i}`" for i in regressions[tag])
            lines.append(f"| `{tag}` | {ids} |")
        lines.append("")

    # ---------------------------------------------------------------- 断言键
    lines.append("## `expect` 支持的断言键")
    lines.append("")
    lines.append("| 键 | 含义 |")
    lines.append("|---|---|")
    for key, desc in _EXPECT_HELP:
        lines.append(f"| `{key}` | {desc} |")
    lines.append("")
    lines.append(
        "`json_path` 支持点号与下标（`a.b[0].c`，键名可带连字符）。"
        "路径中途遇到「像 JSON 的字符串」会自动展开一层 —— 某个服务 这类把结果序列化进 "
        "`{\"result\": \"...\"}` 的服务因此也能直接断言内部字段。"
    )
    lines.append("")

    # ---------------------------------------------------------------- 运行
    lines.append("## 怎么跑")
    lines.append("")
    lines.append("```bash")
    lines.append("# 全量（令牌从环境变量取，绝不写进命令行历史里的字面量）")
    lines.append(f"python -m mcp_suite run --suite {os.path.basename(suite.path)}")
    lines.append("")
    lines.append("# 只跑某一层 / 某一优先级")
    lines.append("python -m mcp_suite run --suite mcp-test-suite.yaml --layer auth,guard")
    lines.append("python -m mcp_suite run --suite mcp-test-suite.yaml --priority P0")
    lines.append("")
    lines.append("# 出报告")
    lines.append("python -m mcp_suite run --suite mcp-test-suite.yaml --md report.md --json report.json")
    lines.append("```")
    lines.append("")
    lines.append("退出码：`0` 全过 / `1` 有失败 / `2` 配置错误。")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_action(case) -> str:
    action = case.action
    parts: List[str] = []
    if case.given:
        parts.append("前置：" + _kv(case.given))
    if action.kind == "tool":
        parts.append(f"tool `{action.tool}`")
        if action.arguments:
            parts.append("args：" + _kv(action.arguments))
        else:
            parts.append("args：`{}`")
    else:
        auth = action.resolved_auth()
        auth_note = {"none": "不带令牌", "business": "业务令牌", "admin": "管理令牌"}[auth]
        parts.append(f"`{action.method} {action.path}`（{auth_note}）")
        if action.body is not None:
            parts.append("body：" + _kv(action.body) if isinstance(action.body, Mapping) else "body：" + str(action.body))
    return "<br>".join(parts)


def _render_expect(case) -> str:
    parts: List[str] = []
    exp = case.expect
    for key in EXPECT_KEYS:
        if key not in exp:
            continue
        val = exp[key]
        if key in ("json_equals", "json_contains", "rows_equals"):
            val_s = json.dumps(val, ensure_ascii=False, separators=(",", ":")) if not isinstance(val, str) else val
            parts.append(f"`{key}` = `{val_s}`")
        elif key == "json_len":
            parts.append(f"`json_len` = {val}")
        elif key == "min_rows":
            parts.append(f"`min_rows` >= {val}")
        elif key == "max_elapsed_ms":
            parts.append(f"`max_elapsed_ms` <= {val}")
        elif key == "is_error":
            parts.append(f"`is_error` = {str(val).lower()}")
        elif key == "status":
            parts.append(f"`status` = {val}")
        else:
            parts.append(f"`{key}` = `{val}`")
    if case.metric:
        parts.append("指标：" + case.metric)
    return "<br>".join(parts)


def _kv(obj: Any) -> str:
    if isinstance(obj, Mapping):
        return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v}" for k, v in obj.items())
    return str(obj)


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>").strip()


_EXPECT_HELP: Sequence[tuple] = (
    ("status", "HTTP 状态码（http 动作最常用）"),
    ("is_error", "MCP `result.isError`"),
    ("error_type", "`structuredContent.error_type`（程序化判定失败原因）"),
    ("json_path", "定位 JSON 里的某个值；支持 `a.b[0].c`"),
    ("json_equals", "该路径的值与期望**精确相等**"),
    ("json_contains", "该路径的值包含期望（list 找元素 / dict 子集 / str 子串）"),
    ("json_len", "该路径的值的长度（list / str / dict）"),
    ("contains", "响应文本包含该子串"),
    ("not_contains", "响应文本**不**包含该子串（防止泄漏/回退）"),
    ("max_elapsed_ms", "本次请求耗时不超过该值（SLO）"),
    ("rows_equals", "`structuredContent.rows` 精确相等"),
    ("min_rows", "`structuredContent.rows` 至少这么多行"),
)


def write_doc(suite: Suite, out_path: str) -> str:
    text = render_doc(suite)
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return out_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mcp_suite.emit_doc",
        description="把 mcp-test-suite.yaml 渲染成 MCP-TEST-SUITE.md（人读版）",
    )
    parser.add_argument("--suite", required=True, help="套件 YAML 路径")
    parser.add_argument("--out", help="输出 Markdown 路径（默认与 YAML 同目录的 MCP-TEST-SUITE.md）")
    args = parser.parse_args(argv)

    from .loader import SuiteConfigError

    try:
        suite = load_file(args.suite)
    except SuiteConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.suite)), "MCP-TEST-SUITE.md")
    write_doc(suite, out)
    print(f"已渲染 {out}（{len(suite.cases)} 条用例）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
