"""Narrative generation behind a small provider interface.

The default TemplateProvider is deterministic and offline. An optional HTTP provider
(any OpenAI-compatible chat endpoint) is selected with CREDIT_MEMO_PROVIDER=llm and
configured through environment variables only. Whatever a provider returns passes
through guard.check_text; on any violation the deterministic draft is used instead.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field

from . import guard
from .refs import RefStore

SECTIONS = ("overview", "ratio_commentary", "risks")
OP_WORDS = {">=": "minimum", "<=": "maximum", ">": "floor", "<": "ceiling"}


@dataclass
class Ctx:
    slug: str
    name: str
    app: dict                      # text fields: business, purpose, collateral
    store: RefStore
    analysis: dict
    findings: list
    item_refs: list = field(default_factory=list)   # [(item, expected_ref, actual_ref, diff_ref)]
    years: set = field(default_factory=set)

    def id(self, name, p=None):
        p = self.analysis["latest"] if p is None else p
        return self.analysis["ids"].get(name, {}).get(p)

    def app_id(self, key):
        return self.analysis["ids"]["app"].get(key)

    def value(self, name, p=None):
        rid = self.id(name, p)
        return self.store[rid].value if rid else None

    def finding(self, rule_id):
        return next((f for f in self.findings if f["rule_id"] == rule_id), None)


def _ph(rid):
    return f"[[{rid}]]" if rid else "n/m"


def _trend(a, b, tol=0.02):
    if a is None or b is None:
        return "moved"
    if b > a * (1 + tol) if a > 0 else b > a + tol:
        return "improved"
    if b < a * (1 - tol) if a > 0 else b < a - tol:
        return "weakened"
    return "was broadly stable"


# ---------------------------------------------------------------- template drafts

def draft_overview(c: Ctx) -> str:
    a, L = c.app, c.analysis["latest"]
    prior = L - 1
    biz = a["business"][:1].lower() + a["business"][1:]
    t = (f"{c.name} ({biz}) has applied for an amortising term loan of {_ph(c.app_id('amount'))}. "
         f"Stated purpose: {a['purpose'].rstrip('.')}. The facility would run for {_ph(c.app_id('term'))} at an "
         f"indicative rate of {_ph(c.app_id('rate'))}, repaid in equal monthly instalments. "
         f"Security offered: {a['collateral'].rstrip('.')}, valued by the applicant at "
         f"{_ph(c.app_id('collateral_value'))}, which is {_ph(c.id('collateral_cover'))} cover of the requested "
         f"amount (applicant's valuation, not independently verified).")
    t += (f"\n\nRevenue for FY{L} was {_ph(c.id('revenue'))} (growth of {_ph(c.id('revenue_growth'))} on FY{prior}), "
          f"with EBITDA of {_ph(c.id('ebitda'))} and net income of {_ph(c.id('net_income'))}. "
          f"The analysis uses the borrower's annual financial statements for FY{c.analysis['periods'][0]} to FY{L}, "
          f"extracted from the supplied documents; every figure below links to its source.")
    return t


def draft_ratio_commentary(c: Ctx) -> str:
    P = c.analysis["periods"]
    L = P[-1]
    m = [c.value("ebitda_margin", p) for p in P]
    lev = [c.value("leverage", p) for p in P]
    parts = []
    parts.append("EBITDA margin was " + ", ".join(
        f"{_ph(c.id('ebitda_margin', p))} in FY{p}" for p in P[:-1]) +
        f" and {_ph(c.id('ebitda_margin', L))} in FY{L}; profitability {_trend(m[0], m[-1])} over the period.")
    parts.append(f"Leverage (total debt to EBITDA) was {_ph(c.id('leverage'))} at FY{L} against "
                 f"{_ph(c.id('leverage', P[0]))} at FY{P[0]}, so it {_trend(-(lev[0] or 0), -(lev[-1] or 0))}. "
                 f"Including the new loan it would be {_ph(c.id('leverage_pro_forma'))}.")
    parts.append(f"Interest cover was {_ph(c.id('interest_cover'))} and the current ratio {_ph(c.id('current_ratio'))} "
                 f"at FY{L}.")
    parts.append(f"Historic DSCR for FY{L} was {_ph(c.id('dscr'))}. Pro forma, existing debt service of "
                 f"{_ph(c.id('debt_service'))} plus new-loan debt service of {_ph(c.id('new_debt_service'))} gives "
                 f"a DSCR of {_ph(c.id('dscr_pro_forma'))}.")
    return " ".join(parts)


def _mitigants(c: Ctx, rule_id: str) -> list[str]:
    out = []
    cov = c.finding("collateral_cover")
    liq = c.finding("current_ratio")
    if rule_id != "collateral_cover" and cov and cov["status"] == "ok":
        out.append(f"security is offered with collateral cover of {_ph(c.id('collateral_cover'))}")
    if rule_id != "current_ratio" and liq and liq["status"] == "ok":
        out.append(f"liquidity is adequate (current ratio {_ph(c.id('current_ratio'))})")
    cash = c.value("cash")
    if rule_id in ("dscr_pro_forma", "leverage_pro_forma", "interest_cover") and cash and cash > 0:
        out.append(f"cash of {_ph(c.id('cash'))} was held at year-end")
    return out


def draft_risks(c: Ctx) -> str:
    L = c.analysis["latest"]
    bullets = []
    for f in c.findings:
        if f["status"] == "ok":
            continue
        thr = _ph(f["threshold_ref"])
        if f["rule_id"] == "reconciliation_failures":
            msg = (f"**{f['title']}** ({f['status']}): {_ph(f['metric_ref'])} reconciliation checks failed against a "
                   f"policy {OP_WORDS[f['op']]} of {thr}. Mitigant: later comparative figures were used where "
                   f"statements disagree, but the borrower should explain each difference before approval.")
        else:
            val = _ph(f["metric_ref"]) if f["value"] is not None else "not meaningful"
            mit = _mitigants(c, f["rule_id"])
            mtxt = ("Mitigants: " + "; ".join(mit) + ".") if mit else \
                "No mitigant identified in the submitted documents."
            msg = (f"**{f['title']}** ({f['status']}): {val} against a policy {OP_WORDS[f['op']]} of {thr}. {mtxt}")
        bullets.append("- " + msg)
    shown = c.item_refs[:8]
    for item, exp, act, diff in shown:
        if item["kind"] == "yoy":
            bullets.append(f"- **Source discrepancy**: {item['label']} for FY{item['period']} is {_ph(exp)} in the "
                           f"original statements but {_ph(act)} in the later comparative column "
                           f"(difference {_ph(diff)}).")
        else:
            bullets.append(f"- **Source discrepancy**: {item['label']} in the {item['scope']}: the lines sum or tie "
                           f"to {_ph(exp)} but the document shows {_ph(act)} (difference {_ph(diff)}).")
    if len(c.item_refs) > len(shown):
        bullets.append("- Further discrepancies are listed under Document integrity.")
    if not bullets:
        bullets.append("- No policy exceptions or source discrepancies were identified by the automated checks. "
                       "This does not replace the credit officer's own assessment of business and sector risk.")
    return "\n".join(bullets)


DRAFTS = {"overview": draft_overview, "ratio_commentary": draft_ratio_commentary, "risks": draft_risks}


# ---------------------------------------------------------------- providers

class NarrativeProvider:
    name = "base"

    def narrative(self, section: str, draft: str, ctx: Ctx) -> str:
        raise NotImplementedError


class TemplateProvider(NarrativeProvider):
    name = "template"

    def narrative(self, section, draft, ctx):
        return draft


class HttpLLMProvider(NarrativeProvider):
    """OpenAI-compatible chat endpoint. Configure with CREDIT_MEMO_LLM_URL, CREDIT_MEMO_LLM_MODEL and
    optionally CREDIT_MEMO_LLM_AUTH (sent as a bearer credential). Nothing is read from files."""
    name = "llm"

    def __init__(self):
        self.url = os.environ["CREDIT_MEMO_LLM_URL"]
        self.model = os.environ.get("CREDIT_MEMO_LLM_MODEL", "default")
        self.auth = os.environ.get("CREDIT_MEMO_LLM_AUTH", "")

    def narrative(self, section, draft, ctx):
        refs = "\n".join(f"{r.id} = {r.display} ({r.label})" for r in ctx.store.values()
                         if r.id.startswith(ctx.slug) or r.id.startswith("policy."))
        prompt = (
            "Rewrite the draft credit-memo section below so it reads fluently. Rules: use only facts in the draft; "
            "write every figure as a [[ref_id]] placeholder copied from the list; never write digits yourself; "
            "do not add recommendations. Keep Markdown bullets if present.\n\n"
            f"Available references:\n{refs}\n\nDraft ({section}):\n{draft}")
        body = json.dumps({"model": self.model, "temperature": 0,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        if self.auth:
            req.add_header("Authorization", f"Bearer {self.auth}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())["choices"][0]["message"]["content"].strip()


def provider_from_env() -> NarrativeProvider:
    return HttpLLMProvider() if os.environ.get("CREDIT_MEMO_PROVIDER", "template") == "llm" else TemplateProvider()


def generate(ctx: Ctx, provider: NarrativeProvider) -> tuple[dict, list]:
    """Return ({section: text with [[ref]] placeholders}, notes about fallbacks)."""
    out, notes = {}, []
    for section in SECTIONS:
        draft = DRAFTS[section](ctx)
        v, linked_draft = guard.check_text(draft, ctx.store, ctx.years)
        if v:
            raise AssertionError(f"template draft for {section} breaks the guard: {v}")
        text = draft
        if not isinstance(provider, TemplateProvider):
            try:
                cand = provider.narrative(section, draft, ctx)
                violations, linked = guard.check_text(cand, ctx.store, ctx.years)
                if violations:
                    notes.append(f"{section}: {provider.name} output rejected "
                                 f"({', '.join(f'{x.kind}: {x.text}' for x in violations[:5])}); template text used")
                else:
                    text = linked
            except Exception as exc:                      # network or provider failure
                notes.append(f"{section}: {provider.name} failed ({type(exc).__name__}); template text used")
        out[section] = text
    return out, notes
