"""Image normalisation (Pillow)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageOps


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_image(src: Path, dst: Path, max_side: int = 2048, quality: int = 92) -> Path:
    """EXIF rotation, RGB, downscale, JPEG. Returns dst."""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst, "JPEG", quality=quality, optimize=True)
    return dst


def pad_to_aspect(src: Path, dst: Path, min_ratio: float = 4 / 5, max_ratio: float = 1.91, quality: int = 92) -> Path:
    """Pad (not crop) an image so width/height is within [min_ratio, max_ratio].

    Instagram feed images must be between 4:5 and 1.91:1. Padding uses a blurred
    copy of the image so it does not look like plain bars.
    """
    from PIL import ImageFilter

    with Image.open(src) as im:
        im = im.convert("RGB")
        w, h = im.size
        ratio = w / h
        if min_ratio <= ratio <= max_ratio:
            im.save(dst, "JPEG", quality=quality)
            return dst
        if ratio < min_ratio:
            new_w, new_h = round(h * min_ratio), h
        else:
            new_w, new_h = w, round(w / max_ratio)
        bg = ImageOps.fit(im, (new_w, new_h)).filter(ImageFilter.GaussianBlur(40))
        bg.paste(im, ((new_w - w) // 2, (new_h - h) // 2))
        dst.parent.mkdir(parents=True, exist_ok=True)
        bg.save(dst, "JPEG", quality=quality)
    return dst
