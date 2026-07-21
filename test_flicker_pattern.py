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
Flicker / pumping test pattern.

Displays a fullscreen sequence of scenes with known average picture levels
(APL), cutting between them on a fixed cadence, so you can watch how the
dynamic contrast responds and tune the smoothing/hysteresis objectively:

- Run main.py so it captures this monitor.
- Run this script fullscreen on that same monitor.
- Each scene cut is printed here with a timestamp; compare against main.py's
  throttled status log to judge how fast and how smoothly the backlight (and
  contrast) settles, and whether it pumps or oscillates.

Scenes include full-field greys spanning the range and a small bright patch on
black -- the case where a percentile-driven backlight should stay up while a
mean-driven one would crush it. Press Esc or Q to quit.
"""

import time
import tkinter as tk

# Each scene: (label, APL 0-100 background, patch_fraction). patch_fraction > 0
# draws a white square covering that fraction of the screen area on a near-black
# background instead of a flat field.
SCENES = [
    ("dark field", 8, 0.0),
    ("bright field", 85, 0.0),
    ("dark field", 8, 0.0),
    ("mid field", 45, 0.0),
    ("small highlight on black", 2, 0.02),
    ("bright field", 85, 0.0),
]

# Seconds to hold each scene before cutting to the next.
HOLD_SECONDS = 4.0


def gray_hex(apl: int) -> str:
    v = max(0, min(255, round(apl / 100 * 255)))
    return f"#{v:02x}{v:02x}{v:02x}"


class FlickerPattern:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.index = 0
        self.start = time.perf_counter()
        self.canvas = tk.Canvas(root, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        root.bind("<Escape>", lambda _e: root.destroy())
        root.bind("q", lambda _e: root.destroy())
        self.root.after(50, self.show)

    def show(self) -> None:
        label, apl, patch = SCENES[self.index % len(SCENES)]
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        self.canvas.delete("all")
        if patch > 0.0:
            self.canvas.configure(bg=gray_hex(apl))
            side = int((patch * w * h) ** 0.5)
            x0, y0 = (w - side) // 2, (h - side) // 2
            self.canvas.create_rectangle(x0, y0, x0 + side, y0 + side, fill="#ffffff", width=0)
        else:
            self.canvas.configure(bg=gray_hex(apl))

        elapsed = time.perf_counter() - self.start
        print(f"[{elapsed:7.2f}s] scene -> {label:26} (APL~{apl:3}, patch={patch:.0%})")
        self.index += 1
        self.root.after(int(HOLD_SECONDS * 1000), self.show)


def main() -> None:
    print("Flicker pattern: fullscreen. Run main.py capturing this monitor.")
    print(f"Scenes: {len(SCENES)}, hold {HOLD_SECONDS}s each. Esc/Q to quit.\n")
    root = tk.Tk()
    root.title("DCR Flicker Test Pattern")
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.configure(bg="black")
    FlickerPattern(root)
    root.mainloop()
    print("\nDone.")


if __name__ == "__main__":
    main()
