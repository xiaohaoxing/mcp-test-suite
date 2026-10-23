"""执行器端到端测试：真的起一个 HTTP 服务，真的走一遍 initialize → tools/call。

用假服务而不是 mock，是为了覆盖真实协议形态：SSE 帧、session header、
`isError` + `structuredContent`、非 2xx 的响应体形状、以及超时。
"""

from __future__ import annotations

import unittest

from mcp_suite.loader import parse_env_value
from mcp_suite.runner import Runner
from tests.fake_server import FakeMcpServer
from tests.support import make_case, make_suite


class RunnerToolTest(unittest.TestCase):
    def setUp(self):
        self.srv = FakeMcpServer().start()
        self.addCleanup(self.srv.stop)

    def suite(self, cases, timeout_s=5):
        return make_suite(
            cases,
            defaults={
                "base_url": self.srv.base_url,
                "timeout_s": timeout_s,
                "token_env": "MCP_TEST_TOKEN",
                "admin_token_env": "MCP_TEST_ADMIN_TOKEN",
            },
        )

    def run_cases(self, cases, token="test-token", timeout_s=5):
        suite = self.suite(cases, timeout_s=timeout_s)
        runner = Runner(suite, token=token, admin_token="admin-token", consumer="tester")
        return runner.run(suite.cases)

    def test_tool_success(self):
        [r] = self.run_cases([
            make_case("c-1", expect={
                "is_error": False,
                "json_path": "ok",
                "json_equals": True,
                "min_rows": 1,
                "contains": "hello",
                "max_elapsed_ms": 3000,
            })
        ])
        self.assertEqual(r.status, "pass", r.error)
        self.assertEqual(len(r.assertions), 5)

    def test_tool_error_type(self):
        [r] = self.run_cases([
            make_case("c-1", action={"kind": "tool", "tool": "err"},
                      expect={"is_error": True, "error_type": "SQL_NOT_ALLOWED"})
        ])
        self.assertEqual(r.status, "pass", r.error)
        self.assertEqual(r.observation.error_type, "SQL_NOT_ALLOWED")

    def test_unknown_tool_reports_available_tools(self):
        [r] = self.run_cases([
            make_case("c-1", action={"kind": "tool", "tool": "nope"},
                      expect={"is_error": True, "error_type": "INVALID_ARGUMENT",
                              "json_path": "available_tools", "json_contains": "echo"})
        ])
        self.assertEqual(r.status, "pass", r.error)

    def test_result_as_json_string_is_unwrapped(self):
        # 示例服务 的形状：structuredContent = {"result": "<json 字符串>"}。
        # json_path 中途遇到「像 JSON 的字符串」会自动展开一层。
        [r] = self.run_cases([
            make_case("c-1", action={"kind": "tool", "tool": "json_string"},
                      expect={"json_path": "result.verdict", "json_equals": "PASS"})
        ])
        self.assertEqual(r.status, "pass", r.error)

    def test_json_string_list_len(self):
        [r] = self.run_cases([
            make_case("c-1", action={"kind": "tool", "tool": "json_string"},
                      expect={"json_path": "result.items", "json_len": 3})
        ])
        self.assertEqual(r.status, "pass", r.error)

    def test_wrong_expectation_fails_with_expected_and_actual(self):
        [r] = self.run_cases([
            make_case("c-1", action={"kind": "tool", "tool": "err"},
                      expect={"error_type": "SOMETHING_ELSE"})
        ])
        self.assertEqual(r.status, "fail")
        assertion = r.assertions[0]
        self.assertFalse(assertion.ok)
        self.assertEqual(assertion.expected, "SOMETHING_ELSE")
        self.assertEqual(assertion.actual, "SQL_NOT_ALLOWED")

    def test_timeout_is_a_failure_not_a_crash(self):
        [r] = self.run_cases([
            make_case("c-1", action={"kind": "tool", "tool": "slow"},
                      expect={"is_error": False})
        ], timeout_s=0.2)
        self.assertEqual(r.status, "fail")
        self.assertIn("超时", r.observation.transport_error or "")

    def test_bad_token_returns_401(self):
        [r] = self.run_cases([
            make_case("c-1", expect={"status": 401, "is_error": True, "error_type": "token_missing"})
        ], token="wrong-token")
        self.assertEqual(r.status, "pass", r.error)
        self.assertEqual(r.observation.status, 401)

    def test_todo_case_is_not_executed(self):
        before = len(self.srv.requests)
        [r] = self.run_cases([make_case("c-1", status="todo")])
        self.assertEqual(r.status, "todo")
        self.assertEqual(len(self.srv.requests), before)

    def test_success_elapsed_is_measured_on_tools_call(self):
        [r] = self.run_cases([make_case("c-1", expect={"max_elapsed_ms": 3000})])
        self.assertGreaterEqual(r.elapsed_ms, 0)
        self.assertLess(r.elapsed_ms, 3000)


class RunnerRateLimitTest(unittest.TestCase):
    """限流不应把「验收工具自己打满配额」误报成用例失败，但断言 429 的用例必须不重试。"""

    def setUp(self):
        self.srv = FakeMcpServer().start()
        self.addCleanup(self.srv.stop)

    def suite(self, cases):
        return make_suite(
            cases,
            defaults={"base_url": self.srv.base_url, "timeout_s": 5,
                      "token_env": "MCP_TEST_TOKEN"},
        )

    def test_429_is_retried(self):
        suite = self.suite([make_case("c-1", action={"kind": "tool", "tool": "flaky_429"},
                                     expect={"is_error": False})])
        [r] = Runner(suite, token="test-token", retry_429=3).run(suite.cases)
        self.assertEqual(r.status, "pass", r.error)
        self.assertGreaterEqual(r.retries, 1)

    def test_case_that_asserts_429_is_not_retried(self):
        suite = self.suite([make_case("c-1", action={"kind": "tool", "tool": "always_429"},
                                     expect={"status": 429, "error_type": "rate_limited"})])
        [r] = Runner(suite, token="test-token", retry_429=3).run(suite.cases)
        self.assertEqual(r.status, "pass", r.error)
        self.assertEqual(r.retries, 0)

    def test_exhausted_retries_is_a_failure_with_explanation(self):
        suite = self.suite([make_case("c-1", action={"kind": "tool", "tool": "always_429"},
                                     expect={"is_error": False})])
        [r] = Runner(suite, token="test-token", retry_429=1).run(suite.cases)
        self.assertEqual(r.status, "fail")
        self.assertIn("限流", r.observation.transport_error or "")


class RunnerHttpTest(unittest.TestCase):
    def setUp(self):
        self.srv = FakeMcpServer().start()
        self.addCleanup(self.srv.stop)

    def suite(self, cases):
        return make_suite(
            cases,
            defaults={
                "base_url": self.srv.base_url,
                "timeout_s": 5,
                "token_env": "MCP_TEST_TOKEN",
                "admin_token_env": "MCP_TEST_ADMIN_TOKEN",
            },
        )

    def test_health_no_auth(self):
        # 一条用例只能有一个 json_path（schema 如此），要断言两个字段就写两条。
        suite = self.suite([
            make_case("c-1",
                      action={"kind": "http", "method": "GET", "path": "/health"},
                      expect={"status": 200, "json_path": "status", "json_equals": "ok"}),
            make_case("c-2",
                      action={"kind": "http", "method": "GET", "path": "/health"},
                      expect={"json_path": "consumers", "json_contains": "tester"}),
        ])
        [r1, r2] = Runner(suite, token=None, admin_token=None).run(suite.cases)
        self.assertEqual(r1.status, "pass", r1.error)
        self.assertEqual(r2.status, "pass", r2.error)

    def test_ready_503_is_assertable(self):
        srv = FakeMcpServer(ready_status=503, ready_body={"status": "not_ready", "failed": ["es"]}).start()
        self.addCleanup(srv.stop)
        suite = make_suite(
            [make_case("c-1", action={"kind": "http", "method": "GET", "path": "/ready"},
                       expect={"status": 503, "json_path": "status", "json_equals": "not_ready"}),
             make_case("c-2", action={"kind": "http", "method": "GET", "path": "/ready"},
                       expect={"json_path": "failed", "json_contains": "es"})],
            defaults={"base_url": srv.base_url, "timeout_s": 5, "token_env": "MCP_TEST_TOKEN"},
        )
        results = Runner(suite).run(suite.cases)
        self.assertEqual([r.status for r in results], ["pass", "pass"])

    def test_admin_uses_admin_token(self):
        suite = self.suite([
            make_case("c-1",
                      action={"kind": "http", "method": "GET", "path": "/admin/v1/health"},
                      expect={"status": 200, "json_path": "version", "json_equals": "1.2.3"}),
            make_case("c-2",
                      action={"kind": "http", "method": "GET", "path": "/admin/v1/health"},
                      expect={"json_path": "kit_version", "json_equals": "9.9.9"}),
        ])
        results = Runner(suite, token="test-token", admin_token="admin-token").run(suite.cases)
        self.assertEqual([r.status for r in results], ["pass", "pass"])

    def test_admin_without_token_401(self):
        suite = self.suite([make_case(
            "c-1",
            action={"kind": "http", "method": "GET", "path": "/admin/v1/health", "auth": "none"},
            expect={"status": 401},
        )])
        [r] = Runner(suite, token="test-token", admin_token="admin-token").run(suite.cases)
        self.assertEqual(r.status, "pass", r.error)

    def test_business_token_on_admin_path_is_401(self):
        suite = self.suite([make_case(
            "c-1",
            action={"kind": "http", "method": "GET", "path": "/admin/v1/health", "auth": "business"},
            expect={"status": 401},
        )])
        [r] = Runner(suite, token="test-token", admin_token="admin-token").run(suite.cases)
        self.assertEqual(r.status, "pass", r.error)

    def test_tools_list_over_http_with_body(self):
        body = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
        suite = self.suite([
            make_case("c-1",
                      action={"kind": "http", "method": "POST", "path": "/mcp", "body": body},
                      expect={"status": 200, "json_path": "result.tools", "json_len": 4}),
            make_case("c-2",
                      action={"kind": "http", "method": "POST", "path": "/mcp", "body": body},
                      expect={"json_path": "result.tools", "json_contains": {"name": "json_string"}}),
            make_case("c-3",
                      action={"kind": "http", "method": "POST", "path": "/mcp", "body": body},
                      expect={"contains": '"name": "echo"'}),
        ])
        results = Runner(suite, token="test-token", admin_token="admin-token").run(suite.cases)
        self.assertEqual([r.status for r in results], ["pass", "pass", "pass"])

    def test_admin_post_body_echoes(self):
        suite = self.suite([make_case(
            "c-1",
            action={
                "kind": "http", "method": "POST", "path": "/admin/v1/echo",
                "body": {"hello": "世界"},
            },
            expect={"status": 200, "json_path": "echo.hello", "json_equals": "世界"},
        )])
        [r] = Runner(suite, token="test-token", admin_token="admin-token").run(suite.cases)
        self.assertEqual(r.status, "pass", r.error)

    def test_missing_admin_token_is_a_failure_with_reason(self):
        suite = self.suite([make_case(
            "c-1",
            action={"kind": "http", "method": "GET", "path": "/admin/v1/health"},
            expect={"status": 200},
        )])
        [r] = Runner(suite, token="test-token", admin_token=None).run(suite.cases)
        self.assertEqual(r.status, "fail")
        self.assertIn("管理令牌", r.observation.transport_error or "")


class RunnerEnvTest(unittest.TestCase):
    def test_consumer_selection_from_token_list(self):
        token, consumer = parse_env_value("t1:client,t2:admin", "admin")
        self.assertEqual((token, consumer), ("t2", "admin"))


if __name__ == "__main__":
    unittest.main()
