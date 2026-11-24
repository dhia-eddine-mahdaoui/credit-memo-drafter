"""Command line: python -m creditmemo run | review | verify-audit"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import audit, evaluation, pipeline, qa, server


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
    q = sub.add_parser("ask", help="ask the analyst a question about one borrower (answers cite sources)")
    q.add_argument("borrower")
    q.add_argument("question")
    q.add_argument("--out", default="out")
    e = sub.add_parser("eval", help="score extraction against the generator's ground truth")
    e.add_argument("--data", default="sample_data")
    e.add_argument("--hard", default="sample_hard")
    e.add_argument("--out", default="out")
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
    elif args.cmd == "ask":
        ws = qa.Workspace.load(Path(args.out) / args.borrower)
        res = qa.answer(ws, args.question, qa.http_chat_from_env())
        print(res["plain"])
        print("\nSources:")
        for line in res["sources"]:
            print("  -", line)
        print(f"\n[planner: {res['planner']}; tools: {', '.join(t['tool'] for t in res['trace']) or 'none'}; "
              f"{res['ref_count']} cited figures]")
        for n in res["notes"]:
            print("note:", n)
    elif args.cmd == "eval":
        summary = evaluation.run_eval({"standard": Path(args.data), "hard": Path(args.hard)})
        evaluation.write_reports(summary, Path(args.out))
        o = summary["overall"]
        print(f"fields {o['fields']}  correct {o['correct']}  precision {o['precision']:.4f}  recall {o['recall']:.4f}  "
              f"F1 {o['f1']:.4f}")
        for name, v in summary["by_set"].items():
            print(f"  {name}: precision {v['precision']:.4f} recall {v['recall']:.4f} ({v['correct']}/{v['fields']})")
        for name, v in summary["by_method"].items():
            print(f"  read by {name}: precision {v['precision']:.4f} recall {v['recall']:.4f} ({v['correct']}/{v['fields']})")
        for k, v in summary["categories"].items():
            print(f"  failure category: {k}: {v}")
        print(f"  wrong values flagged by a reconciliation check: {summary['wrong_flagged']} of {summary['wrong_total']}")
        print(f"Report written to {args.out}/eval.html and eval.json")
    elif args.cmd == "review":
        server.serve(Path(args.out), args.port)
    else:
        bad = False
        for name in ("audit.jsonl", "qa_log.jsonl"):
            ok, msg = audit.verify(Path(args.out) / name)
            print(f"{name}: " + ("intact" if ok else "PROBLEM: " + msg))
            bad = bad or not ok
        raise SystemExit(1 if bad else 0)
