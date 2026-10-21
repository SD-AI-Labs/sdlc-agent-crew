"""Server-rendered dashboard: gate queue, spend, latency, escalations.

No build step and no JS framework on purpose: one HTML page that renders from the same repository
the API uses. A React console can replace it later without touching the pipeline.
"""
from __future__ import annotations

import html


def _esc(v: object) -> str:
    return html.escape(str(v if v is not None else ""))


def _risk_class(risk: str) -> str:
    return {"critical": "crit", "high": "high", "medium": "med"}.get(risk, "low")


def render_dashboard(runs, metrics: dict, escalations) -> str:
    rows = []
    for r in runs:
        state = r.state or {}
        awaiting = state.get("awaiting") or {}
        stage = awaiting.get("stage") or r.stage or "-"
        risk = awaiting.get("risk") or ""
        spend = state.get("spend_usd") or 0.0
        gates = len(state.get("decisions") or [])
        rows.append(
            "<tr>"
            f"<td class=mono>{_esc(r.id[:8])}</td>"
            f"<td>{_esc(r.title)}</td>"
            f"<td><span class=pill>{_esc(r.status)}</span></td>"
            f"<td>{_esc(stage)} <span class='risk {_risk_class(risk)}'>{_esc(risk)}</span></td>"
            f"<td class=num>${spend:.4f}</td>"
            f"<td class=num>{gates}/4</td>"
            "</tr>"
        )
    stage_rows = []
    for stage, m in sorted((metrics.get("by_stage") or {}).items()):
        stage_rows.append(
            "<tr>"
            f"<td>{_esc(stage)}</td><td class=num>{m['calls']}</td>"
            f"<td class=num>${m['cost_usd']:.4f}</td><td class=num>{m['tokens']}</td>"
            f"<td class=num>{m['avg_latency_ms']:.0f} ms</td>"
            f"<td class=num>{m['error_rate'] * 100:.1f}%</td>"
            "</tr>"
        )
    esc_rows = [
        f"<li>{_esc(e.run_id[:8])} - {_esc(e.stage)} - <b>{_esc(e.risk)}</b>: "
        f"{_esc('; '.join(e.reasons))}</li>"
        for e in escalations
    ] or ["<li>No escalations.</li>"]
    latency = metrics.get("latency") or {}
    return f"""<!doctype html>
<html><head><meta charset=utf-8><title>DeliveryOps</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,sans-serif;margin:0;background:#0d1117;color:#c9d1d9}}
 header{{padding:20px 28px;border-bottom:1px solid #21262d;display:flex;gap:28px;
         align-items:baseline}}
 h1{{font-size:18px;margin:0;font-weight:600}}
 .kpi{{color:#8b949e}} .kpi b{{color:#e6edf3}}
 main{{padding:20px 28px}}
 h2{{font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:#8b949e}}
 table{{border-collapse:collapse;width:100%;margin-bottom:28px}}
 th{{text-align:left;font-weight:500;color:#8b949e;border-bottom:1px solid #21262d;
     padding:6px 10px}}
 td{{padding:7px 10px;border-bottom:1px solid #161b22;vertical-align:top}}
 .mono{{font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px;color:#8b949e}}
 .num{{text-align:right;font-variant-numeric:tabular-nums}}
 .pill{{background:#1f6feb22;border:1px solid #1f6feb55;color:#79c0ff;padding:1px 7px;
        border-radius:10px;font-size:12px}}
 .risk{{font-size:11px;padding:1px 6px;border-radius:8px;border:1px solid transparent}}
 .risk.crit{{background:#f8514922;color:#ff7b72;border-color:#f8514955}}
 .risk.high{{background:#db6d2822;color:#ffa657;border-color:#db6d2855}}
 .risk.med{{background:#d2992222;color:#e3b341;border-color:#d2992255}}
 .risk.low{{color:#8b949e}} ul{{padding-left:18px}} li{{margin:3px 0}}
</style></head><body>
<header>
 <h1>DeliveryOps</h1>
 <span class=kpi>Calls <b>{metrics.get('calls', 0)}</b></span>
 <span class=kpi>Spend <b>${metrics.get('cost_usd', 0):.4f}</b></span>
 <span class=kpi>Mean latency <b>{latency.get('mean_ms', 0):.0f} ms</b></span>
 <span class=kpi>p95 <b>{latency.get('p95_ms', 0):.0f} ms</b></span>
 <span class=kpi>Tokens <b>{metrics.get('tokens', 0)}</b></span>
</header>
<main>
 <h2>Runs</h2>
 <table><tr><th>Run</th><th>Ticket</th><th>Status</th><th>Stage</th>
 <th class=num>Spend</th><th class=num>Gates cleared</th></tr>
 {''.join(rows) or '<tr><td colspan=6>No runs yet.</td></tr>'}
 </table>
 <h2>Cost and latency by stage</h2>
 <table><tr><th>Stage</th><th class=num>Calls</th><th class=num>Spend</th><th class=num>Tokens</th>
 <th class=num>Avg latency</th><th class=num>Error rate</th></tr>
 {''.join(stage_rows) or '<tr><td colspan=6>No calls recorded.</td></tr>'}
 </table>
 <h2>Escalations</h2>
 <ul>{''.join(esc_rows)}</ul>
</main></body></html>"""
