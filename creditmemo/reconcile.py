"""Deterministic reconciliation checks over extracted facts.

A check item is {label, period, scope, expected_ids, actual_id, kind}: "expected" is
the sum of the listed facts (signed as printed), "actual" is the single fact the
document reports. Differences larger than the rounding tolerance fail.
"""
from __future__ import annotations

from .model import Check, ExtractedDoc

KEY_NAMES = {
    "revenue": "Revenue", "cogs": "Cost of sales", "gross_profit": "Gross profit", "opex": "Operating expenses",
    "da": "Depreciation and amortisation", "ebit": "Operating profit", "interest": "Interest expense",
    "pbt": "Profit before tax", "tax": "Income tax", "net_income": "Net income", "cash": "Cash",
    "receivables": "Trade receivables", "inventory": "Inventories", "tca": "Total current assets",
    "ppe": "Fixed assets", "ta": "Total assets", "payables": "Trade payables", "std": "Short-term borrowings",
    "tcl": "Total current liabilities", "ltd": "Long-term borrowings", "tl": "Total liabilities",
    "equity": "Total equity", "tle": "Total liabilities and equity", "cf_ni": "Net income (cash flow)",
    "cf_da": "Depreciation (cash flow)", "cf_wc": "Change in working capital", "cfo": "Operating cash flow",
    "capex": "Capital expenditure", "cfi": "Investing cash flow", "draw": "Borrowings drawn",
    "repay": "Borrowings repaid", "dividends": "Dividends paid", "cff": "Financing cash flow",
    "net_change": "Net change in cash", "cash_begin": "Opening cash", "cash_end": "Closing cash",
}

# (check id, group, name, expected components, reported key)
ARITH = [
    ("is_gross_profit", "arithmetic", "Gross profit = revenue less cost of sales", ["revenue", "cogs"], "gross_profit"),
    ("is_ebit", "arithmetic", "Operating profit = gross profit less opex and D&A", ["gross_profit", "opex", "da"], "ebit"),
    ("is_pbt", "arithmetic", "Profit before tax = operating profit less interest", ["ebit", "interest"], "pbt"),
    ("is_net_income", "arithmetic", "Net income = profit before tax less tax", ["pbt", "tax"], "net_income"),
    ("bs_tca", "arithmetic", "Current assets add up", ["cash", "receivables", "inventory"], "tca"),
    ("bs_ta", "arithmetic", "Total assets add up", ["tca", "ppe"], "ta"),
    ("bs_tcl", "arithmetic", "Current liabilities add up", ["payables", "std"], "tcl"),
    ("bs_tl", "arithmetic", "Total liabilities add up", ["tcl", "ltd"], "tl"),
    ("bs_tle", "arithmetic", "Liabilities plus equity add up", ["tl", "equity"], "tle"),
    ("bs_balances", "arithmetic", "Balance sheet balances (assets = liabilities + equity)", ["tle"], "ta"),
    ("cf_cfo", "arithmetic", "Operating cash flow adds up", ["cf_ni", "cf_da", "cf_wc"], "cfo"),
    ("cf_cfi", "arithmetic", "Investing cash flow adds up", ["capex"], "cfi"),
    ("cf_cff", "arithmetic", "Financing cash flow adds up", ["draw", "repay", "dividends"], "cff"),
    ("cf_net", "arithmetic", "Net change in cash adds up", ["cfo", "cfi", "cff"], "net_change"),
    ("cf_roll", "arithmetic", "Opening cash plus net change = closing cash", ["cash_begin", "net_change"], "cash_end"),
]
CROSS = [
    ("ni_tie", "cross-statement", "Net income ties between income statement and cash flow",
     ("net_income", "income"), ("cf_ni", "cashflow")),
    ("cash_tie", "cross-statement", "Closing cash ties to balance sheet cash",
     ("cash", "balance"), ("cash_end", "cashflow")),
    ("da_tie", "cross-statement", "D&A ties between income statement and cash flow (sign aside)",
     None, None),
]


class Dataset:
    def __init__(self, docs: list[ExtractedDoc]):
        self.docs = {d.fy: d for d in docs}
        self.by = {}
        for d in docs:
            for f in d.facts:
                self.by[(d.fy, f.period, f.key)] = f

    def get(self, doc_fy, period, key):
        return self.by.get((doc_fy, period, key))

    def tol(self, doc_fy):
        return self.docs[doc_fy].scale          # one printed unit of rounding

    def selected(self, period, key):
        """Latest document that reports this period wins (restated comparatives)."""
        for fy in sorted(self.docs, reverse=True):
            f = self.by.get((fy, period, key))
            if f is not None:
                return f
        return None

    def periods(self):
        return sorted({f.period for f in self.by.values() if f.kind == "statement"})


def _item(label, scope, period, expected_ids, actual_id, kind="arith"):
    return dict(label=label, scope=scope, period=period, expected_ids=expected_ids,
                actual_id=actual_id, kind=kind)


def _arith_checks(ds: Dataset):
    checks = []
    for fy, doc in sorted(ds.docs.items()):
        for period in (fy, fy - 1):
            col = "current" if period == fy else "comparative"
            scope = f"{doc.doc_id} document, {col} column (FY{period})"
            for cid, group, name, comps, rep in ARITH:
                facts = [ds.get(fy, period, k) for k in comps]
                actual = ds.get(fy, period, rep)
                chk = Check(id=f"{doc.doc_id}.{period}.{cid}", group=group, name=name, scope=scope, status="pass")
                if actual is None or any(f is None for f in facts):
                    chk.status, chk.detail = "skip", "line item not extracted"
                else:
                    exp = sum(f.value for f in facts)
                    diff = actual.value - exp
                    chk.detail = f"expected {exp:,}, reported {actual.value:,}"
                    if abs(diff) > ds.tol(fy):
                        chk.status = "fail"
                        chk.items = [_item(KEY_NAMES[rep], scope, period, [f.id for f in facts], actual.id)]
                checks.append(chk)
            for cid, group, name, a, b in CROSS:
                if cid == "da_tie":
                    fa, fb = ds.get(fy, period, "da"), ds.get(fy, period, "cf_da")
                    exp_ids = [fa.id] if fa else []
                    ok_vals = (fa.value, -fb.value) if fa and fb else None
                else:
                    fa, fb = ds.get(fy, period, a[0]), ds.get(fy, period, b[0])
                    exp_ids = [fa.id] if fa else []
                    ok_vals = (fa.value, fb.value) if fa and fb else None
                chk = Check(id=f"{doc.doc_id}.{period}.{cid}", group=group, name=name, scope=scope, status="pass")
                if ok_vals is None:
                    chk.status, chk.detail = "skip", "line item not extracted"
                else:
                    chk.detail = f"{ok_vals[0]:,} vs {ok_vals[1]:,}"
                    if cid == "da_tie":
                        if abs(ok_vals[0] - ok_vals[1]) > ds.tol(fy):
                            chk.status = "fail"
                    elif abs(ok_vals[0] - ok_vals[1]) > ds.tol(fy):
                        chk.status = "fail"
                        chk.items = [_item(KEY_NAMES[b[0]], scope, period, exp_ids, fb.id, "tie")]
                    if chk.status == "fail" and cid == "da_tie":
                        chk.items = [_item(KEY_NAMES["cf_da"], scope, period, exp_ids, fb.id, "tie")]
                checks.append(chk)
            # continuity inside the document
            prior_cash = ds.get(fy, period - 1, "cash")
            begin = ds.get(fy, period, "cash_begin")
            chk = Check(id=f"{doc.doc_id}.{period}.cash_continuity", group="cross-statement",
                        name="Opening cash equals prior-year balance sheet cash", scope=scope, status="pass")
            if prior_cash is None or begin is None:
                chk.status, chk.detail = "skip", "prior-year column not in this document"
            else:
                chk.detail = f"{prior_cash.value:,} vs {begin.value:,}"
                if abs(prior_cash.value - begin.value) > ds.tol(fy):
                    chk.status = "fail"
                    chk.items = [_item(KEY_NAMES["cash_begin"], scope, period, [prior_cash.id], begin.id, "tie")]
            checks.append(chk)
            eq, eq0 = ds.get(fy, period, "equity"), ds.get(fy, period - 1, "equity")
            ni, dv = ds.get(fy, period, "net_income"), ds.get(fy, period, "dividends")
            chk = Check(id=f"{doc.doc_id}.{period}.equity_roll", group="cross-statement",
                        name="Equity roll-forward: prior equity + net income - dividends", scope=scope, status="pass")
            if None in (eq, eq0, ni, dv):
                chk.status, chk.detail = "skip", "prior-year column not in this document"
            else:
                exp = eq0.value + ni.value + dv.value
                chk.detail = f"expected {exp:,}, reported {eq.value:,}"
                if abs(eq.value - exp) > ds.tol(fy):
                    chk.status = "fail"
                    chk.items = [_item(KEY_NAMES["equity"], scope, period, [eq0.id, ni.id, dv.id], eq.id, "arith")]
            checks.append(chk)
    return checks


def _yoy_checks(ds: Dataset):
    checks = []
    fys = sorted(ds.docs)
    for a, b in zip(fys, fys[1:]):
        da, db = ds.docs[a], ds.docs[b]
        period = a
        chk = Check(id=f"yoy.{da.doc_id}-{db.doc_id}.{period}", group="year-over-year",
                    name=f"FY{period} as reported in {da.doc_id} vs comparative in {db.doc_id}",
                    scope=f"FY{period}", status="pass")
        compared, items = 0, []
        for key in KEY_NAMES:
            fa, fb = ds.get(a, period, key), ds.get(b, period, key)
            if fa is None or fb is None:
                continue
            compared += 1
            if abs(fa.value - fb.value) > max(da.scale, db.scale):
                it = _item(KEY_NAMES[key], f"FY{period}: {da.doc_id} vs {db.doc_id} comparative", period, [fa.id], fb.id, "yoy")
                it["pct"] = (fb.value - fa.value) / fa.value if fa.value else None
                items.append(it)
        chk.detail = f"{compared} line items compared, {len(items)} differ"
        if items:
            chk.status, chk.items = "fail", items
        checks.append(chk)
    return checks


def _scale_checks(ds: Dataset):
    checks = []
    scales = {fy: d.scale for fy, d in ds.docs.items()}
    for fy, d in sorted(ds.docs.items()):
        checks.append(Check(id=f"scale.{d.doc_id}", group="scale", name="Scale statement detected",
                            scope=d.doc_id, status="pass" if "no scale" not in d.scale_note else "fail",
                            detail=f"scale {d.scale:,} ({d.scale_note})"))
    if len(set(scales.values())) > 1:
        checks.append(Check(id="scale.mixed", group="scale", name="Documents use different scales",
                            scope="all documents", status="info",
                            detail="; ".join(f"{d.doc_id}: x{d.scale:,}" for d in ds.docs.values())
                                   + ". Values were normalised to whole pounds before comparison."))
    # plausibility: normalised revenue should not jump by orders of magnitude between years
    pts = []
    for p in ds.periods():
        f = ds.selected(p, "revenue")
        if f:
            pts.append((p, f))
    for (p0, f0), (p1, f1) in zip(pts, pts[1:]):
        ratio = f1.value / f0.value if f0.value else 0
        bad = ratio > 5 or ratio < 0.2
        chk = Check(id=f"scale.plausible.{p1}", group="scale", name="Revenue magnitude plausible vs prior year",
                    scope=f"FY{p0} to FY{p1}", status="fail" if bad else "pass",
                    detail=f"ratio {ratio:.2f}")
        if bad:
            chk.items = [_item("Revenue", chk.scope, p1, [f0.id], f1.id, "scale")]
        checks.append(chk)
    return checks


def reconcile(docs: list[ExtractedDoc]) -> list[Check]:
    ds = Dataset(docs)
    return _arith_checks(ds) + _yoy_checks(ds) + _scale_checks(ds)


def summarize(checks: list[Check]) -> dict:
    out = {"total": len(checks)}
    for s in ("pass", "fail", "info", "skip"):
        out[s] = sum(c.status == s for c in checks)
    return out
