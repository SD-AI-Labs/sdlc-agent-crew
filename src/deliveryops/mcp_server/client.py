"""In-process access to the MCP tool functions.

Agents import this instead of speaking stdio: same functions, no subprocess, no serialisation
cost, and tests can call tools directly. `call_tool` also works against the live server object
(`await server.call_tool(name, args)`), which is what test_mcp_server.py exercises.
"""
from __future__ import annotations

from . import server as srv


def get_ticket(ticket_id: str) -> dict:
    return srv.get_ticket(ticket_id)


def list_tickets() -> list[dict]:
    return srv.list_tickets()


def create_ticket(title: str, description: str, reporter: str = "client") -> dict:
    return srv.create_ticket(title, description, reporter)


def read_repo_file(path: str) -> dict:
    return srv.read_repo_file(path)


def list_repo_files(subdir: str = ".") -> list[str]:
    return srv.list_repo_files(subdir)


def search_knowledge(query: str, k: int = 3) -> list[dict]:
    return srv.search_knowledge(query, k=k)


def validate_patch(diff: str) -> dict:
    return srv.validate_patch(diff)
