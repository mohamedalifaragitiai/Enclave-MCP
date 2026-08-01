"""Generate an image-only ("scanned") PDF fixture for Phase 4 testing.

The real OSHA/EIA PDF corpus is not present yet, so this renders text to a
bitmap, degrades it slightly the way a real scan is degraded, and wraps it in a
PDF with no embedded text layer. That matters: pdfminer must find nothing, so
the OCR fallback path in docs_server is genuinely exercised rather than quietly
short-circuited by extractable text.

The content deliberately covers 1910.119 sections that appear in no other
fixture, so text recovered from it can only have come from OCR.

Runs inside the docs_server container, which already has Pillow and fonts:

  docker run --rm -v /mnt/m/MCP_Project/data:/app/data \\
    -v /mnt/m/MCP_Project/scripts:/app/scripts \\
    docs_server:0.1.0 python /app/scripts/make_scanned_fixture.py
"""

from __future__ import annotations

import os
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT_DIR = Path(os.environ.get("DOCS_DIR", "/app/data/raw_docs"))
OUT_PDF = OUT_DIR / "scanned_osha_hotwork.pdf"

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
FONT_SIZE = 30
WIDTH, HEIGHT = 1700, 2200
MARGIN = 130

LINES = [
    "OSHA 29 CFR 1910.119 - Process Safety Management",
    "SYNTHETIC SCANNED PAGE - Phase 4 test fixture",
    "",
    "Hot Work Permit",
    "",
    "The employer shall issue a hot work permit for hot work",
    "operations conducted on or near a covered process. The",
    "permit shall document that the fire prevention and",
    "protection requirements have been implemented prior to",
    "beginning the hot work operations. It shall indicate the",
    "date authorized for hot work, and identify the object on",
    "which hot work is to be performed.",
    "",
    "Emergency Planning and Response",
    "",
    "The employer shall establish and implement an emergency",
    "action plan for the entire plant. The emergency action",
    "plan shall include procedures for handling small releases.",
    "",
    "Trade Secrets",
    "",
    "Employers shall make all information necessary to comply",
    "with this section available to those persons responsible",
    "for compiling the process safety information, those",
    "assisting in the development of the process hazard",
    "analysis, and those responsible for developing the",
    "operating procedures.",
]


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    random.seed(7)  # deterministic fixture across regenerations

    image = Image.new("L", (WIDTH, HEIGHT), color=255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)

    y = MARGIN
    for line in LINES:
        # Nudge each line slightly: real scans are never perfectly aligned.
        draw.text((MARGIN + random.randint(-2, 2), y), line, fill=18, font=font)
        y += int(FONT_SIZE * 1.6)

    # Scanner artefacts: slight skew, softening, and sensor noise. Kept mild -
    # the point is to prove OCR works on a degraded page, not to defeat it.
    image = image.rotate(0.4, resample=Image.BICUBIC, fillcolor=255)
    image = image.filter(ImageFilter.GaussianBlur(radius=0.6))

    pixels = image.load()
    for _ in range(int(WIDTH * HEIGHT * 0.012)):
        x = random.randrange(WIDTH)
        y = random.randrange(HEIGHT)
        pixels[x, y] = max(0, min(255, pixels[x, y] + random.randint(-45, 45)))

    # Save as a PDF containing only the raster - no text layer whatsoever.
    image.convert("RGB").save(OUT_PDF, "PDF", resolution=200.0)
    print(f"wrote {OUT_PDF} ({OUT_PDF.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
