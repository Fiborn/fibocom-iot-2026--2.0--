#!/usr/bin/env python3
"""Build the single embedded 640x480 challenge JPEG used by the firmware."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageOps

WIDTH = 640
HEIGHT = 480


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_image", type=Path)
    parser.add_argument("output_image", type=Path)
    parser.add_argument("--quality", type=int, default=88)
    args = parser.parse_args()

    if not 1 <= args.quality <= 95:
        parser.error("--quality must be between 1 and 95")

    with Image.open(args.input_image) as source:
        source = ImageOps.exif_transpose(source).convert("RGB")
        fitted = ImageOps.contain(source, (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (WIDTH, HEIGHT), "black")
        canvas.paste(fitted, ((WIDTH - fitted.width) // 2, (HEIGHT - fitted.height) // 2))
        args.output_image.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(
            args.output_image,
            format="JPEG",
            quality=args.quality,
            optimize=True,
            progressive=False,
        )

    print(f"wrote {args.output_image} ({WIDTH}x{HEIGHT})")


if __name__ == "__main__":
    main()
