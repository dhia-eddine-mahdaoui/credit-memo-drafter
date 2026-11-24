"""Extraction of statement line items and loan-application fields, each with its source.

Pages with a text layer are read via PDF words and their boxes. Pages without one
(scans) are rendered and read with OCR. Both paths produce the same Word objects,
so the line parser is shared and every value keeps document, page, quote and box.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pymupdf

from .model import Evidence, ExtractedDoc, Fact

ALIASES = {
    "income": {
        "revenue": ["revenue", "turnover", "net sales", "total revenue"],
        "cogs": ["cost of sales", "cost of goods sold", "direct costs"],
        "gross_profit": ["gross profit"],
        "opex": ["operating expenses", "administrative and selling expenses", "overheads"],
        "da": ["depreciation and amortisation", "depreciation"],
        "ebit": ["operating profit", "operating income"],
        "interest": ["interest expense", "finance costs", "interest payable"],
        "pbt": ["profit before tax", "profit before taxation"],
        "tax": ["income tax", "taxation", "tax expense"],
        "net_income": ["net income", "profit for the year", "net profit"],
    },
    "balance": {
        "cash": ["cash and cash equivalents", "cash at bank"],
        "receivables": ["trade receivables", "trade and other receivables", "accounts receivable"],
        "inventory": ["inventories", "stock"],
        "tca": ["total current assets"],
        "ppe": ["property plant and equipment", "tangible fixed assets"],
        "ta": ["total assets"],
        "payables": ["trade payables", "trade and other payables", "accounts payable"],
        "std": ["current portion of borrowings", "bank loans due within one year"],
        "tcl": ["total current liabilities"],
        "ltd": ["long term borrowings", "bank loans due after one year"],
        "tl": ["total liabilities"],
        "equity": ["total equity", "shareholders funds", "total shareholders equity"],
        "tle": ["total liabilities and equity"],
    },
    "cashflow": {
        "cf_ni": ["net income", "profit for the year", "net profit"],
        "cf_da": ["depreciation and amortisation", "depreciation"],
        "cf_wc": ["change in working capital"],
        "cfo": ["net cash from operating activities"],
        "capex": ["purchase of property plant and equipment", "purchase of tangible fixed assets"],
        "cfi": ["net cash used in investing activities"],
        "draw": ["proceeds from borrowings", "new bank loans"],
        "repay": ["repayment of borrowings", "repayment of bank loans"],
        "dividends": ["dividends paid"],
        "cff": ["net cash from financing activities"],
        "net_change": ["net change in cash"],
        "cash_begin": ["cash at beginning of year"],
        "cash_end": ["cash at end of year"],
    },
}
HEADINGS = {
    "income": r"income statement|profit and loss|profit or loss",
    "balance": r"balance sheet|financial position",
    "cashflow": r"cash flow",
}
MATCH_THRESHOLD = 0.86
LINE_TOL = 0.011
NUM_RE = re.compile(r"^\(?[-−–]?£?\(?\d[\d,]*(?:\.\d+)?\)?$")


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float | None = None


_ocr_engine = None


def ocr_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def text_words(page) -> list[Word]:
    W, H = page.rect.width, page.rect.height
    return [Word(t, x0 / W, y0 / H, x1 / W, y1 / H)
            for x0, y0, x1, y1, t, *_ in page.get_text("words")]


def ocr_words(page, dpi: int = 200) -> list[Word]:
    global _ocr_engine
    from rapidocr_onnxruntime import RapidOCR
    if _ocr_engine is None:
        _ocr_engine = RapidOCR()
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, 3)
    result, _ = _ocr_engine(img)
    words: list[Word] = []
    for box, text, score in result or []:
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        x0, x1, y0, y1 = min(xs) / pix.w, max(xs) / pix.w, min(ys) / pix.h, max(ys) / pix.h
        parts = text.split()
        total = sum(len(p) for p in parts) + max(len(parts) - 1, 0)
        cur = 0
        for p in parts:
            a = x0 + (x1 - x0) * cur / total
            cur += len(p)
            b = x0 + (x1 - x0) * cur / total
            cur += 1
            words.append(Word(p, a, y0, b, y1, float(score)))
    return words


def group_lines(words: list[Word], tol: float = LINE_TOL) -> list[list[Word]]:
    ordered = sorted(words, key=lambda w: (w.y0 + w.y1) / 2)
    lines: list[list[Word]] = []
    for w in ordered:
        yc = (w.y0 + w.y1) / 2
        if lines:
            mean = sum((x.y0 + x.y1) / 2 for x in lines[-1]) / len(lines[-1])
            if abs(yc - mean) <= tol:
                lines[-1].append(w)
                continue
        lines.append([w])
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def parse_number(text: str):
    s = text.strip()
    if s in {"-", "–", "—"}:
        return 0
    if not NUM_RE.match(s):
        return None
    neg = "(" in s or s[0] in "-−–"
    digits = re.sub(r"[^\d.]", "", s)
    v = float(digits) if "." in digits else int(digits)
    return -v if neg else v


def split_line(line: list[Word]):
    i = len(line)
    while i > 0 and parse_number(line[i - 1].text) is not None:
        i -= 1
    return line[:i], line[i:]


def _norm(label: str) -> str:
    return " ".join(re.sub(r"[^a-z ]", " ", label.lower().replace("'", "")).split())


def match_label(statement: str, label: str):
    n = _norm(label)
    best, score = None, 0.0
    for key, names in ALIASES[statement].items():
        for name in names:
            if n == name:
                s = 1.0
            else:
                s = max(difflib.SequenceMatcher(None, n, name).ratio(),
                        difflib.SequenceMatcher(None, n.replace(" ", ""), name.replace(" ", "")).ratio())
            if s > score:
                best, score = key, s
    return (best, score) if score >= MATCH_THRESHOLD else (None, score)


def line_text(line: list[Word]) -> str:
    return " ".join(w.text for w in line)


def detect_statement(lines: list[list[Word]]):
    head = " ".join(line_text(l) for l in lines[:8]).lower()
    for st, rx in HEADINGS.items():
        if re.search(rx, head):
            return st
    scores = {st: 0 for st in ALIASES}
    for line in lines:
        label, nums = split_line(line)
        if label and nums:
            for st in ALIASES:
                scores[st] += match_label(st, line_text(label))[0] is not None
    best = max(scores, key=scores.get)
    return best if scores[best] >= 3 else None


def detect_scale(text: str):
    t = text.lower()
    if not re.search(r"amounts? in", t):
        return None
    if re.search(r"thousand|'000|000s", t):
        return 1000
    if re.search(r"million|\bm\b", t):
        return 1_000_000
    return 1


def _year_header(line):
    """The year words of a header line such as '2023 2022' or 'Note 2023 2022', else None."""
    yrs = [w for w in line if re.fullmatch(r"20\d\d", w.text)]
    others = [w for w in line if w not in yrs]
    if len(yrs) >= 2 and all(re.fullmatch(r"notes?", w.text, re.I) for w in others):
        return yrs
    return None


def column_anchors(lines, fy: int) -> dict:
    """Right edges of the year headers (e.g. '2023 2022') keyed by year."""
    for line in lines:
        yrs = _year_header(line)
        if yrs:
            return {int(w.text): w.x1 for w in yrs}
    return {}


def _channel_after(words: list[Word], end: float, min_gap: float = 0.015, tolerate: int = 2):
    """x in the middle of the first vertical channel to the right of `end` that at most `tolerate` words cross
    (a footer or title line may span it), or None if there is nothing to the right."""
    if not any(w.x0 >= end for w in words):
        return None
    step, x, run = 0.0025, end, []
    while x < 1.0:
        crossing = sum(1 for w in words if w.x0 < x < w.x1)
        if crossing <= tolerate:
            run.append(x)
            if run[-1] - run[0] >= min_gap:
                return (run[0] + run[-1]) / 2
        else:
            run = []
        x += step
    return None


def split_panels(words: list[Word]) -> list[list[Word]]:
    """Pages can carry two statements side by side, or a statement next to prose notes. The year-header line
    marks where each table's numbers end; the first vertical whitespace channel to the right of each group
    of year headers is a panel boundary. Each panel is then parsed on its own (boxes stay page-normalised)."""
    for line in group_lines(words):
        yrs = [w for w in line if re.fullmatch(r"20\d\d", w.text)]     # the line may also hold prose from a neighbour
        if len({w.text for w in yrs}) < 2:          # 'Year ended 31 December 2023' is not the column header
            continue
        yrs = sorted(yrs, key=lambda w: w.x0)
        groups = [[yrs[0]]]
        for w in yrs[1:]:
            if w.x0 - groups[-1][-1].x1 > 0.1:
                groups.append([w])
            else:
                groups[-1].append(w)
        cuts = [c for c in (_channel_after(words, max(w.x1 for w in g)) for g in groups) if c is not None]
        if not cuts:
            return [words]
        edges = [-1.0] + cuts + [2.0]
        panels = [[w for w in words if lo <= (w.x0 + w.x1) / 2 < hi] for lo, hi in zip(edges, edges[1:])]
        # a real panel carries text; stray digits right of a skewed scan's last column go back to their neighbour
        merged: list[list[Word]] = []
        for pnl in panels:
            texty = sum(1 for w in pnl if re.search(r"[A-Za-z]{3}", w.text)) >= 6
            if merged and not texty:
                merged[-1] += pnl
            elif pnl:
                merged.append(pnl)
        return merged
    return [words]


def nearest_period(anchors: dict, x1: float):
    """Column whose year header the number is right-aligned with. The tolerance is a fraction of the spacing
    between year columns, so a note-reference column sitting closer to the first year than that is not a value."""
    xs = sorted(anchors.values())
    tol = min(0.4 * min(b - a for a, b in zip(xs, xs[1:])), 0.06) if len(xs) > 1 else 0.06
    year, dist = min(((y, abs(x - x1)) for y, x in anchors.items()), key=lambda t: t[1])
    return year if dist < tol else None


def _union(ws: list[Word]):
    return (min(w.x0 for w in ws), min(w.y0 for w in ws), max(w.x1 for w in ws), max(w.y1 for w in ws))


def _conf(ws: list[Word]):
    cs = [w.conf for w in ws if w.conf is not None]
    return round(min(cs), 3) if cs else None


def extract_statements(path: Path, borrower: str, rel: str) -> ExtractedDoc:
    m = re.search(r"FY(20\d\d)", path.name)
    if not m:
        raise ValueError(f"cannot read fiscal year from file name {path.name}")
    fy = int(m.group(1))
    doc_id = f"FY{fy}"
    result = ExtractedDoc(borrower=borrower, doc_id=doc_id, fy=fy, path=rel, scale=1, scale_note="")
    raw_facts = []
    page_scales: dict[int, tuple] = {}
    with pymupdf.open(path) as pdf:
        units = []                                   # (page, words of one panel, read method)
        for page in pdf:
            words = text_words(page)
            method = "text"
            if len(words) < 8:
                if not ocr_available():
                    result.warnings.append(f"page {page.number + 1}: no text layer and OCR not installed")
                    continue
                words, method = ocr_words(page), "ocr"
            for panel in split_panels(words):
                units.append((page, panel, method))
        for page, words, method in units:
            lines = group_lines(words)
            statement = detect_statement(lines)
            result.pages.append({"page": page.number + 1, "statement": statement, "method": method})
            if statement is None:
                result.warnings.append(f"page {page.number + 1}: statement type not recognised")
                continue
            head = " ".join(line_text(l) for l in lines[:10])
            pm = re.search(r"year ended.*?(20\d\d)", head, re.I)
            if pm and int(pm.group(1)) != fy:
                result.warnings.append(f"page {page.number + 1}: period {pm.group(1)} differs from file name; using file name")
            for line in lines[:12]:
                s = detect_scale(line_text(line))
                if s is not None:
                    page_scales[page.number + 1] = (s, line_text(line), line, method)
            anchors = column_anchors(lines[:14], fy)
            seen = set()
            seen_labels = set()
            for line in lines:
                label_ws, nums = split_line(line)
                if label_ws and not nums and method == "ocr":
                    k0, _ = match_label(statement, line_text(label_ws))
                    if k0 and k0 not in seen_labels:
                        seen_labels.add(k0)
                        result.warnings.append(f"page {page.number + 1}: {k0} label read by OCR but no numbers "
                                               f"(dash or blank cells are often lost in scans)")
                if not label_ws or not nums:
                    continue
                key, score = match_label(statement, line_text(label_ws))
                if key is None:
                    continue
                if key in seen:
                    result.warnings.append(f"page {page.number + 1}: duplicate line for {key}, kept first")
                    continue
                if anchors:
                    # numbers not right-aligned with a year column (note references) are not values
                    nums = [w for w in nums if nearest_period(anchors, w.x1) is not None]
                    if not nums:
                        continue
                if len(nums) > 2:
                    result.warnings.append(f"page {page.number + 1}: {key} has {len(nums)} numeric columns, skipped")
                    continue
                seen.add(key)
                assigned = {}
                for idx, w in enumerate(nums):
                    # a number belongs to the column whose header it is right-aligned with;
                    # this keeps a missed cell (e.g. a dash read as nothing) from shifting columns
                    period = nearest_period(anchors, w.x1) if anchors else fy - idx
                    if period is None or period in assigned:
                        result.warnings.append(f"page {page.number + 1}: {key} number {w.text!r} not matched to a column")
                        continue
                    assigned[period] = w
                for period, w in assigned.items():
                    raw_facts.append(dict(
                        key=key, statement=statement, label=line_text(label_ws), printed=parse_number(w.text),
                        period=period, column="current" if period == fy else "comparative",
                        ev=Evidence(rel, page.number + 1, line_text(line), tuple(round(v, 4) for v in (w.x0, w.y0, w.x1, w.y1)),
                                    tuple(round(v, 4) for v in _union(line)), method,
                                    _conf(label_ws + [w]))))
    scales = [v[0] for v in page_scales.values()]
    if scales:
        result.scale = max(set(scales), key=scales.count)
        first = next(iter(page_scales.values()))
        result.scale_note = f"stated on page(s) {sorted(page_scales)}: \"{first[1]}\""
        if len(set(scales)) > 1:
            result.warnings.append("pages state different scales; most common used")
        missing = [p["page"] for p in result.pages if p["page"] not in page_scales]
        if missing:
            result.warnings.append(f"scale not read on page(s) {missing}; inherited from other pages")
    else:
        result.scale_note = "no scale statement found; assumed whole pounds"
        result.warnings.append(result.scale_note)
    for rf in raw_facts:
        value = rf["printed"] * result.scale
        value = int(round(value)) if abs(value - round(value)) < 1e-6 else value
        result.facts.append(Fact(
            id=f"{borrower}.{doc_id}.{rf['period']}.{rf['key']}", borrower=borrower, kind="statement",
            key=rf["key"], label=rf["label"], printed=rf["printed"], value=value, doc_id=doc_id,
            period=rf["period"], column=rf["column"], statement=rf["statement"], scale=result.scale,
            evidence=rf["ev"]))
    return result


APP_LABELS = {
    "applicant": "applicant", "business": "business", "amount requested": "amount",
    "purpose": "purpose", "term": "term", "indicative interest rate": "rate",
    "collateral": "collateral", "collateral value": "collateral_value",
}
APP_LINE = re.compile(r"^(?P<label>[A-Za-z ]+):\s*(?P<val>.+)$")


def extract_application(path: Path, borrower: str, rel: str) -> list[Fact]:
    facts = []
    with pymupdf.open(path) as pdf:
        for page in pdf:
            for line in group_lines(text_words(page)):
                text = line_text(line)
                m = APP_LINE.match(text)
                if not m or m["label"].strip().lower() not in APP_LABELS:
                    continue
                key = APP_LABELS[m["label"].strip().lower()]
                val = m["val"].strip()
                if key in ("amount", "collateral_value"):
                    value = int(re.sub(r"[^\d]", "", val))
                elif key == "term":
                    value = int(re.search(r"\d+", val).group())
                elif key == "rate":
                    value = float(re.search(r"\d+(?:\.\d+)?", val).group())
                else:
                    value = val
                # box of the value part only: words after the "Label:" prefix
                n_label = len(m["label"].split())
                vw = line[n_label:] or line
                facts.append(Fact(
                    id=f"{borrower}.app.{key}", borrower=borrower, kind="application", key=key,
                    label=m["label"].strip(), printed=val, value=value, doc_id="application",
                    statement="application",
                    evidence=Evidence(rel, page.number + 1, text,
                                      tuple(round(v, 4) for v in _union(vw)),
                                      tuple(round(v, 4) for v in _union(line)), "text", None)))
    return facts


def extract_borrower(root: Path, slug: str):
    """Return (docs, app_facts) for sample_data/<slug>/."""
    folder = root / slug
    docs = []
    for p in sorted(folder.glob("FY*_financial_statements.pdf")):
        docs.append(extract_statements(p, slug, f"{slug}/{p.name}"))
    app = extract_application(folder / "loan_application.pdf", slug, f"{slug}/loan_application.pdf")
    return docs, app
