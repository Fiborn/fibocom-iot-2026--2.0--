#!/usr/bin/env python3
"""Convert the rendered first PPT slide to a 1024x600 LVGL RGB565 asset."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_image", type=Path)
    parser.add_argument("output_rgb565", type=Path)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()

    with Image.open(args.input_image) as source:
        image = source.convert("RGB").resize((1024, 600), Image.Resampling.LANCZOS)

    if args.preview:
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        image.save(args.preview, format="PNG", optimize=True)

    rgb = image.tobytes()
    output = bytearray((len(rgb) // 3) * 2)
    write = 0
    for read in range(0, len(rgb), 3):
        red, green, blue = rgb[read : read + 3]
        pixel = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
        output[write] = pixel & 0xFF
        output[write + 1] = pixel >> 8
        write += 2

    args.output_rgb565.parent.mkdir(parents=True, exist_ok=True)
    args.output_rgb565.write_bytes(output)
    print(f"wrote {args.output_rgb565} ({len(output)} bytes)")


if __name__ == "__main__":
    main()
