import pytest

from creditmemo import extract, reconcile, synth


def truth(slug, doc_fy, period, key):
    return synth.printed_values(synth.BORROWERS[slug], doc_fy)[period][key] * 1000


def test_text_pdfs_match_printed_values_exactly(extracted):
    docs, _ = extracted["northbrook"]
    assert len(docs) == 3
    for d in docs:
        assert len(d.facts) == 72
        for f in d.facts:
            assert f.value == truth("northbrook", d.fy, f.period, f.key), f.id
            assert f.evidence.method == "text"


def test_every_value_carries_a_source(extracted):
    for slug, (docs, app) in extracted.items():
        for f in [f for d in docs for f in d.facts] + app:
            e = f.evidence
            assert e.doc.startswith(slug + "/") and e.page >= 1 and e.quote
            x0, y0, x1, y1 = e.bbox
            assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1


def test_revenue_snippet_and_box(extracted):
    docs, _ = extracted["northbrook"]
    f = next(f for f in docs[2].facts if f.key == "revenue" and f.period == 2023)
    assert f.value == 12_100_000 and f.evidence.page == 1
    assert "12,100" in f.evidence.quote and "11,300" in f.evidence.quote


def test_scale_detection_whole_pounds_vs_thousands(extracted):
    docs, _ = extracted["ashgrove"]
    by = {d.fy: d for d in docs}
    assert by[2021].scale == 1 and by[2022].scale == 1000
    in_units = next(f for f in by[2021].facts if f.key == "revenue" and f.period == 2021)
    in_thousands = next(f for f in by[2022].facts if f.key == "revenue" and f.period == 2021)
    assert in_units.printed == 6_350_000 and in_thousands.printed == 6_350
    assert in_units.value == in_thousands.value == 6_350_000


def test_application_fields(extracted):
    _, app = extracted["kestrel"]
    v = {f.key: f.value for f in app}
    assert v["amount"] == 2_500_000 and v["term"] == 84 and v["rate"] == 8.5
    assert v["collateral_value"] == 2_900_000
    assert all(f.evidence.quote.startswith(f.label) for f in app)


def test_number_parsing_and_column_assignment():
    assert extract.parse_number("(7,200)") == -7200
    assert extract.parse_number("18,420") == 18420
    assert extract.parse_number("-") == 0
    assert extract.parse_number("Revenue") is None
    anchors = {2023: 0.72, 2022: 0.88}
    # a dash that OCR dropped must not shift the remaining number into the wrong column
    assert extract.nearest_period(anchors, 0.875) == 2022
    assert extract.nearest_period(anchors, 0.3) is None


@pytest.mark.skipif(not extract.ocr_available(), reason="rapidocr not installed")
def test_scanned_pages_are_read_by_ocr(extracted):
    for slug, statement, floor in (("kestrel", "balance", 0.95), ("ashgrove", "cashflow", 0.8)):
        docs, _ = extracted[slug]
        d = docs[2]
        ocr = [f for f in d.facts if f.evidence.method == "ocr"]
        assert ocr and all(f.statement == statement for f in ocr)
        good = [f for f in ocr if f.value == truth(slug, d.fy, f.period, f.key)]
        assert len(good) / len(ocr) >= floor
        bad = [f for f in ocr if f not in good]
        if bad:   # an OCR misread must never pass silently: some deterministic check on the document fails
            checks = reconcile.reconcile(docs)
            assert any(c.status == "fail" and c.id.startswith(d.doc_id) for c in checks)
        assert all(f.evidence.confidence and f.evidence.confidence > 0.5 for f in ocr)
