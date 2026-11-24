"""The "no unsourced numbers" guard.

Narrative text (from the template backend or from an optional LLM) may only carry
figures as [[ref_id]] placeholders. Anything else that looks like a number must be
(a) a fiscal year present in the data, or (b) a bare number that matches exactly one
known value, in which case it is auto-linked. Everything else is a violation and the
text is rejected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .refs import RefStore

PLACEHOLDER = re.compile(r"\[\[([A-Za-z0-9_.\-]+)\]\]")
FISCAL = re.compile(r"\bFY\d{4}\b")
NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
WORD_NUMBERS = re.compile(r"\b(?:\d+(?:st|nd|rd|th))\b")


@dataclass
class Violation:
    kind: str      # unknown_ref | number_not_in_data | ambiguous_number
    text: str


def _matches(numtxt: str, forms) -> bool:
    clean = numtxt.replace(",", "")
    decimals = len(clean.split(".")[1]) if "." in clean else 0
    x = float(clean)
    half = 0.5 * 10 ** (-decimals) + 1e-9
    return any(abs(x - f) <= half for f in forms)


def candidate_refs(numtxt: str, store: RefStore) -> list[str]:
    return [r.id for r in store.values() if _matches(numtxt, r.numeric_forms())]


def check_text(text: str, store: RefStore, years: set[int]) -> tuple[list[Violation], str]:
    """Return (violations, text with unambiguous bare numbers auto-linked)."""
    violations: list[Violation] = []
    for rid in PLACEHOLDER.findall(text):
        if rid not in store:
            violations.append(Violation("unknown_ref", rid))
    out = []
    pos = 0
    # protect placeholders and FY labels from the number scan
    protected = [(m.start(), m.end()) for m in PLACEHOLDER.finditer(text)]
    protected += [(m.start(), m.end()) for m in FISCAL.finditer(text)]
    protected += [(m.start(), m.end()) for m in WORD_NUMBERS.finditer(text)]
    for m in NUMBER.finditer(text):
        if any(a <= m.start() < b for a, b in protected):
            continue
        tok = m.group().rstrip(",")
        if re.fullmatch(r"(19|20)\d\d", tok) and int(tok) in years:
            continue
        cands = candidate_refs(tok, store)
        if not cands:
            violations.append(Violation("number_not_in_data", tok))
        elif len(cands) > 1:
            violations.append(Violation("ambiguous_number", f"{tok} -> {cands[:3]}"))
        else:
            out.append((m.start(), m.end(), f"[[{cands[0]}]]"))
    linked = text
    for a, b, rep in reversed(out):
        linked = linked[:a] + rep + linked[b:]
    return violations, linked


def unlinked_numbers(rendered_markdown: str) -> list[str]:
    """Final safety net on the rendered memo body: digits outside source links."""
    body = rendered_markdown.split("\n## Appendix", 1)[0]
    body = re.sub(r"\[[^\]]*\]\(#[^)]*\)", " ", body)      # linked numbers
    body = FISCAL.sub(" ", body)
    body = re.sub(r"<[^>]+>", " ", body)
    return NUMBER.findall(body)
