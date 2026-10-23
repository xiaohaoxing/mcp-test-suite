"""报告：同一轮结果输出三种视图。

· **JSON** —— 机器消费（历史对比、看板、CI 归档）。字段名稳定，新增只加不改。
· **Markdown** —— 人读，也进 git（和 YAML 一起评审）。
· **控制台** —— 跑完立刻能看，失败项直接打「期望 vs 实际」。

三种视图共用同一份 `build_report()` 结果，避免「控制台说 1 个失败、MD 说 0 个」这种
自相矛盾 —— 那会让整份报告失去可信度。

覆盖矩阵与优先级汇总是**唯一**的验收结论口径：

· 覆盖矩阵 = layers × (pass/fail/todo/skip/用例数)，用来看「哪一层根本没测」；
  一个层 0 条用例不是「没问题」，是「没测」。
· 优先级汇总 = P0/P1/P2 × 结果，P0 有失败即门槛不过（P0 是上线必过，
  允许它红的验收报告没有意义）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .assertions import AssertionResult
from .loader import LAYER_NOTES, PRIORITIES, Suite
from .runner import CaseResult

STATUS_MARK = {"pass": "PASS", "fail": "FAIL", "todo": "TODO", "skip": "SKIP"}


def build_report(
    suite: Suite,
    results: Sequence[CaseResult],
    *,
    base_url: str,
    token_masked: str,
    consumer: Optional[str],
    timeout_s: float,
    started_at: str,
    elapsed_ms: int,
    layers: Optional[Sequence[str]] = None,
    priorities: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    summary = _summary(results)
    by_priority = {
        p: _bucket([r for r in results if r.case.priority == p]) for p in PRIORITIES
    }
    coverage = []
    for layer in suite.layers:
        group = [r for r in results if r.case.layer == layer]
        row = _bucket(group)
        row["layer"] = layer
        row["note"] = LAYER_NOTES.get(layer, "")
        coverage.append(row)

    p0 = by_priority.get("P0", {})
    if p0.get("total", 0) == 0:
        p0_gate = "n/a"
    else:
        p0_gate = "pass" if p0.get("fail", 0) == 0 else "fail"

    return {
        "suite": {
            "suite": suite.suite,
            "version": suite.version,
            "service": suite.service,
            "updated": suite.updated,
            "owner": suite.owner,
            "path": suite.path,
        },
        "run": {
            "base_url": base_url,
            "token_masked": token_masked,
            "consumer": consumer,
            "timeout_s": timeout_s,
            "started_at": started_at,
            "elapsed_ms": elapsed_ms,
            "filters": {
                "layers": list(layers or []),
                "priorities": list(priorities or []),
            },
        },
        "summary": {
            **summary,
            "verdict": "fail" if summary["fail"] else "pass",
            "p0_gate": p0_gate,
        },
        "by_priority": by_priority,
        "coverage": coverage,
        "case_results": [r.to_dict() for r in results],
    }


def _bucket(results: Sequence[CaseResult]) -> Dict[str, int]:
    return {
        "total": len(results),
        "pass": sum(1 for r in results if r.status == "pass"),
        "fail": sum(1 for r in results if r.status == "fail"),
        "todo": sum(1 for r in results if r.status == "todo"),
        "skip": sum(1 for r in results if r.status == "skip"),
    }


def _summary(results: Sequence[CaseResult]) -> Dict[str, int]:
    s = _bucket(results)
    assertions = [a for r in results for a in r.assertions]
    s["assertions_total"] = len(assertions)
    s["assertions_pass"] = sum(1 for a in assertions if a.ok)
    return s


# --------------------------------------------------------------------------- JSON
def render_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------------- Markdown
def render_markdown(report: Mapping[str, Any]) -> str:
    suite = report["suite"]
    run = report["run"]
    s = report["summary"]
    lines: List[str] = []

    lines.append(f"# MCP 能力验收报告 · {suite['service']}")
    lines.append("")
    lines.append(
        f"> 由 `mcp_suite` 生成（YAML 是唯一事实来源）。"
        f"套件 **{suite['suite']} v{suite['version']}** · owner `{suite['owner']}` · 用例更新于 {suite['updated']}"
    )
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 目标 | `{run['base_url']}` |")
    token_line = f"`{run['token_masked']}`" + (f"（消费方 `{run['consumer']}`）" if run.get("consumer") else "")
    lines.append(f"| 令牌（已打码） | {token_line} |")
    lines.append(f"| 运行时间 | {run['started_at']} |")
    lines.append(f"| 总耗时 | {run['elapsed_ms']} ms |")
    layers = run["filters"].get("layers") or []
    prios = run["filters"].get("priorities") or []
    filters = "、".join(
        x
        for x in (
            ("layer=" + ",".join(layers)) if layers else "",
            ("priority=" + ",".join(prios)) if prios else "",
        )
        if x
    )
    lines.append(f"| 过滤条件 | {filters or '无（全量）'} |")
    lines.append("")

    verdict = "**PASS**" if s["verdict"] == "pass" else "**FAIL**"
    gate = {"pass": "PASS", "fail": "FAIL", "n/a": "无 P0 用例"}[s["p0_gate"]]
    lines.append("## 结论")
    lines.append("")
    lines.append(
        f"{verdict} —— {s['pass']} 通过 / {s['fail']} 失败 / {s['todo']} 待补 / {s['skip']} 跳过"
        f"（断言 {s['assertions_pass']}/{s['assertions_total']}）· **P0 门槛：{gate}**"
    )
    lines.append("")

    lines.append("## 覆盖矩阵（layers × 结果）")
    lines.append("")
    lines.append("| 层 | 语义 | pass | fail | todo | skip | 用例数 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for row in report["coverage"]:
        lines.append(
            f"| `{row['layer']}` | {row['note']} | {row['pass']} | {row['fail']} | "
            f"{row['todo']} | {row['skip']} | {row['total']} |"
        )
    lines.append("")
    lines.append("> 某层 0 条用例 = **没测**，不是「没问题」。")
    lines.append("")

    lines.append("## 按优先级汇总（P0 全过才算通过）")
    lines.append("")
    lines.append("| 优先级 | pass | fail | todo | skip | 总数 | 门槛 |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    for p in PRIORITIES:
        row = report["by_priority"][p]
        gate_cell = "—"
        if p == "P0":
            gate_cell = "必过" + ("（已满足）" if row["fail"] == 0 and row["total"] else ("（不满足）" if row["fail"] else "（无用例）"))
        lines.append(
            f"| {p} | {row['pass']} | {row['fail']} | {row['todo']} | {row['skip']} | {row['total']} | {gate_cell} |"
        )
    lines.append("")

    lines.append("## 逐项结果")
    lines.append("")
    lines.append("| # | 用例 | 层 | 优先级 | 结果 | 耗时 |")
    lines.append("|---:|---|---|---|---|---:|")
    for i, r in enumerate(report["case_results"], 1):
        lines.append(
            f"| {i} | `{r['id']}` {_cell(r['title'])}"
            + (f"（429 重试 {r['retries']} 次）" if r.get("retries") else "")
            + f" | `{r['layer']}` | {r['priority']} | "
            f"{STATUS_MARK.get(r['status'], r['status'])} | {r['elapsed_ms']} ms |"
        )
    lines.append("")

    failed = [r for r in report["case_results"] if r["status"] == "fail"]
    if failed:
        lines.append("## 失败明细（期望 vs 实际）")
        lines.append("")
        for r in failed:
            lines.append(f"### `{r['id']}` {r['title']}")
            lines.append("")
            lines.append(
                f"- 层 `{r['layer']}` · 优先级 {r['priority']} · 耗时 {r['elapsed_ms']} ms"
                + (f" · 回归项 {', '.join(r['regression'])}" if r.get("regression") else "")
            )
            lines.append(f"- 目标（为什么测）：{_oneline(r['goal'])}")
            if r.get("error"):
                lines.append(f"- 执行错误：`{r['error']}`")
            lines.append("")
            if r["assertions"]:
                lines.append("| 断言 | 期望 | 实际 |")
                lines.append("|---|---|---|")
                for a in r["assertions"]:
                    if a["ok"]:
                        continue
                    lines.append(
                        f"| `{a['key']}` | {_code(a['expected'])} | {_code(a['actual'])} |"
                    )
                passed = [a for a in r["assertions"] if a["ok"]]
                if passed:
                    lines.append("")
                    lines.append("其余断言通过：" + "、".join(f"`{a['key']}`" for a in passed))
            lines.append("")
    else:
        lines.append("## 失败明细")
        lines.append("")
        lines.append("无失败断言。")
        lines.append("")

    todo = [r for r in report["case_results"] if r["status"] == "todo"]
    if todo:
        lines.append("## 待补（todo：尚未确认的断言，不计入通过率）")
        lines.append("")
        for r in todo:
            lines.append(f"- `{r['id']}` **{r['title']}** —— {_oneline(r['goal'])}")
        lines.append("")

    skipped = [r for r in report["case_results"] if r["status"] == "skip"]
    if skipped:
        lines.append("## 跳过（skip：人工标记本轮不跑）")
        lines.append("")
        for r in skipped:
            lines.append(f"- `{r['id']}` **{r['title']}** —— {_oneline(r['goal'])}")
        lines.append("")

    regressions = [r for r in report["case_results"] if r.get("regression")]
    if regressions:
        lines.append("## 回归项映射")
        lines.append("")
        lines.append("| 回归项 | 用例 | 结果 |")
        lines.append("|---|---|---|")
        for r in regressions:
            for tag in r["regression"]:
                lines.append(f"| `{tag}` | `{r['id']}` | {STATUS_MARK.get(r['status'], r['status'])} |")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- 控制台
def render_console(
    report: Mapping[str, Any], *, show_pass: bool = True, show_cases: bool = True
) -> str:
    suite = report["suite"]
    run = report["run"]
    s = report["summary"]
    out: List[str] = []
    out.append(f"MCP 能力验收 · {suite['service']}（suite {suite['suite']} v{suite['version']}）")
    token_line = run["token_masked"] + (f"  consumer={run['consumer']}" if run.get("consumer") else "")
    out.append(f"目标 {run['base_url']} · 令牌 {token_line} · 超时 {run['timeout_s']:g}s")
    out.append("")

    for r in report["case_results"] if show_cases else []:
        mark = STATUS_MARK.get(r["status"], r["status"])
        if r["status"] == "pass" and not show_pass:
            continue
        out.append(
            f"[{mark}] {r['id']:<22} {r['layer']}/{r['priority']:<2} {r['elapsed_ms']:>6}ms  {r['title']}"
        )
        if r["status"] == "fail":
            if r.get("error"):
                out.append(f"        执行错误：{r['error']}")
            for a in r["assertions"]:
                if a["ok"]:
                    continue
                out.append(
                    f"        {a['key']}: 期望 {_short(a['expected'])} / 实际 {_short(a['actual'])}"
                )

    out.append("")
    gate = {"pass": "PASS", "fail": "FAIL", "n/a": "无 P0 用例"}[s["p0_gate"]]
    out.append(
        f"结论 {s['verdict'].upper()} · 通过 {s['pass']} / 失败 {s['fail']} / 待补 {s['todo']} / "
        f"跳过 {s['skip']} · 断言 {s['assertions_pass']}/{s['assertions_total']} · "
        f"P0 门槛 {gate} · 用时 {run['elapsed_ms']}ms"
    )
    out.append("覆盖矩阵（layer: pass/fail/todo）：")
    for row in report["coverage"]:
        out.append(
            f"  {row['layer']:<11} {row['pass']}/{row['fail']}/{row['todo']}"
            f"  （{row['total']} 条）"
        )
    return "\n".join(out)


# --------------------------------------------------------------------------- 小工具
def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>").strip()


def _oneline(text: Any) -> str:
    text = str(text or "").strip().replace("\n", " ")
    return text if len(text) <= 220 else text[:220] + "…"


def _code(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    text = text.replace("|", "\\|").replace("\n", " ")
    if len(text) > 300:
        text = text[:300] + "…"
    return f"`{text}`" if text else '""'


def _short(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", " ")
    return text if len(text) <= 200 else text[:200] + "…"
