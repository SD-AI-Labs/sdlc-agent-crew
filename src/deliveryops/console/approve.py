"""Tiny approval console for demos and for engineers who live in a terminal.

    deliveryops-approve <run_id>              # show the gate
    deliveryops-approve <run_id> --approve    # approve (add --role senior for escalated gates)
    deliveryops-approve <run_id> --reject --comment "needs a rollback plan"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _api() -> str:
    return os.getenv("DELIVERYOPS_API_URL", "http://localhost:8000")


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{_api()}{path}", timeout=10) as resp:
        return json.loads(resp.read())


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{_api()}{path}", data=json.dumps(body).encode(),
        headers={"content-type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode(), "status": e.code}


def show(run: dict) -> None:
    print(f"run      {run['run_id']}")
    print(f"ticket   {run['ticket']['title']}")
    print(f"status   {run['status']}  (stage {run['stage']})")
    awaiting = run.get("awaiting")
    if not awaiting:
        print("nothing is waiting at a gate")
        return
    print(f"gate     {awaiting['stage']}  risk={awaiting['risk']}"
          f"{'  ESCALATED' if awaiting.get('escalate') else ''}")
    for reason in awaiting.get("reasons") or ["routine review"]:
        print(f"  - {reason}")
    print("summary")
    print("  " + json.dumps(awaiting.get("summary"), indent=2).replace("\n", "\n  ")[:2000])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Inspect or answer a DeliveryOps gate")
    ap.add_argument("run_id")
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--reject", action="store_true")
    ap.add_argument("--approver", default=os.getenv("USER", "engineer"))
    ap.add_argument("--role", default="engineer")
    ap.add_argument("--comment", default="")
    args = ap.parse_args(argv)

    run = _get(f"/runs/{args.run_id}")
    if not (args.approve or args.reject):
        show(run)
        return 0
    body = {"approver": args.approver, "role": args.role, "comment": args.comment,
            "approve": bool(args.approve)}
    result = _post(f"/runs/{args.run_id}/decision", body)
    if "error" in result:
        print(f"HTTP {result['status']}: {result['error']}", file=sys.stderr)
        return 1
    print(f"{'approved' if args.approve else 'rejected'} by {args.approver}; "
          f"now {result['status']} at stage {result['stage']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
