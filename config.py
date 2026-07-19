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

# Which monitor to capture and control, as a DXGI output index on the primary
# adapter (0 is usually the primary monitor, 1 the secondary, and so on).
# Brightness (DDC/CI) and gamma ramp adjustments target this same monitor.
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

# Minimum time between DDC/CI brightness writes, in milliseconds. Targets that
# arrive faster than this are coalesced so only the newest value is sent once
# the interval elapses. Most monitors need 40-50 ms between commands; raising
# this also lowers the total write count (see the EEPROM warning in the README).
MONITOR_LUMINANCE_MIN_WRITE_INTERVAL_MS = 50

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

# Minimum time between gamma ramp writes, in milliseconds. Caps how often the
# tone curve is recomputed and applied during fast luma swings.
GAMMA_RAMP_MIN_WRITE_INTERVAL_MS = 33


# -- Temporal Smoothing -------------------------------------------------------

# Run the scene luma through an exponential moving average before it feeds into
# tone curve and luminance calculations. Prevents fast cuts or flickering content
# from causing rapid adjustments.
TEMPORAL_SMOOTHING = True

# Time constant (in seconds) for the tone curve's reaction to luma changes.
# Higher = slower/smoother eye adaptation. Lower = faster but less stable.
# Frame-rate independent: smoothing behaves the same at 30 or 240 fps.
TEMPORAL_SMOOTHING_GAMMA_TAU = 0.2

# Same idea for luminance. Only active when MONITOR_LUMINANCE_FORCE_INSTANT_ADJUSTMENTS
# is False; when True, raw luma is used instead so brightness reacts immediately.
# Keep this higher than the gamma tau since hardware brightness changes are
# more visually jarring than a gamma ramp shift.
TEMPORAL_SMOOTHING_LUMINANCE_TAU = 0.4


# -- Misc ---------------------------------------------------------------------

# Minimum luma shift (0-100) required to recompute the tone curve ramp.
# Keeps EMA jitter on near-static content from rewriting the ramp every frame.
GAMMA_DIFFERENCE_THRESHOLD = 0.5

# Minimum brightness change (0-100) needed to trigger a luminance adjustment.
# Raise this if brightness flickers on content that is mostly static.
LUMA_DIFFERENCE_THRESHOLD = 0.0

# When a target brightness stays within LUMA_DIFFERENCE_THRESHOLD of the
# current value for this long (in seconds), it is applied anyway so brightness
# converges to the true target instead of staying slightly off forever.
LUMA_DEADBAND_SETTLE_SECONDS = 2.0

# -- Capture Crop -------------------------------------------------------------

# Crop pixels from each edge of the captured frame before luminance is computed.
# Useful for ignoring black bars (e.g. a 16:9 game on a 16:10 monitor).
# Set to 0 to disable cropping on that edge.
CAPTURE_CROP_TOP = 0
CAPTURE_CROP_BOTTOM = 0
CAPTURE_CROP_LEFT = 0
CAPTURE_CROP_RIGHT = 0
