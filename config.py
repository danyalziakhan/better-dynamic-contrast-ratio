# MIT License

# Copyright (c) 2025 Danyal Zia

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# -----------
# -- Monitor
# -----------

# Which monitor to capture from.
# 0 = primary monitor, 1 = secondary/external monitor.
MONITOR_INDEX = 0

# ------------------------------------
# -- Monitor Luminance (DDC/CI Brightness)
# ------------------------------------

# Enable automatic monitor brightness adjustments via DDC/CI.
MONITOR_LUMINANCE_ADJUSTMENTS = True

# When True, brightness changes are applied immediately rather than faded.
MONITOR_LUMINANCE_FORCE_INSTANT_ADJUSTMENTS = False

# The monitor's hardware brightness range (0-100) is remapped linearly to this range.
# Narrowing the range reduces jarring brightness swings, at the cost of less headroom.
MIN_DESIRED_MONITOR_LUMINANCE = 0
MAX_DESIRED_MONITOR_LUMINANCE = 100

# Path to a custom brightness curve file. Each line should be: logical = hardware
# e.g. "50 = 42" maps a logical level of 50 to hardware level 42.
# When set, MIN/MAX_DESIRED_MONITOR_LUMINANCE are ignored.
MONITOR_LUMINANCE_CUSTOM_MAPPING = "luma.txt"

# ---------
# -- Gamma
# ---------

# Enable automatic gamma ramp adjustments based on screen content.
# This is the main perceptual HDR effect.
GAMMA_RAMP_ADJUSTMENTS = True

# The probed hardware gamma range is remapped to this range.
# Wider range = more dramatic contrast effect. Start narrow and expand gradually.
MIN_DESIRED_GAMMA = 0.60
MAX_DESIRED_GAMMA = 1.20

# Path to a custom gamma curve file. Each line should be: computed = applied
# e.g. "0.80 = 0.75" maps a computed gamma of 0.80 to an applied value of 0.75.
# When set, MIN/MAX_DESIRED_GAMMA are ignored.
GAMMA_CUSTOM_MAPPING = "gamma.txt"

# --------
# -- Misc
# --------

# Minimum change in luminance (0-100) required to trigger an adjustment.
# Increase this if the brightness flickers on near-static content.
LUMA_DIFFERENCE_THRESHOLD = 0.0

# Minimum change in gamma required to trigger an adjustment.
# Increase this if the gamma shifts too aggressively on subtle content changes.
GAMMA_DIFFERENCE_THRESHOLD = 0.00

# Adjusts the luminance mid-point used by the gamma curve.
# Negative values bias toward shadows (darker average, less blown highlights).
# Positive values bias toward highlights (lighter average, less crushed blacks).
# Change in small increments of 0.01 until the balance feels right.
MID_POINT_BIAS = 0.0
