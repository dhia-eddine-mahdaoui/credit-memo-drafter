"""Ratio calculations. Every result is a calc Ref that lists its inputs and a formula
whose figures are placeholders, so the memo can show the formula with each number linked."""
from __future__ import annotations

from .model import Fact
from .reconcile import Dataset
from .refs import RefStore


def annuity_payment(amount: float, annual_rate_pct: float, months: int) -> float:
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return amount / months
    return amount * r / (1 - (1 + r) ** -months)


def _div(a, b):
    return None if not b else a / b


def analyse(slug: str, ds: Dataset, app: list[Fact], store: RefStore) -> dict:
    """Populate store with fact and calc refs. Returns {"periods", "latest", "ids", "restated"}."""
    periods = sorted(ds.docs)
    latest = periods[-1]
    ids: dict[str, dict] = {}
    restated = {}

    def F(p, key):
        f = ds.selected(p, key)
        if f is None:
            return None
        store.add_fact(f)
        own = ds.get(p, p, key)
        if own is not None and own.id != f.id and own.value != f.value:
            restated[(p, key)] = (own.id, f.id)
            store.add_fact(own)
        return f.id

    def put(name, p, rid):
        ids.setdefault(name, {})[p] = rid

    for f in app:
        store.add_fact(f)
    A = {f.key: f.id for f in app}

    def calc(name, p, label, unit, inputs, fn, formula, note=None):
        if any(i is None for i in inputs):
            return None
        rid = f"{slug}.FY{p}.{name}"
        store.add_calc(rid, f"{label} FY{p}", unit, inputs, fn, formula, note)
        put(name, p, rid)
        return rid

    for p in periods:
        g = {k: F(p, k) for k in ("revenue", "gross_profit", "ebit", "da", "interest", "tax", "net_income",
                                   "cash", "tca", "tcl", "std", "ltd", "equity", "ta", "cfo", "repay")}
        for k, v in g.items():
            if v:
                put(k, p, v)
        ebitda = calc("ebitda", p, "EBITDA", "money", [g["ebit"], g["da"]],
                      lambda ebit, da: ebit + abs(da),
                      "EBITDA = operating profit + depreciation and amortisation = [[%s]] + abs([[%s]]) = [[self]]"
                      % (g["ebit"], g["da"]))
        calc("ebitda_margin", p, "EBITDA margin", "pct", [ebitda, g["revenue"]], _div,
             "EBITDA margin = EBITDA ÷ revenue = [[%s]] ÷ [[%s]] = [[self]]" % (ebitda, g["revenue"]))
        debt = calc("total_debt", p, "Total debt", "money", [g["std"], g["ltd"]], lambda a, b: a + b,
                    "Total debt = short-term + long-term borrowings = [[%s]] + [[%s]] = [[self]]" % (g["std"], g["ltd"]))
        calc("leverage", p, "Leverage", "ratio", [debt, ebitda],
             lambda d, e: d / e if e and e > 0 else None,
             "Leverage = total debt ÷ EBITDA = [[%s]] ÷ [[%s]] = [[self]]" % (debt, ebitda))
        calc("current_ratio", p, "Current ratio", "ratio", [g["tca"], g["tcl"]], _div,
             "Current ratio = current assets ÷ current liabilities = [[%s]] ÷ [[%s]] = [[self]]"
             % (g["tca"], g["tcl"]))
        calc("interest_cover", p, "Interest cover", "ratio", [g["ebit"], g["interest"]],
             lambda ebit, i: _div(ebit, abs(i)),
             "Interest cover = operating profit ÷ interest expense = [[%s]] ÷ abs([[%s]]) = [[self]]"
             % (g["ebit"], g["interest"]))
        cfads = calc("cfads", p, "Cash available for debt service", "money", [ebitda, g["tax"]],
                     lambda e, t: e - abs(t),
                     "Cash available for debt service = EBITDA less tax = [[%s]] - abs([[%s]]) = [[self]]"
                     % (ebitda, g["tax"]))
        dsv = calc("debt_service", p, "Existing debt service", "money", [g["interest"], g["repay"]],
                   lambda i, r: abs(i) + abs(r),
                   "Existing debt service = interest + principal repaid = abs([[%s]]) + abs([[%s]]) = [[self]]"
                   % (g["interest"], g["repay"]))
        calc("dscr", p, "DSCR", "ratio", [cfads, dsv], _div,
             "DSCR = cash available for debt service ÷ debt service = [[%s]] ÷ [[%s]] = [[self]]"
             % (cfads, dsv))
        prev = F(p - 1, "revenue")
        calc("revenue_growth", p, "Revenue growth", "pct", [g["revenue"], prev],
             lambda r, q: _div(r - q, q),
             "Revenue growth = (revenue - prior-year revenue) ÷ prior-year revenue = ([[%s]] - [[%s]]) ÷ [[%s]] = [[self]]"
             % (g["revenue"], prev, prev))

    L = latest
    amount, rate, term, cval = A.get("amount"), A.get("rate"), A.get("term"), A.get("collateral_value")
    new_ds = calc("new_debt_service", L, "New loan annual debt service", "money", [amount, rate, term],
                  lambda a, r, t: 12 * annuity_payment(a, r, t),
                  "New loan annual debt service = twelve x monthly annuity payment on [[%s]] at [[%s]] over [[%s]] = [[self]]"
                  % (amount, rate, term))
    calc("dscr_pro_forma", L, "Pro forma DSCR", "ratio",
         [ids.get("cfads", {}).get(L), ids.get("debt_service", {}).get(L), new_ds],
         lambda c, d, n: _div(c, d + n),
         "Pro forma DSCR = cash available for debt service ÷ (existing + new debt service) = [[%s]] ÷ ([[%s]] + [[%s]]) = [[self]]"
         % (ids.get("cfads", {}).get(L), ids.get("debt_service", {}).get(L), new_ds))
    calc("leverage_pro_forma", L, "Pro forma leverage", "ratio",
         [ids.get("total_debt", {}).get(L), amount, ids.get("ebitda", {}).get(L)],
         lambda d, a, e: (d + a) / e if e and e > 0 else None,
         "Pro forma leverage = (total debt + new loan) ÷ EBITDA = ([[%s]] + [[%s]]) ÷ [[%s]] = [[self]]"
         % (ids.get("total_debt", {}).get(L), amount, ids.get("ebitda", {}).get(L)))
    calc("collateral_cover", L, "Collateral cover", "ratio", [cval, amount], _div,
         "Collateral cover = collateral value ÷ loan amount = [[%s]] ÷ [[%s]] = [[self]]" % (cval, amount))
    ids["app"] = A
    return {"periods": periods, "latest": latest, "ids": ids, "restated": restated}
