import copy
import re

import pytest

from creditmemo import policy, ratios
from creditmemo.reconcile import Dataset
from creditmemo.refs import Ref, RefStore

from conftest import ROOT


def analyse(extracted, slug, docs=None):
    d, app = extracted[slug]
    store = RefStore()
    return ratios.analyse(slug, Dataset(docs or d), app, store), store


def test_annuity_payment_known_value():
    assert ratios.annuity_payment(100_000, 12, 12) == pytest.approx(8884.88, abs=0.01)
    assert ratios.annuity_payment(1200, 0, 12) == 100


def test_ratios_match_hand_calculation(extracted):
    an, st = analyse(extracted, "northbrook")
    i, L = an["ids"], 2023
    ebit, da, rev = 1_617_000, 560_000, 12_100_000
    tax = 0.25 * (ebit - 185_000)
    ebitda = ebit + da
    assert st[i["ebitda"][L]].value == ebitda
    assert st[i["ebitda_margin"][L]].value == pytest.approx(ebitda / rev)
    assert st[i["leverage"][L]].value == pytest.approx(2_600_000 / ebitda)
    assert st[i["interest_cover"][L]].value == pytest.approx(ebit / 185_000)
    cfads = ebitda - tax
    assert st[i["dscr"][L]].value == pytest.approx(cfads / (185_000 + 400_000))
    new_ds = 12 * ratios.annuity_payment(1_800_000, 7.25, 60)
    assert st[i["dscr_pro_forma"][L]].value == pytest.approx(cfads / (585_000 + new_ds))
    assert st[i["leverage_pro_forma"][L]].value == pytest.approx((2_600_000 + 1_800_000) / ebitda)
    assert st[i["collateral_cover"][L]].value == pytest.approx(2_600_000 / 1_800_000)


def test_every_calc_shows_formula_with_resolvable_inputs(extracted):
    an, st = analyse(extracted, "kestrel")
    calcs = [r for r in st.values() if r.kind == "calc"]
    assert calcs
    for r in calcs:
        ph = re.findall(r"\[\[([^\]]+)\]\]", r.formula)
        assert r.id in ph and all(p in st for p in ph), r.id
        assert set(r.inputs) <= set(ph)
        assert not re.search(r"\d", re.sub(r"\[\[[^\]]+\]\]", "", r.formula)), r.formula


def test_restated_figure_is_used_and_recorded(extracted):
    an, st = analyse(extracted, "kestrel")
    assert an["restated"][(2022, "revenue")] == ("kestrel.FY2022.2022.revenue", "kestrel.FY2023.2022.revenue")
    assert st[an["ids"]["revenue"][2022]].value == 18_015_000


def _evaluate(an, st, pol):
    st.add(Ref("x.fail", "count", "fail", 0, "count", "0"))
    return {f["rule_id"]: f for f in policy.evaluate(pol, "x", an, st, "x.fail")}


def test_negative_ebitda_gives_undefined_leverage_and_policy_exception(extracted):
    docs = copy.deepcopy(extracted["northbrook"][0])
    for f in docs[2].facts:
        if f.period == 2023 and f.key == "ebit":
            f.value = -5_000_000
    an, st = analyse(extracted, "northbrook", docs)
    ref = st[an["ids"]["leverage_pro_forma"][2023]]
    assert ref.value is None and ref.display == "n/m"
    res = _evaluate(an, st, policy.load_policy(ROOT / "policy.yaml"))
    assert res["leverage_pro_forma"]["status"] == "exception"


def test_policy_thresholds_are_configurable(extracted):
    an, st = analyse(extracted, "kestrel")
    pol = policy.load_policy(ROOT / "policy.yaml")
    base = _evaluate(an, st, pol)
    assert base["dscr_pro_forma"]["status"] == "exception" and base["current_ratio"]["status"] == "ok"
    for r in pol["rules"]:
        if r["id"] == "dscr_pro_forma":
            r["threshold"] = 0.5
    assert _evaluate(an, st, pol)["dscr_pro_forma"]["status"] == "ok"
