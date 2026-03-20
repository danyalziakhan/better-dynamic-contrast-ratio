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

# 0 = primary monitor, 1 = secondary monitor.
MONITOR_INDEX = 0


# -- Monitor Luminance (DDC/CI) -----------------------------------------------

# Automatically adjust monitor brightness via DDC/CI based on screen content.
MONITOR_LUMINANCE_ADJUSTMENTS = True

# When True, brightness is applied instantly with no temporal smoothing.
# When False, temporal smoothing is applied first (if enabled), so brightness
# tracks a smoothed luma value rather than the raw per-frame reading. Either
# way the actual hardware write is always instant; this flag just controls
# whether the target value is smoothed before being sent.
MONITOR_LUMINANCE_FORCE_INSTANT_ADJUSTMENTS = False

# The hardware brightness range (0-100) is linearly remapped to this window.
# Narrowing it reduces how aggressively brightness swings between scenes.
MIN_DESIRED_MONITOR_LUMINANCE = 0
MAX_DESIRED_MONITOR_LUMINANCE = 100


# -- Adaptive Tone Curve ------------------------------------------------------

# Recompute and apply a scene-adaptive tone curve to the gamma ramp each frame.
# Lifts shadows and compresses highlights relative to the current scene average.
GAMMA_RAMP_ADJUSTMENTS = True

# How strongly to apply the tone curve. Range 0.1 (subtle) to 1.0 (full effect).
TONE_CURVE_STRENGTH = 0.5


# -- Temporal Smoothing -------------------------------------------------------

# Run the scene luma through an exponential moving average before it feeds into
# tone curve and luminance calculations. Prevents fast cuts or flickering content
# from causing rapid adjustments.
TEMPORAL_SMOOTHING = True

# How quickly the tone curve reacts to luma changes. Lower = slower/smoother
# eye adaptation. Higher = faster but less stable.
TEMPORAL_SMOOTHING_GAMMA_ALPHA = 0.1

# Same idea for luminance. Only active when MONITOR_LUMINANCE_FORCE_INSTANT_ADJUSTMENTS
# is False; when True, raw luma is used instead so brightness reacts immediately.
# Keep this lower than the gamma alpha since hardware brightness changes are
# more visually jarring than a gamma ramp shift.
TEMPORAL_SMOOTHING_LUMINANCE_ALPHA = 0.05


# -- Misc ---------------------------------------------------------------------

# Minimum luma shift (0-100) required to recompute the tone curve ramp.
# A small non-zero value avoids redundant ramp writes on near-static content.
GAMMA_DIFFERENCE_THRESHOLD = 0.1

# Minimum brightness change (0-100) needed to trigger a luminance adjustment.
# Raise this if brightness flickers on content that is mostly static.
LUMA_DIFFERENCE_THRESHOLD = 0.0
