"""离线测试用的假 MCP 服务。

为什么要一个**真的 HTTP 服务**（而不是 mock httpx）：

· 验收工具最容易出错的地方不是断言，而是**协议细节**：SSE 还是 JSON、会话 id 怎么带、
  `isError` 在哪一层、非 2xx 时响应体是什么形状。用 mock 替换掉这一层，
  测试就只能验证「我以为是那样」的那套假设。
· 这个假服务把四种真实形态都造出来：SSE 帧、JSON-RPC error、`isError=true` +
  `structuredContent.error_type`、以及「结果被序列化成字符串」（示例服务 的形状）。

它**只**监听 127.0.0.1 的随机端口，不依赖任何真实服务。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


class FakeMcpServer:
    """可配置的假服务。用法：

        with FakeMcpServer() as srv:
            ... srv.base_url ...
    """

    def __init__(
        self,
        *,
        token: str = "test-token",
        admin_token: str = "admin-token",
        ready_status: int = 200,
        ready_body: Optional[Dict[str, Any]] = None,
        health_status: int = 200,
    ):
        self.token = token
        self.admin_token = admin_token
        self.ready_status = ready_status
        self.health_status = health_status
        self.ready_body = ready_body or {"status": "ready", "checks": {"es": {"reachable": True}}, "critical": ["es"]}
        self.requests: List[Dict[str, Any]] = []
        self.flaky_seen = False
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> "FakeMcpServer":
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    @property
    def port(self) -> int:
        assert self._httpd is not None, "server 未启动"
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "FakeMcpServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------ 记录
    def record(self, entry: Dict[str, Any]) -> None:
        self.requests.append(entry)


def _make_handler(srv: FakeMcpServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FakeMCP/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # 静音，测试输出只留断言信息
            return

        # -------------------------------------------------------------- 工具
        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except ValueError:
                return {"_raw": raw.decode("utf-8", "replace")}

        def _send(self, code: int, payload: Any, ctype: str = "application/json") -> None:
            if isinstance(payload, (dict, list)):
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                data = str(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_sse(self, payload: Any, headers: Optional[Dict[str, str]] = None) -> None:
            body = "event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _auth_ok(self, token: str) -> bool:
            return self.headers.get("Authorization") == f"Bearer {token}"

        # -------------------------------------------------------------- GET
        def do_GET(self) -> None:  # noqa: N802
            srv.record({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
            path = self.path.split("?", 1)[0]
            if path == "/health":
                if srv.health_status != 200:
                    self._send(srv.health_status, {"service": "fake-mcp", "status": "misconfigured"})
                    return
                self._send(200, {
                    "service": "fake-mcp",
                    "mcp_endpoint": "/mcp",
                    "auth_configured": True,
                    "consumers": ["tester"],
                    "status": "ok",
                })
                return
            if path == "/ready":
                self._send(srv.ready_status, srv.ready_body)
                return
            if path.startswith("/admin/"):
                if not self._auth_ok(srv.admin_token):
                    self._send(401, {"error_type": "token_missing", "detail": "需要管理令牌"})
                    return
                if path == "/admin/v1/health":
                    self._send(200, {
                        "service": "fake-mcp",
                        "version": "1.2.3",
                        "kit_version": "9.9.9",
                        "admin_api_version": "v1",
                    })
                    return
                if path == "/admin/v1/ping":
                    self._send(200, {"ok": True, "path": self.path})
                    return
                self._send(404, {"error_type": "not_found"})
                return
            if path == "/plain":
                self._send(200, "hello plain text", ctype="text/plain; charset=utf-8")
                return
            self._send(404, {"error_type": "not_found", "path": path})

        # -------------------------------------------------------------- POST
        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            body = self._body()
            srv.record({
                "method": "POST",
                "path": path,
                "auth": self.headers.get("Authorization"),
                "body": body,
            })
            if path.startswith("/admin/"):
                if not self._auth_ok(srv.admin_token):
                    self._send(401, {"error_type": "token_missing"})
                    return
                if path == "/admin/v1/echo":
                    self._send(200, {"ok": True, "echo": body})
                    return
                self._send(404, {"error_type": "not_found"})
                return
            if path != "/mcp":
                self._send(404, {"error_type": "not_found"})
                return
            if not self._auth_ok(srv.token):
                self._send(401, {"error_type": "token_missing", "detail": "请求缺少令牌"})
                return

            method = body.get("method") if isinstance(body, dict) else None
            if method == "initialize":
                self._send_sse(
                    {
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": "fake-mcp", "version": "1.2.3"},
                        },
                    },
                    headers={"Mcp-Session-Id": "sess-fake-1"},
                )
                return
            if method == "notifications/initialized":
                self._send(202, "")
                return
            if method == "tools/list":
                self._send_sse({
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {"tools": [
                        {"name": "echo", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "two_rows", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "err", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "json_string", "inputSchema": {"type": "object", "properties": {}}},
                    ]},
                })
                return
            if method == "tools/call":
                params = body.get("params") or {}
                name = params.get("name")
                args = params.get("arguments") or {}
                # 429 相关：验证「验收工具自己的突发流量」不会把用例判成失败
                if name == "always_429":
                    self.send_response(429)
                    payload = json.dumps({"error_type": "rate_limited"}).encode("utf-8")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Retry-After", "0.05")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if name == "flaky_429" and not srv.flaky_seen:
                    srv.flaky_seen = True
                    self.send_response(429)
                    payload = json.dumps({"error_type": "rate_limited"}).encode("utf-8")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Retry-After", "0.05")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                result = _call_tool(name, args)
                self._send_sse({"jsonrpc": "2.0", "id": body.get("id"), "result": result})
                return
            self._send(400, {"error": {"code": -32601, "message": f"unknown method {method}"}})

    return Handler


def _call_tool(name: Optional[str], args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "echo":
        return {
            "content": [{"type": "text", "text": "hello world"}],
            "structuredContent": {
                "ok": True,
                "rows": [[1]],
                "text": "hello world",
                "nested": {"a": [1, 2], "b": "x"},
                "count": 3,
            },
            "isError": False,
        }
    if name == "two_rows":
        return {
            "content": [{"type": "text", "text": "[[1],[2]]"}],
            "structuredContent": {"rows": [[1], [2]], "row_count": 2},
            "isError": False,
        }
    if name == "json_string":
        return {
            "content": [{"type": "text", "text": "{\"verdict\": \"PASS\"}"}],
            "structuredContent": {
                "result": json.dumps({"verdict": "PASS", "items": [1, 2, 3], "error_type": None}),
            },
            "isError": False,
        }
    if name == "err":
        return {
            "content": [{"type": "text", "text": "SQL_NOT_ALLOWED: 资源函数"}],
            "structuredContent": {"error_type": "SQL_NOT_ALLOWED", "error": "资源函数", "mysql_errno": 0},
            "isError": True,
        }
    if name == "flaky_429":
        return {
            "content": [{"type": "text", "text": "ok after retry"}],
            "structuredContent": {"ok": True, "rows": []},
            "isError": False,
        }
    if name == "slow":
        time.sleep(0.4)
        return {
            "content": [{"type": "text", "text": "slow done"}],
            "structuredContent": {"ok": True, "rows": []},
            "isError": False,
        }
    if name == "cjk":
        return {
            "content": [{"type": "text", "text": "中文响应"}],
            "structuredContent": {"message": "中文响应", "rows": []},
            "isError": False,
        }
    return {
        "content": [{"type": "text", "text": f"未知工具 {name}"}],
        "structuredContent": {
            "error_type": "INVALID_ARGUMENT",
            "error": f"未知工具 {name!r}",
            "available_tools": ["echo", "two_rows", "err", "json_string"],
        },
        "isError": True,
    }
