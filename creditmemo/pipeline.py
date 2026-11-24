"""End-to-end run: sample PDFs -> facts -> reconciliation -> ratios -> policy -> memo files."""
from __future__ import annotations

import html
import json
from pathlib import Path

from . import extract, guard, memo as memo_mod, narrative, policy as policy_mod, ratios, reconcile, synth
from .evidence import render_crops
from .narrative import Ctx
from .reconcile import Dataset
from .refs import Ref, RefStore

SCALE_WORDS = {1: "whole pounds", 1000: "thousands of pounds", 1_000_000: "millions of pounds"}


def process_borrower(data_root: Path, slug: str, pol: dict, provider, out_root: Path) -> dict:
    docs, app = extract.extract_borrower(data_root, slug)
    checks = reconcile.reconcile(docs)
    stats = reconcile.summarize(checks)
    ds = Dataset(docs)
    store = RefStore()
    analysis = ratios.analyse(slug, ds, app, store, pol.get("dscr"))
    facts_by_id = {f.id: f for d in docs for f in d.facts}
    facts_by_id.update({f.id: f for f in app})

    tot, fail = f"{slug}.recon.total", f"{slug}.recon.fail"
    note = "Counted from reconciliation.json (statuses pass, fail, info, skip)."
    store.add(Ref(tot, "count", "Reconciliation checks run", stats["total"], "count", str(stats["total"]), note=note))
    store.add(Ref(fail, "count", "Reconciliation checks failed", stats["fail"], "count", str(stats["fail"]), note=note))

    findings = policy_mod.evaluate(pol, slug, analysis, store, fail)
    item_refs = memo_mod.build_item_refs(slug, checks, facts_by_id, store)
    texts = {f.key: f.value for f in app if isinstance(f.value, str)}
    years = set(analysis["periods"]) | {analysis["periods"][0] - 1}
    ctx = Ctx(slug=slug, name=texts["applicant"], app=texts, store=store, analysis=analysis, findings=findings,
              item_refs=item_refs, years=years)
    narratives, notes = narrative.generate(ctx, provider)

    scales = {d.doc_id: d.scale for d in docs}
    scale_note = None
    if len(set(scales.values())) > 1:
        common = max(set(scales.values()), key=list(scales.values()).count)
        odd = [f"{k}" for k, v in scales.items() if v != common]
        scale_note = (f"Scale detection: the {', '.join(odd)} statements state amounts in "
                      f"{SCALE_WORDS.get(scales[odd[0]], 'a different unit')} while the others use "
                      f"{SCALE_WORDS.get(common, 'another unit')}; all values were normalised to pounds before "
                      f"any comparison.")
    ocr_pages = sorted({f"{d.doc_id} {p['statement']} page" for d in docs for p in d.pages
                        if p["method"] == "ocr" and p["statement"]})

    secs = memo_mod.build_sections(ctx, narratives, dict(total=tot, fail=fail, scale_note=scale_note),
                                   ocr_pages, analysis["restated"])
    md, reach = memo_mod.render_markdown(texts["applicant"], secs, store, notes)
    leftover = guard.unlinked_numbers(md)
    if leftover:
        raise AssertionError(f"{slug}: unlinked numbers in memo body: {leftover[:8]}")
    by_rule = {f["rule_id"]: f for f in findings}
    kpis = [dict(label=by_rule[r]["title"], ref=by_rule[r]["metric_ref"], thr=by_rule[r]["threshold_ref"],
                 op=by_rule[r]["op"], status=by_rule[r]["status"])
            for r in ("dscr_pro_forma", "leverage_pro_forma", "interest_cover", "collateral_cover",
                      "reconciliation_failures") if r in by_rule and by_rule[r]["value"] is not None]
    A = analysis["ids"]["app"]
    facility = (f"Requested: [[{A['amount']}]] amortising term loan over [[{A['term']}]] at an indicative "
                f"[[{A['rate']}]]. Synthetic borrower, not a real company.")
    html_doc = memo_mod.render_html(texts["applicant"], secs, store, notes, reach, "evidence", kpis, facility)

    out = out_root / slug
    out.mkdir(parents=True, exist_ok=True)
    (out / "memo.md").write_text(md, encoding="utf-8")
    (out / "memo.html").write_text(html_doc, encoding="utf-8")
    (out / "facts.json").write_text(json.dumps([f.to_dict() for f in list(facts_by_id.values())], indent=1), encoding="utf-8")
    (out / "reconciliation.json").write_text(json.dumps([c.to_dict() for c in checks], indent=1), encoding="utf-8")
    (out / "ratios.json").write_text(json.dumps({k: v.to_json() for k, v in store.items() if v.kind == "calc"}, indent=1), encoding="utf-8")
    (out / "policy_findings.json").write_text(json.dumps(findings, indent=1), encoding="utf-8")
    (out / "refs.json").write_text(json.dumps({k: v.to_json() for k, v in store.items()}, indent=1), encoding="utf-8")
    (out / "analysis.json").write_text(json.dumps(dict(name=texts["applicant"], periods=analysis["periods"],
                                                      latest=analysis["latest"], ids=analysis["ids"])), encoding="utf-8")
    (out / "item_refs.json").write_text(json.dumps([dict(label=i["label"], scope=i["scope"], kind=i["kind"],
                                                         period=i["period"], expected=e, actual=a, diff=d)
                                                    for i, e, a, d in item_refs], indent=1), encoding="utf-8")
    crop_store = RefStore()          # a source crop for every extracted value, so the analyst Q&A can cite any of them
    n_img = render_crops([crop_store.add_fact(f) for f in facts_by_id.values()], data_root, out / "evidence")
    L = analysis["latest"]
    key = lambda n: store[analysis["ids"][n][L]] if L in analysis["ids"].get(n, {}) else None   # noqa: E731
    return dict(
        borrower=texts["applicant"], slug=slug, docs=[dict(doc=d.doc_id, scale=d.scale, facts=len(d.facts),
                    ocr_pages=sorted({p["page"] for p in d.pages if p["method"] == "ocr"}), warnings=d.warnings) for d in docs],
        checks=stats, failing=[f"{c.id}: {c.detail}" for c in checks if c.status == "fail"],
        metrics={n: key(n).display for n in ("revenue", "ebitda", "ebitda_margin", "leverage", "current_ratio",
                                              "interest_cover", "dscr", "new_debt_service", "dscr_pro_forma",
                                              "leverage_pro_forma", "collateral_cover") if key(n)},
        findings=[f"{f['rule_id']}: {f['status']}" for f in findings if f["status"] != "ok"],
        narrative_notes=notes, evidence_images=n_img, unlinked_numbers=len(leftover),
        facility=dict(amount=store[A["amount"]].display, term=store[A["term"]].display, rate=store[A["rate"]].display),
        statuses={f["rule_id"]: f["status"] for f in findings})


INDEX_CSS = """
:root{--bg:#f3f4f7;--fg:#1b2230;--mut:#5d6677;--line:#dfe2e8;--acc:#1f4fa3;--panel:#fff;--bar:#12203a}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--fg:#e6e9f0;--mut:#98a2b6;--line:#272d39;--acc:#7aa7ff;--panel:#171b24;--bar:#0a0e16}}
body{margin:0;background:var(--bg);color:var(--fg);font:14.5px/1.5 system-ui,Segoe UI,sans-serif}
.top{background:var(--bar);color:#fff;padding:0 24px;height:46px;display:flex;align-items:center;font-weight:600}
.top a{margin-left:auto;color:#c9d6f2;font-weight:400;text-decoration:none;font-size:13px}
.wrap{max-width:1100px;margin:0 auto;padding:22px 24px}h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 16px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:8px;font-variant-numeric:tabular-nums}
th{font-size:12px;color:var(--mut);text-align:left;padding:8px 12px;border-bottom:1px solid var(--line)}
td{padding:10px 12px;border-bottom:1px solid var(--line)}tr:last-child td{border-bottom:0}a{color:var(--acc);text-decoration:none}
.b{font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px}.exc{background:#fbe3e1;color:#8c1d18}.ok{background:#dcf3e4;color:#11603a}
.watch{background:#fcefc7;color:#7a5300}
"""


def write_index(out_root: Path, summaries: list):
    rows = []
    for s in summaries:
        exc = sum(1 for v in s["statuses"].values() if v == "exception")
        watch = sum(1 for v in s["statuses"].values() if v == "watch")
        badge = (f'<span class="b exc">{exc} exception{"s" if exc != 1 else ""}</span>' if exc else
                 f'<span class="b watch">{watch} watch</span>' if watch else '<span class="b ok">within policy</span>')
        m, c, f = s["metrics"], s["checks"], s["facility"]
        rows.append(
            f'<tr><td><a href="{s["slug"]}/memo.html"><b>{html.escape(s["borrower"])}</b></a></td>'
            f'<td>{f["amount"]}, {f["term"]}, {f["rate"]}</td><td>{m.get("dscr_pro_forma", "n/m")}</td>'
            f'<td>{m.get("leverage_pro_forma", "n/m")}</td><td>{m.get("collateral_cover", "n/m")}</td>'
            f'<td>{c["fail"]} of {c["total"]}</td><td>{badge}</td></tr>')
    (out_root / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Credit memo drafts</title><style>{INDEX_CSS}</style>'
        '<div class="top">Credit workbench<a href="eval.html">Extraction accuracy</a></div><div class="wrap">'
        '<h1>Credit memo drafts</h1><p class="sub">Synthetic, fictional borrowers. Open a memo, click any figure for its source, '
        'ask the analyst a question and mark sections.</p>'
        '<table><thead><tr><th>Borrower</th><th>Facility requested</th><th>Pro forma DSCR</th><th>Pro forma leverage</th>'
        '<th>Collateral cover</th><th>Checks failed</th><th>Policy</th></tr></thead><tbody>'
        + "".join(rows) + "</tbody></table></div></html>", encoding="utf-8")


def run(data_root: Path, out_root: Path, policy_path: Path, regen: bool = False, provider=None) -> list:
    data_root, out_root = Path(data_root), Path(out_root)
    if regen or not (data_root / "northbrook" / "loan_application.pdf").exists():
        synth.generate(data_root)
    pol = policy_mod.load_policy(Path(policy_path))
    provider = provider or narrative.provider_from_env()
    out_root.mkdir(parents=True, exist_ok=True)
    summaries = [process_borrower(data_root, s, pol, provider, out_root)
                 for s in sorted(p.name for p in data_root.iterdir() if p.is_dir())]
    (out_root / "run_summary.json").write_text(json.dumps(summaries, indent=1), encoding="utf-8")
    write_index(out_root, summaries)
    return summaries
