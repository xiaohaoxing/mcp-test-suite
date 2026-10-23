"""断言层的测试：12 个键都要有 pass/fail 两个分支。

fail 分支不只断言 `ok is False`，还要断言 `expected` / `actual` 都被保留下来 ——
「期望 vs 实际」是这份工具的核心交付物，丢了它，失败报告就退化成一句 FAIL。
"""

from __future__ import annotations

import unittest

from mcp_suite.assertions import Observation, check_expect, resolve_json_path, summarize


def obs_tool(**kwargs):
    base = dict(
        kind="tool",
        status=200,
        is_error=False,
        error_type=None,
        structured={"ok": True, "rows": [[1]], "nested": {"a": [1, 2], "b": "x"}, "text": "hello world"},
        json_doc={"ok": True, "rows": [[1]], "nested": {"a": [1, 2], "b": "x"}, "text": "hello world"},
        text="hello world",
        rows=[[1]],
        elapsed_ms=120,
        tool="echo",
    )
    base.update(kwargs)
    return Observation(**base)


def one(expect, obs):
    results = check_expect(expect, obs)
    assert len(results) == 1, f"期望 1 条断言，实际 {len(results)}"
    return results[0]


class StatusTest(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(one({"status": 200}, obs_tool(status=200)).ok)

    def test_fail_keeps_expected_and_actual(self):
        r = one({"status": 200}, obs_tool(status=503))
        self.assertFalse(r.ok)
        self.assertEqual(r.expected, 200)
        self.assertEqual(r.actual, 503)


class IsErrorTest(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(one({"is_error": True}, obs_tool(is_error=True)).ok)

    def test_default_false_when_absent(self):
        # 没有 isError 字段的响应按 false 处理（MCP 语义）
        self.assertTrue(one({"is_error": False}, obs_tool(is_error=False)).ok)

    def test_fail(self):
        r = one({"is_error": True}, obs_tool(is_error=False))
        self.assertFalse(r.ok)
        self.assertEqual(r.expected, True)
        self.assertEqual(r.actual, False)


class ErrorTypeTest(unittest.TestCase):
    def test_pass(self):
        r = one({"error_type": "SQL_NOT_ALLOWED"}, obs_tool(error_type="SQL_NOT_ALLOWED", is_error=True))
        self.assertTrue(r.ok)

    def test_fail_prints_both(self):
        r = one({"error_type": "SQL_NOT_ALLOWED"}, obs_tool(error_type="UPSTREAM_UNAVAILABLE"))
        self.assertFalse(r.ok)
        self.assertEqual(r.expected, "SQL_NOT_ALLOWED")
        self.assertEqual(r.actual, "UPSTREAM_UNAVAILABLE")


class JsonPathTest(unittest.TestCase):
    def test_nested_key(self):
        self.assertEqual(resolve_json_path({"a": {"b": 1}}, "a.b"), 1)

    def test_index(self):
        self.assertEqual(resolve_json_path({"a": [10, 20]}, "a[1]"), 20)

    def test_hyphenated_key(self):
        self.assertEqual(resolve_json_path({"checks": {"b2b-qa": {"reachable": False}}},
                                           "checks.b2b-qa.reachable"), False)

    def test_json_string_mid_path_is_unwrapped(self):
        doc = {"result": '{"verdict": "PASS", "items": [1, 2, 3]}'}
        self.assertEqual(resolve_json_path(doc, "result.verdict"), "PASS")
        self.assertEqual(resolve_json_path(doc, "result.items[2]"), 3)

    def test_missing_key_raises(self):
        with self.assertRaises(LookupError):
            resolve_json_path({"a": 1}, "b")

    def test_out_of_range_raises(self):
        with self.assertRaises(LookupError):
            resolve_json_path({"a": [1]}, "a[3]")


class JsonEqualsTest(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(one({"json_path": "nested.b", "json_equals": "x"}, obs_tool()).ok)

    def test_fail(self):
        r = one({"json_path": "nested.b", "json_equals": "y"}, obs_tool())
        self.assertFalse(r.ok)
        self.assertEqual(r.expected, "y")
        self.assertEqual(r.actual, "x")

    def test_list_deep_equality(self):
        self.assertTrue(one({"json_path": "rows", "json_equals": [[1]]}, obs_tool()).ok)

    def test_bool_is_not_int(self):
        obs = obs_tool(json_doc={"flag": True})
        r = one({"json_path": "flag", "json_equals": 1}, obs)
        self.assertFalse(r.ok)

    def test_int_equals_float(self):
        obs = obs_tool(json_doc={"n": 1})
        self.assertTrue(one({"json_path": "n", "json_equals": 1.0}, obs).ok)

    def test_bad_path_fails_with_reason(self):
        r = one({"json_path": "ghost.key", "json_equals": 1}, obs_tool())
        self.assertFalse(r.ok)
        self.assertIn("路径不可达", str(r.actual))
        self.assertIn("ghost", r.detail)


class JsonContainsTest(unittest.TestCase):
    def test_list_contains_scalar(self):
        self.assertTrue(one({"json_path": "nested.a", "json_contains": 2}, obs_tool()).ok)

    def test_list_contains_dict_subset(self):
        doc = {"tools": [{"name": "query", "x": 1}, {"name": "list_tables"}]}
        obs = obs_tool(json_doc=doc)
        self.assertTrue(one({"json_path": "tools", "json_contains": {"name": "list_tables"}}, obs).ok)
        self.assertFalse(one({"json_path": "tools", "json_contains": {"name": "ghost"}}, obs).ok)

    def test_dict_subset(self):
        self.assertTrue(one({"json_path": "nested", "json_contains": {"b": "x"}}, obs_tool()).ok)
        self.assertFalse(one({"json_path": "nested", "json_contains": {"b": "y"}}, obs_tool()).ok)

    def test_string_substring(self):
        self.assertTrue(one({"json_path": "text", "json_contains": "hello"}, obs_tool()).ok)
        self.assertFalse(one({"json_path": "text", "json_contains": "bye"}, obs_tool()).ok)


class JsonLenTest(unittest.TestCase):
    def test_list_len_pass(self):
        r = one({"json_path": "nested.a", "json_len": 2}, obs_tool())
        self.assertTrue(r.ok)
        self.assertEqual(r.actual, 2)

    def test_list_len_fail(self):
        r = one({"json_path": "nested.a", "json_len": 3}, obs_tool())
        self.assertFalse(r.ok)
        self.assertEqual(r.actual, 2)

    def test_not_sized(self):
        r = one({"json_path": "nested.b", "json_len": 1}, obs_tool())
        # str 有长度：'x' 长度 1 → 通过
        self.assertTrue(r.ok)


class TextTest(unittest.TestCase):
    def test_contains_pass(self):
        self.assertTrue(one({"contains": "hello"}, obs_tool()).ok)

    def test_contains_fail_keeps_text(self):
        r = one({"contains": "goodbye"}, obs_tool())
        self.assertFalse(r.ok)
        self.assertEqual(r.expected, "goodbye")
        self.assertIn("hello world", str(r.actual))

    def test_not_contains_pass(self):
        self.assertTrue(one({"not_contains": "sk-secret-"}, obs_tool(text="token clean leaked")).ok)

    def test_not_contains_fail(self):
        # 文本里必须**真的含有**那个 needle，否则 not_contains 会正确地判"通过"，
        # 用例就变成永远绿的空断言（我改夹具时把尾部连字符改丢了，正是这么红的）。
        r = one({"not_contains": "sk-secret-"}, obs_tool(text="token sk-secret-abc leaked"))
        self.assertFalse(r.ok)
        self.assertIn("sk-secret-", str(r.expected))


class ElapsedTest(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(one({"max_elapsed_ms": 200}, obs_tool(elapsed_ms=120)).ok)

    def test_fail(self):
        r = one({"max_elapsed_ms": 100}, obs_tool(elapsed_ms=120))
        self.assertFalse(r.ok)
        self.assertEqual(r.expected, "<= 100 ms")
        self.assertEqual(r.actual, "120 ms")


class RowsTest(unittest.TestCase):
    def test_rows_equals_pass(self):
        self.assertTrue(one({"rows_equals": [[1]]}, obs_tool(rows=[[1]])).ok)

    def test_rows_equals_fail(self):
        r = one({"rows_equals": [[2]]}, obs_tool(rows=[[1]]))
        self.assertFalse(r.ok)
        self.assertEqual(r.expected, [[2]])
        self.assertEqual(r.actual, [[1]])

    def test_rows_missing(self):
        r = one({"rows_equals": [[1]]}, obs_tool(rows=None))
        self.assertFalse(r.ok)
        self.assertIn("没有 rows", str(r.actual))

    def test_min_rows_pass(self):
        self.assertTrue(one({"min_rows": 1}, obs_tool(rows=[[1], [2]])).ok)

    def test_min_rows_fail(self):
        r = one({"min_rows": 3}, obs_tool(rows=[[1]]))
        self.assertFalse(r.ok)
        self.assertEqual(r.actual, "1 行")


class CombinationTest(unittest.TestCase):
    def test_order_is_stable(self):
        results = check_expect(
            {"status": 200, "is_error": False, "contains": "hello", "max_elapsed_ms": 500},
            obs_tool(),
        )
        self.assertEqual([r.key for r in results], ["status", "is_error", "contains", "max_elapsed_ms"])

    def test_summarize(self):
        results = check_expect({"status": 200, "is_error": True}, obs_tool())
        self.assertEqual(summarize(results), (1, 2))

    def test_json_path_alone_is_not_an_assertion(self):
        self.assertEqual(check_expect({"json_path": "ok"}, obs_tool()), [])

    def test_transport_error_surfaces_in_status_detail(self):
        obs = obs_tool(status=None, transport_error="connect refused")
        r = one({"status": 200}, obs)
        self.assertFalse(r.ok)
        self.assertIn("connect refused", r.detail)


if __name__ == "__main__":
    unittest.main()
