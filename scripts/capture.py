"""Re-create the README screenshots and the demo GIF from a real pipeline run.

    python scripts/capture.py shots     # docs/screenshots/*.png
    python scripts/capture.py demo      # docs/demo.gif (and docs/demo.mp4)

Needs `python -m creditmemo run` and `python -m creditmemo eval` to have produced out/, plus
    pip install -r requirements-dev.txt     (playwright, imageio-ffmpeg)
and a Chrome or Chromium binary (set CHROME_BIN, otherwise the common locations are tried).
A scratch copy of out/ is served on localhost so the audit logs of the real out/ stay untouched.
Pages are captured as page screenshots (no browser chrome, so no address bar or paths).
"""
from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from creditmemo import server  # noqa: E402

SHOTS = ROOT / "docs" / "screenshots"


def chrome_path():
    for c in (os.environ.get("CHROME_BIN"), "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if c and Path(c).exists():
            return c
    return None


class Served:
    """Serve a scratch copy of out/ so recorded review actions never touch the real audit log."""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cm_"))
        shutil.copytree(ROOT / "out", self.tmp / "out", ignore=shutil.ignore_patterns("audit.jsonl", "qa_log.jsonl"))
        self.httpd = ThreadingHTTPServer(("localhost", 0), server.make_handler(self.tmp / "out"))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://localhost:{self.httpd.server_address[1]}"
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def launch(p):
    return p.chromium.launch(executable_path=chrome_path(), args=["--no-sandbox", "--disable-gpu"])


def scroll_to(page, sel, offset=70):
    page.evaluate("([s,o]) => window.scrollTo(0, document.querySelector(s).getBoundingClientRect().top + window.scrollY - o)",
                  [sel, offset])
    page.wait_for_timeout(150)


def click_ref(page, ref):
    page.locator(f'main a.num[data-ref="{ref}"]').first.click()
    page.wait_for_timeout(250)


# ------------------------------------------------------------------ screenshots

def shots():
    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(parents=True, exist_ok=True)
    with Served() as srv, sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 1440, "height": 1250})
        # 1 memo with linked figures
        pg.goto(f"{srv.base}/kestrel/memo.html")
        pg.evaluate("document.querySelector('#sec-ask').style.display='none'")
        pg.screenshot(path=SHOTS / "01_memo_linked_figures.png")
        pg.set_viewport_size({"width": 1440, "height": 900})
        # 2 click a figure, source crop panel (scanned page, value boxed)
        click_ref(pg, "kestrel.FY2023.2023.cash")
        pg.wait_for_selector("aside img")
        pg.wait_for_timeout(300)
        pg.screenshot(path=SHOTS / "02_source_crop_panel.png")
        # 3 reconciliation failures with the OCR misread opened
        pg.goto(f"{srv.base}/ashgrove/memo.html")
        scroll_to(pg, "#sec-integrity", 20)
        click_ref(pg, "ashgrove.FY2023.2023.net_change")
        pg.wait_for_selector("aside img")
        pg.wait_for_timeout(300)
        pg.screenshot(path=SHOTS / "03_reconciliation_failures.png")
        # 4 policy exceptions
        pg.goto(f"{srv.base}/kestrel/memo.html")
        scroll_to(pg, "#sec-exceptions", 20)
        pg.screenshot(path=SHOTS / "04_policy_exceptions.png")
        # 5 analyst Q&A with a cited figure opened
        pg.goto(f"{srv.base}/kestrel/memo.html")
        scroll_to(pg, "#sec-ask", 20)
        pg.fill("#q", "Why did DSCR fall?")
        pg.click("#askform button")
        pg.wait_for_selector("#answer .ans")
        pg.locator('#answer a.num[data-ref$=".2023.interest"]').first.click()
        pg.wait_for_timeout(300)
        pg.screenshot(path=SHOTS / "05_analyst_qa.png")
        # 6 extraction accuracy
        pg.goto(f"{srv.base}/eval.html")
        pg.set_viewport_size({"width": 1440, "height": 1350})
        pg.screenshot(path=SHOTS / "06_extraction_eval.png")
        b.close()
    print("screenshots written to docs/screenshots/")


# ------------------------------------------------------------------ demo gif

TERMINAL = """<!doctype html><meta charset=utf-8><style>
body{margin:0;background:#0b1220;font:15px/1.5 ui-monospace,Menlo,Consolas,monospace;color:#d6deeb}
.win{margin:36px auto;width:1180px;background:#0f1a2e;border:1px solid #24324d;border-radius:10px;overflow:hidden}
.bar{background:#16233c;height:34px;display:flex;align-items:center;gap:7px;padding:0 14px;color:#8da2c7;font:13px system-ui}
.bar i{width:11px;height:11px;border-radius:50%;background:#33466b;display:inline-block}
pre{margin:0;padding:16px 20px;white-space:pre-wrap;min-height:560px}
.cmd{color:#7fdbca}.fail{color:#ff8a80}.hd{color:#ffd479;font-weight:700}.dim{color:#7d8fb0}.ok{color:#9be28d}
</style><div class=win><div class=bar><i></i><i></i><i></i><span style="margin-left:10px">creditmemo</span></div><pre id=t></pre></div>
<script>
const LINES=__LINES__;
function setProgress(n){ // n = number of characters shown across all lines
  let out='',left=n;
  for(const [cls,text] of LINES){
    if(left<=0)break;
    const part=text.slice(0,left); left-=text.length+1;
    out+='<span class="'+cls+'">'+part.replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</span>\\n';
  }
  document.getElementById('t').innerHTML=out;
}
</script>"""


def terminal_lines():
    """Real output: the stored `run` output for Ashgrove plus a live `ask` answer."""
    run = (ROOT / "docs" / "demo_run_output.txt").read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(run) if l.startswith("== Ashgrove"))
    block = []
    for l in run[start:]:
        if l.startswith("== ") and block:
            break
        if "written to" in l or l.strip().startswith("note:"):
            continue
        block.append(l)
    lines = [("cmd", "python -m creditmemo run")]
    for l in block:
        cls = "hd" if l.startswith("==") else "fail" if l.strip().startswith("FAIL") else "dim" if "latest year" in l else ""
        lines.append((cls, l[:150] + ("..." if len(l) > 150 else "")))
    return lines


def demo():
    from playwright.sync_api import sync_playwright
    import imageio_ffmpeg
    work = Path(tempfile.mkdtemp(prefix="cmdemo_"))
    frames, durs = [], []

    def snap(page, dur):
        f = work / f"f{len(frames):04d}.png"
        page.screenshot(path=f)
        frames.append(f)
        durs.append(dur)

    cursor_js = """() => { if (document.getElementById('fc')) return; const d=document.createElement('div'); d.id='fc';
      d.style.cssText='position:fixed;z-index:99999;left:0;top:0;width:22px;height:22px;pointer-events:none;transition:none';
      d.innerHTML='<svg viewBox="0 0 24 24" width="22" height="22"><path d="M3 2l17 9-7.5 2L9 21z" fill="#111" stroke="#fff" stroke-width="1.5"/></svg>';
      document.body.appendChild(d); }"""

    def move_to(page, loc, dur=0.25):
        page.evaluate(cursor_js)
        box = loc.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.evaluate("([x,y]) => { const c=document.getElementById('fc'); c.style.left=x+'px'; c.style.top=y+'px'; }", [x, y])
        snap(page, dur)

    with Served() as srv, sync_playwright() as p:
        b = launch(p)
        pg = b.new_page(viewport={"width": 1280, "height": 800})
        # scene 1: terminal replay of real output
        lines = terminal_lines()
        pg.set_content(TERMINAL.replace("__LINES__", json.dumps(lines)))
        total = sum(len(t) + 1 for _, t in lines)
        cmd_len = len(lines[0][1])
        for n in range(0, cmd_len + 1, 4):
            pg.evaluate("n => setProgress(n)", n)
            snap(pg, 0.08)
        snap(pg, 0.4)
        pos = cmd_len + 1
        for l in lines[1:]:
            pos += len(l[1]) + 1
            pg.evaluate("n => setProgress(n)", pos)
            snap(pg, 0.22 if l[0] != "fail" else 0.5)
        snap(pg, 1.2)
        # scene 2: drafted memo
        pg.goto(f"{srv.base}/ashgrove/memo.html")
        pg.wait_for_timeout(300)
        snap(pg, 1.3)
        scroll_to(pg, "#sec-financials", 20)
        snap(pg, 1.0)
        # scene 3: failed check, click the misread figure, see the source crop
        scroll_to(pg, "#sec-integrity", 20)
        snap(pg, 1.0)
        loc = pg.locator('main a.num[data-ref="ashgrove.FY2023.2023.net_change"]').first
        move_to(pg, loc)
        loc.click()
        pg.wait_for_selector("aside img")
        pg.wait_for_timeout(250)
        snap(pg, 3.0)
        # scene 4: analyst Q&A
        scroll_to(pg, "#sec-ask", 20)
        snap(pg, 0.5)
        q = "Why did DSCR fall?"
        for k in range(2, len(q) + 1, 3):
            pg.fill("#q", q[:k])
            snap(pg, 0.09)
        pg.fill("#q", q)
        snap(pg, 0.3)
        move_to(pg, pg.locator("#askform button"))
        pg.click("#askform button")
        pg.wait_for_selector("#answer .ans")
        pg.wait_for_timeout(250)
        snap(pg, 2.2)
        rid = pg.evaluate("[...document.querySelectorAll('#answer a.num')].map(a => a.dataset.ref).find(r => REFS[r].kind === 'fact')")
        link = pg.locator(f'#answer a.num[data-ref="{rid}"]').first
        move_to(pg, link)
        link.click()
        pg.wait_for_selector("aside img")
        pg.wait_for_timeout(250)
        snap(pg, 3.0)
        # scene 5: reviewer approves a section
        scroll_to(pg, "#sec-ratios", 20)
        pg.fill("#reviewer", "A. Reviewer")
        snap(pg, 0.8)
        btn = pg.locator('#sec-ratios .review button[data-decision="approved"]')
        move_to(pg, btn)
        btn.click()
        pg.wait_for_timeout(500)
        snap(pg, 1.6)
        b.close()

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    listing = work / "list.txt"
    with open(listing, "w", encoding="utf-8") as fh:
        for f, d in zip(frames, durs):
            fh.write(f"file '{f.name}'\nduration {d}\n")
        fh.write(f"file '{frames[-1].name}'\n")
    gif = ROOT / "docs" / "demo.gif"
    vf = "fps=10,scale=1000:-1:flags=lanczos"
    subprocess.run([ff, "-y", "-v", "error", "-f", "concat", "-i", str(listing), "-vf",
                    f"{vf},split[a][b];[a]palettegen=max_colors=192:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle",
                    "-loop", "0", str(gif)], check=True, cwd=work)
    subprocess.run([ff, "-y", "-v", "error", "-f", "concat", "-i", str(listing), "-vf", "fps=15,scale=1280:-2,format=yuv420p",
                    "-c:v", "libx264", "-crf", "26", "-movflags", "+faststart", str(ROOT / "docs" / "demo.mp4")],
                   check=True, cwd=work)
    secs = sum(durs)
    print(f"docs/demo.gif {gif.stat().st_size / 1e6:.2f} MB, about {secs:.0f} s, {len(frames)} frames")
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    {"shots": shots, "demo": demo}[sys.argv[1] if len(sys.argv) > 1 else "shots"]()
