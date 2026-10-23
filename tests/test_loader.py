"""loader 校验的测试：每条错误都必须「可定位、说人话」。

这些用例保护的是验收资产的**可信度**：一条没有断言的用例看起来是绿的，
一条拼错的断言键看起来也是绿的。所以这里不只断言「报错了」，还断言错误里
出现了能定位的字段名与允许值清单。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from mcp_suite.loader import (
    SuiteConfigError,
    env_consumer_names,
    load_env_file,
    load_file,
    loads,
    mask_token,
    parse_env_value,
)
from tests.support import dump, make_case, make_suite, suite_dict


class LoaderValidTest(unittest.TestCase):
    def test_minimal_suite_loads(self):
        suite = make_suite([make_case("db-live-001")])
        self.assertEqual(suite.suite, "fake-suite")
        self.assertEqual(suite.version, 1)
        self.assertEqual(suite.defaults.base_url, "http://127.0.0.1:9")
        self.assertEqual(suite.defaults.timeout_s, 5.0)
        self.assertEqual([c.id for c in suite.cases], ["db-live-001"])
        self.assertEqual(suite.layers, ["liveness", "auth", "guard", "domain", "governance", "admin"])

    def test_tool_action_defaults_arguments_to_empty(self):
        case = make_case("c-1")
        case["action"] = {"kind": "tool", "tool": "list_datasources"}
        suite = make_suite([case])
        self.assertEqual(suite.cases[0].action.arguments, {})

    def test_http_action_auth_inferred_from_path(self):
        suite = make_suite([
            make_case("c-1", action={"kind": "http", "method": "GET", "path": "/ready"}),
            make_case("c-2", action={"kind": "http", "method": "GET", "path": "/admin/v1/health"}),
            make_case("c-3", action={"kind": "http", "method": "POST", "path": "/mcp",
                                     "body": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}}),
        ])
        self.assertEqual([c.action.resolved_auth() for c in suite.cases], ["none", "admin", "business"])

    def test_regression_accepts_string_or_list(self):
        suite = make_suite([
            make_case("c-1", regression="MCP-01"),
            make_case("c-2", regression=["MCP-02", "MCP-03"]),
        ])
        self.assertEqual(suite.cases[0].regression, ["MCP-01"])
        self.assertEqual(suite.cases[1].regression, ["MCP-02", "MCP-03"])

    def test_status_todo_is_kept_but_not_runnable(self):
        suite = make_suite([make_case("c-1", status="todo")])
        self.assertFalse(suite.cases[0].runnable)

    def test_select_by_layer_and_priority(self):
        suite = make_suite([
            make_case("c-1", layer="auth", priority="P0"),
            make_case("c-2", layer="guard", priority="P1"),
            make_case("c-3", layer="guard", priority="P0"),
        ])
        self.assertEqual([c.id for c in suite.select(["guard"])], ["c-2", "c-3"])
        self.assertEqual([c.id for c in suite.select(None, ["P0"])], ["c-1", "c-3"])
        self.assertEqual([c.id for c in suite.select(["guard"], ["P0"])], ["c-3"])
        self.assertEqual(len(suite.select()), 3)


class LoaderErrorTest(unittest.TestCase):
    def assert_error(self, text: str, *needles: str) -> None:
        with self.assertRaises(SuiteConfigError) as ctx:
            loads(text)
        message = str(ctx.exception)
        for needle in needles:
            self.assertIn(needle, message, f"错误信息里应该提到 {needle!r}，实际：\n{message}")

    def test_missing_goal(self):
        case = make_case("c-1")
        del case["goal"]
        self.assert_error(dump(suite_dict([case])), "cases[0].goal", "为什么值得测")

    def test_missing_action(self):
        case = make_case("c-1")
        del case["action"]
        self.assert_error(dump(suite_dict([case])), "cases[0].action", "kind: tool | http")

    def test_missing_expect(self):
        case = make_case("c-1")
        del case["expect"]
        self.assert_error(dump(suite_dict([case])), "cases[0].expect", "预期指标")

    def test_goal_too_short(self):
        self.assert_error(dump(suite_dict([make_case("c-1", goal="测一下")])), "goal")

    def test_unknown_expect_key(self):
        case = make_case("c-1", expect={"json_equal": 1})
        self.assert_error(
            dump(suite_dict([case])),
            "未知断言键 'json_equal'",
            "json_equals",
            "rows_equals",
        )

    def test_empty_expect(self):
        self.assert_error(dump(suite_dict([make_case("c-1", expect={})])), "expect 不能为空")

    def test_duplicate_id(self):
        text = dump(suite_dict([make_case("db-live-001"), make_case("db-live-001", title="第二条")]))
        self.assert_error(text, "cases[1].id", "重复", "cases[0]")

    def test_bad_id_charset(self):
        self.assert_error(dump(suite_dict([make_case("DB_Live_001")])), "非法")

    def test_missing_top_level(self):
        doc = suite_dict([make_case()])
        del doc["suite"]
        self.assert_error(dump(doc), "缺少顶层字段", "suite")

    def test_unknown_top_level_key(self):
        doc = suite_dict([make_case()])
        doc["case"] = []
        self.assert_error(dump(doc), "未知字段 'case'", "cases")

    def test_unknown_layer(self):
        self.assert_error(dump(suite_dict([make_case("c-1", layer="performance")])), "layer", "performance")

    def test_bad_priority(self):
        self.assert_error(dump(suite_dict([make_case("c-1", priority="P9")])), "priority", "P0/P1/P2")

    def test_bad_version(self):
        self.assert_error(dump(suite_dict(version=0)), "version")

    def test_bad_updated(self):
        self.assert_error(dump(suite_dict(updated="2026/10/23")), "updated", "YYYY-MM-DD")

    def test_bad_defaults_timeout(self):
        doc = suite_dict([make_case()])
        doc["defaults"]["timeout_s"] = 0
        self.assert_error(dump(doc), "defaults.timeout_s", "正数")

    def test_bad_token_env(self):
        doc = suite_dict([make_case()])
        doc["defaults"]["token_env"] = "sk-looks-like-a-token"
        self.assert_error(dump(doc), "defaults.token_env", "环境变量名")

    def test_tool_action_requires_tool_name(self):
        self.assert_error(
            dump(suite_dict([make_case("c-1", action={"kind": "tool"})])),
            "cases[0].action.tool",
        )

    def test_tool_action_rejects_http_fields(self):
        case = make_case("c-1", action={"kind": "tool", "tool": "x", "path": "/health"})
        self.assert_error(dump(suite_dict([case])), "未知字段 'path'")

    def test_http_action_rejects_tool_fields(self):
        case = make_case("c-1", action={"kind": "http", "method": "GET", "path": "/health", "tool": "x"})
        self.assert_error(dump(suite_dict([case])), "未知字段 'tool'")

    def test_http_bad_method(self):
        case = make_case("c-1", action={"kind": "http", "method": "FETCH", "path": "/health"})
        self.assert_error(dump(suite_dict([case])), "action.method")

    def test_http_path_must_start_with_slash(self):
        case = make_case("c-1", action={"kind": "http", "method": "GET", "path": "health"})
        self.assert_error(dump(suite_dict([case])), "action.path")

    def test_unknown_action_kind(self):
        self.assert_error(
            dump(suite_dict([make_case("c-1", action={"kind": "grpc"})])),
            "action.kind",
        )

    def test_json_equals_without_json_path(self):
        self.assert_error(dump(suite_dict([make_case("c-1", expect={"json_equals": 1})])), "json_path")

    def test_json_path_without_consumer(self):
        self.assert_error(dump(suite_dict([make_case("c-1", expect={"json_path": "a.b"})])), "json_path")

    def test_bad_json_len_type(self):
        case = make_case("c-1", expect={"json_path": "a", "json_len": "4"})
        self.assert_error(dump(suite_dict([case])), "json_len", "整数")

    def test_bad_is_error_type(self):
        self.assert_error(dump(suite_dict([make_case("c-1", expect={"is_error": "true"})])), "is_error")

    def test_bad_status_type(self):
        self.assert_error(dump(suite_dict([make_case("c-1", expect={"status": "200"})])), "status")

    def test_rows_equals_must_be_list(self):
        self.assert_error(
            dump(suite_dict([make_case("c-1", expect={"rows_equals": 1})])),
            "rows_equals",
        )

    def test_unknown_case_field(self):
        case = make_case("c-1")
        case["expects"] = {"is_error": False}
        self.assert_error(dump(suite_dict([case])), "未知字段 'expects'")

    def test_multiple_errors_reported_together(self):
        cases = [
            make_case("c-1", expect={"nope": 1}),
            make_case("c-1", layer="nope"),
            make_case("c-3", action={"kind": "tool"}),
        ]
        with self.assertRaises(SuiteConfigError) as ctx:
            loads(dump(suite_dict(cases)))
        self.assertGreaterEqual(len(ctx.exception.errors), 4)

    def test_yaml_syntax_error_is_config_error(self):
        self.assert_error("suite: [unclosed", "YAML 语法错误")

    def test_file_not_found(self):
        with self.assertRaises(SuiteConfigError) as ctx:
            load_file("/nonexistent/mcp-test-suite.yaml")
        self.assertIn("无法读取文件", str(ctx.exception))


class LoaderHelpersTest(unittest.TestCase):
    def test_mask_token_shows_only_prefix(self):
        masked = mask_token("sk-demo-0123456789abcdef0123456789abcdef")
        self.assertTrue(masked.startswith("sk-demo-"))
        self.assertNotIn("0123456789abcdef", masked)
        self.assertIn("len 40", masked)
        self.assertEqual(mask_token(None), "<未设置>")

    def test_parse_plain_token(self):
        self.assertEqual(parse_env_value("sk-abc"), ("sk-abc", None))

    def test_parse_token_list_takes_first(self):
        self.assertEqual(parse_env_value("tok1:client,tok2:admin"), ("tok1", "client"))

    def test_parse_token_list_by_consumer(self):
        self.assertEqual(parse_env_value("tok1:client,tok2:admin", "admin"), ("tok2", "admin"))

    def test_parse_token_list_consumer_missing(self):
        self.assertEqual(parse_env_value("tok1:client", "ghost"), (None, None))

    def test_consumer_names(self):
        self.assertEqual(env_consumer_names("tok1:client,tok2:admin"), ["client", "admin"])
        self.assertEqual(env_consumer_names(""), [])

    def test_env_file_does_not_override_existing(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, ".env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# 注释行\nA=1\nexport B='two'\nBAD LINE\nC=\"three\"\n")
        env = {"A": "keep"}
        skipped = load_env_file(path, env)
        self.assertEqual(env["A"], "keep")
        self.assertEqual(env["B"], "two")
        self.assertEqual(env["C"], "three")
        self.assertEqual(skipped, ["A"])


if __name__ == "__main__":
    unittest.main()
