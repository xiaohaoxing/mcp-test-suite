"""emit_doc 测试：人读版必须保留三件套、覆盖矩阵与回归映射。

这份 Markdown 会进 git、会被贴给客户。它最容易出的问题是**和 YAML 漂移**，
所以这里除了断言内容，还断言「同一份 YAML 渲染两次结果完全一致」（可复现）。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from mcp_suite.emit_doc import main as emit_main
from mcp_suite.emit_doc import render_doc, write_doc
from tests.support import make_case, make_suite, suite_dict, write_yaml


class EmitDocTest(unittest.TestCase):
    def setUp(self):
        self.suite = make_suite([
            make_case(
                "db-guard-003",
                layer="guard",
                priority="P0",
                title="资源消耗函数（SLEEP）必须被拒",
                goal="单条 SLEEP 可占住数据库连接 30 秒，且 MySQL 在打断 SLEEP 时返回 1 而不是报错。",
                action={"kind": "tool", "tool": "query",
                        "arguments": {"datasource": "<datasource-name>", "customer_id": "<tenant-id>",
                                      "sql": "SELECT SLEEP(1)"}},
                expect={"is_error": True, "error_type": "SQL_NOT_ALLOWED", "max_elapsed_ms": 2000},
                metric="拒绝率 100%；P95 响应 < 2s",
                regression="MCP-03",
            ),
            make_case(
                "db-admin-001",
                layer="admin",
                priority="P1",
                action={"kind": "http", "method": "GET", "path": "/admin/v1/health"},
                expect={"status": 200, "json_path": "version", "json_equals": "0.3.0"},
                status="todo",
            ),
        ])
        self.md = render_doc(self.suite)

    def test_title_and_header(self):
        self.assertIn("# MCP 能力验收套件 · fake-mcp", self.md)
        self.assertIn("请勿手工编辑", self.md)

    def test_overview_shows_env_names_not_tokens(self):
        self.assertIn("MCP_TEST_TOKEN", self.md)
        self.assertIn("MCP_TEST_ADMIN_TOKEN", self.md)
        self.assertNotIn("Bearer", self.md)

    def test_three_column_table(self):
        self.assertIn("目标（为什么测）", self.md)
        self.assertIn("操作（怎么触发）", self.md)
        self.assertIn("预期指标（怎么判定通过）", self.md)

    def test_goal_is_the_reason_not_the_action(self):
        self.assertIn("单条 SLEEP 可占住数据库连接 30 秒", self.md)
        self.assertIn("指标：拒绝率 100%", self.md)

    def test_action_rendered(self):
        self.assertIn("tool `query`", self.md)
        self.assertIn("SELECT SLEEP(1)", self.md)
        self.assertIn("`GET /admin/v1/health`（管理令牌）", self.md)

    def test_expect_rendered(self):
        self.assertIn("`error_type` = `SQL_NOT_ALLOWED`", self.md)
        self.assertIn("`max_elapsed_ms` <= 2000", self.md)

    def test_todo_marked(self):
        self.assertIn("P1（todo）", self.md)

    def test_coverage_matrix(self):
        self.assertIn("## 覆盖矩阵（layer × priority）", self.md)
        self.assertIn("| `guard` |", self.md)
        self.assertIn("| `admin` |", self.md)

    def test_regression_mapping(self):
        self.assertIn("## 回归项映射", self.md)
        self.assertIn("`MCP-03`", self.md)

    def test_expect_keys_documented(self):
        self.assertIn("`rows_equals`", self.md)
        self.assertIn("`min_rows`", self.md)

    def test_deterministic(self):
        self.assertEqual(render_doc(self.suite), render_doc(self.suite))

    def test_write_doc_creates_parent_dirs(self):
        tmp = tempfile.mkdtemp()
        out = os.path.join(tmp, "a", "b", "MCP-TEST-SUITE.md")
        write_doc(self.suite, out)
        self.assertTrue(os.path.exists(out))

    def test_module_main(self):
        tmp = tempfile.mkdtemp()
        path = write_yaml(tmp, suite_dict([make_case("c-1")]))
        code = emit_main(["--suite", path])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(tmp, "MCP-TEST-SUITE.md")))

    def test_module_main_config_error(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "bad.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("suite: x\n")
        self.assertEqual(emit_main(["--suite", path]), 2)


if __name__ == "__main__":
    unittest.main()
