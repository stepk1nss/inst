"""Synthetic test material for dev mode (no real tickets in the repo)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_demo_ticket(out: Path) -> Path:
    """A fake exam ticket: crossroads sketch + question + three options."""
    out.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (1080, 1350), "#f2f2ee")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 1080, 640], fill="#8fb27a")
    d.rectangle([440, 0, 640, 640], fill="#5d5d5d")
    d.rectangle([0, 220, 1080, 420], fill="#5d5d5d")
    d.rectangle([480, 470, 600, 600], fill="#d33b3b")  # our car (red), from the south
    d.rectangle([780, 250, 910, 370], fill="#3b6fd3")  # other car (blue), from the right
    d.text((40, 680), "ДЕМО-БИЛЕТ (тестовый материал)", font=_font(34), fill="#555")
    d.text((40, 740), "Кто должен уступить дорогу\nна нерегулируемом перекрёстке\nравнозначных дорог?", font=_font(46), fill="#111")
    options = ["1. Красный автомобиль", "2. Синий автомобиль", "3. Тот, кто едет быстрее"]
    for i, opt in enumerate(options):
        d.text((60, 1000 + i * 90), opt, font=_font(42), fill="#111")
    im.save(out)
    return out
