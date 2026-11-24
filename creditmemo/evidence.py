"""Render a cropped page image around each cited source value, with the value boxed."""
from __future__ import annotations

from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

DPI = 130


def render_crops(refs: list, data_root: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    docs: dict = {}
    n = 0
    for r in refs:
        e = r.evidence
        if r.kind != "fact" or not e:
            continue
        if e["doc"] not in docs:
            docs[e["doc"]] = pymupdf.open(data_root / e["doc"])
        page = docs[e["doc"]][e["page"] - 1]
        W, H = page.rect.width, page.rect.height
        lx0, ly0, lx1, ly1 = e["line_bbox"]
        clip = pymupdf.Rect(0, max(0, ly0 * H - 38), W, min(H, ly1 * H + 38))
        pix = page.get_pixmap(dpi=DPI, clip=clip, colorspace=pymupdf.csRGB)
        img = Image.frombytes("RGB", (pix.w, pix.h), pix.samples)
        s = DPI / 72
        d = ImageDraw.Draw(img)
        bx0, by0, bx1, by1 = e["bbox"]
        d.rectangle([(bx0 * W - clip.x0) * s - 3, (by0 * H - clip.y0) * s - 3,
                     (bx1 * W - clip.x0) * s + 3, (by1 * H - clip.y0) * s + 3], outline=(214, 40, 40), width=3)
        img.save(out_dir / f"{r.id}.png", optimize=True)
        n += 1
    for d in docs.values():
        d.close()
    return n
