"""报告层测试：覆盖矩阵、P0 门槛、以及「期望 vs 实际」必须出现在 Markdown 里。

这些是验收结论的**唯一口径**，口径错了比没有报告更糟：
一份把 P1 失败算成「通过」的报告会让上线决策直接反掉。
"""

from __future__ import annotations

import json
import unittest

from mcp_suite.assertions import Observation, check_expect
from mcp_suite.report import build_report, render_console, render_json, render_markdown
from mcp_suite.runner import CaseResult
from tests.support import make_case, make_suite


def result(case, status, *, expect_actual=None, elapsed=10):
    assertions = []
    if expect_actual is not None:
        obs = Observation(
            kind="tool",
            status=expect_actual.get("status", 200),
            is_error=expect_actual.get("is_error", False),
            error_type=expect_actual.get("error_type"),
            json_doc=expect_actual.get("json_doc", {}),
            text=expect_actual.get("text", ""),
            rows=expect_actual.get("rows"),
            elapsed_ms=expect_actual.get("elapsed_ms", elapsed),
        )
        assertions = check_expect(case.expect, obs)
    return CaseResult(case=case, status=status, elapsed_ms=elapsed, assertions=assertions)


class ReportBuildTest(unittest.TestCase):
    def setUp(self):
        self.suite = make_suite([
            make_case("c-live-1", layer="liveness", priority="P0", expect={"status": 200}),
            make_case("c-live-2", layer="liveness", priority="P1", expect={"status": 200}),
            make_case("c-guard-1", layer="guard", priority="P0",
                      expect={"error_type": "SQL_NOT_ALLOWED", "is_error": True}),
            make_case("c-domain-1", layer="domain", priority="P2", expect={"is_error": False}, status="todo"),
        ])

    def build(self, results, **over):
        kwargs = dict(
            base_url="http://example.test",
            token_masked="sk-test-…",
            consumer="tester",
            timeout_s=5,
            started_at="2026-10-23T17:00:00+08:00",
            elapsed_ms=1234,
        )
        kwargs.update(over)
        return build_report(self.suite, results, **kwargs)

    def test_summary_and_verdict_pass(self):
        rs = [
            result(self.suite.cases[0], "pass", expect_actual={"status": 200}),
            result(self.suite.cases[1], "pass", expect_actual={"status": 200}),
            result(self.suite.cases[2], "pass", expect_actual={"is_error": True, "error_type": "SQL_NOT_ALLOWED"}),
            result(self.suite.cases[3], "todo"),
        ]
        report = self.build(rs)
        self.assertEqual(report["summary"]["pass"], 3)
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertEqual(report["summary"]["todo"], 1)
        self.assertEqual(report["summary"]["verdict"], "pass")
        self.assertEqual(report["summary"]["p0_gate"], "pass")

    def test_p0_failure_fails_gate(self):
        rs = [
            result(self.suite.cases[0], "pass", expect_actual={"status": 200}),
            result(self.suite.cases[2], "fail",
                   expect_actual={"is_error": False, "error_type": "ALLOWED"}),
            result(self.suite.cases[3], "todo"),
        ]
        report = self.build(rs)
        self.assertEqual(report["summary"]["verdict"], "fail")
        self.assertEqual(report["summary"]["p0_gate"], "fail")
        self.assertEqual(report["by_priority"]["P0"]["fail"], 1)

    def test_p1_failure_fails_verdict_but_not_p0_gate(self):
        rs = [
            result(self.suite.cases[0], "pass", expect_actual={"status": 200}),
            result(self.suite.cases[1], "fail", expect_actual={"status": 503}),
            result(self.suite.cases[2], "pass", expect_actual={"is_error": True, "error_type": "SQL_NOT_ALLOWED"}),
        ]
        report = self.build(rs)
        self.assertEqual(report["summary"]["verdict"], "fail")
        self.assertEqual(report["summary"]["p0_gate"], "pass")

    def test_coverage_matrix_counts_every_declared_layer(self):
        rs = [
            result(self.suite.cases[0], "pass", expect_actual={"status": 200}),
            result(self.suite.cases[1], "fail", expect_actual={"status": 503}),
            result(self.suite.cases[2], "pass", expect_actual={"is_error": True, "error_type": "SQL_NOT_ALLOWED"}),
            result(self.suite.cases[3], "todo"),
        ]
        report = self.build(rs)
        rows = {row["layer"]: row for row in report["coverage"]}
        self.assertEqual(len(rows), 6)  # 六层都出现，0 条也要出现（0 条 = 没测）
        self.assertEqual(rows["liveness"]["pass"], 1)
        self.assertEqual(rows["liveness"]["fail"], 1)
        self.assertEqual(rows["guard"]["pass"], 1)
        self.assertEqual(rows["domain"]["todo"], 1)
        self.assertEqual(rows["auth"]["total"], 0)

    def test_json_serializable(self):
        rs = [result(self.suite.cases[0], "pass", expect_actual={"status": 200})]
        text = render_json(self.build(rs))
        doc = json.loads(text)
        self.assertEqual(doc["case_results"][0]["id"], "c-live-1")
        self.assertIn("coverage", doc)


class MarkdownTest(unittest.TestCase):
    def setUp(self):
        self.suite = make_suite([
            make_case("c-guard-003", layer="guard", priority="P0",
                      title="资源消耗函数（SLEEP）必须被拒",
                      expect={"is_error": True, "error_type": "SQL_NOT_ALLOWED", "max_elapsed_ms": 2000}),
            make_case("c-domain-001", layer="domain", priority="P1",
                      title="正常查询能出数", expect={"is_error": False}, status="todo"),
        ])
        self.failed = result(
            self.suite.cases[0],
            "fail",
            expect_actual={"is_error": True, "error_type": "ALLOWED_INSTEAD", "elapsed_ms": 4100},
            elapsed=4100,
        )
        self.report = build_report(
            self.suite,
            [self.failed, result(self.suite.cases[1], "todo")],
            base_url="http://example.test",
            token_masked="sk-db-ca…(len 52)",
            consumer="demo-consumer",
            timeout_s=30,
            started_at="2026-10-23T17:00:00+08:00",
            elapsed_ms=900,
        )
        self.md = render_markdown(self.report)

    def test_header_and_token_masked(self):
        self.assertIn("# MCP 能力验收报告 · fake-mcp", self.md)
        self.assertIn("sk-db-ca…(len 52)", self.md)
        self.assertIn("demo-consumer", self.md)

    def test_verdict_line(self):
        self.assertIn("**FAIL**", self.md)
        self.assertIn("0 通过 / 1 失败 / 1 待补", self.md)  # todo 既不算 pass 也不算 fail
        self.assertIn("P0 门槛：FAIL", self.md)

    def test_coverage_matrix_present(self):
        self.assertIn("## 覆盖矩阵", self.md)
        self.assertIn("| `liveness` |", self.md)
        self.assertIn("| `guard` |", self.md)

    def test_priority_summary_present(self):
        self.assertIn("## 按优先级汇总", self.md)
        self.assertIn("| P0 |", self.md)

    def test_failure_shows_expected_vs_actual(self):
        self.assertIn("## 失败明细（期望 vs 实际）", self.md)
        self.assertIn("`error_type`", self.md)
        self.assertIn("SQL_NOT_ALLOWED", self.md)
        self.assertIn("ALLOWED_INSTEAD", self.md)
        self.assertIn("<= 2000 ms", self.md)
        self.assertIn("4100 ms", self.md)

    def test_todo_section(self):
        self.assertIn("## 待补", self.md)
        self.assertIn("c-domain-001", self.md)

    def test_per_case_table_has_status_and_elapsed(self):
        self.assertIn("| 结果 | 耗时 |", self.md)
        self.assertIn("FAIL", self.md)
        self.assertIn("4100 ms", self.md)


class ConsoleTest(unittest.TestCase):
    def test_console_prints_expected_vs_actual(self):
        suite = make_suite([make_case("c-1", expect={"error_type": "SQL_NOT_ALLOWED"})])
        bad = result(suite.cases[0], "fail", expect_actual={"error_type": "UPSTREAM_UNAVAILABLE"})
        report = build_report(
            suite, [bad], base_url="http://x", token_masked="sk-…", consumer=None,
            timeout_s=5, started_at="t", elapsed_ms=1,
        )
        text = render_console(report)
        self.assertIn("期望 SQL_NOT_ALLOWED / 实际 UPSTREAM_UNAVAILABLE", text)
        self.assertIn("结论 FAIL", text)
        self.assertIn("覆盖矩阵", text)


if __name__ == "__main__":
    unittest.main()
