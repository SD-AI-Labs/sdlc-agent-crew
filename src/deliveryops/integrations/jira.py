"""Jira Cloud REST intake: real tickets through the same seam the mock store fills.

Configured only by environment: JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN and (to create or scope
listing) JIRA_PROJECT. When they are absent the mock TicketStore stays the default, so tests,
CI and offline development never touch the network. Partial configuration is a loud error,
never a silent fall back to the mock: a pipeline that believes it reads Jira must read Jira.

Nothing here is stubbed when configured - every call is a real Jira Cloud REST v3 request.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime

from ..mcp_server.jira import Ticket, TicketStore

FIELDS = "summary,description,reporter,labels,created,status"


class JiraError(RuntimeError):
    """Jira Cloud could not be reached, rejected the request, or is partially configured."""


class JiraNotFound(JiraError):
    """The requested issue does not exist (or is not visible to this user)."""


def adf_to_text(node: object) -> str:
    """Flatten Atlassian Document Format to plain text (Jira v3 returns descriptions as ADF)."""
    if not isinstance(node, Mapping):
        return ""
    parts: list[str] = []
    text = node.get("text")
    if isinstance(text, str):
        parts.append(text)
    for child in node.get("content") or []:
        chunk = adf_to_text(child)
        if chunk:
            parts.append(chunk)
    joined = "".join(parts) if text else "\n".join(parts)
    if node.get("type") in ("paragraph", "heading", "listItem", "codeBlock"):
        joined += "\n"
    return joined


def text_to_adf(text: str) -> dict:
    """Minimal ADF document: one paragraph per line, so plain text survives the round trip."""
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        for line in text.splitlines() if line.strip()
    ]
    return {"type": "doc", "version": 1, "content": paragraphs or
            [{"type": "paragraph", "content": [{"type": "text", "text": text or " "}]}]}


def _parse_created(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()
    except ValueError:
        return 0.0


Opener = Callable[..., object]


class JiraCloudStore:
    """Ticket provider backed by the Jira Cloud REST API (v3).

    Same get/list/create surface as the mock TicketStore, so the MCP layer and the API cannot
    tell which tracker they are talking to. The opener is injectable so tests script HTTP
    without a network.
    """

    def __init__(self, site: str, email: str, api_token: str, project: str | None = None,
                 opener: Opener | None = None) -> None:
        self._base = site.rstrip("/")
        self._auth = "Basic " + base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._project = project
        self._opener: Opener = opener or urllib.request.urlopen

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> JiraCloudStore:
        env = os.environ if env is None else env
        missing = [k for k in ("JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN") if not env.get(k)]
        if missing:
            raise JiraError(f"jira intake is partially configured; missing {', '.join(missing)}")
        return cls(env["JIRA_SITE"], env["JIRA_EMAIL"], env["JIRA_API_TOKEN"],
                   project=env.get("JIRA_PROJECT") or None)

    # ---------- REST plumbing ----------

    def _request(self, method: str, path: str, query: dict | None = None,
                 body: dict | None = None) -> dict:
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._auth)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener(req, timeout=15) as resp:
                raw = resp.read()  # type: ignore[attr-defined]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise JiraNotFound(f"jira {method} {path}: not found") from e
            raise JiraError(f"jira {method} {path}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise JiraError(f"jira unreachable: {e}") from e
        try:
            return json.loads(raw or b"{}")
        except ValueError as e:
            raise JiraError(f"jira {method} {path}: response is not JSON") from e

    @staticmethod
    def _to_ticket(issue: Mapping) -> Ticket:
        fields = issue.get("fields") or {}
        reporter = fields.get("reporter") or {}
        return Ticket(
            id=str(issue.get("key", "")),
            title=str(fields.get("summary", "")),
            description=adf_to_text(fields.get("description")).strip(),
            reporter=str(reporter.get("displayName") or "unknown"),
            labels=[str(lb) for lb in fields.get("labels") or []],
            created_at=_parse_created(fields.get("created")),
        )

    # ---------- TicketStore surface ----------

    def get(self, ticket_id: str) -> Ticket | None:
        try:
            issue = self._request("GET", f"/rest/api/3/issue/{ticket_id}",
                                  query={"fields": FIELDS})
        except JiraNotFound:
            return None
        return self._to_ticket(issue)

    def list(self) -> list[Ticket]:
        jql = "statusCategory != Done ORDER BY created DESC"
        if self._project:
            jql = f"project = {self._project} AND {jql}"
        data = self._request("GET", "/rest/api/3/search/jql",
                             query={"jql": jql, "maxResults": 50, "fields": FIELDS})
        return [self._to_ticket(i) for i in data.get("issues", [])]

    def create(self, title: str, description: str, reporter: str = "api",
               labels: list[str] | None = None) -> Ticket:
        if not self._project:
            raise JiraError("JIRA_PROJECT is required to create issues")
        body = {"fields": {
            "project": {"key": self._project},
            "summary": title,
            "description": text_to_adf(description),
            "issuetype": {"name": "Task"},
            "labels": labels or [],
        }}
        data = self._request("POST", "/rest/api/3/issue", body=body)
        created = self.get(str(data.get("key", "")))
        return created or Ticket(id=str(data.get("key", "")), title=title,
                                 description=description, reporter=reporter,
                                 labels=labels or [])


def jira_configured(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return any(env.get(k) for k in ("JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN"))


def get_ticket_store(env: Mapping[str, str] | None = None):
    """Real Jira when any JIRA_* is set (partial config raises), else the seeded mock store."""
    env = os.environ if env is None else env
    if jira_configured(env):
        return JiraCloudStore.from_env(env)
    return TicketStore()
