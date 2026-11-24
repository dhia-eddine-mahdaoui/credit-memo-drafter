"""Analyst Q&A: a small tool-using loop over the extracted data, ratios, checks and policy findings.

Every number in an answer is a [[ref]] placeholder that resolves to a source fact (document, page, quote, crop) or to
a calculation whose inputs are themselves refs. Calculations the tools create on the fly (changes, effects of the
numerator and denominator, stress cases) are added to a request-scoped copy of the reference store, so they show
their formula and inputs in the review page exactly like the memo's own figures.

The loop is: planner -> tool call -> observation -> planner ... -> final text -> guard. Two planners share it:

* RulePlanner, deterministic and offline (keyword intents mapped to tool sequences, fixed answer templates);
* LLMPlanner, any chat-completion function that answers with JSON ({"tool", "args"} or {"final"}).

Whatever the planner returns as the final text goes through guard.check_text. If the model's text breaks the guard
(unknown ref, number not in the data) or the model fails, the deterministic planner answers instead and the
response says so.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import guard
from .model import Evidence, Fact
from .reconcile import KEY_NAMES
from .refs import Ref, RefStore, fmt_value

OP_WORDS = {">=": "minimum", "<=": "maximum", ">": "floor", "<": "ceiling"}

METRICS = {   # canonical name -> phrases (longest match wins)
    "dscr_pro_forma": ["pro forma dscr", "pro forma debt service cover", "dscr with the new loan", "dscr including the new loan"],
    "leverage_pro_forma": ["pro forma leverage"],
    "dscr": ["dscr", "debt service cover", "debt service coverage", "debt coverage"],
    "leverage": ["leverage", "gearing", "debt to ebitda", "debt/ebitda"],
    "interest_cover": ["interest cover", "interest coverage"],
    "current_ratio": ["current ratio", "liquidity"],
    "ebitda_margin": ["ebitda margin", "margin", "profitability"],
    "revenue_growth": ["revenue growth", "growth"],
    "collateral_cover": ["collateral cover", "collateral", "security cover"],
    "ebitda": ["ebitda"],
    "cfads": ["cash available for debt service", "cfads"],
    "total_debt": ["total debt", "borrowings", "debt"],
    "revenue": ["revenue", "turnover", "sales"],
    "net_income": ["net income", "profit for the year", "net profit", "profit"],
    "cash": ["cash"],
}
CHG = {"ratio": "ratio_chg", "pct": "pts", "money": "money_chg"}
DIVISION = {"dscr", "leverage", "current_ratio", "interest_cover", "ebitda_margin"}   # ratio = inputs[0] / inputs[1]
FALL = r"(fall|fell|drop|declin|decreas|worsen|weaken|deteriorat|lower|down|reduc)"
RISE = r"(rise|rose|increas|improv|higher|up|grow|climb)"


# ---------------------------------------------------------------- workspace

class Workspace:
    """Everything the tools may read, loaded from a borrower's output folder."""

    def __init__(self, slug, name, store, ids, periods, latest, facts, findings, item_refs, checks):
        self.slug, self.name, self.store, self.ids = slug, name, store, ids
        self.periods, self.latest, self.facts = periods, latest, facts
        self.findings, self.item_refs, self.checks = findings, item_refs, checks
        self.owner = {rid: (n, p) for n, byp in ids.items() if n != "app" for p, rid in byp.items()}

    @classmethod
    def load(cls, folder: Path):
        folder = Path(folder)
        rj = lambda n: json.loads((folder / n).read_text(encoding="utf-8"))   # noqa: E731
        refs = rj("refs.json")
        store = RefStore()
        for d in refs.values():
            store.add(Ref(**{k: d[k] for k in ("id", "kind", "label", "value", "unit", "display", "evidence",
                                               "formula", "inputs", "note")}))
        an = rj("analysis.json")
        ids = {n: ({int(p): r for p, r in byp.items()} if n != "app" else byp) for n, byp in an["ids"].items()}
        facts = {}
        for d in rj("facts.json"):
            ev = d.pop("evidence")
            facts[d["id"]] = Fact(**d, evidence=Evidence(**{**ev, "bbox": tuple(ev["bbox"]),
                                                            "line_bbox": tuple(ev["line_bbox"])}))
        return cls(folder.name, an["name"], store, ids, an["periods"], an["latest"], facts,
                   rj("policy_findings.json"), rj("item_refs.json"), rj("reconciliation.json"))


@dataclass
class Result:
    tool: str
    args: dict
    refs: list
    summary: str
    data: dict = field(default_factory=dict)


class Tools:
    """Read-only tools. Each returns a Result; refs created here live only in this request's store."""

    SPEC = {
        "lookup": "lookup(metric, period?) value of a metric (or all years) with its formula",
        "explain_change": "explain_change(metric, period?) why a metric moved versus the prior year: input changes, "
                          "numerator/denominator effects, second-level drivers",
        "policy_status": "policy_status() every policy rule with value, limit and status",
        "failed_checks": "failed_checks() reconciliation checks that failed, expected vs reported",
        "ocr_figures": "ocr_figures() figures read from scanned pages with OCR confidence, lowest first",
        "stress_ebitda": "stress_ebitda(percent) pro forma DSCR and leverage if EBITDA falls by percent",
    }

    def __init__(self, ws: Workspace):
        self.ws = ws
        self.store = RefStore(ws.store)
        self._n = 0

    # -- helpers
    def _new(self, tag):
        self._n += 1
        return f"{self.ws.slug}.qa.{tag}{self._n}"

    def _calc(self, tag, label, unit, inputs, fn, formula):
        rid = self._new(tag)
        self.store.add_calc(rid, label, unit, inputs, fn, formula)
        return rid

    def _fact_ref(self, fid):
        if fid not in self.store:
            self.store.add_fact(self.ws.facts[fid])
        return fid

    def resolve(self, name, period=None):
        p = self.ws.latest if period is None else period
        return self.ws.ids.get(name, {}).get(p)

    def _delta(self, a, b, label, unit):
        unit = CHG.get(unit, unit)
        return self._calc("chg", label, unit, [a, b], lambda x, y: y - x,
                          f"{label} = [[{b}]] - [[{a}]] = [[self]]")

    def _pct(self, a, b):
        return self._calc("pct", "Percentage change in size", "pctchg", [a, b],
                          lambda x, y: (abs(y) - abs(x)) / abs(x) if x else None,
                          f"Percentage change in size = (abs([[{b}]]) - abs([[{a}]])) / abs([[{a}]]) = [[self]]")

    # -- tools
    def lookup(self, metric, period=None):
        ids = self.ws.ids.get(metric, {})
        if period is not None:
            ids = {p: r for p, r in ids.items() if p == period}
        refs = [ids[p] for p in sorted(ids)]
        return Result("lookup", {"metric": metric, "period": period}, refs,
                      f"{metric} by year: " + ", ".join(f"FY{p}={self.store[ids[p]].display}" for p in sorted(ids)),
                      dict(metric=metric, by_period={p: ids[p] for p in sorted(ids)}))

    def explain_change(self, metric, period=None):
        L = self.ws.latest if period is None else period
        P = L - 1
        b, a = self.resolve(metric, L), self.resolve(metric, P)
        if b is None:
            return Result("explain_change", {"metric": metric}, [], f"no {metric} available", dict(error=True))
        rb = self.store[b]
        out = dict(metric=metric, L=L, P=P, cur=b, prev=a, parts=[], second=[], label=rb.label.rsplit(" FY", 1)[0])
        refs = [b] + ([a] if a else [])
        if a is None:                                  # single-year measure (pro forma): show composition only
            out["composition"] = list(rb.inputs)
            return Result("explain_change", {"metric": metric, "period": L}, refs + list(rb.inputs),
                          f"{metric} FY{L} has inputs {rb.inputs}", out)
        out["delta"] = self._delta(a, b, f"Change in {out['label']}", rb.unit)
        refs.append(out["delta"])
        ratio = None
        if metric in DIVISION and len(rb.inputs) == 2:
            n1, d1 = rb.inputs
            n0 = self._prior(n1, L)
            d0 = self._prior(d1, L)
            if n0 and d0:
                cf = self._calc("cf", f"{out['label']} with prior-year denominator", rb.unit, [n1, d0],
                                lambda x, y: x / abs(y) if y else None, f"[[{n1}]] / abs([[{d0}]]) = [[self]]")
                num_eff = self._calc("eff", "Effect of the numerator", CHG.get(rb.unit, rb.unit), [cf, a], lambda x, y: x - y,
                                     f"Effect of the numerator = [[{cf}]] - [[{a}]] = [[self]]")
                den_eff = self._calc("eff", "Effect of the denominator", CHG.get(rb.unit, rb.unit), [b, cf], lambda x, y: x - y,
                                     f"Effect of the denominator = [[{b}]] - [[{cf}]] = [[self]]")
                ratio = dict(num=(n0, n1), den=(d0, d1), cf=cf, num_eff=num_eff, den_eff=den_eff,
                             num_pct=self._pct(n0, n1), den_pct=self._pct(d0, d1))
                refs += [n0, n1, d0, d1, cf, num_eff, den_eff, ratio["num_pct"], ratio["den_pct"]]
        out["ratio"] = ratio
        movers = [(k, ratio[k]) for k in ("num", "den")] if ratio else [("in", (self._prior(i, L), i)) for i in rb.inputs]
        pairs = [(k, p0, p1) for k, (p0, p1) in movers if p0 and p1]
        if not ratio:
            for k, p0, p1 in pairs:
                out["parts"].append(dict(role=k, prev=p0, cur=p1, pct=self._pct(p0, p1)))
                refs += [p0, p1, out["parts"][-1]["pct"]]
        # second level: inputs of the input that moved the most (relative)
        def rel(pp):
            v0, v1 = self.store[pp[1]].value, self.store[pp[2]].value
            return abs((abs(v1) - abs(v0)) / v0) if v0 and v1 is not None else 0
        if pairs:
            top = max(pairs, key=rel)
            out["top"] = top[0]
            for i in self.store[top[2]].inputs:
                i0 = self._prior(i, L)
                if i0 and self.store[i0].value is not None:
                    pc = self._pct(i0, i)
                    out["second"].append(dict(label=self.store[i].label, prev=i0, cur=i, pct=pc))
                    refs += [i0, i, pc]
            out["top_ref"] = (top[1], top[2])
        # policy limit if there is one
        out["finding"] = next((f for f in self.ws.findings if f["metric"] == metric), None)
        if out["finding"]:
            refs.append(out["finding"]["threshold_ref"])
        return Result("explain_change", {"metric": metric, "period": L}, list(dict.fromkeys(refs)),
                      f"{metric} FY{P} -> FY{L}", out)

    def _prior(self, ref_id, L):
        """The same measure one year earlier, via the analysis ids."""
        own = self.ws.owner.get(ref_id)
        if own and (own[0] in self.ws.ids) and (own[1] - 1) in self.ws.ids[own[0]]:
            return self.ws.ids[own[0]][own[1] - 1]
        return None

    def policy_status(self):
        rows = self.ws.findings
        refs = [f["threshold_ref"] for f in rows] + [f["metric_ref"] for f in rows if f["value"] is not None]
        return Result("policy_status", {}, list(dict.fromkeys(refs)), f"{len(rows)} policy rules", dict(rows=rows))

    def failed_checks(self):
        refs = [f"{self.ws.slug}.recon.fail", f"{self.ws.slug}.recon.total"]
        for it in self.ws.item_refs:
            refs += [it["expected"], it["actual"], it["diff"]]
        return Result("failed_checks", {}, [r for r in refs if r in self.store], f"{len(self.ws.item_refs)} failing items",
                      dict(items=self.ws.item_refs, fail=refs[0], total=refs[1]))

    def ocr_figures(self, limit=8):
        facts = [f for f in self.ws.facts.values() if f.evidence.method == "ocr" and f.kind == "statement"]
        facts.sort(key=lambda f: (f.evidence.confidence or 1, f.id))
        shown = facts[:limit]
        refs = [self._fact_ref(f.id) for f in shown]
        self.conf = {}
        for f in shown:
            cid = f"{f.id}.ocr"
            self.store.add(Ref(cid, "ocr", f"OCR confidence for {f.label} FY{f.period}", f.evidence.confidence, "score",
                               f"{f.evidence.confidence:.2f}", note="Lowest word confidence of the OCR engine on this "
                               "line; open the figure's source crop to compare with the scan."))
            self.conf[f.id] = cid
        refs += list(self.conf.values())
        return Result("ocr_figures", {"limit": limit}, refs, f"{len(shown)} lowest-confidence OCR figures",
                      dict(ids=[f.id for f in shown], conf=self.conf, total_ocr=len(facts)))

    def stress_ebitda(self, percent):
        L = self.ws.latest
        pct = float(percent) / 100
        sid = self._new("shock")
        self.store.add(Ref(sid, "assumption", "Assumed fall in EBITDA", pct, "pct", f"{pct * 100:.1f}%",
                           note="Assumption taken from the question; tax and debt service held constant."))
        cf, eb, ds, nds = (self.resolve(n, L) for n in ("cfads", "ebitda", "debt_service", "new_debt_service"))
        debt, loan = self.resolve("total_debt", L), self.ws.ids["app"].get("amount")
        if not all((cf, eb, ds, nds, debt, loan)):
            return Result("stress_ebitda", {"percent": percent}, [sid], "inputs missing", dict(error=True))
        scf = self._calc("stress", "Stressed cash available for debt service", "money", [cf, sid, eb],
                         lambda c, s, e: c - s * e,
                         f"Stressed cash available = [[{cf}]] - [[{sid}]] x [[{eb}]] = [[self]]")
        sd = self._calc("stress", "Stressed pro forma DSCR", "ratio", [scf, ds, nds],
                        lambda c, d, n: c / (d + n) if d + n else None,
                        f"Stressed pro forma DSCR = [[{scf}]] / ([[{ds}]] + [[{nds}]]) = [[self]]")
        sl = self._calc("stress", "Stressed pro forma leverage", "ratio", [debt, loan, eb, sid],
                        lambda d, a, e, s: (d + a) / (e * (1 - s)) if e * (1 - s) > 0 else None,
                        f"Stressed pro forma leverage = ([[{debt}]] + [[{loan}]]) / ([[{eb}]] - [[{sid}]] x [[{eb}]]) = [[self]]")
        base_d, base_l = self.resolve("dscr_pro_forma", L), self.resolve("leverage_pro_forma", L)
        refs = [sid, cf, eb, scf, sd, sl, base_d, base_l]
        thr = {f["metric"]: f["threshold_ref"] for f in self.ws.findings}
        refs += [thr.get("dscr_pro_forma"), thr.get("leverage_pro_forma")]
        return Result("stress_ebitda", {"percent": percent}, [r for r in refs if r], "stress case",
                      dict(shock=sid, scf=scf, dscr=sd, lev=sl, base_dscr=base_d, base_lev=base_l,
                           thr_dscr=thr.get("dscr_pro_forma"), thr_lev=thr.get("leverage_pro_forma")))

    def call(self, name, args) -> Result:
        if name not in self.SPEC:
            raise ValueError(f"unknown tool {name}")
        return getattr(self, name)(**args)


# ---------------------------------------------------------------- planners

def _ph(r):
    return f"[[{r}]]" if r else "n/m"


def detect_metric(q: str):
    ql = q.lower()
    best, blen = None, 0
    for name, phrases in METRICS.items():
        for ph in phrases:
            if re.search(r"(?<![a-z])" + re.escape(ph) + r"(?![a-z])", ql) and len(ph) > blen:
                best, blen = name, len(ph)
    return best


def detect_year(q: str, periods):
    m = re.search(r"\bFY\s?(20\d\d)\b|\b(20\d\d)\b", q, re.I)
    if m:
        y = int(m.group(1) or m.group(2))
        return y if y in periods else None
    return None


class RulePlanner:
    """Deterministic planner: intent keywords choose the tool calls, templates write the answer."""
    name = "rules"

    def __init__(self, ws: Workspace, store: RefStore):
        self.ws, self.store = ws, store

    def intent(self, q: str):
        ql = q.lower()
        metric = detect_metric(q)
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", ql)
        if m and re.search(r"what if|stress|scenario|sensitiv|if .*(ebitda|profit|earnings)", ql):
            return "stress", dict(percent=float(m.group(1)))
        if re.search(r"\bwhy\b|what (drove|caused|explains)|reason for|driver", ql) and metric:
            return "explain", dict(metric=metric, period=detect_year(q, self.ws.periods))
        if re.search(r"restat|reconcil|discrepan|mismatch|do(es)? not (balance|tie)|failed|failing|inconsisten|check", ql):
            return "checks", {}
        if re.search(r"\bocr\b|scan|double.?check|low confidence|least reliable|uncertain", ql):
            return "ocr", {}
        if re.search(r"polic|exception|breach|covenant|outside|within limits|limit", ql) and not metric:
            return "policy", {}
        if re.search(r"approve|recommend|should (we|i)|decline|lend|credit decision", ql):
            return "decision", {}
        if metric:
            return "lookup", dict(metric=metric, period=detect_year(q, self.ws.periods))
        if re.search(r"summar|overview|key (risk|issue)|biggest|main (risk|concern)|red flag", ql):
            return "summary", {}
        return "help", {}

    def start(self, q):
        kind, args = self.intent(q)
        self.kind, self.args = kind, args
        self.calls = {
            "stress": [("stress_ebitda", dict(percent=args.get("percent")))],
            "explain": [("explain_change", dict(metric=args.get("metric"), period=args.get("period")))],
            "checks": [("failed_checks", {})],
            "ocr": [("ocr_figures", {})],
            "policy": [("policy_status", {})],
            "decision": [("policy_status", {}), ("failed_checks", {})],
            "lookup": [("lookup", dict(metric=args.get("metric"), period=args.get("period")))],
            "summary": [("policy_status", {}), ("failed_checks", {})],
            "help": [],
        }[kind]

    def step(self, history):
        if len(history) < len(self.calls):
            name, args = self.calls[len(history)]
            return ("tool", name, {k: v for k, v in args.items() if v is not None})
        return ("final", self.compose(history))

    # -- answer templates
    def compose(self, history):
        k = self.kind
        R = {r.tool: r for r in history}
        if k == "stress":
            return self._stress(R["stress_ebitda"])
        if k == "explain":
            return self._explain(R["explain_change"])
        if k == "checks":
            return self._checks(R["failed_checks"])
        if k == "ocr":
            return self._ocr(R["ocr_figures"])
        if k == "policy":
            return self._policy(R["policy_status"])
        if k == "decision":
            return ("This tool prepares evidence and does not make or suggest a credit decision; that stays with the credit "
                    "officer. What the evidence shows:\n" + self._policy(R["policy_status"]) + "\n"
                    + self._checks(R["failed_checks"]))
        if k == "summary":
            return self._policy(R["policy_status"]) + "\n" + self._checks(R["failed_checks"])
        if k == "lookup":
            return self._lookup(R["lookup"])
        return ("I can answer from the extracted statements, ratios, reconciliation checks and policy results. Try: "
                "\"Why did DSCR fall?\", \"Which reconciliation checks failed?\", \"What are the policy exceptions?\", "
                "\"What is leverage?\", \"Which figures were read by OCR?\" or \"What if EBITDA falls by a given percentage?\". "
                "I do not recommend or decline loans.")

    def _lookup(self, r):
        ids = r.data["by_period"]
        if not ids:
            return "That measure is not available for this borrower."
        store = self.store
        label = store[next(iter(ids.values()))].label.rsplit(" FY", 1)[0]
        parts = ", ".join(f"FY{p}: {_ph(i)}" for p, i in ids.items())
        out = f"{label} by year: {parts}."
        last = store[list(ids.values())[-1]]
        if last.formula:
            out += f"\nFormula for FY{list(ids)[-1]}: {last.formula}"
        f = next((f for f in self.ws.findings if f["metric"] == r.data["metric"]), None)
        if f:
            out += (f"\nPolicy: {OP_WORDS[f['op']]} of {_ph(f['threshold_ref'])}; latest status {f['status'].upper()}.")
        return out

    def _explain(self, r):
        d = r.data
        if d.get("error"):
            return "That measure is not available for this borrower."
        lab, L, P = d["label"], d["L"], d["P"]
        if d.get("composition"):
            parts = ", ".join(_ph(i) for i in d["composition"])
            return (f"{lab} is calculated for FY{L} only (it includes the proposed loan); there is no prior year to compare. "
                    f"It is built from {parts}. Result: {_ph(d['cur'])}.")
        st = self.store
        cur, prev = st[d["cur"]].value, st[d["prev"]].value
        move = "fell" if cur < prev else "rose" if cur > prev else "was unchanged"
        out = f"{lab} {move} from {_ph(d['prev'])} in FY{P} to {_ph(d['cur'])} in FY{L} (change {_ph(d['delta'])})."
        lines = []
        if d["ratio"]:
            q = d["ratio"]
            nl = st[q["num"][1]].label.rsplit(" FY", 1)[0]
            dl = st[q["den"][1]].label.rsplit(" FY", 1)[0]
            lines.append(f"- Numerator, {nl}: {_ph(q['num'][0])} to {_ph(q['num'][1])} ({_ph(q['num_pct'])}); "
                         f"effect on the ratio {_ph(q['num_eff'])}.")
            lines.append(f"- Denominator, {dl}: {_ph(q['den'][0])} to {_ph(q['den'][1])} ({_ph(q['den_pct'])}); "
                         f"effect on the ratio {_ph(q['den_eff'])}.")
            ne, de = st[q["num_eff"]].value or 0, st[q["den_eff"]].value or 0
            drv = "the numerator" if abs(ne) >= abs(de) else "the denominator"
            lines.append(f"- Main driver: {drv}. With the prior-year denominator the ratio would have been {_ph(q['cf'])}.")
        else:
            for p in d["parts"]:
                lines.append(f"- {st[p['cur']].label.rsplit(' FY', 1)[0]}: {_ph(p['prev'])} to {_ph(p['cur'])} ({_ph(p['pct'])}).")
        if d["second"]:
            top = st[d["top_ref"][1]].label.rsplit(" FY", 1)[0]
            lines.append(f"Behind {top}:")
            for s in d["second"]:
                lines.append(f"- {s['label'].rsplit(' FY', 1)[0]}: {_ph(s['prev'])} to {_ph(s['cur'])} ({_ph(s['pct'])}).")
        f = d.get("finding")
        if f:
            lines.append(f"Policy: {OP_WORDS[f['op']]} of {_ph(f['threshold_ref'])}; latest status {f['status'].upper()}.")
        return out + "\n" + "\n".join(lines)

    def _policy(self, r):
        rows = r.data["rows"]
        bad = [f for f in rows if f["status"] != "ok"]
        ok = [f["title"] for f in rows if f["status"] == "ok"]
        if not bad:
            return "No policy rule is breached. Rules checked: " + ", ".join(ok) + "."
        lines = ["Policy results (latest year, pro forma where stated):"]
        for f in bad:
            val = _ph(f["metric_ref"]) if f["value"] is not None else "not meaningful"
            lines.append(f"- {f['title']} ({f['status'].upper()}): {val} against a {OP_WORDS[f['op']]} of {_ph(f['threshold_ref'])}.")
        if ok:
            lines.append("Within policy: " + ", ".join(ok) + ".")
        return "\n".join(lines)

    def _checks(self, r):
        items = r.data["items"]
        head = f"{_ph(r.data['fail'])} of {_ph(r.data['total'])} reconciliation checks failed."
        if not items:
            return head + " No failing item to list."
        lines = [head]
        for it in items:
            lines.append(f"- {it['label']} ({it['scope']}): expected {_ph(it['expected'])}, reported {_ph(it['actual'])}, "
                         f"difference {_ph(it['diff'])}.")
        return "\n".join(lines)

    def _ocr(self, r):
        if not r.data["ids"]:
            return "No figure was read by OCR; every value came from a PDF text layer."
        st = r.data
        lines = ["Figures read by OCR, lowest confidence first (click a figure to see the scan crop and compare):"]
        for i in st["ids"]:
            ev = self.ws.facts[i].evidence
            lines.append(f"- {KEY_NAMES.get(self.ws.facts[i].key, self.ws.facts[i].label)} FY{self.ws.facts[i].period}: {_ph(i)} "
                         f"(OCR confidence {_ph(st['conf'][i])}, {self.ws.facts[i].doc_id}).")
        lines.append("OCR errors that break an accounting identity also show up as failed reconciliation checks.")
        return "\n".join(lines)

    def _stress(self, r):
        d = r.data
        if d.get("error"):
            return "The inputs for a stress case are not available for this borrower."
        st = self.store
        out = [f"If EBITDA fell by {_ph(d['shock'])} (tax and debt service held constant), cash available for debt service "
               f"would be {_ph(d['scf'])}."]
        out.append(f"- Pro forma DSCR: {_ph(d['base_dscr'])} now, {_ph(d['dscr'])} stressed, against a minimum of {_ph(d['thr_dscr'])}.")
        out.append(f"- Pro forma leverage: {_ph(d['base_lev'])} now, {_ph(d['lev'])} stressed, against a maximum of {_ph(d['thr_lev'])}.")
        return "\n".join(out)


class LLMPlanner:
    """Planner backed by a chat function: chat(messages) -> str. The model answers in JSON, either
    {"tool": name, "args": {...}} or {"final": "text with [[ref]] placeholders"}."""
    name = "llm"

    def __init__(self, ws: Workspace, chat, max_steps=6):
        self.ws, self.chat, self.max_steps = ws, chat, max_steps
        self.msgs = []

    def start(self, q):
        tools = "\n".join(f"- {s}" for s in Tools.SPEC.values())
        metrics = ", ".join(METRICS)
        self.msgs = [
            {"role": "system", "content": (
                "You are a credit analyst assistant. Answer only from tool results. Reply with ONE JSON object: either "
                '{"tool": "<name>", "args": {...}} to call a tool, or {"final": "<answer>"}. '
                "In the final answer write every figure as a [[ref_id]] placeholder copied from tool results; never write "
                "digits yourself except fiscal years like FY2023; do not recommend approving or declining.\n"
                f"Tools:\n{tools}\nMetric names: {metrics}\nBorrower: {self.ws.name}; latest year FY{self.ws.latest}.")},
            {"role": "user", "content": q}]

    def observe(self, res: Result, store: RefStore):
        lines = [f"{res.summary}"] + [f"{r} = {store[r].display} ({store[r].label})" for r in res.refs if r in store]
        self.msgs.append({"role": "user", "content": "Tool result:\n" + "\n".join(lines)})

    def step(self, history):
        if len(history) >= self.max_steps:
            raise ValueError("step limit reached")
        raw = self.chat(self.msgs)
        self.msgs.append({"role": "assistant", "content": raw})
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise ValueError("model did not return JSON")
        obj = json.loads(m.group())
        if "final" in obj:
            return ("final", str(obj["final"]))
        if obj.get("tool") in Tools.SPEC:
            return ("tool", obj["tool"], obj.get("args") or {})
        raise ValueError("model returned neither a final answer nor a known tool")


def http_chat_from_env():
    """OpenAI-compatible chat function from CREDIT_MEMO_LLM_URL / _MODEL / _AUTH, or None when not configured."""
    if os.environ.get("CREDIT_MEMO_PROVIDER", "template") != "llm" or not os.environ.get("CREDIT_MEMO_LLM_URL"):
        return None
    url = os.environ["CREDIT_MEMO_LLM_URL"]
    model = os.environ.get("CREDIT_MEMO_LLM_MODEL", "default")
    auth = os.environ.get("CREDIT_MEMO_LLM_AUTH", "")

    def chat(messages):
        body = json.dumps({"model": model, "temperature": 0, "messages": messages}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        if auth:
            req.add_header("Authorization", f"Bearer {auth}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())["choices"][0]["message"]["content"]
    return chat


# ---------------------------------------------------------------- loop

def _run(planner, tools: Tools, question: str, max_steps: int = 6):
    planner.start(question)
    history, trace = [], []
    for _ in range(max_steps + 1):
        action = planner.step(history)
        if action[0] == "final":
            return action[1], trace
        _, name, args = action
        res = tools.call(name, args)
        history.append(res)
        trace.append(dict(tool=name, args=args, refs=res.refs, summary=res.summary))
        if hasattr(planner, "observe"):
            planner.observe(res, tools.store)
    raise ValueError("too many steps")


def source_line(r: Ref, store: RefStore) -> str:
    if r.kind == "fact":
        e = r.evidence
        conf = f", OCR {e['confidence']:.2f}" if e.get("confidence") else ""
        return f"{r.display}  {r.label}: {e['doc'].split('/')[-1]} p.{e['page']} ({e['method']}{conf}) \"{e['quote']}\""
    if r.kind == "calc":
        return f"{r.display}  {guard.PLACEHOLDER.sub(lambda m: store[m.group(1)].display, r.formula)}"
    return f"{r.display}  {r.label}. {r.note or ''}".strip()


def ref_payload(store: RefStore, rid: str) -> dict:
    from .memo import _inline_html
    r = store[rid].to_json()
    if r["kind"] == "fact":
        r["image"] = f"evidence/{rid}.png"
    if r["formula"]:
        r["formula_html"] = _inline_html(r["formula"], store, set())
    return r


def _closure(store, rids):
    seen = set()

    def go(r):
        if r in seen or r not in store:
            return
        seen.add(r)
        for i in store[r].inputs:
            go(i)
        for m in guard.PLACEHOLDER.finditer(store[r].formula or ""):
            go(m.group(1))
    for r in rids:
        go(r)
    return seen


def answer(ws: Workspace, question: str, chat=None) -> dict:
    """Answer one question. Returns {answer, answer_html, trace, refs, planner, notes}."""
    from .memo import _inline_html
    question = question.strip()[:400]
    notes, used = [], "rules"
    tools = Tools(ws)
    text = trace = None
    if chat is not None:
        try:
            text, trace = _run(LLMPlanner(ws, chat), tools, question)
            violations, linked = guard.check_text(text, tools.store, set(ws.periods) | {ws.periods[0] - 1})
            if violations:
                notes.append("Model answer rejected by the guard (" +
                             ", ".join(f"{v.kind}: {v.text}" for v in violations[:4]) + "); deterministic answer shown.")
                text = None
            else:
                text, used = linked, "llm"
        except Exception as exc:                                  # network, JSON or step-limit failure
            notes.append(f"Model planner failed ({type(exc).__name__}); deterministic answer shown.")
            text = None
    if text is None:
        tools = Tools(ws)
        text, trace = _run(RulePlanner(ws, tools.store), tools, question)
        violations, text = guard.check_text(text, tools.store, set(ws.periods) | {ws.periods[0] - 1})
        if violations:
            raise AssertionError(f"deterministic answer breaks the guard: {violations}")
    cited = []
    cited_ids = guard.PLACEHOLDER.findall(text)
    store = tools.store
    html_lines, in_list = [], False
    for ln in text.split("\n"):
        item = ln.startswith("- ")
        body = _inline_html(ln[2:] if item else ln, store, set())
        if item and not in_list:
            html_lines.append("<ul>")
        if not item and in_list:
            html_lines.append("</ul>")
        in_list = item
        html_lines.append(f"<li>{body}</li>" if item else f"<p>{body}</p>")
    if in_list:
        html_lines.append("</ul>")
    reach = _closure(store, cited_ids)
    plain = guard.PLACEHOLDER.sub(lambda m: store[m.group(1)].display, text)
    sources = [source_line(store[r], store) for r in dict.fromkeys(cited_ids)]
    return dict(plain=plain, sources=sources, question=question, answer=text, answer_html="".join(html_lines), trace=trace,
                refs={r: ref_payload(store, r) for r in sorted(reach)}, planner=used, notes=notes,
                ref_count=len(set(cited_ids)))
