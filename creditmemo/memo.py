"""Memo assembly and rendering (Markdown + HTML). Figures appear only as [[ref]] placeholders
until render time, where each becomes a link to its source or calculation."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from . import guard
from .narrative import OP_WORDS, Ctx
from .refs import Ref, RefStore

SECTION_TITLES = [
    ("overview", "Borrower overview"),
    ("financials", "Financial summary"),
    ("ratios", "Ratio analysis"),
    ("integrity", "Document integrity and reconciliation"),
    ("risks", "Risks and mitigants"),
    ("exceptions", "Policy exceptions"),
    ("recommendation", "Recommendation"),
]
SUMMARY_ROWS = [  # (label, calc/fact name, underlying fact keys for restatement marker)
    ("Revenue", "revenue", ["revenue"]), ("Gross profit", "gross_profit", ["gross_profit"]),
    ("EBITDA", "ebitda", ["ebit", "da"]), ("Operating profit (EBIT)", "ebit", ["ebit"]),
    ("Net income", "net_income", ["net_income"]), ("Cash", "cash", ["cash"]),
    ("Total assets", "ta", ["ta"]), ("Total debt", "total_debt", ["std", "ltd"]),
    ("Total equity", "equity", ["equity"]), ("Operating cash flow", "cfo", ["cfo"]),
]
RATIO_ROWS = [  # (label, name, policy rule id, all years?)
    ("EBITDA margin", "ebitda_margin", "ebitda_margin", True),
    ("Revenue growth", "revenue_growth", "revenue_growth", True),
    ("Current ratio", "current_ratio", "current_ratio", True),
    ("Interest cover", "interest_cover", "interest_cover", True),
    ("Leverage (total debt / EBITDA)", "leverage", None, True),
    ("DSCR (historic)", "dscr", None, True),
    ("DSCR (pro forma, with new loan)", "dscr_pro_forma", "dscr_pro_forma", False),
    ("Leverage (pro forma)", "leverage_pro_forma", "leverage_pro_forma", False),
    ("Collateral cover", "collateral_cover", "collateral_cover", False),
]
FORMULA_NAMES = ["ebitda", "ebitda_margin", "total_debt", "leverage", "current_ratio", "interest_cover",
                 "cfads", "debt_service", "dscr", "new_debt_service", "dscr_pro_forma",
                 "leverage_pro_forma", "collateral_cover"]


def build_item_refs(slug, checks, facts_by_id, store: RefStore) -> list:
    """Create expected/difference refs for every failing check item. Returns [(item, exp, act, diff)]."""
    out, n = [], 0
    for chk in checks:
        for item in chk.items:
            n += 1
            for fid in item["expected_ids"] + [item["actual_id"]]:
                store.add_fact(facts_by_id[fid])
            if len(item["expected_ids"]) == 1:
                exp = item["expected_ids"][0]
            else:
                exp = f"{slug}.chk{n}.expected"
                store.add_calc(exp, f"Expected {item['label']}", "money", item["expected_ids"],
                               lambda *v: sum(v),
                               "Expected = " + " + ".join(f"[[{i}]]" for i in item["expected_ids"]) + " = [[self]]")
            diff = f"{slug}.chk{n}.diff"
            store.add_calc(diff, f"Difference in {item['label']}", "money", [item["actual_id"], exp],
                           lambda a, e: a - e, f"Difference = reported - expected = [[{item['actual_id']}]] - [[{exp}]] = [[self]]")
            out.append((item, exp, item["actual_id"], diff))
    return out


def _cell(rid, mark=""):
    return (f"[[{rid}]]{mark}") if rid else "n/a"


def build_sections(c: Ctx, narratives: dict, recon_refs: dict, ocr_pages: list, restated: dict) -> list[dict]:
    """Return [{id, title, blocks}] where blocks are ("p", text) | ("ul", [text]) | ("table", head, rows)."""
    P, L = c.analysis["periods"], c.analysis["latest"]
    restated_keys = {k for k in restated}
    secs = []

    secs.append(dict(id="overview", blocks=[("p", t) for t in narratives["overview"].split("\n\n")]))

    head = ["GBP thousands"] + [f"FY{p}" for p in P]
    rows = []
    for label, name, keys in SUMMARY_ROWS:
        cells = []
        for p in P:
            mark = " †" if any((p, k) in restated_keys for k in keys) else ""
            cells.append(_cell(c.id(name, p), mark))
        rows.append([label] + cells)
    fin = [("table", head, rows)]
    if restated_keys:
        fin.append(("p", "† Figure taken from the later comparative column because it differs from the figure "
                         "originally reported; see Document integrity and reconciliation."))
    fin.append(("p", "Figures are as printed in the statements, shown in GBP thousands, after scale normalisation. "
                     "EBITDA, total debt and the ratios are calculated; click any figure for its source or formula."))
    secs.append(dict(id="financials", blocks=fin))

    head = ["Ratio"] + [f"FY{p}" for p in P] + ["Policy limit"]
    rows = []
    for label, name, rule, allyears in RATIO_ROWS:
        cells = [_cell(c.id(name, p)) if allyears or p == L else "–" for p in P]
        lim = "–"
        if rule:
            f = c.finding(rule)
            if f:
                lim = f"{OP_WORDS[f['op']]} [[{f['threshold_ref']}]]"
        rows.append([label] + cells + [lim])
    ratios = [("table", head, rows), ("p", narratives["ratio_commentary"]),
              ("p", f"**Formulas (FY{L}, with inputs linked)**")]
    lines = []
    for name in FORMULA_NAMES:
        rid = c.id(name)
        if rid and c.store[rid].formula:
            lines.append(c.store[rid].formula)
    ratios.append(("ul", lines))
    secs.append(dict(id="ratios", blocks=ratios))

    ib = [("p", f"Deterministic checks were run on the extracted figures: [[{recon_refs['total']}]] in total, "
                f"[[{recon_refs['fail']}]] failed. Checks cover statement arithmetic, balance sheet balance, "
                f"net income tie between statements, opening/closing cash, equity roll-forward, year-over-year "
                f"consistency between each year's statements and the next year's comparatives, and scale detection.")]
    scale_note = recon_refs.get("scale_note")
    if scale_note:
        ib.append(("p", scale_note))
    if ocr_pages:
        ib.append(("p", "Pages read by OCR (scanned): " + "; ".join(ocr_pages) +
                   ". OCR confidence is shown in each figure's source panel; compare with the scan image."))
    if c.item_refs:
        rows = []
        for item, exp, act, diff in c.item_refs:
            rows.append([item["label"], item["scope"], f"[[{exp}]]", f"[[{act}]]", f"[[{diff}]]"])
        ib.append(("table", ["Line item", "Where", "Expected", "Reported", "Difference"], rows))
    else:
        ib.append(("p", "No reconciliation failures."))
    secs.append(dict(id="integrity", blocks=ib))

    lines = [ln[2:] for ln in narratives["risks"].split("\n") if ln.startswith("- ")]
    secs.append(dict(id="risks", blocks=[("ul", lines)]))

    rows = []
    for f in c.findings:
        status = {"ok": "OK", "exception": "EXCEPTION", "watch": "WATCH"}[f["status"]]
        val = f"[[{f['metric_ref']}]]" if f["value"] is not None else "n/m"
        rows.append([f["title"], val, f"{OP_WORDS[f['op']]} [[{f['threshold_ref']}]]", f"**{status}**"])
    ex = [("table", ["Rule", "Result", "Limit", "Status"], rows)]
    breached = [f for f in c.findings if f["status"] != "ok"]
    ex.append(("p", ("Exceptions need documented sign-off before approval; watch items are flagged for the credit officer."
                     if breached else "No policy exceptions.")))
    secs.append(dict(id="exceptions", blocks=ex))

    open_items = []
    if c.item_refs:
        open_items.append("Resolve the source discrepancies listed under Document integrity with the borrower.")
    if any(f["status"] == "exception" for f in c.findings):
        open_items.append("Document sign-off or mitigation for each policy exception.")
    if any(f["status"] == "watch" for f in c.findings):
        open_items.append("Consider the watch items in the context of sector and management.")
    open_items.append("Verify applicant-stated collateral value independently.")
    secs.append(dict(id="recommendation", blocks=[
        ("p", "**[To be completed by the credit officer.]** This tool prepares evidence and analysis only; "
              "it does not make or suggest a credit decision."),
        ("p", "Open items before a recommendation can be written:"),
        ("ul", open_items)]))
    titles = dict(SECTION_TITLES)
    for s in secs:
        s["title"] = titles[s["id"]]
    return secs


# ---------------------------------------------------------------- rendering

PH = guard.PLACEHOLDER


def _inline_md(text, store, cited):
    def sub(m):
        rid = m.group(1)
        cited.add(rid)
        return f"[{store[rid].display}](#ref-{rid})"
    return PH.sub(sub, text)


def _inline_html(text, store, cited):
    text = html.escape(text, quote=False)

    def sub(m):
        rid = m.group(1)
        cited.add(rid)
        return f'<a class="num" href="#ref-{rid}" data-ref="{rid}">{html.escape(store[rid].display)}</a>'
    text = PH.sub(sub, text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)


def _collect(ref_id, store, seen: set):
    """All refs reachable from ref_id through formula inputs."""
    if ref_id in seen or ref_id not in store:
        return
    seen.add(ref_id)
    for i in store[ref_id].inputs:
        _collect(i, store, seen)
    for m in PH.finditer(store[ref_id].formula or ""):
        _collect(m.group(1), store, seen)


def render_markdown(title: str, secs: list, store: RefStore, notes: list) -> tuple[str, set]:
    cited: set = set()
    out = [f"# Credit memo (draft): {title}", "",
           "> Draft prepared from the borrower's source documents. Every figure is a link to its source snippet "
           "or calculation (see the appendix). The recommendation is reserved for the credit officer.", ""]
    for s in secs:
        out += [f"## {s['title']}", ""]
        for b in s["blocks"]:
            if b[0] == "p":
                out += [_inline_md(b[1], store, cited), ""]
            elif b[0] == "ul":
                out += [f"- {_inline_md(t, store, cited)}" for t in b[1]] + [""]
            else:
                _, head, rows = b
                out.append("| " + " | ".join(head) + " |")
                out.append("|" + "|".join(["---"] * len(head)) + "|")
                for r in rows:
                    out.append("| " + " | ".join(_inline_md(x, store, cited) for x in r) + " |")
                out.append("")
    if notes:
        out += ["_Generation notes: " + " ".join(notes) + "_", ""]
    reach: set = set()
    for rid in sorted(cited):
        _collect(rid, store, reach)
    out += ["## Appendix: sources and calculations", ""]
    for rid in sorted(reach):
        out.append(_appendix_line(store[rid], store))
    return "\n".join(out) + "\n", reach


def _appendix_line(r: Ref, store: RefStore) -> str:
    anchor = f'<a id="ref-{r.id}"></a>'
    if r.kind == "fact":
        e = r.evidence
        conf = f", OCR confidence {e['confidence']:.2f}" if e.get("confidence") else ""
        return f"- {anchor}`{r.id}`: {r.label}, {r.display}. Source: {e['doc']}, page {e['page']} ({e['method']}{conf}): \"{e['quote']}\""
    if r.kind == "calc":
        return f"- {anchor}`{r.id}`: {_inline_md(r.formula, store, set())}"
    return f"- {anchor}`{r.id}`: {r.label}, {r.display}. {r.note or ''}"


def _badges(h: str) -> str:
    for word, cls in (("EXCEPTION", "exc"), ("WATCH", "watch"), ("OK", "ok")):
        h = h.replace(f"<strong>{word}</strong>", f'<span class="badge {cls}">{word}</span>')
    return h


def _td(h: str) -> str:
    plain = re.sub(r"<[^>]+>", "", h).strip()
    numeric = 'class="num"' in h or plain in ("", "–", "n/a")
    return f'<td{"" if numeric else " class=t"}>{_badges(h)}</td>'


def render_html(title: str, secs: list, store: RefStore, notes: list, reach: set, evidence_dir: str,
                kpis: list | None = None, facility: str = "") -> str:
    cited: set = set()
    parts, nav = [], []
    for s in secs:
        body = []
        for b in s["blocks"]:
            if b[0] == "p":
                body.append(f"<p>{_badges(_inline_html(b[1], store, cited))}</p>")
            elif b[0] == "ul":
                body.append("<ul>" + "".join(f"<li>{_badges(_inline_html(t, store, cited))}</li>" for t in b[1]) + "</ul>")
            else:
                _, head, rows = b
                th = "".join(f"<th{' class=t' if h in ('Where',) else ''}>{html.escape(h)}</th>" for h in head)
                tr = "".join("<tr>" + "".join(_td(_inline_html(x, store, cited)) for x in r) + "</tr>" for r in rows)
                body.append(f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>')
        nav.append(f'<a href="#sec-{s["id"]}" data-nav="{s["id"]}"><i></i>{html.escape(s["title"])}</a>')
        parts.append(
            f'<section class="card" id="sec-{s["id"]}" data-section="{s["id"]}"><h2>{html.escape(s["title"])}</h2>'
            + "".join(body) +
            f'<div class="review" data-section="{s["id"]}"><span class="status" data-status="none">Not reviewed</span>'
            f'<span class="sp"></span><input class="comment" placeholder="Comment (optional)">'
            f'<button data-decision="approved">Approve</button>'
            f'<button data-decision="needs-changes">Needs changes</button></div></section>')
    tiles = []
    for k in kpis or []:
        v = _inline_html(f"[[{k['ref']}]]", store, cited)
        lim = _inline_html(f"{OP_WORDS[k['op']]} [[{k['thr']}]]", store, cited)
        tiles.append(f'<div class="kpi s-{k["status"]}"><span class="l">{html.escape(k["label"])}</span>'
                     f'<span class="v">{v}</span><span class="lim">{lim}</span></div>')
    fac = _inline_html(facility, store, cited) if facility else ""
    refs = {rid: store[rid].to_json() for rid in sorted(reach | cited)}
    for r in refs.values():
        if r["kind"] == "fact":
            r["image"] = f"{evidence_dir}/{r['id']}.png"
        if r["formula"]:
            r["formula_html"] = _inline_html(r["formula"], store, set())
    data = json.dumps(refs).replace("</", "<\\/")
    notes_html = f'<p class="notes">{html.escape(" ".join(notes))}</p>' if notes else ""
    return (HTML_TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__BODY__", "\n".join(parts))
            .replace("__NOTES__", notes_html).replace("__KPIS__", "".join(tiles)).replace("__FACILITY__", fac)
            .replace("__NAV__", "".join(nav)).replace("__REFS__", data))


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Credit memo (draft): __TITLE__</title>
<style>
:root{--bg:#f3f4f7;--fg:#1b2230;--mut:#5d6677;--line:#dfe2e8;--acc:#1f4fa3;--ok:#177a46;--bad:#b3261e;--warn:#9a6700;
--panel:#fff;--bar:#12203a;--soft:#f7f8fa}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--fg:#e6e9f0;--mut:#98a2b6;--line:#272d39;--acc:#7aa7ff;--ok:#4cc38a;
--bad:#ff7b72;--warn:#e3b341;--panel:#171b24;--bar:#0a0e16;--soft:#1c212c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14.5px/1.55 system-ui,Segoe UI,Helvetica,Arial,sans-serif}
.top{background:var(--bar);color:#fff;display:flex;align-items:center;gap:14px;padding:0 20px;height:46px}
.top b{letter-spacing:.2px}.top .sep{opacity:.35}.top .who{opacity:.9}.top .draft{margin-left:6px;font-size:11.5px;
background:#27406e;border-radius:10px;padding:2px 9px}.top a{color:#c9d6f2;text-decoration:none;font-size:13px;margin-left:auto}
.top a+a{margin-left:12px}
.hero{max-width:1360px;margin:0 auto;padding:16px 20px 0}
.hero h1{font-size:21px;margin:0 0 2px}.hero .fac{color:var(--mut);margin:0 0 12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.kpi{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:7px;padding:8px 12px;
display:flex;flex-direction:column}
.kpi .l{font-size:12px;color:var(--mut)}.kpi .v{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
.kpi .lim{font-size:12px;color:var(--mut)}
.kpi.s-exception{border-left-color:var(--bad)}.kpi.s-watch{border-left-color:var(--warn)}.kpi.s-ok{border-left-color:var(--ok)}
.kpi.s-exception .v a{color:var(--bad)}.kpi.s-watch .v a{color:var(--warn)}
.wrap{display:grid;grid-template-columns:190px minmax(0,1fr) 400px;gap:20px;max-width:1360px;margin:0 auto;padding:16px 20px 40px}
nav{position:sticky;top:12px;align-self:start;font-size:13.5px}
nav a{display:flex;align-items:center;gap:8px;color:var(--fg);text-decoration:none;padding:6px 8px;border-radius:6px}
nav a:hover{background:var(--panel)}nav a i{width:9px;height:9px;border-radius:50%;background:var(--line);flex:none}
nav a.approved i{background:var(--ok)}nav a.needs-changes i{background:var(--bad)}
nav .h{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--mut);margin:0 8px 6px}
main{min-width:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:4px 18px 14px;margin-bottom:14px}
h2{font-size:15.5px;margin:12px 0 6px;padding-bottom:6px;border-bottom:1px solid var(--line)}
p{margin:8px 0}ul{margin:6px 0;padding-left:20px}li{margin:3px 0}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13.5px;font-variant-numeric:tabular-nums}
th{font-size:12px;color:var(--mut);font-weight:600;background:var(--soft)}
th,td{border-bottom:1px solid var(--line);padding:6px 10px;text-align:right}
th:first-child,td:first-child,td.t,th.t{text-align:left}tbody tr:hover{background:var(--soft)}
a.num{color:var(--acc);text-decoration:none;border-bottom:1px dotted var(--acc);cursor:pointer}
a.num.active{background:rgba(31,79,163,.2);border-radius:3px}
.badge{font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px;letter-spacing:.3px}
.badge.exc{background:#fbe3e1;color:#8c1d18}.badge.watch{background:#fcefc7;color:#7a5300}.badge.ok{background:#dcf3e4;color:#11603a}
@media (prefers-color-scheme:dark){.badge.exc{background:#4a1f1c;color:#ffb3ad}.badge.watch{background:#4a3b12;color:#f0cf74}.badge.ok{background:#173d29;color:#8be0b0}}
.lead,.notes{color:var(--mut)}.notes{font-style:italic}
.review{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:12px;padding-top:10px;border-top:1px dashed var(--line);font-size:13px;color:var(--mut)}
.review .sp{flex:1}.review .status,.review button{white-space:nowrap}
.review button{border:1px solid var(--line);background:var(--panel);color:var(--fg);padding:4px 12px;border-radius:6px;cursor:pointer;font:inherit}
.review button[data-decision=approved]:hover{border-color:var(--ok);color:var(--ok)}
.review button[data-decision=needs-changes]:hover{border-color:var(--bad);color:var(--bad)}
.review .comment{width:150px;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--fg);font:inherit}
.status[data-status=approved]{color:var(--ok);font-weight:600}.status[data-status=needs-changes]{color:var(--bad);font-weight:600}
.bar{display:flex;gap:12px;align-items:center;margin:2px 0 12px;font-size:13px}
.bar input{padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--fg);font:inherit}
.ask form{display:flex;gap:8px;margin:8px 0}
.ask input[type=text]{flex:1;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--fg);font:inherit}
.ask button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:0 16px;font:inherit;cursor:pointer}
@media (prefers-color-scheme:dark){.ask button{color:#0a0e16}}
.chips{display:flex;flex-wrap:wrap;gap:6px}.chips button{border:1px solid var(--line);background:var(--soft);color:var(--fg);
border-radius:14px;padding:3px 11px;font-size:12.5px;cursor:pointer}
#answer{margin-top:10px}#answer .q{font-weight:600}
#answer .tr{font-size:12px;color:var(--mut);margin-top:6px}
#answer .ans{background:var(--soft);border:1px solid var(--line);border-radius:7px;padding:2px 14px;margin-top:6px}
aside{position:sticky;top:12px;align-self:start;max-height:calc(100vh - 24px);overflow:auto;background:var(--panel);
border:1px solid var(--line);border-radius:9px;padding:14px;font-size:13.5px}
aside h3{margin:0 0 2px;font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--mut)}
aside .big{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
aside .t{font-size:14.5px;font-weight:600;margin:4px 0 2px}
aside pre{white-space:pre-wrap;background:var(--soft);padding:8px;border-radius:6px;margin:8px 0;font:12.5px/1.45 ui-monospace,Menlo,Consolas,monospace}
aside img{max-width:100%;border:1px solid var(--line);border-radius:4px;margin-top:6px}
aside dl{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;margin:8px 0}aside dt{color:var(--mut)}aside dd{margin:0}
.pill{display:inline-block;font-size:11px;padding:1px 8px;border-radius:9px;background:var(--soft);border:1px solid var(--line);margin-left:6px}
#log{font-size:12.5px;color:var(--mut)}
@media (max-width:1100px){.wrap{grid-template-columns:minmax(0,1fr)}nav{display:none}aside{position:static;max-height:none}}
</style></head><body>
<div class="top"><b>Credit workbench</b><span class="sep">|</span><span class="who">__TITLE__</span><span class="draft">Draft memo</span>
<a href="../index.html">All memos</a><a href="../eval.html">Extraction accuracy</a></div>
<div class="hero"><h1>__TITLE__</h1><p class="fac">__FACILITY__</p><div class="kpis">__KPIS__</div></div>
<div class="wrap">
<nav><div class="h">Sections</div><a href="#sec-ask">Ask the analyst</a>__NAV__<a href="#sec-audit">Audit trail</a></nav>
<main>
<div class="bar"><label>Reviewer <input id="reviewer" placeholder="name or initials" size="14"></label>
<span id="mode" class="lead"></span></div>
__NOTES__
<section class="card ask" id="sec-ask"><h2>Ask the analyst</h2>
<p class="lead">Questions are answered by tools that read the extracted data, ratios, checks and policy results. Every figure in the answer links to its source or calculation.</p>
<form id="askform"><input type="text" id="q" placeholder="e.g. Why did DSCR fall?" autocomplete="off"><button type="submit">Ask</button></form>
<div class="chips" id="chips"></div><div id="answer"></div></section>
__BODY__
<section class="card" id="sec-audit"><h2>Audit trail</h2><div id="log">No review actions recorded yet.</div></section>
</main><aside id="panel"><h3>Source</h3><p class="lead">Click any figure in the memo to see where it came from.</p></aside></div>
<script>
const REFS = __REFS__;
const memo = location.pathname.split('/').filter(Boolean).slice(-2,-1)[0] || 'memo';
const live = location.protocol.startsWith('http');
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function show(id){
  const r = REFS[id]; if(!r) return;
  document.querySelectorAll('a.num.active').forEach(a=>a.classList.remove('active'));
  document.querySelectorAll('a.num[data-ref="'+id+'"]').forEach(a=>a.classList.add('active'));
  const kind = {fact:'Source document',calc:'Calculation',policy:'Policy limit',count:'Count',assumption:'Assumption',ocr:'OCR confidence'}[r.kind]||r.kind;
  let h = '<h3>'+kind+'</h3><div class="t">'+esc(r.label)+'</div><div class="big">'+esc(r.display)+'</div>';
  if(r.kind==='fact' && r.evidence){
    const e=r.evidence;
    h += '<dl><dt>Document</dt><dd>'+esc(e.doc.split('/').pop())+'</dd><dt>Page</dt><dd>'+e.page+'</dd><dt>Read by</dt><dd>'+esc(e.method==='ocr'?'OCR':'PDF text layer')+(e.confidence?' <span class="pill">confidence '+e.confidence+'</span>':'')+'</dd></dl>';
    h += '<pre>'+esc(e.quote)+'</pre><div class="lead">Printed value: '+esc(e.printed)+(e.scale>1?' (scale x'+e.scale+' applied)':'')+'</div>';
    h += '<img alt="source crop with the value boxed" src="'+esc(r.image)+'">';
  } else if(r.kind==='calc'){
    h += '<p>'+(r.formula_html||'')+'</p><div class="lead">Inputs (click to follow)</div><ul>'+r.inputs.map(i=>'<li><a class="num" data-ref="'+esc(i)+'" href="#ref-'+esc(i)+'">'+esc(REFS[i]?REFS[i].display:i)+'</a> '+esc(REFS[i]?REFS[i].label:'')+'</li>').join('')+'</ul>';
  } else { h += '<p>'+esc(r.note||'')+'</p>'; }
  document.getElementById('panel').innerHTML = h;
}
document.addEventListener('click', ev=>{
  const a = ev.target.closest('a.num'); if(!a) return;
  ev.preventDefault(); show(a.dataset.ref);
});
async function loadLog(){
  if(!live) return;
  try{
    const res = await fetch('/api/audit?memo='+encodeURIComponent(memo)); const rows = await res.json();
    const last = {}; rows.forEach(r=>last[r.section]=r);
    document.querySelectorAll('.review').forEach(div=>{
      const r=last[div.dataset.section], s=div.querySelector('.status');
      if(r){s.dataset.status=r.decision; s.textContent=(r.decision==='approved'?'Approved':'Needs changes')+' by '+r.reviewer;}
    });
    document.querySelectorAll('nav a[data-nav]').forEach(a=>{const r=last[a.dataset.nav]; a.className=r?r.decision:'';});
    if(rows.length) document.getElementById('log').innerHTML = rows.slice().reverse().map(r=>esc(r.ts.replace('T',' ').replace('+00:00',' UTC'))+' | '+esc(r.reviewer)+' | '+esc(r.section)+' | <b>'+esc(r.decision)+'</b>'+(r.comment?' | '+esc(r.comment):'')).join('<br>');
  }catch(e){}
}
document.querySelectorAll('.review button').forEach(btn=>btn.addEventListener('click', async ()=>{
  const div=btn.closest('.review'); const reviewer=document.getElementById('reviewer').value.trim();
  if(!live){alert('Open this memo through the review server to record decisions.');return;}
  if(!reviewer){alert('Enter a reviewer name first.');return;}
  const res = await fetch('/api/review',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({memo,section:div.dataset.section,decision:btn.dataset.decision,reviewer,comment:div.querySelector('.comment').value})});
  if(!res.ok){alert('Not recorded: '+await res.text());return;}
  div.querySelector('.comment').value=''; loadLog();
}));
const EXAMPLES=['Why did DSCR fall?','Which reconciliation checks failed?','What are the policy exceptions?','What if EBITDA falls 15%?','Which figures were read by OCR?'];
const chips=document.getElementById('chips');
EXAMPLES.forEach(t=>{const b=document.createElement('button');b.type='button';b.textContent=t;b.onclick=()=>{document.getElementById('q').value=t;ask(t);};chips.appendChild(b);});
async function ask(question){
  const box=document.getElementById('answer');
  if(!live){box.innerHTML='<p class="lead">Start the review server to ask questions.</p>';return;}
  box.innerHTML='<p class="lead">Working...</p>';
  const res=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({memo,question})});
  if(!res.ok){box.innerHTML='<p class="lead">'+esc(await res.text())+'</p>';return;}
  const d=await res.json(); Object.assign(REFS,d.refs);
  box.innerHTML='<div class="q">'+esc(d.question)+'</div><div class="ans">'+d.answer_html+'</div><div class="tr">Tools used: '+
    (d.trace.map(t=>esc(t.tool)).join(', ')||'none')+' | planner: '+esc(d.planner)+' | '+d.ref_count+' cited figures, each linked to its source'+
    (d.notes.length?'<br>'+d.notes.map(esc).join('<br>'):'')+'</div>';
}
document.getElementById('askform').addEventListener('submit',ev=>{ev.preventDefault();const q=document.getElementById('q').value.trim();if(q)ask(q);});
document.getElementById('mode').textContent = live ? 'Review mode: decisions are appended to the audit log.' : 'Read-only: run the review server to record decisions.';
loadLog();
if(location.hash.startsWith('#ref-')) show(decodeURIComponent(location.hash.slice(5)));
</script></body></html>
"""
