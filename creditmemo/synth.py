"""Synthetic, fictional borrowers: statements are built from a small model that
balances by construction, then defects are injected into the *printed* values.

Money inside this module is in GBP thousands (what the statements print).
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

SERIES_YEARS = (2020, 2021, 2022, 2023)
DOC_YEARS = (2021, 2022, 2023)

# rev, cogs_pct, opex, da, interest, tax_rate, capex, draw, repay, dividends, dso, dio, dpo, std
BORROWERS = {
    "northbrook": dict(
        name="Northbrook Precision Tooling Ltd",
        style=0,
        opening=dict(cash=900, rec=1500, inv=1300, ppe=5200, pay=1000, debt=4200),
        years={
            2020: (9800, .640, 2050, 500, 215, .19, 600, 0, 400, 150, 55, 60, 45, 400),
            2021: (10450, .635, 2150, 520, 205, .19, 650, 0, 400, 200, 54, 58, 45, 400),
            2022: (11300, .630, 2250, 540, 195, .19, 700, 0, 400, 250, 53, 57, 45, 400),
            2023: (12100, .630, 2300, 560, 185, .25, 800, 0, 400, 300, 52, 56, 45, 400),
        },
        units_docs=(), scan=(), defects=(),
        application=dict(
            business="Precision machined components for industrial customers",
            amount=1_800_000, purpose="Purchase of a five-axis CNC machining centre",
            term=60, rate=7.25,
            collateral="First charge over the financed machine and the freehold workshop",
            collateral_value=2_600_000),
    ),
    "kestrel": dict(
        name="Kestrel Bay Logistics Ltd",
        style=1,
        opening=dict(cash=900, rec=2600, inv=150, ppe=9800, pay=2300, debt=9000),
        years={
            2020: (16500, .820, 1150, 1000, 610, .19, 900, 800, 900, 0, 55, 5, 40, 1000),
            2021: (17300, .820, 1180, 1040, 640, .19, 950, 800, 950, 0, 56, 5, 42, 1000),
            2022: (18420, .820, 1210, 1080, 680, .19, 1000, 850, 1000, 0, 57, 5, 42, 1050),
            2023: (19100, .825, 1250, 1100, 720, .25, 1000, 800, 1000, 0, 58, 5, 44, 1100),
        },
        units_docs=(),
        scan=((2023, "balance"),),
        # FY2023 comparatives restate FY2022 revenue and direct costs (reclassification)
        defects=((2023, 2022, "revenue", -405), (2023, 2022, "cogs", +405)),
        application=dict(
            business="Regional road freight and warehousing",
            amount=2_500_000, purpose="Fleet expansion: refrigerated delivery vehicles",
            term=84, rate=8.50,
            collateral="Charge over the financed vehicles and a floating charge",
            collateral_value=2_900_000),
    ),
    "ashgrove": dict(
        name="Ashgrove Bakery Co Ltd",
        style=2,
        opening=dict(cash=900, rec=500, inv=420, ppe=3300, pay=650, debt=2600),
        years={
            2020: (6100, .570, 1850, 290, 160, .19, 200, 0, 300, 100, 30, 45, 40, 300),
            2021: (6350, .570, 1900, 300, 150, .19, 200, 0, 300, 100, 30, 45, 40, 300),
            2022: (6050, .575, 1900, 310, 140, .19, 180, 0, 300, 80, 31, 46, 40, 300),
            2023: (5700, .580, 1900, 320, 130, .25, 150, 0, 300, 0, 32, 48, 40, 300),
        },
        units_docs=(2021,),            # FY2021 statements are in whole pounds
        scan=((2023, "cashflow"),),
        # FY2022 balance sheet prints equity 40 too low (does not balance);
        # FY2023 cash-flow statement prints net income 9 too high
        defects=((2022, 2022, "equity", -40), (2022, 2022, "tle", -40),
                 (2023, 2023, "cf_ni", +9)),
        application=dict(
            business="Wholesale bakery supplying retail chains",
            amount=600_000, purpose="Working capital and oven line refurbishment",
            term=48, rate=9.00,
            collateral="Debenture over company assets and a director guarantee",
            collateral_value=450_000),
    ),
}

HEADINGS = [
    {"income": "Income Statement", "balance": "Balance Sheet", "cashflow": "Cash Flow Statement"},
    {"income": "Statement of Profit or Loss", "balance": "Statement of Financial Position",
     "cashflow": "Statement of Cash Flows"},
    {"income": "Profit and Loss Account", "balance": "Balance Sheet", "cashflow": "Cash Flow Statement"},
]
UNIT_LINES = ["All amounts in £'000", "Amounts in GBP thousands", "Amounts in GBP thousands"]
UNIT_LINE_WHOLE = "Amounts in GBP (whole pounds)"

_BASE = {
    "revenue": "Revenue", "cogs": "Cost of sales", "gross_profit": "Gross profit",
    "opex": "Operating expenses", "da": "Depreciation and amortisation", "ebit": "Operating profit",
    "interest": "Interest expense", "pbt": "Profit before tax", "tax": "Income tax",
    "net_income": "Net income",
    "cash": "Cash and cash equivalents", "receivables": "Trade receivables",
    "inventory": "Inventories", "tca": "Total current assets",
    "ppe": "Property, plant and equipment", "ta": "Total assets", "payables": "Trade payables",
    "std": "Current portion of borrowings", "tcl": "Total current liabilities",
    "ltd": "Long-term borrowings", "tl": "Total liabilities", "equity": "Total equity",
    "tle": "Total liabilities and equity",
    "cf_ni": "Net income", "cf_da": "Depreciation and amortisation",
    "cf_wc": "Change in working capital", "cfo": "Net cash from operating activities",
    "capex": "Purchase of property, plant and equipment",
    "cfi": "Net cash used in investing activities", "draw": "Proceeds from borrowings",
    "repay": "Repayment of borrowings", "dividends": "Dividends paid",
    "cff": "Net cash from financing activities", "net_change": "Net change in cash",
    "cash_begin": "Cash at beginning of year", "cash_end": "Cash at end of year",
}
LABELS = [dict(_BASE) for _ in range(3)]
LABELS[1].update(revenue="Turnover", cogs="Direct costs", opex="Administrative and selling expenses",
                 da="Depreciation", ebit="Operating income", interest="Finance costs",
                 pbt="Profit before taxation", tax="Taxation", net_income="Profit for the year",
                 cash="Cash at bank", receivables="Trade and other receivables", inventory="Stock",
                 ppe="Tangible fixed assets", payables="Trade and other payables",
                 std="Bank loans due within one year", ltd="Bank loans due after one year",
                 equity="Shareholders' funds", cf_ni="Profit for the year", cf_da="Depreciation",
                 capex="Purchase of tangible fixed assets", draw="New bank loans",
                 repay="Repayment of bank loans")
LABELS[2].update(revenue="Net sales", cogs="Cost of goods sold", opex="Overheads",
                 ebit="Operating income", interest="Interest payable", tax="Tax expense",
                 net_income="Net profit", cf_ni="Net profit")

LAYOUT = {
    "income": [("line", "revenue"), ("line", "cogs"), ("total", "gross_profit"), ("line", "opex"),
               ("line", "da"), ("total", "ebit"), ("line", "interest"), ("total", "pbt"),
               ("line", "tax"), ("total", "net_income")],
    "balance": [("head", "Current assets"), ("line", "cash"), ("line", "receivables"),
                ("line", "inventory"), ("total", "tca"), ("head", "Non-current assets"),
                ("line", "ppe"), ("total", "ta"), ("head", "Current liabilities"),
                ("line", "payables"), ("line", "std"), ("total", "tcl"),
                ("head", "Non-current liabilities"), ("line", "ltd"), ("total", "tl"),
                ("head", "Equity"), ("line", "equity"), ("total", "tle")],
    "cashflow": [("line", "cf_ni"), ("line", "cf_da"), ("line", "cf_wc"), ("total", "cfo"),
                 ("line", "capex"), ("total", "cfi"), ("line", "draw"), ("line", "repay"),
                 ("line", "dividends"), ("total", "cff"), ("total", "net_change"),
                 ("line", "cash_begin"), ("total", "cash_end")],
}
PAGES = ("income", "balance", "cashflow")


def build_series(spec) -> dict:
    """Return {year: {key: value}} in GBP thousands, signed as printed."""
    o = spec["opening"]
    prev = dict(o)
    prev["equity"] = o["cash"] + o["rec"] + o["inv"] + o["ppe"] - o["pay"] - o["debt"]
    out = {}
    for y in SERIES_YEARS:
        (rev, cogs_pct, opex, da, interest, tax_rate, capex, draw, repay, div,
         dso, dio, dpo, std) = spec["years"][y]
        cogs = round(rev * cogs_pct)
        gp = rev - cogs
        ebit = gp - opex - da
        pbt = ebit - interest
        tax = round(max(pbt, 0) * tax_rate)
        ni = pbt - tax
        rec = round(rev * dso / 365)
        inv = round(cogs * dio / 365)
        pay = round(cogs * dpo / 365)
        ppe = prev["ppe"] + capex - da
        debt = prev["debt"] + draw - repay
        wc = -(rec - prev["rec"]) - (inv - prev["inv"]) + (pay - prev["pay"])
        cfo = ni + da + wc
        cff = draw - repay - div
        net = cfo - capex + cff
        cash = prev["cash"] + net
        equity = prev["equity"] + ni - div
        tca = cash + rec + inv
        ta = tca + ppe
        tcl = pay + std
        ltd = debt - std
        tl = tcl + ltd
        assert ta == tl + equity, (y, ta, tl + equity)
        assert cash >= 0, ("negative cash", y)
        out[y] = dict(
            revenue=rev, cogs=-cogs, gross_profit=gp, opex=-opex, da=-da, ebit=ebit,
            interest=-interest, pbt=pbt, tax=-tax, net_income=ni,
            cash=cash, receivables=rec, inventory=inv, tca=tca, ppe=ppe, ta=ta,
            payables=pay, std=std, tcl=tcl, ltd=ltd, tl=tl, equity=equity, tle=tl + equity,
            cf_ni=ni, cf_da=da, cf_wc=wc, cfo=cfo, capex=-capex, cfi=-capex, draw=draw,
            repay=-repay, dividends=-div, cff=cff, net_change=net, cash_begin=prev["cash"],
            cash_end=cash,
        )
        prev = dict(cash=cash, rec=rec, inv=inv, ppe=ppe, pay=pay, debt=debt, equity=equity)
    return out


def printed_values(spec, doc_year: int) -> dict:
    """{column_year: {key: value}} as printed in the FY<doc_year> document (thousands)."""
    series = build_series(spec)
    cols = {doc_year: dict(series[doc_year]), doc_year - 1: dict(series[doc_year - 1])}
    for (dy, col, key, delta) in spec["defects"]:
        if dy == doc_year:
            cols[col][key] += delta
    return cols


def _fmt(v_thousands: int, units: bool) -> str:
    x = v_thousands * (1000 if units else 1)
    if x == 0:
        return "-"
    s = f"{abs(x):,}"
    return f"({s})" if x < 0 else s


def _draw_page(c, spec, doc_year, kind, cols, units, page_no):
    W, H = A4
    style = spec["style"]
    c.setFont("Helvetica-Bold", 14)
    c.drawString(60, H - 60, spec["name"])
    c.setFont("Helvetica-Bold", 12)
    c.drawString(60, H - 86, HEADINGS[style][kind])
    c.setFont("Helvetica", 9)
    c.drawString(60, H - 104, f"Year ended 31 December {doc_year}")
    c.drawString(60, H - 117, UNIT_LINE_WHOLE if units else UNIT_LINES[style])
    xa, xb = 430, 520
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(xa, H - 145, str(doc_year))
    c.drawRightString(xb, H - 145, str(doc_year - 1))
    y = H - 172
    for row in LAYOUT[kind]:
        if row[0] == "head":
            y -= 6
            c.setFont("Helvetica-Bold", 10)
            c.drawString(60, y, row[1])
            y -= 20
            continue
        key = row[1]
        total = row[0] == "total"
        c.setFont("Helvetica-Bold" if total else "Helvetica", 10)
        if total:
            c.setLineWidth(0.5)
            c.line(300, y + 13, xb + 2, y + 13)
        c.drawString(72 if not total else 60, y, LABELS[style][key])
        c.drawRightString(xa, y, _fmt(cols[doc_year][key], units))
        c.drawRightString(xb, y, _fmt(cols[doc_year - 1][key], units))
        y -= 22
    c.setFont("Helvetica-Oblique", 8)
    if kind == "income" and spec["defects"] and any(d[0] == doc_year and d[2] == "revenue"
                                                     for d in spec["defects"]):
        c.drawString(60, y - 14, "Comparative figures have been restated for the reclassification "
                                 "of pass-through fuel charges.")
    c.setFont("Helvetica", 8)
    c.drawString(60, 40, f"Synthetic sample document for demonstration only. Page {page_no} of 3")
    c.showPage()


def _degrade(img: Image.Image, seed: int) -> Image.Image:
    rng = np.random.default_rng(seed)
    img = img.rotate(0.6, resample=Image.BICUBIC, fillcolor=255)
    a = np.asarray(img, dtype=np.float32)
    a = a * 0.92 + 12 + rng.normal(0, 7, a.shape)
    img = Image.fromarray(np.clip(a, 0, 255).astype("uint8"))
    return img.filter(ImageFilter.GaussianBlur(0.7))


def write_statement_pdf(spec, doc_year: int, path: Path) -> None:
    units = doc_year in spec["units_docs"]
    cols = printed_values(spec, doc_year)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    c.setTitle(f"{spec['name']} FY{doc_year} financial statements (synthetic)")
    for n, kind in enumerate(PAGES, 1):
        _draw_page(c, spec, doc_year, kind, cols, units, n)
    c.save()
    src = pymupdf.open(stream=buf.getvalue(), filetype="pdf")
    out = pymupdf.open()
    for i, kind in enumerate(PAGES):
        if (doc_year, kind) in spec["scan"]:
            pix = src[i].get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
            img = Image.frombytes("L", (pix.w, pix.h), pix.samples)
            img = _degrade(img, seed=doc_year * 10 + i)
            jpg = io.BytesIO()
            img.save(jpg, format="JPEG", quality=70)
            page = out.new_page(width=A4[0], height=A4[1])
            page.insert_image(page.rect, stream=jpg.getvalue())
        else:
            out.insert_pdf(src, from_page=i, to_page=i)
    out.set_metadata({"title": f"{spec['name']} FY{doc_year} financial statements (synthetic)"})
    path.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(path), garbage=4, deflate=True)


def write_application_pdf(spec, path: Path) -> None:
    a = spec["application"]
    lines = [
        ("Applicant", spec["name"]),
        ("Business", a["business"]),
        ("Facility requested", "Amortising term loan"),
        ("Amount requested", f"GBP {a['amount']:,}"),
        ("Purpose", a["purpose"]),
        ("Term", f"{a['term']} months"),
        ("Indicative interest rate", f"{a['rate']:.2f}% per annum"),
        ("Repayment", "Equal monthly instalments, fully amortising"),
        ("Collateral", a["collateral"]),
        ("Collateral value", f"GBP {a['collateral_value']:,}"),
    ]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    W, H = A4
    c.setTitle(f"{spec['name']} loan application (synthetic)")
    c.setFont("Helvetica-Bold", 14)
    c.drawString(60, H - 60, "Loan Application")
    y = H - 100
    c.setFont("Helvetica", 10)
    for label, value in lines:
        c.drawString(60, y, f"{label}: {value}")
        y -= 24
    c.setFont("Helvetica", 8)
    c.drawString(60, 40, "Synthetic sample document for demonstration only.")
    c.save()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())


def generate(root: Path) -> list[Path]:
    """Write all sample PDFs under root/<borrower>/ and return their paths."""
    written = []
    for slug, spec in BORROWERS.items():
        for y in DOC_YEARS:
            p = root / slug / f"FY{y}_financial_statements.pdf"
            write_statement_pdf(spec, y, p)
            written.append(p)
        p = root / slug / "loan_application.pdf"
        write_application_pdf(spec, p)
        written.append(p)
    return written
