"""Minimal local review server: serves out/ and records section decisions to the audit log."""
from __future__ import annotations

import hashlib
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import audit, qa


def make_handler(root: Path):
    root = root.resolve()
    log = root / "audit.jsonl"
    qa_log = root / "qa_log.jsonl"
    chat = qa.http_chat_from_env()          # None unless an LLM is configured through the environment
    workspaces: dict = {}

    def memos():
        return sorted(p.parent.name for p in root.glob("*/memo.md"))

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/api/audit":
                memo = parse_qs(url.query).get("memo", [None])[0]
                return self._send(200, json.dumps(audit.read(log, memo)).encode())
            rel = url.path.lstrip("/") or "index.html"
            target = (root / rel).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                return self._send(403, b"forbidden", "text/plain")
            if target.is_dir():
                target = target / "memo.html"
            if not target.is_file() or target.name in ("audit.jsonl", "qa_log.jsonl"):
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))

        def _ask(self):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(min(n, 5_000)))
                memo, question = data["memo"], str(data["question"]).strip()
                if memo not in memos() or not question:
                    raise ValueError("unknown memo or empty question")
                if memo not in workspaces:
                    workspaces[memo] = qa.Workspace.load(root / memo)
                res = qa.answer(workspaces[memo], question, chat)
            except (ValueError, KeyError, json.JSONDecodeError, FileNotFoundError) as exc:
                return self._send(400, str(exc).encode(), "text/plain")
            audit.append_event(qa_log, memo, "analyst_question", dict(
                question=res["question"], answer=res["plain"], planner=res["planner"], notes=res["notes"],
                tools=[dict(tool=t["tool"], args=t["args"]) for t in res["trace"]],
                cited=sorted(res["refs"])))
            self._send(200, json.dumps(res).encode())

        def do_POST(self):
            if urlparse(self.path).path == "/api/ask":
                return self._ask()
            if urlparse(self.path).path != "/api/review":
                return self._send(404, b"not found", "text/plain")
            try:
                n = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(min(n, 10_000)))
                memo = data["memo"]
                if memo not in memos():
                    raise ValueError("unknown memo")
                sha = hashlib.sha256((root / memo / "memo.md").read_bytes()).hexdigest()
                entry = audit.append(log, memo, data["section"], data["decision"], data["reviewer"],
                                     data.get("comment", ""), sha)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                return self._send(400, str(exc).encode(), "text/plain")
            self._send(200, json.dumps(entry).encode())

        def log_message(self, *args):
            pass

    return Handler


def serve(root: Path, port: int = 8765):
    httpd = ThreadingHTTPServer(("localhost", port), make_handler(root))
    print(f"Review server on http://localhost:{port}/  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
