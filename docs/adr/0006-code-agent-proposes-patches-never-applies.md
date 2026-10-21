# ADR 0006: The code agent proposes patches; humans apply them

Status: accepted

## Context
An agent that writes to a working tree needs a sandbox, resource limits, credential isolation,
cleanup on failure, and a story for what happens when it half-applies a change. That is a week of
infrastructure and it is not what this project is demonstrating.

## Decision
1. The code agent returns a **unified diff** plus a rationale and confidence. It never touches a
   filesystem.
2. `agents/diff.py` parses the diff and proves it is well formed: file headers, at least one hunk,
   counts of added/removed lines. Malformed patches are contract violations.
3. The **diff is the source of truth**, not the model's claim: files are taken from the diff
   headers, and a diff that touches a path the model did not declare is rejected.
4. `GET /runs/{id}/patch` returns the patch **only after every gate cleared**. Until then it is 409.
5. The MCP tool layer exposes the same validation (`validate_patch`) without applying anything, plus
   a repository sandbox that refuses traversal and gated files.
6. Guardrails treat CI/eval/secret/infra paths as forbidden and escalate at `critical`.

## Consequences
- No sandbox to build or secure; the human applies the patch with normal review.
- The demo is honest: "proposed a change, blocked it, or escalated it" is a claim that can be
  checked against the diff and the audit log.
- Next step if execution is ever added: apply the patch inside a disposable container and keep the
  same gate payload and audit entries.
