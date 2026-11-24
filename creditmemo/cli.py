"""Command line: python -m creditmemo run | review | verify-audit"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import audit, pipeline, server


def main(argv=None):
    ap = argparse.ArgumentParser(prog="creditmemo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="generate (if needed), extract, reconcile, analyse and draft memos")
    r.add_argument("--data", default="sample_data")
    r.add_argument("--out", default="out")
    r.add_argument("--policy", default="policy.yaml")
    r.add_argument("--regen", action="store_true", help="regenerate the synthetic sample PDFs")
    v = sub.add_parser("review", help="serve the memos locally for review")
    v.add_argument("--out", default="out")
    v.add_argument("--port", type=int, default=8765)
    a = sub.add_parser("verify-audit", help="check the audit log hash chain")
    a.add_argument("--out", default="out")
    args = ap.parse_args(argv)

    if args.cmd == "run":
        summaries = pipeline.run(Path(args.data), Path(args.out), Path(args.policy), regen=args.regen)
        for s in summaries:
            print(f"\n== {s['borrower']}")
            for d in s["docs"]:
                ocr = f", OCR on page(s) {d['ocr_pages']}" if d["ocr_pages"] else ""
                print(f"   {d['doc']}: scale x{d['scale']:,}, {d['facts']} values extracted{ocr}")
            c = s["checks"]
            print(f"   reconciliation: {c['total']} checks, {c['fail']} failed, {c['info']} info, {c['skip']} skipped")
            for f in s["failing"]:
                print(f"     FAIL {f}")
            print("   latest year / pro forma:", ", ".join(f"{k}={v}" for k, v in s["metrics"].items()))
            print("   policy:", ", ".join(s["findings"]) or "no exceptions")
            for n in s["narrative_notes"]:
                print("   note:", n)
        print(f"\nMemos written to {args.out}/<borrower>/memo.md and memo.html. Review: python -m creditmemo review")
    elif args.cmd == "review":
        server.serve(Path(args.out), args.port)
    else:
        ok, msg = audit.verify(Path(args.out) / "audit.jsonl")
        print(("audit log intact: " if ok else "AUDIT LOG PROBLEM: ") + msg)
        raise SystemExit(0 if ok else 1)
