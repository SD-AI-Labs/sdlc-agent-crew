# ADR 0009: Guardrails for untrusted external content

## Context

Since ADR 0007 the pipeline consumes attacker-shaped input: Jira tickets anyone can file and
GitHub repository content the code agent reads into its prompt. The existing guardrails
(guardrails/policy.py, guardrails/injection.py) were designed when tickets were the only
external text. The question was what a current production setup adds - and what is framework
theater.

## Inventory (what already existed)

- Injection screening of ticket title/description, with matched phrases recorded and escalation
  on hits (gate payload `injection_phrases`).
- Escalation authority: escalated gates require a senior role; enforced in the pipeline, not
  advisory.
- Diff trust rules: the patch, not the model's claim, defines touched files; undeclared files
  fail the run; patch-size and forbidden-path signals feed the code gate.
- Deterministic secret/eval scan on proposed diffs (`static_scan`), independent of the model's
  own review.
- Repo-read gating: no path traversal, no `.env*`/`*.pem`, byte caps - on the local sandbox and
  the GitHub reader alike.

## What was added

- **Pre-prompt screening of repository content.** File names and file contents fetched from the
  target repo are injection-screened before they enter the code agent's prompt (names with
  separator-evasion normalized). Matches are excluded with a visible "Excluded by injection
  screening" note in the prompt - auditable, never silent. Jira ticket text was already
  screened at the intake/code gates; the run still escalates on hits there, because triage of a
  hostile ticket is itself a human decision.
- **Per-run cost cap.** `DELIVERYOPS_MAX_RUN_COST_USD` (default 1.00, 0 disables) is checked
  before every paid model call; exceeding it fails the run closed with the spend in the error.

## What was considered and deferred

- **PII redaction (e.g. LangChain's PIIMiddleware):** middleware requires the create_agent path
  deferred in ADR 0008, and regex PII redaction on a single-tenant demo gives false confidence.
  Revisit with real multi-tenant data.
- **Model-based moderation endpoints:** a second model judging the first adds cost and latency
  to catch what the deterministic scanner + human gates already catch at this scale.
- **Rate limiting / authn on the API:** real gap, owned by the deployment target (IAP / API
  gateway), noted in README limitations - not something a regex can fix.

## Consequences

External content now has two screening points (at the boundary into prompts, and at the gates),
spend is bounded per run, and every addition is deterministic and testable offline - the same
standard the rest of the guardrails meet.
