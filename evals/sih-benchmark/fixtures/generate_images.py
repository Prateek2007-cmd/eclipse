#!/usr/bin/env python3
"""Regenerate the fixture images (scanned-form.png, photo-1.png,
photo-2.png) with PIL.

The PNGs are binary, so they cannot go through the snapshot push;
they are either uploaded to the repo by hand or regenerated from
this script after cloning:

    python3 evals/sih-benchmark/fixtures/generate_images.py

The drawings must match ground_truth.json: the scanned form's 6
text lines (see ocr_text) and the face boxes for photo-1 and photo-2.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent


def scanned_form(path: Path) -> None:
    img = Image.new("RGB", (800, 1000), "white")
    dr = ImageDraw.Draw(img)
    dr.rectangle([40, 40, 760, 960], outline="black", width=3)
    lines = ["APPLICATION FORM", "", "Name: Test User",
             "Email: test.user@example.com", "Phone: 99999 99999",
             "Roll: TEST-24-0841",
             "College: Test College of Engineering", "",
             "Signature: _________________"]
    y = 90
    for line in lines:
        dr.text((80, y), line, fill="black")
        y += 70
    img.save(path)


def face(path: Path, size: tuple[int, int],
         faces: list[tuple[int, int, int]]) -> None:
    im = Image.new("RGB", size, (220, 220, 220))
    dr = ImageDraw.Draw(im)
    for (cx, cy, r) in faces:
        dr.ellipse([cx - r, cy - r, cx + r, cy + r],
                   fill=(232, 190, 150), outline="black", width=2)
        dr.ellipse([cx - r // 2, cy - r // 3,
                    cx - r // 2 + 12, cy - r // 3 + 12], fill="black")
        dr.ellipse([cx + r // 2 - 12, cy - r // 3,
                    cx + r // 2, cy - r // 3 + 12], fill="black")
        dr.arc([cx - r // 2, cy, cx + r // 2, cy + r // 2],
               0, 180, fill="black", width=2)
    im.save(path)


def main() -> None:
    scanned_form(HERE / "scanned-form.png")
    face(HERE / "photo-1.png", (400, 300),
         [(110, 130, 70), (290, 140, 65)])
    face(HERE / "photo-2.png", (200, 200), [(100, 100, 75)])
    print("generated: scanned-form.png, photo-1.png, photo-2.png")


if __name__ == "__main__":
    main()
