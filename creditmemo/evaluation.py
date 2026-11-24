"""Extraction accuracy evaluation against the generator's ground truth.

The generator knows exactly which value it printed in every cell of every statement, so the extractor can be
scored field by field: a field is (borrower, document, column year, line item). Results are split by read method
(text layer vs OCR), by statement and by sample set, wrong values are classified, and every wrong value is checked
against the reconciliation output to see whether a deterministic check flagged it (a silent error is one that no
check noticed).
"""
from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from pathlib import Path

from . import extract, reconcile, synth

STATEMENT_OF = {k: st for st, rows in synth.LAYOUT.items() for kind, k in rows if kind != "head"}
APP_TRUTH = ("amount", "term", "rate", "collateral_value")


def truth_for(slug: str, spec: dict) -> dict:
    """{(doc_fy, period, key): value in pounds} exactly as printed in each document."""
    out = {}
    for doc_fy in synth.DOC_YEARS:
        for period, vals in synth.printed_values(spec, doc_fy).items():
            for key, v in vals.items():
                out[(doc_fy, period, key)] = v * 1000
    return out


def classify(extracted_value, true_value, other_period_value):
    if true_value and (extracted_value == true_value * 1000 or extracted_value * 1000 == true_value):
        return "scale error"
    if other_period_value is not None and extracted_value == other_period_value and other_period_value != true_value:
        return "wrong column"
    if str(abs(int(extracted_value))) == str(abs(int(true_value)))[::-1]:
        return "digit order"
    return "misread digits"


def score_borrower(slug: str, spec: dict, docs: list, app: list) -> dict:
    truth = truth_for(slug, spec)
    checks = reconcile.reconcile(docs)
    flagged_ids = set()
    for c in checks:
        if c.status == "fail":
            for item in c.items:
                flagged_ids.update(item["expected_ids"] + [item["actual_id"]])
    skipped_by_doc = Counter(c.id.split(".")[0] for c in checks if c.status == "skip")
    rows, errors = [], []
    seen = set()
    method_of_page = {}
    for d in docs:
        for p in d.pages:
            method_of_page[(d.fy, p["statement"])] = p["method"]
    for d in docs:
        got = {(d.fy, f.period, f.key): f for f in d.facts}
        for k, tv in truth.items():
            if k[0] != d.fy:
                continue
            f = got.get(k)
            st = STATEMENT_OF[k[2]]
            method = f.evidence.method if f else method_of_page.get((d.fy, st), "text")
            rec = dict(slug=slug, doc=f"FY{d.fy}", period=k[1], key=k[2], statement=st, method=method,
                       truth=tv, got=None if f is None else f.value)
            if f is None:
                rec["result"] = "missing"
            elif f.value == tv:
                rec["result"] = "correct"
            else:
                other = truth.get((d.fy, k[1] - 1 if k[1] == d.fy else d.fy, k[2]))
                rec["result"] = "wrong"
                rec["category"] = classify(f.value, tv, other)
                rec["flagged"] = f.id in flagged_ids
            rows.append(rec)
            seen.add(k)
        for k, f in got.items():
            if k not in truth:
                rows.append(dict(slug=slug, doc=f"FY{d.fy}", period=k[1], key=k[2], statement=f.statement,
                                 method=f.evidence.method, truth=None, got=f.value, result="spurious"))
    app_truth = spec["application"]
    got_app = {f.key: f.value for f in app}
    for key in APP_TRUTH:
        ok = got_app.get(key) == app_truth[key]
        rows.append(dict(slug=slug, doc="application", period=None, key=key, statement="application", method="text",
                         truth=app_truth[key], got=got_app.get(key), result="correct" if ok else "wrong",
                         category=None if ok else "misread digits", flagged=False))
    return dict(rows=rows, skipped_checks=dict(skipped_by_doc))


def _prf(rows):
    c = sum(r["result"] == "correct" for r in rows)
    extracted = sum(r["result"] in ("correct", "wrong", "spurious") for r in rows)
    truth_n = sum(r["result"] in ("correct", "wrong", "missing") for r in rows)
    p = c / extracted if extracted else None
    r_ = c / truth_n if truth_n else None
    f1 = 2 * p * r_ / (p + r_) if p and r_ else None
    return dict(fields=truth_n, extracted=extracted, correct=c, precision=p, recall=r_, f1=f1)


def summarise(rows: list) -> dict:
    out = dict(overall=_prf(rows), by_method={}, by_statement={}, by_set={}, by_borrower={}, errors=[], categories={})
    for key, group in (("by_method", "method"), ("by_statement", "statement"), ("by_set", "set"), ("by_borrower", "slug")):
        buckets = defaultdict(list)
        for r in rows:
            buckets[r[group]].append(r)
        out[key] = {k: _prf(v) for k, v in sorted(buckets.items())}
    cat = Counter()
    for r in rows:
        if r["result"] == "missing":
            cat["missing: dash (zero) cell lost by OCR" if r["truth"] == 0 and r["method"] == "ocr"
                else "missing (value not extracted)"] += 1
        elif r["result"] == "spurious":
            cat["spurious (value not in the document)"] += 1
        elif r["result"] == "wrong":
            cat[r["category"]] += 1
    out["categories"] = dict(cat)
    wrong = [r for r in rows if r["result"] == "wrong"]
    out["wrong_total"] = len(wrong)
    out["wrong_flagged"] = sum(1 for r in wrong if r.get("flagged"))
    out["silent_errors"] = [r for r in wrong if not r.get("flagged")]
    out["errors"] = [r for r in rows if r["result"] in ("wrong", "missing", "spurious")]
    return out


def run_eval(sets: dict, extracted: dict | None = None) -> dict:
    """sets: {"standard": Path, "hard": Path}. extracted: optional {(set, slug): (docs, app)} cache."""
    rows, skipped = [], {}
    for set_name, root in sets.items():
        specs = synth.all_specs()
        for slug in sorted(p.name for p in Path(root).iterdir() if p.is_dir()):
            if extracted and (set_name, slug) in extracted:
                docs, app = extracted[(set_name, slug)]
            else:
                docs, app = extract.extract_borrower(Path(root), slug)
            res = score_borrower(slug, specs[slug], docs, app)
            for r in res["rows"]:
                r["set"] = set_name
            rows += res["rows"]
            skipped[slug] = res["skipped_checks"]
    summary = summarise(rows)
    summary["skipped_checks"] = skipped
    summary["rows_total"] = len(rows)
    return summary


# ---------------------------------------------------------------- rendering

def _n(v):
    return "" if v is None else f"{v:,}"


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _table(title, data, first):
    body = "".join(
        f"<tr><td>{html.escape(str(k))}</td><td>{v['fields']}</td><td>{v['correct']}</td>"
        f"<td>{_pct(v['precision'])}</td><td>{_pct(v['recall'])}</td><td>{_pct(v['f1'])}</td></tr>"
        for k, v in data.items())
    return (f"<h3>{html.escape(title)}</h3><table><thead><tr><th>{first}</th><th>Fields</th><th>Correct</th>"
            f"<th>Precision</th><th>Recall</th><th>F1</th></tr></thead><tbody>{body}</tbody></table>")


def render_html(summary: dict) -> str:
    o = summary["overall"]
    cats = summary["categories"]
    cat_rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>" for k, v in sorted(cats.items(), key=lambda t: -t[1]))
    err_rows = "".join(
        f"<tr><td>{html.escape(r['slug'])}</td><td>{r['doc']}</td><td>{html.escape(str(r['key']))}</td>"
        f"<td>{r['period'] if r['period'] else ''}</td><td>{r['method']}</td><td>{html.escape(r['result'])}"
        f"{' / ' + html.escape(r['category']) if r.get('category') else ''}</td>"
        f"<td>{_n(r['truth'])}</td><td>{_n(r['got'])}</td>"
        f"<td>{'<span class=ok>flagged</span>' if r.get('flagged') else ('<span class=bad>silent</span>' if r['result'] == 'wrong' else '')}</td></tr>"
        for r in summary["errors"][:40])
    silent = len(summary["silent_errors"])
    wt, wf = summary["wrong_total"], summary["wrong_flagged"]
    return EVAL_HTML.replace("__PREC__", _pct(o["precision"])).replace("__REC__", _pct(o["recall"])) \
        .replace("__F1__", _pct(o["f1"])).replace("__FIELDS__", str(o["fields"])) \
        .replace("__CORRECT__", str(o["correct"])) \
        .replace("__FLAG__", f"{wf} of {wt}" if wt else "none wrong") \
        .replace("__SILENT__", str(silent)) \
        .replace("__TABLES__", _table("By read method", summary["by_method"], "Method")
                 + _table("By statement", summary["by_statement"], "Statement")
                 + _table("By sample set", summary["by_set"], "Set")
                 + _table("By borrower", summary["by_borrower"], "Borrower")) \
        .replace("__CATS__", cat_rows or "<tr><td colspan=2>no errors</td></tr>") \
        .replace("__ERRS__", err_rows or "<tr><td colspan=9>no errors</td></tr>") \
        .replace("__NERR__", str(len(summary["errors"])))


EVAL_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Extraction accuracy</title>
<style>
:root{--bg:#f4f5f7;--fg:#1b2230;--mut:#5d6677;--line:#dde0e6;--acc:#1f4fa3;--ok:#177a46;--bad:#b3261e;--panel:#fff;--bar:#12203a}
@media (prefers-color-scheme:dark){:root{--bg:#10131a;--fg:#e6e9f0;--mut:#98a2b6;--line:#272d39;--acc:#7aa7ff;--ok:#4cc38a;--bad:#ff7b72;--panel:#181c25;--bar:#0b0f18}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Segoe UI,sans-serif}
header{background:var(--bar);color:#fff;padding:12px 24px;font-weight:600;letter-spacing:.2px}
header span{opacity:.65;font-weight:400;margin-left:10px}
.wrap{max-width:1100px;margin:0 auto;padding:20px 24px 40px}
h1{font-size:20px;margin:6px 0 2px}p.sub{color:var(--mut);margin:0 0 16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.kpi b{display:block;font-size:22px;white-space:nowrap;font-variant-numeric:tabular-nums}.kpi span{color:var(--mut);font-size:12.5px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 16px 14px;margin-bottom:18px}
h3{font-size:14px;margin:14px 0 4px}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--mut);font-weight:600;font-size:12px}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}
.note{color:var(--mut);font-size:12.5px}
@media (max-width:860px){.cols{grid-template-columns:1fr}}
</style></head><body>
<header>Credit memo workbench<span>Extraction accuracy against generator ground truth</span></header>
<div class="wrap">
<h1>Extraction accuracy</h1>
<p class="sub">Every value the extractor read is compared with the value the generator printed in that cell, per borrower, document, column year and line item, plus the four loan-application fields.</p>
<div class="kpis">
<div class="kpi"><b>__PREC__</b><span>precision</span></div>
<div class="kpi"><b>__REC__</b><span>recall</span></div>
<div class="kpi"><b>__F1__</b><span>F1</span></div>
<div class="kpi"><b>__CORRECT__ / __FIELDS__</b><span>fields correct</span></div>
<div class="kpi"><b>__FLAG__</b><span>wrong values flagged by a reconciliation check</span></div>
<div class="kpi"><b>__SILENT__</b><span>silent errors (wrong and not flagged)</span></div>
</div>
<div class="cols"><div class="card">__TABLES__</div>
<div><div class="card"><h3>Failure categories</h3><table><thead><tr><th>Category</th><th>Fields</th></tr></thead><tbody>__CATS__</tbody></table>
<p class="note">Wrong values are classified as scale error (x1000), wrong column, digit order or misread digits (OCR). Missing means the cell was not extracted.</p></div></div></div>
<div class="card"><h3>Every error (__NERR__)</h3><table><thead><tr><th>Borrower</th><th>Doc</th><th>Line item</th><th>Year</th><th>Read by</th><th>Result</th><th>Truth</th><th>Extracted</th><th>Check</th></tr></thead><tbody>__ERRS__</tbody></table>
<p class="note">Truth and extracted values are in pounds. "flagged" means the wrong value takes part in a failed reconciliation check; "silent" means no deterministic check noticed it.</p></div>
</div></body></html>
"""


def write_reports(summary: dict, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    slim = {k: v for k, v in summary.items() if k not in ("errors", "silent_errors")}
    slim["errors"] = summary["errors"]
    (out / "eval.json").write_text(json.dumps(slim, indent=1, default=str), encoding="utf-8")
    (out / "eval.html").write_text(render_html(summary), encoding="utf-8")
