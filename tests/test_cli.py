"""CLI 测试：退出码 0/1/2、层过滤、报告落盘、令牌来源。

退出码是给 CI 与 Makefile 用的契约：
`0 全过 / 1 有失败 / 2 配置错误`。把「配置错」混进「失败」会让夜间任务
把「套件本身坏了」误报成「服务不达标」。
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from mcp_suite.cli import main
from tests.fake_server import FakeMcpServer
from tests.support import make_case, suite_dict, write_yaml


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class CliRunTest(unittest.TestCase):
    def setUp(self):
        self.srv = FakeMcpServer().start()
        self.addCleanup(self.srv.stop)
        self.tmp = tempfile.mkdtemp()

    def defaults(self, **over):
        base = {
            "base_url": self.srv.base_url,
            "timeout_s": 5,
            "token_env": "MCP_TEST_TOKEN",
            "admin_token_env": "MCP_TEST_ADMIN_TOKEN",
        }
        base.update(over)
        return base

    def suite_path(self, cases, **over):
        doc = suite_dict(cases, defaults=self.defaults(**over))
        return write_yaml(self.tmp, doc)

    def test_exit_0_when_all_pass(self):
        path = self.suite_path([make_case("c-1", expect={"is_error": False})])
        env = {"MCP_TEST_TOKEN": "test-token", "MCP_TEST_ADMIN_TOKEN": "admin-token"}
        with mock.patch.dict(os.environ, env, clear=False):
            code, out, err = run_cli(["run", "--suite", path])
        self.assertEqual(code, 0, err + out)
        self.assertIn("结论 PASS", out)
        self.assertIn("覆盖矩阵", out)

    def test_exit_1_when_a_case_fails(self):
        path = self.suite_path([make_case("c-1", expect={"error_type": "WRONG"})])
        env = {"MCP_TEST_TOKEN": "test-token"}
        with mock.patch.dict(os.environ, env, clear=False):
            code, out, err = run_cli(["run", "--suite", path])
        self.assertEqual(code, 1, err + out)
        # 失败必须当场打印期望 vs 实际
        self.assertIn("期望 WRONG / 实际", out)

    def test_exit_2_on_config_error(self):
        path = os.path.join(self.tmp, "broken.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("suite: broken\nversion: 1\n")
        code, out, err = run_cli(["run", "--suite", path])
        self.assertEqual(code, 2)
        self.assertIn("缺少顶层字段", err)

    def test_exit_2_when_token_missing(self):
        path = self.suite_path([make_case("c-1")])
        with mock.patch.dict(os.environ, {}, clear=True):
            code, out, err = run_cli(["run", "--suite", path])
        self.assertEqual(code, 2)
        self.assertIn("MCP_TEST_TOKEN", err)

    def test_liveness_only_does_not_need_token(self):
        path = self.suite_path([
            make_case("c-live", layer="liveness",
                      action={"kind": "http", "method": "GET", "path": "/health"},
                      expect={"status": 200}),
            make_case("c-domain", layer="domain"),
        ])
        with mock.patch.dict(os.environ, {}, clear=True):
            code, out, err = run_cli(["run", "--suite", path, "--layer", "liveness"])
        self.assertEqual(code, 0, err + out)
        self.assertIn("c-live", out)
        self.assertNotIn("c-domain", out)

    def test_layer_filter_records_filter_in_json(self):
        path = self.suite_path([
            make_case("c-live", layer="liveness",
                      action={"kind": "http", "method": "GET", "path": "/health"},
                      expect={"status": 200}),
            make_case("c-domain", layer="domain"),
        ])
        out_json = os.path.join(self.tmp, "r.json")
        with mock.patch.dict(os.environ, {"MCP_TEST_TOKEN": "test-token"}, clear=False):
            code, _, err = run_cli(["run", "--suite", path, "--layer", "liveness", "--json", out_json])
        self.assertEqual(code, 0, err)
        with open(out_json, encoding="utf-8") as fh:
            report = json.loads(fh.read())
        self.assertEqual(report["run"]["filters"]["layers"], ["liveness"])
        self.assertEqual(len(report["case_results"]), 1)
        self.assertEqual(report["case_results"][0]["id"], "c-live")

    def test_priority_filter(self):
        path = self.suite_path([
            make_case("c-p0", priority="P0", expect={"error_type": "WRONG"}),
            make_case("c-p1", priority="P1", expect={"is_error": False}),
        ])
        with mock.patch.dict(os.environ, {"MCP_TEST_TOKEN": "test-token"}, clear=False):
            code, out, err = run_cli(["run", "--suite", path, "--priority", "P1"])
        self.assertEqual(code, 0, err + out)
        self.assertIn("c-p1", out)
        self.assertNotIn("c-p0 ", out)

    def test_unknown_layer_is_config_error(self):
        path = self.suite_path([make_case("c-1")])
        with mock.patch.dict(os.environ, {"MCP_TEST_TOKEN": "test-token"}, clear=False):
            code, _, err = run_cli(["run", "--suite", path, "--layer", "performance"])
        self.assertEqual(code, 2)
        self.assertIn("不在套件声明的层里", err)

    def test_json_and_markdown_written(self):
        path = self.suite_path([make_case("c-1", expect={"is_error": False})])
        out_json = os.path.join(self.tmp, "out", "r.json")
        out_md = os.path.join(self.tmp, "out", "r.md")
        with mock.patch.dict(os.environ, {"MCP_TEST_TOKEN": "test-token"}, clear=False):
            code, _, err = run_cli(
                ["run", "--suite", path, "--json", out_json, "--md", out_md]
            )
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(out_json))
        self.assertTrue(os.path.exists(out_md))
        with open(out_md, encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("# MCP 能力验收报告", md)
        self.assertIn("## 覆盖矩阵", md)

    def test_env_file_supplies_token(self):
        path = self.suite_path([make_case("c-1", expect={"is_error": False})])
        env_file = os.path.join(self.tmp, "service.env")
        with open(env_file, "w", encoding="utf-8") as fh:
            fh.write("MCP_TEST_TOKEN=test-token\nMCP_TEST_ADMIN_TOKEN=admin-token\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            code, _, err = run_cli(["run", "--suite", path, "--env-file", env_file])
        self.assertEqual(code, 0, err)

    def test_consumer_selection_by_cli(self):
        path = self.suite_path(
            [make_case("c-1", expect={"is_error": False})],
            token_env="FAKE_TOKENS",
        )
        env = {"FAKE_TOKENS": "bad:wrong,test-token:tester"}
        with mock.patch.dict(os.environ, env, clear=False):
            code, out, err = run_cli(["run", "--suite", path, "--consumer", "tester"])
        self.assertEqual(code, 0, err + out)
        self.assertIn("consumer=tester", out)

    def test_consumer_missing_is_config_error(self):
        path = self.suite_path([make_case("c-1")], token_env="FAKE_TOKENS")
        env = {"FAKE_TOKENS": "t1:client"}
        with mock.patch.dict(os.environ, env, clear=False):
            code, _, err = run_cli(["run", "--suite", path, "--consumer", "ghost"])
        self.assertEqual(code, 2)
        self.assertIn("没有消费者", err)
        self.assertIn("client", err)  # 报错要列出可选消费者（名字，不是令牌）

    def test_todo_is_not_a_failure(self):
        path = self.suite_path([make_case("c-1", status="todo")])
        with mock.patch.dict(os.environ, {"MCP_TEST_TOKEN": "test-token"}, clear=False):
            code, out, err = run_cli(["run", "--suite", path])
        self.assertEqual(code, 0, err + out)
        self.assertIn("待补 1", out)

    def test_base_url_override(self):
        path = self.suite_path(
            [make_case("c-1", action={"kind": "http", "method": "GET", "path": "/health"},
                       expect={"status": 200})],
            base_url="http://127.0.0.1:9",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            code, _, err = run_cli(["run", "--suite", path, "--base-url", self.srv.base_url])
        self.assertEqual(code, 0, err)

    def test_quiet_mode(self):
        path = self.suite_path([make_case("c-1")])
        with mock.patch.dict(os.environ, {"MCP_TEST_TOKEN": "test-token"}, clear=False):
            code, out, err = run_cli(["run", "--suite", path, "--quiet"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("[PASS]", out)


class CliValidateDocTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_validate_ok(self):
        path = write_yaml(self.tmp, suite_dict([make_case("c-1")]))
        code, out, err = run_cli(["validate", "--suite", path])
        self.assertEqual(code, 0, err)
        self.assertIn("OK", out)
        self.assertIn("1 条用例", out)

    def test_validate_config_error(self):
        path = os.path.join(self.tmp, "bad.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("suite: x\n")
        code, _, err = run_cli(["validate", "--suite", path])
        self.assertEqual(code, 2)
        self.assertIn("缺少顶层字段", err)

    def test_doc_command_writes_next_to_yaml(self):
        path = write_yaml(self.tmp, suite_dict([make_case("db-guard-003")]))
        code, out, err = run_cli(["doc", "--suite", path])
        self.assertEqual(code, 0, err)
        target = os.path.join(self.tmp, "MCP-TEST-SUITE.md")
        self.assertTrue(os.path.exists(target))
        with open(target, encoding="utf-8") as fh:
            self.assertIn("db-guard-003", fh.read())

    def test_doc_command_with_explicit_out(self):
        path = write_yaml(self.tmp, suite_dict([make_case("c-1")]))
        out_path = os.path.join(self.tmp, "docs", "SUITE.md")
        code, _, err = run_cli(["doc", "--suite", path, "--out", out_path])
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(out_path))


if __name__ == "__main__":
    unittest.main()
