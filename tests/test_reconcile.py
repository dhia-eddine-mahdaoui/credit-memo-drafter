import copy

from creditmemo import reconcile


def fails(checks):
    return [c for c in checks if c.status == "fail"]


def test_clean_borrower_has_no_failures(extracted):
    checks = reconcile.reconcile(extracted["northbrook"][0])
    assert not fails(checks)
    assert any(c.id.endswith("bs_balances") and c.status == "pass" for c in checks)


def test_restated_comparative_is_flagged_with_amount(extracted):
    f = fails(reconcile.reconcile(extracted["kestrel"][0]))
    assert [c.group for c in f] == ["year-over-year"]
    items = {i["label"]: i for i in f[0].items}
    assert set(items) == {"Revenue", "Cost of sales"}
    assert items["Revenue"]["expected_ids"] == ["kestrel.FY2022.2022.revenue"]
    assert items["Revenue"]["actual_id"] == "kestrel.FY2023.2022.revenue"
    assert abs(items["Revenue"]["pct"] + 405 / 18420) < 1e-9


def test_unbalanced_balance_sheet_and_net_income_tie(extracted):
    by = {d.id: d for d in reconcile.reconcile(extracted["ashgrove"][0])}
    bs = by["FY2022.2022.bs_balances"]
    assert bs.status == "fail" and "4,381,000" in bs.detail and "4,421,000" in bs.detail
    assert by["FY2023.2023.ni_tie"].status == "fail" and "33,000 vs 42,000" in by["FY2023.2023.ni_tie"].detail
    assert by["FY2023.2023.cf_cfo"].status == "fail"
    assert by["FY2022.2022.equity_roll"].status == "fail"
    assert by["FY2022.2021.bs_balances"].status == "pass"


def test_mixed_scale_is_reported_not_failed(extracted):
    by = {c.id: c for c in reconcile.reconcile(extracted["ashgrove"][0])}
    assert by["scale.mixed"].status == "info"
    assert not [c for c in by.values() if c.group == "scale" and c.status == "fail"]


def test_scale_error_is_caught_by_plausibility_and_yoy(extracted):
    docs = copy.deepcopy(extracted["northbrook"][0])
    d = docs[2]
    for f in d.facts:                      # simulate a missed "in thousands" header on the latest document
        f.value = f.value // 1000
    d.scale = 1
    ids = {c.id for c in fails(reconcile.reconcile(docs))}
    assert any(i.startswith("scale.plausible") for i in ids)
    assert any(i.startswith("yoy.FY2022-FY2023") for i in ids)


def test_summary_counts(extracted):
    s = reconcile.summarize(reconcile.reconcile(extracted["kestrel"][0]))
    assert s["total"] == s["pass"] + s["fail"] + s["info"] + s["skip"] and s["fail"] == 1
