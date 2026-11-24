from creditmemo import guard, narrative, policy, ratios
from creditmemo.narrative import Ctx, NarrativeProvider
from creditmemo.reconcile import Dataset
from creditmemo.refs import Ref, RefStore

from conftest import ROOT

YEARS = {2020, 2021, 2022, 2023}


def make_ctx(extracted, slug="northbrook"):
    docs, app = extracted[slug]
    st = RefStore()
    an = ratios.analyse(slug, Dataset(docs), app, st)
    st.add(Ref("x.fail", "count", "fail", 0, "count", "0"))
    findings = policy.evaluate(policy.load_policy(ROOT / "policy.yaml"), slug, an, st, "x.fail")
    texts = {f.key: f.value for f in app if isinstance(f.value, str)}
    return Ctx(slug, texts["applicant"], texts, st, an, findings, [], YEARS), st


def test_placeholders_pass_and_unknown_refs_fail(extracted):
    ctx, st = make_ctx(extracted)
    rid = ctx.id("revenue")
    v, text = guard.check_text(f"Revenue was [[{rid}]] in FY2023.", st, YEARS)
    assert not v and text.count("[[") == 1
    v, _ = guard.check_text("Revenue was [[made.up.ref]].", st, YEARS)
    assert [x.kind for x in v] == ["unknown_ref"]


def test_invented_number_is_rejected(extracted):
    ctx, st = make_ctx(extracted)
    v, _ = guard.check_text("Revenue grew to 99.9m and margin reached 41.3%.", st, YEARS)
    assert {x.text for x in v} == {"99.9", "41.3"} and all(x.kind == "number_not_in_data" for x in v)
    v, _ = guard.check_text("The company was founded in 1987.", st, YEARS)
    assert v and v[0].text == "1987"


def test_bare_number_that_matches_data_is_auto_linked(extracted):
    ctx, st = make_ctx(extracted)
    v, text = guard.check_text("EBITDA was 2,177 thousand pounds.", st, YEARS)
    assert not v and f"[[{ctx.id('ebitda')}]]" in text


def test_template_drafts_pass_the_guard(extracted):
    ctx, st = make_ctx(extracted)
    for section, fn in narrative.DRAFTS.items():
        v, _ = guard.check_text(fn(ctx), st, YEARS)
        assert not v, (section, v)


class Fabricating(NarrativeProvider):
    name = "fake-llm"

    def narrative(self, section, draft, ctx):
        return draft + " Revenue is forecast to reach 25.4m next year."


class Faithful(NarrativeProvider):
    name = "fake-llm"

    def narrative(self, section, draft, ctx):
        return draft.replace("has applied for", "is seeking") if section == "overview" else draft


def test_llm_output_with_unsourced_number_falls_back_to_template(extracted):
    ctx, st = make_ctx(extracted)
    out, notes = narrative.generate(ctx, Fabricating())
    assert out == narrative.generate(ctx, narrative.TemplateProvider())[0]
    assert len(notes) == 3 and all("number_not_in_data: 25.4" in n for n in notes)


def test_faithful_llm_output_is_accepted(extracted):
    ctx, st = make_ctx(extracted)
    out, notes = narrative.generate(ctx, Faithful())
    assert not notes and "is seeking" in out["overview"]


def test_rendered_memo_has_no_unlinked_numbers(run_out):
    out, summaries = run_out
    for slug, s in summaries.items():
        md = (out / slug / "memo.md").read_text(encoding="utf-8")
        assert guard.unlinked_numbers(md) == [], slug
        assert s["unlinked_numbers"] == 0


def test_unlinked_number_detector_catches_a_bare_figure():
    md = "## A\n\nRevenue was [GBP 12,100k](#ref-x) and costs 7,200.\n\n## Appendix: sources\n- 1,234"
    assert guard.unlinked_numbers(md) == ["7,200"]
