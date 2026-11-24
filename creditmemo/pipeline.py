"""End-to-end run: sample PDFs -> facts -> reconciliation -> ratios -> policy -> memo files."""
from __future__ import annotations

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
    analysis = ratios.analyse(slug, ds, app, store)
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
    ocr_pages = [f"{d.doc_id} {p['statement']} page" for d in docs for p in d.pages if p["method"] == "ocr"]

    secs = memo_mod.build_sections(ctx, narratives, dict(total=tot, fail=fail, scale_note=scale_note),
                                   ocr_pages, analysis["restated"])
    md, reach = memo_mod.render_markdown(texts["applicant"], secs, store, notes)
    leftover = guard.unlinked_numbers(md)
    if leftover:
        raise AssertionError(f"{slug}: unlinked numbers in memo body: {leftover[:8]}")
    html_doc = memo_mod.render_html(texts["applicant"], secs, store, notes, reach, "evidence")

    out = out_root / slug
    out.mkdir(parents=True, exist_ok=True)
    (out / "memo.md").write_text(md, encoding="utf-8")
    (out / "memo.html").write_text(html_doc, encoding="utf-8")
    (out / "facts.json").write_text(json.dumps([f.to_dict() for f in list(facts_by_id.values())], indent=1), encoding="utf-8")
    (out / "reconciliation.json").write_text(json.dumps([c.to_dict() for c in checks], indent=1), encoding="utf-8")
    (out / "ratios.json").write_text(json.dumps({k: v.to_json() for k, v in store.items() if v.kind == "calc"}, indent=1), encoding="utf-8")
    (out / "policy_findings.json").write_text(json.dumps(findings, indent=1), encoding="utf-8")
    n_img = render_crops([store[r] for r in sorted(reach | set(store)) if r in store and store[r].kind == "fact"
                          and r in reach], data_root, out / "evidence")
    L = analysis["latest"]
    key = lambda n: store[analysis["ids"][n][L]] if L in analysis["ids"].get(n, {}) else None   # noqa: E731
    return dict(
        borrower=texts["applicant"], slug=slug, docs=[dict(doc=d.doc_id, scale=d.scale, facts=len(d.facts),
                    ocr_pages=[p["page"] for p in d.pages if p["method"] == "ocr"], warnings=d.warnings) for d in docs],
        checks=stats, failing=[f"{c.id}: {c.detail}" for c in checks if c.status == "fail"],
        metrics={n: key(n).display for n in ("revenue", "ebitda", "ebitda_margin", "leverage", "current_ratio",
                                              "interest_cover", "dscr", "new_debt_service", "dscr_pro_forma",
                                              "leverage_pro_forma", "collateral_cover") if key(n)},
        findings=[f"{f['rule_id']}: {f['status']}" for f in findings if f["status"] != "ok"],
        narrative_notes=notes, evidence_images=n_img, unlinked_numbers=len(leftover))


def write_index(out_root: Path, summaries: list):
    rows = "".join(
        f'<li><a href="{s["slug"]}/memo.html">{s["borrower"]}</a> &mdash; '
        f'{"no policy exceptions" if not any("exception" in f for f in s["findings"]) else "policy exceptions flagged"}</li>'
        for s in summaries)
    (out_root / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Credit memo drafts</title>'
        '<style>body{font:16px system-ui;max-width:640px;margin:40px auto;padding:0 16px}</style>'
        f"<h1>Credit memo drafts</h1><ul>{rows}</ul>"
        "<p>Synthetic, fictional borrowers. Open a memo, click any figure for its source, and mark sections.</p>",
        encoding="utf-8")


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
