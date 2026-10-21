"""MCP tool layer: tickets, repo access, knowledge search, patch validation, run status.

Exposed over stdio for MCP clients (`deliveryops-mcp`). Agents call the same functions in-process
through `mcp_server/client.py`, so behaviour cannot drift between the MCP path and the local path.
"""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from ..agents.diff import InvalidPatch
from ..agents.diff import parse as parse_diff
from ..agents.review import static_scan
from ..knowledge.retriever import get_retriever
from .jira import TicketStore

REPO_ROOT = Path(os.getenv("DELIVERYOPS_REPO_ROOT", ".")).resolve()
MAX_FILE_BYTES = 200_000

_store = TicketStore()

server = MCPServer(
    "deliveryops",
    title="DeliveryOps tools",
    instructions="Ticket, repository, knowledge and patch tools for the DeliveryOps agent crew.",
)


# ---------- tickets ----------

@server.tool()
def get_ticket(ticket_id: str) -> dict:
    """Fetch a tracker ticket (id like OPS-101)."""
    t = _store.get(ticket_id)
    return t.to_dict() if t else {"error": f"ticket {ticket_id} not found"}


@server.tool()
def list_tickets() -> list[dict]:
    """List open tracker tickets."""
    return [t.to_dict() for t in _store.list()]


@server.tool()
def create_ticket(title: str, description: str, reporter: str = "mcp") -> dict:
    """Create a tracker ticket and return it."""
    return _store.create(title, description, reporter).to_dict()


# ---------- repository ----------

def _safe_path(rel: str) -> Path:
    p = (REPO_ROOT / rel).resolve()
    if not str(p).startswith(str(REPO_ROOT)):
        raise PermissionError(f"path escapes repo root: {rel}")
    if any(part.startswith(".env") for part in p.parts) or p.name.endswith(".pem"):
        raise PermissionError(f"refusing to read gated file: {rel}")
    return p


@server.tool()
def read_repo_file(path: str) -> dict:
    """Read a file from the sandboxed repository root."""
    try:
        p = _safe_path(path)
    except PermissionError as e:
        return {"error": str(e)}
    if not p.is_file():
        return {"error": f"not a file: {path}"}
    data = p.read_bytes()[:MAX_FILE_BYTES]
    return {"path": path, "bytes": len(data), "text": data.decode("utf-8", "replace")}


@server.tool()
def list_repo_files(subdir: str = ".") -> list[str]:
    """List files under a subdirectory of the sandboxed repository root."""
    try:
        root = _safe_path(subdir)
    except PermissionError as e:
        return [str(e)]
    if not root.exists():
        return []
    return sorted(str(p.relative_to(REPO_ROOT)) for p in root.rglob("*") if p.is_file())[:500]


# ---------- knowledge ----------

@server.tool()
def search_knowledge(query: str, k: int = 3) -> list[dict]:
    """Search the process library (definition of done, estimation, severity, security, rollout)."""
    return [
        {"citation": h.citation(), "title": h.title, "content": h.content, "score": h.score}
        for h in get_retriever().search(query, k=k)
    ]


# ---------- change safety ----------

@server.tool()
def validate_patch(diff: str) -> dict:
    """Validate a proposed unified diff without applying it."""
    try:
        info = parse_diff(diff)
    except InvalidPatch as e:
        return {"valid": False, "error": str(e)}
    return {
        "valid": True,
        "files": list(info.files),
        "added": info.added,
        "removed": info.removed,
        "hunks": info.hunks,
        "lines": info.lines,
        "findings": [f.__dict__ for f in static_scan(diff)],
    }


# ---------- runs ----------

@server.tool()
def run_status(run_id: str) -> dict:
    """Read a pipeline run from the API or repository (status, stage, spend)."""
    import json
    import urllib.error
    import urllib.request

    base = os.getenv("DELIVERYOPS_API_URL", "http://localhost:8000")
    try:
        with urllib.request.urlopen(f"{base}/runs/{run_id}", timeout=5) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"error": f"api unreachable: {e}"}
    return {
        "run_id": data.get("run_id"),
        "status": data.get("status"),
        "stage": data.get("stage"),
        "spend_usd": data.get("spend_usd"),
    }


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
