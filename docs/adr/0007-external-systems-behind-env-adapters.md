# ADR 0007: Jira and GitHub are env-configured adapters; the mock stays the default

## Context

The pipeline needs to consume real Jira tickets and ground the code agent's diffs in a real
GitHub repository before it can run against anything but demo data. The seams already existed:
the MCP ticket store (`mcp_server/jira.TicketStore`) and the sandboxed repo reader in the MCP
tool layer. What did not exist was anything real behind them.

## Decision

- **Jira Cloud REST v3** (`integrations/jira.py`) implements the same get/list/create surface
  as the mock store. Selected when any `JIRA_*` variable is set. A *partial* configuration is a
  loud startup error, never a silent fall back: a pipeline that believes it reads Jira must
  read Jira.
- **GitHub REST** (`integrations/github.py`) is a strictly read-only repo reader
  (`DELIVERYOPS_GITHUB_REPO`, `GITHUB_TOKEN`). The code agent receives the real file listing
  and the contents of the files the design names; the MCP repo tools answer from GitHub instead
  of the local root. The same safety rules as the local sandbox apply (no traversal, no
  `.env`/`.pem`, byte cap).
- **No write path to GitHub exists.** ADR 0006 stands: the output is a proposed diff; a human
  applies it. Nothing in this repo commits, branches, or opens PRs.
- Credentials arrive only as environment variables (Secret Manager in the deploy config). The
  mock store and local sandbox remain the defaults, so tests, CI and offline development never
  touch the network.

## Consequences

The four-stage pipeline can now run end to end against a real tracker and a real repository
with zero code changes - only configuration. Every external call is behind an injectable
opener, so the new behaviour is unit-tested without network access. The failure mode of an
unreachable tracker is explicit (502 at intake, an "unreachable" note in the code agent's
prompt) rather than a quiet degradation to fake data.
