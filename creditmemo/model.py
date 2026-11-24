"""Plain data containers shared by the pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class Evidence:
    doc: str                      # path of the source PDF, relative to the data root
    page: int                     # 1-based
    quote: str                    # the text line the value was read from
    bbox: tuple                   # normalised (x0, y0, x1, y1) of the value itself
    line_bbox: tuple              # normalised box of the whole line
    method: str                   # "text" (PDF text layer) or "ocr"
    confidence: float | None = None


@dataclass
class Fact:
    id: str
    borrower: str
    kind: str                     # "statement" or "application"
    key: str
    label: str                    # label as printed in the document
    printed: float | int | str    # value as printed
    value: float | int | str | None   # normalised value (GBP units for money)
    doc_id: str = ""              # e.g. "FY2023"
    period: int | None = None     # fiscal year the value describes
    column: str = ""              # "current" or "comparative"
    statement: str = ""
    scale: int = 1
    evidence: Evidence | None = None

    def to_dict(self):
        return asdict(self)


@dataclass
class ExtractedDoc:
    borrower: str
    doc_id: str                   # "FY2023"
    fy: int
    path: str
    scale: int
    scale_note: str
    facts: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    pages: list = field(default_factory=list)   # [{page, statement, method}]


@dataclass
class Check:
    id: str
    group: str                    # arithmetic | cross-statement | year-over-year | scale
    name: str
    scope: str
    status: str                   # pass | fail | info | skip
    detail: str = ""
    items: list = field(default_factory=list)   # failing items, see reconcile.py

    def to_dict(self):
        return asdict(self)
