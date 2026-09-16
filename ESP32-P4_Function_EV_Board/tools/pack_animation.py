#!/usr/bin/env python3
"""Pack an ordered image directory into the P4Control local animation format."""

from __future__ import annotations

import argparse
import io
import re
import struct
from pathlib import Path

from PIL import Image, ImageOps

MAGIC = b"P4ANIM1\0"
WIDTH = 640
HEIGHT = 480
SUPPORTED = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


def natural_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def encode_frame(path: Path, quality: int) -> bytes:
    with Image.open(path) as source:
        source = ImageOps.exif_transpose(source).convert("RGB")
        fitted = ImageOps.contain(source, (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (WIDTH, HEIGHT), "black")
        canvas.paste(fitted, ((WIDTH - fitted.width) // 2, (HEIGHT - fitted.height) // 2))
        output = io.BytesIO()
        canvas.save(output, format="JPEG", quality=quality, optimize=True, progressive=False)
        return output.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_directory", type=Path)
    parser.add_argument("output_pack", type=Path)
    parser.add_argument("--delay-ms", type=int, default=50)
    parser.add_argument("--quality", type=int, default=85)
    args = parser.parse_args()
    if not 20 <= args.delay_ms <= 60000:
        parser.error("--delay-ms must be between 20 and 60000")
    if not 1 <= args.quality <= 95:
        parser.error("--quality must be between 1 and 95")

    frames = sorted(
        (path for path in args.input_directory.iterdir() if path.suffix.lower() in SUPPORTED),
        key=natural_key,
    )
    if not frames:
        parser.error("the input directory contains no supported images")

    args.output_pack.parent.mkdir(parents=True, exist_ok=True)
    with args.output_pack.open("wb") as output:
        output.write(MAGIC)
        output.write(struct.pack("<IIII", WIDTH, HEIGHT, len(frames), args.delay_ms))
        for index, path in enumerate(frames, start=1):
            jpeg = encode_frame(path, args.quality)
            output.write(struct.pack("<I", len(jpeg)))
            output.write(jpeg)
            print(f"[{index}/{len(frames)}] {path.name}: {len(jpeg)} bytes")
    print(f"wrote {args.output_pack}")


if __name__ == "__main__":
    main()
