# MIT License
#
# Copyright (c) 2025 Danyal Zia Khan
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Crop visualizer for better-dynamic-contrast-ratio.

Captures a screenshot of the configured monitor, overlays semi-transparent red
on the pixels that would be cropped out by the CAPTURE_CROP_* config values,
and saves the result as 'crop_preview.png' in the current directory.

Run this to verify your crop values before using them in the main script.
"""

import sys

import mss
import numpy as np
from PIL import Image, ImageDraw

import config

OVERLAY_ALPHA = 120  # 0 (invisible) to 255 (fully opaque)
OUTPUT_FILE = "crop_preview.png"


def main() -> None:
    crop_t = getattr(config, "CAPTURE_CROP_TOP", 0)
    crop_b = getattr(config, "CAPTURE_CROP_BOTTOM", 0)
    crop_l = getattr(config, "CAPTURE_CROP_LEFT", 0)
    crop_r = getattr(config, "CAPTURE_CROP_RIGHT", 0)

    print(f"Crop values  ->  top={crop_t}  bottom={crop_b}  left={crop_l}  right={crop_r}")

    with mss.mss() as sct:
        monitors = sct.monitors  # index 0 = all monitors combined, 1+ = individual
        monitor_index = config.MONITOR_INDEX + 1  # mss uses 1-based indexing
        if monitor_index >= len(monitors):
            print(
                f"[!] MONITOR_INDEX={config.MONITOR_INDEX} is out of range "
                f"({len(monitors) - 1} monitor(s) detected). Falling back to monitor 1."
            )
            monitor_index = 1
        monitor = monitors[monitor_index]
        screenshot = sct.grab(monitor)

    img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
    w, h = img.size

    print(f"Frame size   ->  {w}x{h}")

    # Validate that crop values don't exceed frame dimensions
    if crop_t + crop_b >= h:
        print(f"[!] crop_top ({crop_t}) + crop_bottom ({crop_b}) >= frame height ({h}). Aborting.")
        sys.exit(1)
    if crop_l + crop_r >= w:
        print(f"[!] crop_left ({crop_l}) + crop_right ({crop_r}) >= frame width ({w}). Aborting.")
        sys.exit(1)

    # Build a full-size RGBA overlay, paint red only on the cropped edges.
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    red = (220, 30, 30, OVERLAY_ALPHA)

    # Each band is drawn as a rectangle covering that edge.
    # Regions overlap at corners, which is fine visually.
    if crop_t:
        draw.rectangle([(0, 0), (w - 1, crop_t - 1)], fill=red)
    if crop_b:
        draw.rectangle([(0, h - crop_b), (w - 1, h - 1)], fill=red)
    if crop_l:
        draw.rectangle([(0, 0), (crop_l - 1, h - 1)], fill=red)
    if crop_r:
        draw.rectangle([(w - crop_r, 0), (w - 1, h - 1)], fill=red)

    if not any([crop_t, crop_b, crop_l, crop_r]):
        print("[i] All crop values are 0. Saving plain screenshot with no overlay.")

    # Composite the overlay onto the screenshot.
    base = img.convert("RGBA")
    result = Image.alpha_composite(base, overlay).convert("RGB")
    result.save(OUTPUT_FILE)

    active_area = (w - crop_l - crop_r, h - crop_t - crop_b)
    print(f"Active area  ->  {active_area[0]}x{active_area[1]}  (after crop)")
    print(f"Saved        ->  {OUTPUT_FILE}")


if __name__ == "__main__":
    main()