"""Append-only, hash-chained review log (JSON lines)."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

DECISIONS = ("approved", "needs-changes")
SECTIONS = ("overview", "financials", "ratios", "integrity", "risks", "exceptions", "recommendation")
_lock = threading.Lock()


def _digest(prev: str, entry: dict) -> str:
    body = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()


def read(path: Path, memo: str | None = None) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if memo is None or r["memo"] == memo]


def append(path: Path, memo: str, section: str, decision: str, reviewer: str, comment: str = "",
           memo_sha256: str = "") -> dict:
    if section not in SECTIONS:
        raise ValueError("unknown section")
    if decision not in DECISIONS:
        raise ValueError("decision must be approved or needs-changes")
    reviewer = reviewer.strip()
    if not 1 <= len(reviewer) <= 60 or len(comment) > 500:
        raise ValueError("reviewer name required (max 60 chars); comment max 500 chars")
    path = Path(path)
    with _lock:
        rows = read(path)
        prev = rows[-1]["hash"] if rows else "0" * 64
        entry = dict(ts=datetime.now(timezone.utc).isoformat(timespec="seconds"), memo=memo, section=section,
                     decision=decision, reviewer=reviewer, comment=comment, memo_sha256=memo_sha256, prev=prev)
        entry["hash"] = _digest(prev, entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    return entry


def append_event(path: Path, memo: str, kind: str, payload: dict) -> dict:
    """Append a free-form event (for example an analyst question and its answer) to a hash-chained log."""
    path = Path(path)
    with _lock:
        rows = read(path)
        prev = rows[-1]["hash"] if rows else "0" * 64
        entry = dict(ts=datetime.now(timezone.utc).isoformat(timespec="seconds"), memo=memo, kind=kind,
                     payload=payload, prev=prev)
        entry["hash"] = _digest(prev, entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    return entry


def verify(path: Path) -> tuple[bool, str]:
    prev = "0" * 64
    for i, row in enumerate(read(path), 1):
        h = row.pop("hash")
        if row["prev"] != prev or _digest(prev, row) != h:
            return False, f"chain broken at entry {i}"
        prev = h
    return True, "ok"
