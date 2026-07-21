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

# EEPROM guard: hard cap on DDC/CI brightness writes per rolling 60 seconds.
# The min-write-interval already bounds the peak rate; this bounds the sustained
# rate as a backstop against a write runaway wearing an EEPROM-backed monitor
# (see the WARNING in the README). When the cap is hit, writes are throttled
# (the newest target still wins once the window frees). 0 disables the cap.
MONITOR_LUMINANCE_MAX_WRITES_PER_MINUTE = 600

# The hardware brightness range (0-100) is linearly remapped to this window.
# Narrowing it reduces how aggressively brightness swings between scenes, which
# both looks calmer and keeps the swing on a comparable footing to the contrast
# window below (rather than brightness sweeping the full 0-100 while contrast
# only nudges). Widen toward 0-100 for a stronger dimming effect.
MIN_DESIRED_MONITOR_LUMINANCE = 35
MAX_DESIRED_MONITOR_LUMINANCE = 90

# Maximum rate the backlight is allowed to move, in brightness units per second,
# when brightening (the scene got brighter). Temporal smoothing filters jitter,
# but an EMA moves fastest right after a scene cut (a dark->bright cut can lurch
# ~200+ units/sec at the default time constant), which looks fast and drastic.
# This caps the peak rate so large jumps ramp gradually. Higher = snappier.
# 0 disables the cap (falls back to smoothing only).
MONITOR_LUMINANCE_MAX_CHANGE_PER_SECOND = 25

# Same cap for darkening (the scene got darker). Human dark-adaptation is slower
# than light-adaptation, so easing the backlight down more gently than it comes
# up feels natural and is less distracting. Defaults to 60% of the brighten rate.
MONITOR_LUMINANCE_MAX_CHANGE_PER_SECOND_DARKEN = 15

# Scene-cut handling: when the backlight target jumps by at least this many
# units (0-100), it is treated as a scene cut and adapts on the fast time
# constant below; smaller drifts within a scene adapt on the slow one. This
# keeps the backlight stable during gentle content changes while still catching
# up promptly on hard cuts.
LUMINANCE_SCENE_CUT_THRESHOLD = 25

# Scene statistic that drives the backlight: a percentile of the luma
# histogram. 95 tracks the highlights, so a dark scene with a small bright
# area (moon, torch, muzzle flash) keeps the backlight up instead of crushing
# it -- the case where the mean fails. 50 would track the median instead.
LUMINANCE_SCENE_PERCENTILE = 95

# Percentile that measures how crushed the shadows are; feeds the tone curve's
# shadow lift (lower = more crushed = more lift). Only used when
# GAMMA_RAMP_ADJUSTMENTS is on.
SHADOW_SCENE_PERCENTILE = 5


# -- Dynamic Contrast (DDC/CI VCP 0x12) ---------------------------------------

# Also adapt the monitor's contrast control alongside brightness: raise contrast
# in dark scenes for a punchier image and lower it in bright ones. Contrast is
# derived instantaneously from the backlight command (a dimmer backlight -> more
# contrast) instead of being a second independent loop, so the two are evaluated
# from the same scene level at the same instant and move in exact lockstep --
# they always assist each other and never fight. Contrast writes share the single
# DDC/CI writer thread and the same per-minute write budget as brightness (they
# interleave on the one I2C bus, so there is no separate write budget to worry
# about). Auto-disabled if the monitor does not report contrast support at start.
CONTRAST_ADJUSTMENTS = True

# Contrast (0-100 of the response) is linearly remapped to this window, kept on a
# comparable footing to the brightness window above so neither dominates. Widen
# for a stronger effect, narrow it (or set min == max) to damp it toward off.
MIN_DESIRED_CONTRAST = 45
MAX_DESIRED_CONTRAST = 85

# Perceptual exponent for the fallback APL->contrast mapping, used only when
# MONITOR_LUMINANCE_ADJUSTMENTS is off (otherwise contrast follows the backlight
# directly). Higher = contrast only climbs on genuinely dark scenes.
CONTRAST_MAPPING_EXPONENT = 1.5

# Time constant (seconds) for contrast smoothing on the fallback (brightness-off)
# path only. When brightness control is on, contrast tracks the already-smoothed
# backlight with no extra lag, so this is not applied (extra lag is what made
# contrast trail the backlight and flicker).
TEMPORAL_SMOOTHING_CONTRAST_TAU = 1.0

# Minimum contrast change (0-100, hardware units) needed to trigger a write. A
# coarse deadband keeps contrast writes infrequent so they rarely stagger with
# brightness writes on the shared bus (a stagger between the two reads as
# flicker). Raise it to write contrast even less often.
CONTRAST_DIFFERENCE_THRESHOLD = 3.0


# -- Auto Black-Bar Detection -------------------------------------------------

# Detect letterbox/pillarbox black bars each frame and exclude them from the
# scene statistics, so a 21:9 film on a 16:9 screen is measured on the image
# only, not the black bars (which would otherwise drag the backlight down).
# Applied on top of any manual CAPTURE_CROP_* below.
AUTO_BLACK_BAR_DETECTION = True

# A row/column counts as a black bar only if its brightest pixel is at or below
# this luma (0-255). Keep it low so dark content is not mistaken for a bar.
BLACK_BAR_LUMA_THRESHOLD = 8

# Perceptual mapping from that statistic (0-1) to the backlight level: the
# statistic is raised to this power before entering the brightness window.
# 1.0 = linear. 2.2 approximates the display transfer, so the backlight tracks
# the linear light the highlights actually need; higher values dim mid-bright
# scenes more, leaving headroom for true highlights.
LUMINANCE_MAPPING_EXPONENT = 2.2


# -- Adaptive Tone Curve ------------------------------------------------------

# Recompute and apply a scene-adaptive tone curve to the gamma ramp each frame.
# Lifts shadows and compresses highlights relative to the current scene average.
#
# Off by default. The system-level gamma ramp is a coarse, global tone control
# and is easy to make look wrong (washed-out shadows, banding), and for games a
# proper in-engine shader does this far better. If you play games, drive the
# tone mapping with a ReShade shader instead (see the README's ReShade section)
# and leave this off, using this program purely for the backlight (DDC/CI) side.
GAMMA_RAMP_ADJUSTMENTS = False

# How strongly to apply the tone curve. Range 0.1 (subtle) to 1.0 (full effect).
TONE_CURVE_STRENGTH = 0.5

# Minimum time between gamma ramp writes, in milliseconds. Caps how often the
# tone curve is recomputed and applied during fast luma swings.
GAMMA_RAMP_MIN_WRITE_INTERVAL_MS = 33

# Compensate the tone curve for the backlight level actually applied to the
# hardware: when the backlight dims, shadows and mids are lifted slightly back
# toward how the content looked at your default backlight (whites stay
# anchored), and pulled down when it brightens. Requires
# MONITOR_LUMINANCE_ADJUSTMENTS.
#
# Off by default: with a global backlight this partly undoes the black-level
# gain that dimming buys you, so on dark scenes it reads as washed out. Turn it
# on only if you specifically want a constant-perceived-brightness (fake-HDR)
# look, and keep the strength low.
GAMMA_BACKLIGHT_COMPENSATION = False

# 0.0 = no compensation. The per-frame gain is hard-capped to a narrow band
# around 1.0 that widens with this value (roughly +/-30% of it), so even at
# 1.0 the lift stays gentle and can never blow out shadows.
GAMMA_BACKLIGHT_COMPENSATION_STRENGTH = 0.4


# -- Temporal Smoothing -------------------------------------------------------

# Run the scene luma through an exponential moving average before it feeds into
# tone curve and luminance calculations. Prevents fast cuts or flickering content
# from causing rapid adjustments.
TEMPORAL_SMOOTHING = True

# Time constant (in seconds) for the tone curve's reaction to luma changes.
# Higher = slower/smoother eye adaptation. Lower = faster but less stable.
# Frame-rate independent: smoothing behaves the same at 30 or 240 fps.
TEMPORAL_SMOOTHING_GAMMA_TAU = 0.2

# Same idea for luminance, for gentle drift within a scene. Only active when
# MONITOR_LUMINANCE_FORCE_INSTANT_ADJUSTMENTS is False; when True, raw luma is
# used instead so brightness reacts immediately. Keep this higher than the gamma
# tau since hardware brightness changes are more visually jarring than a gamma
# ramp shift. The peak rate is additionally bounded by the max-change-per-second
# settings above.
TEMPORAL_SMOOTHING_LUMINANCE_TAU = 0.6

# Faster time constant used when a scene cut is detected (a jump of at least
# LUMINANCE_SCENE_CUT_THRESHOLD). Lets the backlight catch up promptly on hard
# cuts without making gentle drift twitchy. Should be smaller than the drift tau.
TEMPORAL_SMOOTHING_LUMINANCE_CUT_TAU = 0.15


# -- Misc ---------------------------------------------------------------------

# Minimum luma shift (0-100) required to recompute the tone curve ramp.
# Keeps EMA jitter on near-static content from rewriting the ramp every frame.
GAMMA_DIFFERENCE_THRESHOLD = 0.5

# Minimum brightness change (0-100) needed to trigger a luminance adjustment.
# A small deadband stops the backlight from chasing every one-unit wobble in
# the scene statistic (which spams DDC/CI writes and can pump visibly). Raise
# it further if brightness still flickers on mostly-static content; lower it
# toward 0 for the most responsive tracking.
LUMA_DIFFERENCE_THRESHOLD = 3.0

# When a target brightness stays within LUMA_DIFFERENCE_THRESHOLD of the
# current value for this long (in seconds), it is applied anyway so brightness
# converges to the true target instead of staying slightly off forever.
LUMA_DEADBAND_SETTLE_SECONDS = 2.0

# Minimum seconds between status lines printed to the console. Printing on
# every adjustment floods the terminal, and -- if the program happens to be
# capturing that terminal -- the scrolling text feeds back into the scene
# statistic and drives a self-sustaining oscillation. Throttling breaks that.
# Set to 0 to log every change (useful for debugging, not while capturing the
# console).
STATUS_LOG_INTERVAL_SECONDS = 1.0

# -- Capture ------------------------------------------------------------------

# Capture frame rate cap. The control loop cannot act faster than the DDC/CI
# write interval and the gamma write cap anyway, while uncapped capture makes
# the Desktop Duplication thread copy GPU frames as fast as possible,
# competing with games for GPU time and adding input lag. 0 = uncapped.
CAPTURE_TARGET_FPS = 60

# Analyze every Nth pixel in each direction when computing scene statistics.
# 4 reads 1/16th of the pixels, which is visually indistinguishable for global
# statistics and cuts CPU/memory traffic accordingly. 1 = full resolution.
CAPTURE_DOWNSAMPLE_STRIDE = 4

# -- Capture Crop -------------------------------------------------------------

# Crop pixels from each edge of the captured frame before luminance is computed.
# Useful for ignoring black bars (e.g. a 16:9 game on a 16:10 monitor).
# Set to 0 to disable cropping on that edge.
CAPTURE_CROP_TOP = 0
CAPTURE_CROP_BOTTOM = 0
CAPTURE_CROP_LEFT = 0
CAPTURE_CROP_RIGHT = 0
