import http.client
import json
import re
import threading
from http.server import ThreadingHTTPServer

import pytest

from creditmemo import audit, server


def test_outputs_exist_and_every_link_has_a_target(run_out):
    out, summaries = run_out
    assert set(summaries) == {"northbrook", "kestrel", "ashgrove"}
    for slug in summaries:
        md = (out / slug / "memo.md").read_text(encoding="utf-8")
        links = set(re.findall(r"\]\(#(ref-[^)]+)\)", md))
        anchors = set(re.findall(r'<a id="(ref-[^"]+)"></a>', md))
        assert links and links <= anchors, links - anchors
        html = (out / slug / "memo.html").read_text(encoding="utf-8")
        assert 'class="num"' in html and 'data-section="recommendation"' in html
        for name in ("facts.json", "reconciliation.json", "ratios.json", "policy_findings.json"):
            json.loads((out / slug / name).read_text(encoding="utf-8"))
        assert any((out / slug / "evidence").glob("*.png"))


def test_recommendation_is_left_to_the_human(run_out):
    out, _ = run_out
    md = (out / "northbrook" / "memo.md").read_text(encoding="utf-8")
    assert "To be completed by the credit officer" in md


def test_expected_policy_outcomes(run_out):
    _, s = run_out
    assert s["northbrook"]["findings"] == []
    assert "dscr_pro_forma: exception" in s["kestrel"]["findings"]
    assert "collateral_cover: exception" in s["ashgrove"]["findings"]


def test_audit_log_is_append_only_and_tamper_evident(tmp_path):
    log = tmp_path / "audit.jsonl"
    audit.append(log, "kestrel", "ratios", "approved", "reviewer one")
    audit.append(log, "kestrel", "risks", "needs-changes", "reviewer one", "add fleet age")
    assert audit.verify(log) == (True, "ok")
    assert [r["section"] for r in audit.read(log, "kestrel")] == ["ratios", "risks"]
    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text(lines[0].replace("approved", "needs-changes") + "\n" + lines[1] + "\n", encoding="utf-8")
    assert audit.verify(log)[0] is False
    with pytest.raises(ValueError):
        audit.append(log, "kestrel", "ratios", "maybe", "x")
    with pytest.raises(ValueError):
        audit.append(log, "kestrel", "nonsense", "approved", "x")


def test_review_server_records_decisions(run_out):
    out, _ = run_out
    httpd = ThreadingHTTPServer(("localhost", 0), server.make_handler(out))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def call(method, path, body=None):
            c = http.client.HTTPConnection("localhost", port, timeout=10)
            c.request(method, path, body=json.dumps(body) if body else None)
            r = c.getresponse()
            return r.status, r.read()

        assert call("GET", "/kestrel/memo.html")[0] == 200
        assert call("GET", "/../etc/passwd")[0] in (403, 404)
        ok = {"memo": "kestrel", "section": "integrity", "decision": "needs-changes", "reviewer": "tester",
              "comment": "explain the restatement"}
        assert call("POST", "/api/review", ok)[0] == 200
        assert call("POST", "/api/review", {**ok, "decision": "bogus"})[0] == 400
        assert call("POST", "/api/review", {**ok, "memo": "nope"})[0] == 400
        status, body = call("GET", "/api/audit?memo=kestrel")
        rows = json.loads(body)
        assert status == 200 and len(rows) == 1 and rows[0]["memo_sha256"]
        assert audit.verify(out / "audit.jsonl")[0]
    finally:
        httpd.shutdown()
        httpd.server_close()
