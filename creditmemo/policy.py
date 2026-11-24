"""Policy evaluation: configurable YAML rules applied to the latest-year metrics."""
from __future__ import annotations

from pathlib import Path

import yaml

from .refs import Ref, RefStore, fmt_value

OPS = {">=": lambda v, t: v >= t, "<=": lambda v, t: v <= t, ">": lambda v, t: v > t, "<": lambda v, t: v < t}
OP_WORDS = {">=": "minimum", "<=": "maximum", ">": "floor", "<": "ceiling"}


def load_policy(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        pol = yaml.safe_load(fh)
    for r in pol["rules"]:
        if r["op"] not in OPS or r["severity"] not in ("exception", "watch"):
            raise ValueError(f"bad policy rule {r.get('id')}")
    from .ratios import dscr_settings
    dscr_settings(pol.get("dscr"))        # fail early on an unknown definition
    return pol


def evaluate(policy: dict, slug: str, analysis: dict, store: RefStore, recon_fail_ref: str) -> list[dict]:
    ids, latest = analysis["ids"], analysis["latest"]
    findings = []
    for rule in policy["rules"]:
        metric = rule["metric"]
        ref_id = recon_fail_ref if metric == "recon_failures" else ids.get(metric, {}).get(latest)
        thr_id = f"policy.{rule['id']}"
        if thr_id not in store:
            store.add(Ref(id=thr_id, kind="policy", label=f"Policy limit: {rule['title']}", value=rule["threshold"],
                          unit=rule["unit"], display=fmt_value(rule["threshold"], rule["unit"]),
                          note=f"policy.yaml rule '{rule['id']}': {rule['rationale']}"))
        value = store[ref_id].value if ref_id in store else None
        if value is None:
            status, ok = rule["severity"], False
        else:
            ok = OPS[rule["op"]](value, rule["threshold"])
            status = "ok" if ok else rule["severity"]
        findings.append(dict(rule_id=rule["id"], title=rule["title"], metric=metric, metric_ref=ref_id,
                             threshold_ref=thr_id, op=rule["op"], severity=rule["severity"], status=status,
                             value=value, threshold=rule["threshold"], rationale=rule["rationale"]))
    return findings
