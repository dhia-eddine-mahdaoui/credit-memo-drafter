import http.client
import json
import re
import threading
from http.server import ThreadingHTTPServer

import pytest

from creditmemo import evaluation, extract, guard, policy, qa, ratios, server, synth
from creditmemo.reconcile import Dataset
from creditmemo.refs import RefStore

from conftest import ROOT


# ------------------------------------------------------------ DSCR definition per policy

def _dscr(extracted, slug, cfg):
    d, app = extracted[slug]
    st = RefStore()
    an = ratios.analyse(slug, Dataset(d), app, st, cfg)
    return an, st


def test_dscr_definition_is_configurable(extracted):
    L = 2023
    an, st = _dscr(extracted, "northbrook", None)
    base = st[an["ids"]["dscr"][L]].value
    cf_base = st[an["ids"]["cfads"][L]].value
    an2, st2 = _dscr(extracted, "northbrook", {"cfads": "ebitda_less_tax_less_capex", "capex_share": 0.5})
    assert st2[an2["ids"]["cfads"][L]].value == pytest.approx(cf_base - 0.5 * 800_000)
    assert st2[an2["ids"]["dscr"][L]].value < base
    an3, st3 = _dscr(extracted, "northbrook", {"cfads": "cfo_plus_interest"})
    cfo = st3[an3["ids"]["cfo"][L]].value
    assert st3[an3["ids"]["cfads"][L]].value == cfo + 185_000
    for r in st2.values():                       # formulas still carry no bare digits
        if r.kind == "calc":
            assert not re.search(r"\d", re.sub(r"\[\[[^\]]+\]\]", "", r.formula)), r.formula
    with pytest.raises(ValueError):
        _dscr(extracted, "northbrook", {"cfads": "magic"})


def test_policy_file_declares_dscr_block():
    pol = policy.load_policy(ROOT / "policy.yaml")
    assert pol["dscr"]["cfads"] == "ebitda_less_tax"


# ------------------------------------------------------------ analyst Q&A

@pytest.fixture(scope="module")
def ws(run_out):
    out, _ = run_out
    return qa.Workspace.load(out / "kestrel")


def test_dscr_question_is_answered_with_sourced_numbers(ws):
    res = qa.answer(ws, "Why did DSCR fall?")
    assert res["planner"] == "rules" and [t["tool"] for t in res["trace"]] == ["explain_change"]
    assert "fell" in res["answer"] and res["ref_count"] >= 8
    # every digit in the answer sits inside a placeholder or an FY label
    bare = re.sub(r"\[\[[^\]]+\]\]|FY\d{4}", " ", res["answer"])
    assert not re.search(r"\d", bare), bare
    # every cited ref resolves, and calc refs list inputs that resolve too
    for rid in guard.PLACEHOLDER.findall(res["answer"]):
        r = res["refs"][rid]
        assert r["display"] and (r["kind"] != "calc" or all(i in res["refs"] for i in r["inputs"]))
    assert any(r["kind"] == "fact" and r["evidence"]["page"] for r in res["refs"].values())
    # the effects are real arithmetic: numerator effect + denominator effect = change in DSCR
    vals = {r["label"]: r["value"] for r in res["refs"].values() if r["kind"] == "calc"}
    assert vals["Effect of the numerator"] + vals["Effect of the denominator"] == pytest.approx(vals["Change in DSCR"])


def test_other_intents(ws):
    checks = qa.answer(ws, "Which reconciliation checks failed?")
    assert checks["trace"][0]["tool"] == "failed_checks" and "Revenue" in checks["answer"]
    pol = qa.answer(ws, "What are the policy exceptions?")
    assert "EXCEPTION" in pol["answer"] and "Pro forma debt service cover" in pol["answer"]
    stress = qa.answer(ws, "What if EBITDA falls 15%?")
    base = ws.store[ws.ids["dscr_pro_forma"][2023]].value
    stressed = [r for r in stress["refs"].values() if r["label"] == "Stressed pro forma DSCR"][0]["value"]
    assert stressed < base
    ocr = qa.answer(ws, "Which figures were read by OCR?")
    assert ocr["trace"][0]["tool"] == "ocr_figures"
    decision = qa.answer(ws, "Should we approve this loan?")
    assert "does not make or suggest a credit decision" in decision["answer"]
    assert qa.answer(ws, "tell me a joke")["trace"] == []


def test_llm_planner_goes_through_the_same_guard(ws):
    rid = ws.ids["dscr"][2023]
    script = iter([json.dumps({"tool": "lookup", "args": {"metric": "dscr"}}),
                   json.dumps({"final": f"DSCR is [[{rid}]] for FY2023."})])
    ok = qa.answer(ws, "dscr?", chat=lambda msgs: next(script))
    assert ok["planner"] == "llm" and ok["ref_count"] == 1

    bad = qa.answer(ws, "Why did DSCR fall?", chat=lambda msgs: json.dumps({"final": "DSCR collapsed to 0.42 in FY2023."}))
    assert bad["planner"] == "rules" and any("rejected" in n for n in bad["notes"])
    ghost = qa.answer(ws, "dscr?", chat=lambda msgs: json.dumps({"final": "It is [[made.up.ref]]."}))
    assert ghost["planner"] == "rules" and ghost["notes"]
    broken = qa.answer(ws, "dscr?", chat=lambda msgs: "not json")
    assert broken["planner"] == "rules" and ghost["notes"]

    def boom(msgs):
        raise OSError("down")
    assert qa.answer(ws, "dscr?", chat=boom)["planner"] == "rules"


def test_ask_endpoint_logs_to_a_hash_chain(run_out):
    out, _ = run_out
    httpd = ThreadingHTTPServer(("localhost", 0), server.make_handler(out))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("localhost", port, timeout=20)
        c.request("POST", "/api/ask", body=json.dumps({"memo": "ashgrove", "question": "Why did DSCR fall?"}))
        r = c.getresponse()
        body = json.loads(r.read())
        assert r.status == 200 and body["answer_html"].count('class="num"') >= 8
        c.request("POST", "/api/ask", body=json.dumps({"memo": "nope", "question": "x"}))
        assert c.getresponse().status == 400
        c.request("GET", "/qa_log.jsonl")
        assert c.getresponse().status == 404
        from creditmemo import audit
        assert audit.verify(out / "qa_log.jsonl")[0]
        assert audit.read(out / "qa_log.jsonl")[0]["payload"]["question"] == "Why did DSCR fall?"
    finally:
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------------ extraction evaluation and the harder set

@pytest.fixture(scope="session")
def hard_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("sample_hard")
    synth.generate_hard(root)
    return root


@pytest.fixture(scope="session")
def eval_summary(data_root, hard_root, extracted):
    cache = {("standard", s): v for s, v in extracted.items()}
    return evaluation.run_eval({"standard": data_root, "hard": hard_root}, cache)


def test_eval_numbers(eval_summary):
    o = eval_summary["overall"]
    assert o["fields"] == 1100 and o["precision"] > 0.995 and o["recall"] > 0.97
    assert eval_summary["by_method"]["text"]["precision"] == 1.0
    assert eval_summary["by_set"]["hard"]["recall"] > 0.95
    # every wrong value is caught by some reconciliation check; nothing wrong passes silently
    assert eval_summary["wrong_total"] == eval_summary["wrong_flagged"]
    assert not eval_summary["silent_errors"]
    html = evaluation.render_html(eval_summary)
    assert "Failure categories" in html and "precision" in html


def test_note_column_and_two_panel_pages_are_read(hard_root):
    docs, app = extract.extract_borrower(hard_root, "wexcombe")
    assert all(len(d.facts) == 72 for d in docs)
    f = next(f for f in docs[2].facts if f.key == "receivables" and f.period == 2023)
    assert f.value == 1_244_000 and "9" in f.evidence.quote          # the note reference 9 is not the value
    keys = {d.doc_id: {p["statement"] for p in d.pages} for d in docs}
    assert keys["FY2023"] >= {"income", "cashflow", "balance"}


def test_hard_set_runs_through_the_whole_pipeline(hard_root, tmp_path):
    from creditmemo import pipeline
    summaries = {s["slug"]: s for s in pipeline.run(hard_root, tmp_path, ROOT / "policy.yaml")}
    assert summaries["wexcombe"]["checks"]["fail"] == 0
    assert summaries["harcombe"]["checks"]["fail"] >= 1                 # the planted working-capital defect
    assert all(s["unlinked_numbers"] == 0 for s in summaries.values())
