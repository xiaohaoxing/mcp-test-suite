"""测试共用的小工具：造套件、造用例、写临时 YAML。"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import yaml

from mcp_suite.loader import Suite, loads

DEFAULTS: Dict[str, Any] = {
    "base_url": "http://127.0.0.1:9",
    "timeout_s": 5,
    "token_env": "MCP_TEST_TOKEN",
    "admin_token_env": "MCP_TEST_ADMIN_TOKEN",
}

LAYERS = ["liveness", "auth", "guard", "domain", "governance", "admin"]


def make_case(cid: str = "case-001", **overrides: Any) -> Dict[str, Any]:
    case: Dict[str, Any] = {
        "id": cid,
        "title": "一条用于测试的用例",
        "layer": "domain",
        "priority": "P0",
        "goal": "这条值得测：出现过真实故障，且这次验收要能复现那个形态。",
        "action": {"kind": "tool", "tool": "echo", "arguments": {}},
        "expect": {"is_error": False},
    }
    case.update(overrides)
    return case


def suite_dict(cases: Optional[List[Dict[str, Any]]] = None, **overrides: Any) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "suite": "fake-suite",
        "version": 1,
        "service": "fake-mcp",
        "updated": "2026-10-23",
        "owner": "platform",
        "defaults": dict(DEFAULTS),
        "layers": list(LAYERS),
        "cases": cases if cases is not None else [make_case()],
    }
    doc.update(overrides)
    return doc


def dump(doc: Dict[str, Any]) -> str:
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def make_suite(cases: Optional[List[Dict[str, Any]]] = None, **overrides: Any) -> Suite:
    return loads(dump(suite_dict(cases, **overrides)), source="<test>")


def write_yaml(tmpdir: str, doc: Dict[str, Any], name: str = "mcp-test-suite.yaml") -> str:
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(dump(doc))
    return path


def write_env(tmpdir: str, name: str, content: str) -> str:
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def jsonl(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
