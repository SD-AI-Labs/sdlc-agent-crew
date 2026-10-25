"""Read-only access to one GitHub repository: listings and file contents, nothing else.

Configured by environment: DELIVERYOPS_GITHUB_REPO ("owner/repo"), GITHUB_TOKEN (optional for
public repos, required for private), DELIVERYOPS_GITHUB_REF (branch or sha, default HEAD).
ADR 0006 stands here too: the code agent reads this repo to ground its proposed diff, and no
code path in this process writes to GitHub - no commits, no branches, no PRs.

The same safety rules as the local sandbox reader apply: no path traversal, no .env / .pem
files, a byte cap, and an error dict instead of an exception on a bad path.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping

MAX_FILE_BYTES = 200_000
MAX_LISTING = 2000


class GitHubError(RuntimeError):
    """GitHub could not be reached or rejected the request."""


Opener = Callable[..., object]


def _gated(path: str) -> str | None:
    """Why a path is refused, or None when it is safe to read."""
    if path.startswith("/") or ".." in path.split("/"):
        return f"path escapes repo root: {path}"
    parts = path.split("/")
    if any(p.startswith(".env") for p in parts) or parts[-1].endswith(".pem"):
        return f"refusing to read gated file: {path}"
    return None


class GitHubRepoReader:
    """Reads one repo over the GitHub REST API. Injectable opener so tests script HTTP."""

    def __init__(self, repo: str, token: str | None = None, ref: str = "HEAD",
                 opener: Opener | None = None) -> None:
        if "/" not in repo:
            raise GitHubError(f"DELIVERYOPS_GITHUB_REPO must be 'owner/repo', got {repo!r}")
        self._repo = repo
        self._ref = ref
        self._token = token
        self._opener: Opener = opener or urllib.request.urlopen
        self._tree: list[str] | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GitHubRepoReader:
        env = os.environ if env is None else env
        repo = env.get("DELIVERYOPS_GITHUB_REPO", "")
        if not repo:
            raise GitHubError("DELIVERYOPS_GITHUB_REPO is not set")
        return cls(repo, token=env.get("GITHUB_TOKEN") or None,
                   ref=env.get("DELIVERYOPS_GITHUB_REF") or "HEAD")

    def _request(self, path: str, query: dict | None = None, raw: bool = False) -> bytes:
        url = "https://api.github.com" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github.raw" if raw
                       else "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with self._opener(req, timeout=15) as resp:
                return resp.read()  # type: ignore[attr-defined]
        except urllib.error.HTTPError as e:
            raise GitHubError(f"github GET {path}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GitHubError(f"github unreachable: {e}") from e

    # ---------- reader surface ----------

    def list_files(self, subdir: str = ".") -> list[str]:
        if self._tree is None:
            ref = urllib.parse.quote(self._ref, safe="")
            data = json.loads(self._request(f"/repos/{self._repo}/git/trees/{ref}",
                                            query={"recursive": "1"}))
            self._tree = sorted(str(e["path"]) for e in data.get("tree", [])
                                if e.get("type") == "blob")[:MAX_LISTING]
        if subdir in ("", "."):
            return list(self._tree)
        prefix = subdir.strip("/") + "/"
        return [p for p in self._tree if p.startswith(prefix)]

    def read_file(self, path: str) -> dict:
        refusal = _gated(path)
        if refusal:
            return {"error": refusal}
        quoted = urllib.parse.quote(path, safe="")
        try:
            data = self._request(f"/repos/{self._repo}/contents/{quoted}",
                                 query={"ref": self._ref}, raw=True)
        except GitHubError as e:
            return {"error": str(e)}
        data = data[:MAX_FILE_BYTES]
        return {"path": path, "bytes": len(data), "text": data.decode("utf-8", "replace")}


def get_repo_reader(env: Mapping[str, str] | None = None) -> GitHubRepoReader | None:
    """A GitHub reader when DELIVERYOPS_GITHUB_REPO is set, else None (local sandbox stays)."""
    env = os.environ if env is None else env
    if not env.get("DELIVERYOPS_GITHUB_REPO"):
        return None
    return GitHubRepoReader.from_env(env)
