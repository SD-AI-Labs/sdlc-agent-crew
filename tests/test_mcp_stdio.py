"""End-to-end MCP check: spawn the real server and speak JSON-RPC over stdio.

The in-process tests prove the tool functions work; this proves the server a client would actually
launch does too (handshake, tools/list, tools/call).
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest


def _server_binary() -> str:
    path = shutil.which("deliveryops-mcp")
    if not path:
        pytest.skip("deliveryops-mcp console script not installed")
    return path


def _rpc(proc, payload: dict, want_id: int) -> dict:
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    for _ in range(50):
        line = proc.stdout.readline()
        if not line:
            break
        msg = json.loads(line)
        if msg.get("id") == want_id:
            return msg
    raise AssertionError(f"no response for id {want_id}")


def test_stdio_handshake_lists_tools_and_calls_one():
    proc = subprocess.Popen(
        [_server_binary()], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )
    try:
        init = _rpc(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "pytest", "version": "0"}},
        }, want_id=1)
        assert "tools" in init["result"]["capabilities"]
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

        listing = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, want_id=2)
        names = {t["name"] for t in listing["result"]["tools"]}
        assert {"get_ticket", "search_knowledge", "validate_patch"} <= names

        call = _rpc(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_ticket", "arguments": {"ticket_id": "OPS-101"}},
        }, want_id=3)
        payload = json.loads(call["result"]["content"][0]["text"])
        assert payload["id"] == "OPS-101"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
