"""Reference store: every number the memo may show is a Ref with a source or a formula."""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Fact


def fmt_value(value, unit: str) -> str:
    if value is None:
        return "n/m"
    if unit == "money":
        sign = "-" if value < 0 else ""
        return f"{sign}£{abs(value) / 1000:,.0f}k"
    if unit == "ratio":
        return f"{value:.2f}x"
    if unit == "pct":
        return f"{value * 100:.1f}%"
    if unit == "rate":
        return f"{value:.2f}%"
    if unit == "money_chg":
        return f"{'-' if value < 0 else '+'}£{abs(value) / 1000:,.0f}k"
    if unit == "ratio_chg":
        return f"{value:+.2f}x"
    if unit == "pctchg":
        return f"{value * 100:+.1f}%"
    if unit == "pts":
        return f"{value * 100:+.1f} pts"
    if unit == "months":
        return f"{int(value)} months"
    if unit == "count":
        return f"{int(value)}"
    return str(value)


@dataclass
class Ref:
    id: str
    kind: str                     # fact | calc | policy | count
    label: str
    value: float | int | None
    unit: str                     # money | ratio | pct | rate | months | count
    display: str
    evidence: dict | None = None  # for facts: doc, page, quote, bbox, method, confidence
    formula: str | None = None    # for calcs: text with [[ref]] placeholders
    inputs: list = field(default_factory=list)
    note: str | None = None

    def numeric_forms(self) -> set:
        """Plain numbers that legitimately describe this value (rounding handled by the guard)."""
        v = self.value
        if v is None or isinstance(v, str):
            return set()
        forms = {float(v), abs(float(v))}
        if self.unit == "money":
            forms |= {v / 1000, abs(v) / 1000, v / 1e6, abs(v) / 1e6}
        if self.unit in ("pct", "pctchg", "pts"):
            forms |= {v * 100, abs(v) * 100}
        return forms

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in
                ("id", "kind", "label", "value", "unit", "display", "evidence", "formula", "inputs", "note")}


class RefStore(dict):
    def add(self, ref: Ref) -> Ref:
        self[ref.id] = ref
        return ref

    def add_fact(self, f: Fact, label: str | None = None) -> Ref:
        if f.id in self:
            return self[f.id]
        unit = {"amount": "money", "collateral_value": "money", "term": "months", "rate": "rate"}.get(f.key)
        if f.kind == "statement":
            unit = "money"
        ev = f.evidence
        return self.add(Ref(
            id=f.id, kind="fact", label=label or f.label, value=f.value if not isinstance(f.value, str) else None,
            unit=unit or "text", display=fmt_value(f.value, unit) if unit else str(f.value),
            evidence=dict(doc=ev.doc, page=ev.page, quote=ev.quote, bbox=ev.bbox, line_bbox=ev.line_bbox,
                          method=ev.method, confidence=ev.confidence, printed=f.printed, scale=f.scale,
                          period=f.period, doc_id=f.doc_id)))

    def add_calc(self, id: str, label: str, unit: str, inputs: list, fn, formula: str, note: str | None = None):
        vals = [self[i].value for i in inputs]
        value = None if any(v is None for v in vals) else fn(*vals)
        return self.add(Ref(id=id, kind="calc", label=label, value=value, unit=unit,
                            display=fmt_value(value, unit), formula=formula.replace("[[self]]", f"[[{id}]]"),
                            inputs=list(inputs), note=note))
