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

import argparse
import atexit
import collections
import math
import threading
import time
from ctypes import WINFUNCTYPE, Structure, byref, windll
from ctypes.wintypes import BOOL, BYTE, DWORD, HANDLE, HDC, WCHAR

import dxcam
import numpy as np
from numba import njit

import config

# Luminance formulas for reference. Only luminance_from_grayscale is used in
# the main loop since it is the fastest and the frame is already grayscale.
# See: https://stackoverflow.com/questions/596216/formula-to-determine-perceived-brightness-of-rgb-color


def luminance_from_rgb_weighted(arr: np.ndarray) -> float:
    total_pixels = np.prod(arr.shape[:-1])
    luminance_sum = (arr / [2550.299, 2550.587, 1770.833]).sum()
    return (luminance_sum / total_pixels) * 255


def luminance_bt709(arr: np.ndarray) -> float:
    """ITU BT.709 (HDTV) luma coefficients."""
    mean_rgb = arr.reshape(-1, 3).mean(axis=0)
    return float((mean_rgb * [0.2126, 0.7152, 0.0722]).sum() / 255 * 100)


def luminance_bt601(arr: np.ndarray) -> float:
    """ITU BT.601 (SDTV) luma coefficients."""
    mean_rgb = arr.reshape(-1, 3).mean(axis=0)
    return float((mean_rgb * [0.299, 0.587, 0.114]).sum() / 255 * 100)


@njit(cache=True)
def sum_to_0_100(total: float, width: int, height: int) -> float:
    return ((total / (width * height)) / 255) * 100


@njit(cache=True)
def luminance_from_grayscale(arr: np.ndarray) -> float:
    return sum_to_0_100(arr.sum(), arr.shape[0], arr.shape[1])


def percentile_from_histogram(hist: np.ndarray, q: float) -> int:
    """Smallest bin index at or below which fraction q of the pixels fall."""
    cum = np.cumsum(hist)
    total = cum[-1]
    if total == 0:
        return 0
    return int(np.searchsorted(cum, q * total))


def scene_statistics(
    frame: np.ndarray, highlight_percentile: float, shadow_percentile: float
) -> tuple[float, float, float]:
    """Mean luma (0-100), highlight level (0-1) and shadow level (0-1).

    All three come from one 256-bin histogram pass:

    - The highlight percentile (e.g. p95) drives the backlight -- a night scene
      with a small bright moon and a flat grey scene can share the same mean,
      but only the percentile tells the backlight the night scene still has
      highlights worth preserving.
    - The shadow percentile (e.g. p5) drives the tone curve's shadow lift: a low
      value means the shadows are crushed and there is detail to recover.
    """
    hist = np.bincount(frame.ravel(), minlength=256)
    mean_luma = float(np.dot(hist, np.arange(256))) / frame.size / 255.0 * 100.0
    highlight = percentile_from_histogram(hist, highlight_percentile / 100.0)
    shadow = percentile_from_histogram(hist, shadow_percentile / 100.0)
    return mean_luma, highlight / 255.0, shadow / 255.0


def detect_black_bars(frame: np.ndarray, threshold: int) -> tuple[int, int, int, int]:
    """Rows/columns to trim from (top, bottom, left, right) for letterbox or
    pillarbox bars -- contiguous edge rows/columns whose brightest pixel is at
    or below ``threshold``.

    Only fully-black edge bands qualify (max <= threshold), so genuinely dark
    content is not mistaken for a bar. If the whole frame is that dark (e.g. a
    black scene) nothing is cropped, leaving the statistics on the full frame.
    """
    g = frame[:, :, 0] if frame.ndim == 3 else frame
    row_max = g.max(axis=1)
    col_max = g.max(axis=0)
    dark_rows = row_max <= threshold
    dark_cols = col_max <= threshold

    def leading(mask: np.ndarray) -> int:
        nz = np.flatnonzero(~mask)
        return int(nz[0]) if nz.size else mask.size

    top = leading(dark_rows)
    bottom = leading(dark_rows[::-1])
    left = leading(dark_cols)
    right = leading(dark_cols[::-1])

    # Guard against cropping everything (all-dark frame) -> no crop.
    if top + bottom >= g.shape[0] or left + right >= g.shape[1]:
        return 0, 0, 0, 0
    return top, bottom, left, right


def contrast_target(mean_luma: float, exponent: float) -> float:
    """Contrast target (0-100) from scene APL (mean luma, 0-100).

    Contrast rises in low-APL (dark) scenes and falls in high-APL (bright)
    scenes: a dark scene benefits from a punchier curve, while a bright scene
    would clip if contrast were pushed. The response is 1 - (apl)**exponent so
    the mapping is perceptual rather than linear, mirroring the backlight map.
    """
    apl = min(max(mean_luma / 100.0, 0.0), 1.0)
    return 100.0 * (1.0 - apl**exponent)


class PhysicalMonitor(Structure):
    _fields_ = [("handle", HANDLE), ("description", WCHAR * 128)]


def get_physical_monitor_handle(hmonitor) -> HANDLE:
    physical_monitors = (PhysicalMonitor * 1)()
    if not windll.dxva2.GetPhysicalMonitorsFromHMONITOR(hmonitor, 1, physical_monitors):
        raise RuntimeError("Failed to get a physical monitor handle from the display monitor.")
    return physical_monitors[0].handle


def destroy_physical_monitor_handle(handle) -> None:
    windll.dxva2.DestroyPhysicalMonitor(HANDLE(handle))


def get_primary_monitor_handle() -> HANDLE:
    return get_physical_monitor_handle(windll.user32.MonitorFromPoint(0, 0, 1))


def get_display_dc(devicename: str) -> HDC:
    # A DC bound to the given display device (e.g. r"\\.\DISPLAY2"), unlike
    # GetDC(None) which always refers to the primary monitor. CreateDC-created
    # DCs must be freed with DeleteDC, not ReleaseDC.
    hdc = HDC(windll.gdi32.CreateDCW("DISPLAY", devicename, None, None))
    if not hdc:
        raise RuntimeError(f"Failed to create a device context for {devicename}.")
    return hdc


# DDC/CI VCP feature codes.
VCP_LUMINANCE = 0x10
VCP_CONTRAST = 0x12


def vcp_set_feature(handle, code: int, value: int) -> bool:
    return bool(windll.dxva2.SetVCPFeature(HANDLE(handle), BYTE(code), DWORD(value)))


def vcp_get_feature(handle, code: int) -> tuple[int, int] | None:
    """(current, maximum) for a VCP feature, or None if the monitor does not
    support it over DDC/CI."""
    current = DWORD()
    maximum = DWORD()
    if not windll.dxva2.GetVCPFeatureAndVCPFeatureReply(
        HANDLE(handle), BYTE(code), None, byref(current), byref(maximum)
    ):
        return None
    return current.value, maximum.value


def vcp_set_luminance(handle, value: int) -> bool:
    return vcp_set_feature(handle, VCP_LUMINANCE, value)


def vcp_get_luminance_range(handle) -> tuple[int, int]:
    """Read the monitor's current and maximum luminance. The maximum is not
    always 100; the VCP spec only guarantees the value is in [0, maximum]."""
    result = vcp_get_feature(handle, VCP_LUMINANCE)
    if result is None:
        raise RuntimeError(
            "Failed to read the monitor's luminance over DDC/CI. "
            "Make sure DDC/CI is enabled in the monitor's OSD."
        )
    return result


def vcp_get_luminance(handle) -> int:
    return vcp_get_luminance_range(handle)[0]


class VcpWriter:
    """Serializes all DDC/CI feature writes on one dedicated thread.

    Every VCP feature this program drives (brightness 0x10 and, optionally,
    contrast 0x12) shares this single writer so they share the one I2C bus:
    each feature has its own one-slot mailbox, and the writer interleaves them
    round-robin, one write per settle interval. Two writer threads would race on
    the bus with no ordering guarantee; one thread makes writes ordered,
    coalesced (a newer target replaces an unsent one), and jointly rate-limited
    -- so contrast writes draw from the same budget as brightness rather than
    doubling the load on an EEPROM-backed monitor (see the README).

    The writer is also the source of truth for each feature's state: it records
    a value only once the driver confirms the write, so a failed write leaves the
    state unchanged and the caller's next submit of the still-differing target
    acts as a natural, rate-limited retry.
    """

    def __init__(
        self,
        handle,
        min_write_interval_s: float,
        initial: dict[int, int],
        max_writes_per_minute: int = 0,
        dry_run: bool = False,
    ) -> None:
        self._handle = handle
        self._min_write_interval_s = min_write_interval_s
        self._max_writes_per_minute = max_writes_per_minute
        self._dry_run = dry_run
        self._cond = threading.Condition()
        self._codes = list(initial.keys())
        self._targets: dict[int, int] = {}
        self._last_written: dict[int, int] = dict(initial)
        self._write_failing: dict[int, bool] = dict.fromkeys(self._codes, False)
        self._rr = 0  # round-robin cursor for interleaving codes
        self._write_count = 0  # session total, for EEPROM-budget visibility
        self._stopped = False
        # Timestamps of recent writes, for the per-minute EEPROM cap.
        self._write_times: collections.deque[float] = collections.deque()
        self._cap_warned = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, code: int, value: int) -> bool:
        """Queue a new target for a feature. Returns False if it already is the
        effective state (pending or confirmed) and nothing was queued."""
        with self._cond:
            if self._stopped:
                return False
            effective = self._targets.get(code, self._last_written.get(code))
            if value == effective:
                return False
            self._targets[code] = value
            self._cond.notify()
            return True

    def effective(self, code: int) -> int:
        """Pending target for a feature if queued, else the last confirmed write."""
        with self._cond:
            return self._targets.get(code, self._last_written[code])

    def write_count(self) -> int:
        with self._cond:
            return self._write_count

    def stop(self) -> None:
        # Pending unwritten targets are dropped; the caller decides the final
        # state (e.g. restoring the defaults).
        with self._cond:
            self._stopped = True
            self._cond.notify()
        self._thread.join(timeout=2.0)

    def _eeprom_cap_wait_seconds(self) -> float:
        """Seconds to wait before the next write to honor the per-minute cap.

        Ages out timestamps older than 60 s; if the cap would still be exceeded,
        returns how long until the oldest write leaves the window. 0 means the
        write may proceed now.
        """
        cap = self._max_writes_per_minute
        if cap <= 0:
            return 0.0
        now = time.monotonic()
        while self._write_times and now - self._write_times[0] >= 60.0:
            self._write_times.popleft()
        if len(self._write_times) < cap:
            return 0.0
        return 60.0 - (now - self._write_times[0])

    def _pick_next(self) -> int | None:
        # Round-robin over codes that have a pending target so neither feature
        # starves the other on the shared bus.
        pending = [c for c in self._codes if c in self._targets]
        if not pending:
            return None
        self._rr = (self._rr + 1) % len(self._codes)
        order = self._codes[self._rr :] + self._codes[: self._rr]
        for code in order:
            if code in self._targets:
                return code
        return pending[0]

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._targets and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                code = self._pick_next()
                if code is None:
                    continue
                value = self._targets.pop(code)

            wait_s = self._eeprom_cap_wait_seconds()
            if wait_s > 0.0:
                with self._cond:
                    # Re-queue the value (coalescing with anything newer) and wait
                    # until the window frees or a change/stop wakes us.
                    self._targets.setdefault(code, value)
                    if not self._cap_warned:
                        self._cap_warned = True
                        print(
                            "[!] DDC/CI write rate cap reached; throttling writes "
                            "(MONITOR_LUMINANCE_MAX_WRITES_PER_MINUTE)."
                        )
                    self._cond.wait(timeout=wait_s)
                continue

            ok = True if self._dry_run else vcp_set_feature(self._handle, code, value)
            with self._cond:
                if ok:
                    self._last_written[code] = value
                    self._write_failing[code] = False
                    self._write_times.append(time.monotonic())
                    self._write_count += 1
                    self._cap_warned = False
                elif not self._write_failing[code]:
                    self._write_failing[code] = True
                    print(
                        f"[!] DDC/CI write of feature 0x{code:02X} failed; "
                        "retrying with the newest target."
                    )
            # Sleep outside the lock so newer targets coalesce while the bus
            # settles. In dry-run there is no bus, but keep the cadence so the
            # printed decisions mirror real timing.
            time.sleep(self._min_write_interval_s)


def get_default_gamma_ramp(GetDeviceGammaRamp, hdc) -> np.ndarray:
    ramp = np.empty((3, 256), dtype=np.uint16)
    if not GetDeviceGammaRamp(hdc, ramp.ctypes):
        raise RuntimeError("Failed to read the current gamma ramp from the display driver.")
    return ramp


@njit(cache=True)
def scale_gamma_ramp(multiplier: float, ramp: np.ndarray) -> np.ndarray:
    return np.round(np.multiply(multiplier, ramp)).astype(np.uint16)


def probe_supported_gamma_range(
    SetDeviceGammaRamp, hdc, base_ramp: np.ndarray
) -> tuple[float, float]:
    # Find the lowest and highest 0.01-step multipliers in [0.50, 1.50] that the
    # driver accepts. The result varies by GPU driver and active color profile.
    # Driver validation is a threshold on how far the ramp deviates from linear,
    # so acceptance is contiguous around 1.0 and each edge can be binary
    # searched (~12 probes) instead of sweeping all 101 steps, which strobed
    # the screen visibly at startup.
    def accepts(raw: int) -> bool:
        return bool(SetDeviceGammaRamp(hdc, scale_gamma_ramp(raw / 100, base_ramp).ctypes))

    try:
        if not accepts(100):
            raise RuntimeError("Driver rejected the unmodified gamma ramp.")

        lo, hi = 50, 100
        while lo < hi:
            mid = (lo + hi) // 2
            if accepts(mid):
                hi = mid
            else:
                lo = mid + 1
        min_raw = lo

        lo, hi = 100, 150
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if accepts(mid):
                lo = mid
            else:
                hi = mid - 1
        max_raw = lo
    finally:
        # Leave the screen at the base ramp, not whatever was probed last.
        SetDeviceGammaRamp(hdc, base_ramp.ctypes)

    return min_raw / 100, max_raw / 100


def apply_gamma_ramp(SetDeviceGammaRamp, hdc, ramp: np.ndarray) -> None:
    if not SetDeviceGammaRamp(hdc, ramp.ctypes):
        raise ValueError("Display driver rejected the gamma ramp.")


def reset_gamma_to_default(SetDeviceGammaRamp, base_ramp: np.ndarray, devicename: str) -> None:
    # Acquire a fresh DC rather than reusing the one from the main loop, since
    # that one may be in an inconsistent state after an abrupt interrupt.
    fresh_hdc = get_display_dc(devicename)
    try:
        SetDeviceGammaRamp(fresh_hdc, base_ramp.ctypes)
    finally:
        windll.gdi32.DeleteDC(fresh_hdc)


def build_tone_curve(
    shadow_norm: float,
    strength: float,
    max_multiplier: float,
    backlight_ratio: float = 1.0,
    compensation_strength: float = 0.0,
) -> np.ndarray:
    """A normalized tone curve T: [0,1] -> [0,1], sampled at 256 input levels.

    This is the single perceptual model. It lifts shadows and, optionally,
    compensates for the applied backlight, and is expressed purely as a mapping
    of input luma to output luma:

    - White is anchored (T(1) = 1) and black stays black (T(0) = 0), so the
      curve can never clip highlights or crush blacks by construction -- unlike
      a flat multiplier on the ramp, which saturates at the top.
    - Shadow lift is driven by how crushed the shadows are (the p5 percentile):
      a low ``shadow_norm`` means there is dark detail to recover, so the curve
      lifts more there.
    - The effective gain T(x)/x is bounded by the driver's accepted multiplier
      window (``max_multiplier``) so the composed ramp stays valid and near-black
      is never blown up.
    """
    internal_strength = max(0.0, min(strength, 1.0))
    x = np.arange(256, dtype=np.float64) / 255.0

    # Shadow lift as a gamma curve x**(1/g), g >= 1 -> lifts shadows/mids while
    # anchoring 0 and 1. Lift grows when the shadows are crushed (low p5) and
    # with strength, but is capped so it stays a gentle recovery, not a wash-out.
    shadow_ref = 0.25
    shadow_crush = min(max((shadow_ref - shadow_norm) / shadow_ref, 0.0), 1.0)
    g = 1.0 + internal_strength * 0.6 * shadow_crush
    tone = x ** (1.0 / g)

    # Backlight compensation: a gentle, bounded overall gain that lifts mids when
    # the backlight has dimmed (ratio < 1) and lowers them when it has brightened
    # so perceived mid-tones stay closer to constant while black level rides the
    # backlight. The gain band only widens with compensation_strength, so it can
    # never override the tone curve or blow out shadows.
    if compensation_strength > 0.0:
        gain = (1.0 / max(backlight_ratio, 0.05)) ** compensation_strength
        gain_span = 0.3 * compensation_strength
        gain = min(max(gain, 1.0 - gain_span), 1.0 + gain_span)
        tone = tone * gain

    # Bound the effective gain T(x)/x to the driver-accepted window so the
    # composed ramp is accepted and near-black cannot explode.
    min_multiplier = 1.0 / max_multiplier if max_multiplier > 0 else 1.0
    with np.errstate(divide="ignore", invalid="ignore"):
        mult = np.where(x > 0.0, tone / np.maximum(x, 1e-6), 1.0)
    mult = np.clip(mult, min_multiplier, max_multiplier)
    tone = x * mult

    # White-anchored rolloff: fade the deviation from identity to zero toward
    # white so T(1) = 1 exactly and highlights are never touched.
    rolloff = 1.0 - x**3
    tone = x + (tone - x) * rolloff

    # Monotonic and in-range, so the resampled ramp stays valid.
    tone = np.clip(tone, 0.0, 1.0)
    return np.maximum.accumulate(tone)


def compose_ramp_with_tone(base_ramp: np.ndarray, tone: np.ndarray) -> np.ndarray:
    """LUT composition: resample the base ramp through the tone curve.

    ``new_ramp[c][i] = base_ramp[c][ round(tone[i] * 255) ]``. Because the tone
    curve maps into [0, 1] and is monotonic, the output only ever gathers
    existing (valid, monotonic) base-ramp entries -- so it cannot clip highlights
    the way multiplying the ramp values does, and needs no post-clamp.
    """
    idx = np.clip(np.round(tone * 255.0), 0, 255).astype(np.intp)
    return np.ascontiguousarray(base_ramp[:, idx])


def build_tone_curve_ramp(
    base_ramp: np.ndarray,
    shadow_norm: float,
    strength: float,
    max_multiplier: float,
    backlight_ratio: float = 1.0,
    compensation_strength: float = 0.0,
) -> np.ndarray:
    tone = build_tone_curve(
        shadow_norm, strength, max_multiplier, backlight_ratio, compensation_strength
    )
    return compose_ramp_with_tone(base_ramp, tone)


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


def scale_value(
    value: float, src_min: float, src_max: float, dst_min: float, dst_max: float
) -> float:
    return (dst_max - dst_min) * (value - src_min) / (src_max - src_min) + dst_min


def scale_list(values: list, dst_min: float, dst_max: float) -> list:
    src_min, src_max = min(values), max(values)
    return [scale_value(v, src_min, src_max, dst_min, dst_max) for v in values]


def ema(current: float, previous: float, alpha: float) -> float:
    return alpha * current + (1.0 - alpha) * previous


def ema_alpha(dt: float, tau: float) -> float:
    # EMA weight for a step of dt seconds under time constant tau, derived from
    # the continuous-time filter. Makes smoothing speed independent of frame
    # rate: the same wall-clock time always produces the same adaptation,
    # whether that time spanned 10 frames or 100.
    if tau <= 0.0:
        return 1.0
    return 1.0 - math.exp(-dt / tau)


def adaptation_tau(delta: float, drift_tau: float, cut_tau: float, cut_threshold: float) -> float:
    """Pick the smoothing time constant from the size of the pending change.

    A large jump is a scene cut -- adapt fast (short tau) so the backlight is
    not left lagging a whole new scene. A small change is drift within a scene
    -- adapt slowly (long tau) for stability. Between the two the tau blends
    linearly so there is no abrupt switch in behaviour.
    """
    if cut_threshold <= 0:
        return drift_tau
    blend = min(abs(delta) / cut_threshold, 1.0)
    return drift_tau + (cut_tau - drift_tau) * blend


def slew_toward(
    current: float, target: float, dt: float, up_rate: float, down_rate: float
) -> float:
    """Move ``current`` toward ``target`` capped at an asymmetric rate.

    Brightening (up) and darkening (down) get separate caps so the backlight can
    mimic eye adaptation, where adapting to darkness is slower than to light:
    a smaller ``down_rate`` eases the screen down gently after a bright scene
    while a larger ``up_rate`` opens it up promptly when a bright scene arrives.
    """
    if dt <= 0:
        return target
    rate = up_rate if target >= current else down_rate
    if rate <= 0:
        return target
    max_step = rate * dt
    return current + max(-max_step, min(target - current, max_step))


def evaluate_deadband(
    luma_delta: float,
    threshold: float,
    now: float,
    deadband_since: float | None,
    settle_seconds: float,
) -> tuple[bool, float | None]:
    """Decide whether a brightness change should be applied given the deadband.

    Changes above the threshold apply immediately. Changes within the deadband
    are held back to avoid flicker, but if the offset persists for
    settle_seconds it is applied anyway -- a plain deadband would otherwise
    leave the brightness stuck up to `threshold` away from the target forever.
    Returns (apply, new_deadband_since).
    """
    if luma_delta == 0:
        return False, None
    if luma_delta > threshold:
        return True, None
    if deadband_since is None:
        return False, now
    if now - deadband_since >= settle_seconds:
        return True, None
    return False, deadband_since


if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser(description="Better Dynamic Contrast Ratio")
    arg_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the brightness/contrast/gamma decisions without touching the "
        "hardware (no DDC/CI writes, no gamma-ramp changes).",
    )
    cli_args = arg_parser.parse_args()
    dry_run: bool = cli_args.dry_run
    if dry_run:
        print("[dry-run] No hardware will be changed; decisions are printed only.\n")

    SetDeviceGammaRamp = None
    hdc: HDC | None = None
    default_gamma_ramp: np.ndarray | None = None
    min_gamma_multiplier: float = 0.5
    max_gamma_multiplier: float = 1.5

    # Separate EMA accumulators. The tone curve tracks the shadow statistic; the
    # backlight tracks the highlight-derived target; contrast tracks the APL.
    # They react at different speeds and have independent time constants.
    smoothed_shadow_norm: float = -1.0
    smoothed_luma_luminance: float = -1.0
    smoothed_contrast: float = -1.0

    last_frame_time: float | None = None
    last_gamma_write_time: float = 0.0
    last_status_time: float = 0.0
    deadband_since: float | None = None
    prev_ramp_key: tuple[int, int] | None = None
    commanded_luminance: float = -1.0

    with dxcam.create(output_idx=config.MONITOR_INDEX, output_color="GRAY") as camera:
        # The camera knows which DXGI output it captures; derive the DDC/CI
        # handle and the gamma DC from that same output so brightness and
        # gamma always target the monitor being captured, not just the primary.
        monitor_hmonitor = camera._output.hmonitor
        monitor_devicename = camera._output.devicename
        print(f"Target monitor: {monitor_devicename} (output index {config.MONITOR_INDEX})")
        print()

        if config.GAMMA_RAMP_ADJUSTMENTS:
            SetDeviceGammaRamp = windll.gdi32.SetDeviceGammaRamp
            GetDeviceGammaRamp = windll.gdi32.GetDeviceGammaRamp

            hdc = get_display_dc(monitor_devicename)

            default_gamma_ramp = get_default_gamma_ramp(GetDeviceGammaRamp, hdc)

            if dry_run:
                # Probing writes ramps to the display, so skip it in dry-run and
                # use the conservative default window instead.
                print("[dry-run] Skipping gamma-range probe; assuming [0.50, 1.50].")
            else:
                min_gamma_multiplier, max_gamma_multiplier = probe_supported_gamma_range(
                    SetDeviceGammaRamp, hdc, default_gamma_ramp
                )
            print("Adaptive tone curve: ready.")
            print(f"Tone curve strength:   {config.TONE_CURVE_STRENGTH}")
            print(
                f"Driver gamma range:    [{min_gamma_multiplier:.2f}, {max_gamma_multiplier:.2f}]"
            )
            print()

        handle = get_physical_monitor_handle(monitor_hmonitor)
        default_monitor_luminance, max_hardware_luminance = vcp_get_luminance_range(handle)
        if max_hardware_luminance <= 0:
            # Some monitors report 0 for the maximum; fall back to the usual range.
            max_hardware_luminance = 100
        print(
            f"Default monitor luminance: {default_monitor_luminance} "
            f"(hardware range 0-{max_hardware_luminance})"
        )
        print()

        # The desired 0-100 window from config, rescaled into the hardware's
        # reported luminance range (not all monitors use 0-100).
        luminance_map: dict[int, int] = {
            raw: int(mapped * max_hardware_luminance / 100)
            for raw, mapped in zip(
                range(101),
                scale_list(
                    list(range(101)),
                    config.MIN_DESIRED_MONITOR_LUMINANCE,
                    config.MAX_DESIRED_MONITOR_LUMINANCE,
                ),
                strict=False,
            )
        }
        print(f"Min monitor luminance: {config.MIN_DESIRED_MONITOR_LUMINANCE}")
        print(f"Max monitor luminance: {config.MAX_DESIRED_MONITOR_LUMINANCE}")
        print(f"Monitor luminance values: {', '.join(str(v) for v in luminance_map.values())}")
        print()

        # Probe contrast (VCP 0x12) support and, if present, build its map into
        # the desired window scaled by the reported hardware maximum.
        contrast_enabled = False
        default_contrast = 0
        contrast_map: dict[int, int] = {}
        if config.CONTRAST_ADJUSTMENTS:
            contrast_probe = vcp_get_feature(handle, VCP_CONTRAST)
            if contrast_probe is None:
                print("Dynamic contrast: monitor does not report contrast support; disabled.")
                print()
            else:
                default_contrast, max_hardware_contrast = contrast_probe
                if max_hardware_contrast <= 0:
                    max_hardware_contrast = 100
                contrast_enabled = True
                contrast_map = {
                    raw: int(mapped * max_hardware_contrast / 100)
                    for raw, mapped in zip(
                        range(101),
                        scale_list(
                            list(range(101)),
                            config.MIN_DESIRED_CONTRAST,
                            config.MAX_DESIRED_CONTRAST,
                        ),
                        strict=False,
                    )
                }
                print(
                    f"Dynamic contrast: ready (default {default_contrast}, "
                    f"hardware range 0-{max_hardware_contrast}, "
                    f"window {config.MIN_DESIRED_CONTRAST}-{config.MAX_DESIRED_CONTRAST})"
                )
                print()

        initial_writes = {VCP_LUMINANCE: default_monitor_luminance}
        if contrast_enabled:
            initial_writes[VCP_CONTRAST] = default_contrast

        vcp_writer = VcpWriter(
            handle,
            config.MONITOR_LUMINANCE_MIN_WRITE_INTERVAL_MS / 1000.0,
            initial_writes,
            config.MONITOR_LUMINANCE_MAX_WRITES_PER_MINUTE,
            dry_run,
        )

        # Cache of tone-curve ramps keyed by quantized (shadow, backlight) bins.
        # build_tone_curve_ramp is 256-entry array math; repeated/oscillating
        # scenes reuse a precomputed ramp instead of rebuilding it each write.
        tone_ramp_cache: dict[tuple[int, int], np.ndarray] = {}

        # Restore the display on every exit path, not just Ctrl+C: normal exit and
        # unhandled exceptions go through finally/atexit, while closing the console
        # window (or logoff/shutdown) only gives us a console ctrl event, so a
        # handler is registered for that too.
        #
        # The console ctrl handler fires on a separate OS thread, so restore can
        # be entered concurrently with the main thread's finally/atexit. The
        # lock makes the check-set-and-run atomic: a plain "if is_set(): return"
        # is a race where both callers pass the check and both run the body,
        # which would double-free the physical monitor handle and issue
        # overlapping DDC/CI writes.
        restore_lock = threading.Lock()
        restore_done = threading.Event()

        def restore_defaults() -> None:
            with restore_lock:
                if restore_done.is_set():
                    return
                restore_done.set()

                # Stop the writer first so no in-flight write races the restore.
                vcp_writer.stop()
                print(f"Session DDC/CI writes: {vcp_writer.write_count()}")

                if dry_run:
                    # Nothing was changed on the hardware, so nothing to restore.
                    destroy_physical_monitor_handle(handle)
                    return

                if (
                    config.GAMMA_RAMP_ADJUSTMENTS
                    and SetDeviceGammaRamp is not None
                    and default_gamma_ramp is not None
                ):
                    reset_gamma_to_default(
                        SetDeviceGammaRamp, default_gamma_ramp, monitor_devicename
                    )

                # Re-derive the physical handle in case the original went stale
                # (e.g. the monitor power-cycled while the program was running).
                restore_handle = get_physical_monitor_handle(monitor_hmonitor)
                try:
                    if not vcp_set_luminance(restore_handle, default_monitor_luminance):
                        print(
                            f"[!] Could not restore monitor luminance to {default_monitor_luminance}."
                        )
                    if contrast_enabled and not vcp_set_feature(
                        restore_handle, VCP_CONTRAST, default_contrast
                    ):
                        print(f"[!] Could not restore monitor contrast to {default_contrast}.")
                finally:
                    destroy_physical_monitor_handle(restore_handle)
                    destroy_physical_monitor_handle(handle)

        atexit.register(restore_defaults)

        # CTRL_CLOSE_EVENT = 2, CTRL_LOGOFF_EVENT = 5, CTRL_SHUTDOWN_EVENT = 6.
        # Ctrl+C / Ctrl+Break (0 and 1) are left to Python's own handler so the
        # KeyboardInterrupt path still works; returning False passes the event on.
        @WINFUNCTYPE(BOOL, DWORD)
        def console_ctrl_handler(event: int) -> bool:
            if event in (2, 5, 6):
                restore_defaults()
            return False

        windll.kernel32.SetConsoleCtrlHandler(console_ctrl_handler, True)

        try:
            screen_h, screen_w = camera.height, camera.width

            crop_t = config.CAPTURE_CROP_TOP
            crop_b = config.CAPTURE_CROP_BOTTOM
            crop_l = config.CAPTURE_CROP_LEFT
            crop_r = config.CAPTURE_CROP_RIGHT

            left = crop_l
            top = crop_t
            right = screen_w - crop_r if crop_r else screen_w
            bottom = screen_h - crop_b if crop_b else screen_h

            if left >= right or top >= bottom:
                raise RuntimeError("Crop values exceed frame dimensions.")

            region = (left, top, right, bottom)

            # Subsampling stride for scene statistics; global statistics do not
            # need every pixel.
            sample_stride = max(1, int(config.CAPTURE_DOWNSAMPLE_STRIDE))

            # Cap the per-frame time delta. get_latest_frame() blocks until the
            # content actually changes, so on static content dt would balloon to
            # the whole idle gap and let the time-based EMA/slew take one giant
            # catch-up step -- the "sudden jump" symptom. video_mode below keeps
            # frames flowing at the target fps so the control loop ticks
            # continuously and ramps smoothly; this clamp is the safety bound
            # for the first frame and any scheduling hitch.
            max_frame_dt = 4.0 / config.CAPTURE_TARGET_FPS if config.CAPTURE_TARGET_FPS > 0 else 0.1

            # video_mode=True re-delivers the last frame each timer tick when the
            # screen is static, so smoothing and slew-rate limiting keep
            # advancing toward the target instead of freezing until the next
            # real change (which would make the next update a large jump).
            camera.start(region=region, target_fps=config.CAPTURE_TARGET_FPS, video_mode=True)

            while True:
                if (frame := camera.get_latest_frame()) is None:
                    continue

                now = time.perf_counter()
                dt = 0.0 if last_frame_time is None else min(now - last_frame_time, max_frame_dt)
                last_frame_time = now

                if frame.size == 0:
                    continue

                analysis = frame[::sample_stride, ::sample_stride]

                # Auto black-bar detection: exclude letterbox/pillarbox bars from
                # the statistics so they do not drag the backlight/contrast down.
                if config.AUTO_BLACK_BAR_DETECTION:
                    bt, bb, bl, br = detect_black_bars(analysis, config.BLACK_BAR_LUMA_THRESHOLD)
                    if bt or bb or bl or br:
                        analysis = analysis[
                            bt : analysis.shape[0] - bb, bl : analysis.shape[1] - br
                        ]

                raw_mean_luma, highlight_norm, shadow_norm = scene_statistics(
                    analysis,
                    config.LUMINANCE_SCENE_PERCENTILE,
                    config.SHADOW_SCENE_PERCENTILE,
                )

                # Perceptual backlight target (0-100): the highlight percentile
                # raised to the mapping exponent. The exponent approximates the
                # display transfer, so the backlight tracks the linear light
                # the scene's highlights actually need instead of spending most
                # of its travel on bright-ish content.
                raw_backlight_target = 100.0 * (highlight_norm**config.LUMINANCE_MAPPING_EXPONENT)

                # The tone curve's shadow lift is driven by the shadow percentile
                # and smoothed on the gamma time constant so it does not jitter.
                # The backlight follows the highlight-derived target; when
                # smoothing is on it uses scene-cut-adaptive EMA (fast on cuts,
                # slow on drift), and FORCE_INSTANT bypasses smoothing entirely.
                if config.TEMPORAL_SMOOTHING:
                    if smoothed_shadow_norm < 0:
                        smoothed_shadow_norm = shadow_norm
                    else:
                        smoothed_shadow_norm = ema(
                            shadow_norm,
                            smoothed_shadow_norm,
                            ema_alpha(dt, config.TEMPORAL_SMOOTHING_GAMMA_TAU),
                        )

                    if config.MONITOR_LUMINANCE_FORCE_INSTANT_ADJUSTMENTS:
                        luma_for_luminance = raw_backlight_target
                    else:
                        if smoothed_luma_luminance < 0:
                            smoothed_luma_luminance = raw_backlight_target
                        else:
                            # Scene-cut detection: a large gap between the raw
                            # target and the smoothed value adapts on the fast
                            # tau; small drift adapts on the slow tau.
                            tau = adaptation_tau(
                                raw_backlight_target - smoothed_luma_luminance,
                                config.TEMPORAL_SMOOTHING_LUMINANCE_TAU,
                                config.TEMPORAL_SMOOTHING_LUMINANCE_CUT_TAU,
                                config.LUMINANCE_SCENE_CUT_THRESHOLD,
                            )
                            smoothed_luma_luminance = ema(
                                raw_backlight_target,
                                smoothed_luma_luminance,
                                ema_alpha(dt, tau),
                            )
                        luma_for_luminance = smoothed_luma_luminance
                else:
                    smoothed_shadow_norm = shadow_norm
                    luma_for_luminance = raw_backlight_target

                # Backlight first, gamma second: the tone curve compensates
                # for the backlight actually applied, so it must see the
                # freshest hardware state.
                if config.MONITOR_LUMINANCE_ADJUSTMENTS:
                    # Slew-rate limit the commanded backlight so large target
                    # jumps (e.g. a scene cut) ramp gradually instead of
                    # lurching. The EMA above smooths jitter, but its response
                    # is steepest right after a jump; this bounds the peak rate,
                    # which is what makes big changes look fast and drastic.
                    # Darkening is capped more tightly than brightening to mimic
                    # eye adaptation (dark-adapt slower than light-adapt).
                    if (
                        commanded_luminance < 0
                        or config.MONITOR_LUMINANCE_MAX_CHANGE_PER_SECOND <= 0
                        or config.MONITOR_LUMINANCE_FORCE_INSTANT_ADJUSTMENTS
                        or dt <= 0
                    ):
                        commanded_luminance = luma_for_luminance
                    else:
                        commanded_luminance = slew_toward(
                            commanded_luminance,
                            luma_for_luminance,
                            dt,
                            config.MONITOR_LUMINANCE_MAX_CHANGE_PER_SECOND,
                            config.MONITOR_LUMINANCE_MAX_CHANGE_PER_SECOND_DARKEN,
                        )

                    target_monitor_luminance = int(clamp(round(commanded_luminance), 0, 100))
                    target_luminance_map_value = luminance_map[target_monitor_luminance]

                    # Compare against the writer's view of the hardware state
                    # (pending target, else last driver-confirmed write) so a
                    # failed write is retried instead of being assumed applied.
                    current_monitor_luminance = vcp_writer.effective(VCP_LUMINANCE)

                    # Delta based on mapped values, i.e. what is actually sent
                    # to the hardware.
                    luma_delta = abs(target_luminance_map_value - current_monitor_luminance)

                    apply_change, deadband_since = evaluate_deadband(
                        luma_delta,
                        config.LUMA_DIFFERENCE_THRESHOLD,
                        now,
                        deadband_since,
                        config.LUMA_DEADBAND_SETTLE_SECONDS,
                    )
                    if apply_change:
                        vcp_writer.submit(VCP_LUMINANCE, target_luminance_map_value)

                # Dynamic contrast (VCP 0x12): raise contrast in dark scenes,
                # lower it in bright ones. Smoothed slowly and submitted through
                # the same writer, so contrast interleaves with brightness on the
                # shared bus and the shared write budget.
                if contrast_enabled:
                    raw_contrast_target = contrast_target(
                        raw_mean_luma, config.CONTRAST_MAPPING_EXPONENT
                    )
                    if config.TEMPORAL_SMOOTHING:
                        if smoothed_contrast < 0:
                            smoothed_contrast = raw_contrast_target
                        else:
                            smoothed_contrast = ema(
                                raw_contrast_target,
                                smoothed_contrast,
                                ema_alpha(dt, config.TEMPORAL_SMOOTHING_CONTRAST_TAU),
                            )
                        contrast_command = smoothed_contrast
                    else:
                        contrast_command = raw_contrast_target

                    target_contrast_value = contrast_map[
                        int(clamp(round(contrast_command), 0, 100))
                    ]
                    if (
                        abs(target_contrast_value - vcp_writer.effective(VCP_CONTRAST))
                        > config.CONTRAST_DIFFERENCE_THRESHOLD
                    ):
                        vcp_writer.submit(VCP_CONTRAST, target_contrast_value)

                if config.GAMMA_RAMP_ADJUSTMENTS:
                    assert SetDeviceGammaRamp is not None
                    assert hdc is not None
                    assert default_gamma_ramp is not None

                    # Ratio of the backlight actually applied to the hardware
                    # vs. the user's default backlight. Feeding the applied
                    # value (not the target) into the tone curve keeps the two
                    # systems in lockstep even while DDC/CI writes lag behind.
                    if config.MONITOR_LUMINANCE_ADJUSTMENTS and config.GAMMA_BACKLIGHT_COMPENSATION:
                        backlight_ratio = vcp_writer.effective(VCP_LUMINANCE) / max(
                            default_monitor_luminance, 1
                        )
                        compensation_strength = config.GAMMA_BACKLIGHT_COMPENSATION_STRENGTH
                    else:
                        backlight_ratio = 1.0
                        compensation_strength = 0.0

                    # Rate-cap ramp writes on top of the change threshold:
                    # recomputing and applying the ramp at capture rate during
                    # fast luma swings wastes CPU and can stutter games.
                    gamma_write_due = (now - last_gamma_write_time) * 1000.0 >= (
                        config.GAMMA_RAMP_MIN_WRITE_INTERVAL_MS
                    )

                    # Quantize the tone-curve inputs into coarse bins. Repeated or
                    # oscillating scenes then reuse a cached ramp instead of
                    # rebuilding the 256-entry curve, and a changed bin is what
                    # signals a meaningful update is due.
                    shadow_bin = int(clamp(round(smoothed_shadow_norm * 63), 0, 63))
                    backlight_bin = int(clamp(round(backlight_ratio * 32), 0, 96))
                    ramp_key = (shadow_bin, backlight_bin)

                    if prev_ramp_key is None or (gamma_write_due and ramp_key != prev_ramp_key):
                        tone_ramp = tone_ramp_cache.get(ramp_key)
                        if tone_ramp is None:
                            tone_ramp = build_tone_curve_ramp(
                                default_gamma_ramp,
                                smoothed_shadow_norm,
                                config.TONE_CURVE_STRENGTH,
                                max_gamma_multiplier,
                                backlight_ratio,
                                compensation_strength,
                            )
                            if len(tone_ramp_cache) < 4096:
                                tone_ramp_cache[ramp_key] = tone_ramp
                        if not dry_run:
                            apply_gamma_ramp(SetDeviceGammaRamp, hdc, tone_ramp)
                        prev_ramp_key = ramp_key
                        last_gamma_write_time = now

                # Throttled status line. Printing on every adjustment floods the
                # console and, when the program is capturing that console, feeds
                # the scrolling text back into the scene statistic and drives a
                # self-sustaining oscillation. One line per interval avoids both.
                if (
                    config.STATUS_LOG_INTERVAL_SECONDS <= 0
                    or now - last_status_time >= config.STATUS_LOG_INTERVAL_SECONDS
                ):
                    parts = [f"scene_mean={raw_mean_luma / 100.0:.3f}"]
                    if config.MONITOR_LUMINANCE_ADJUSTMENTS:
                        parts.append(
                            f"backlight={vcp_writer.effective(VCP_LUMINANCE)}"
                            f"->{int(clamp(round(luma_for_luminance), 0, 100))}"
                        )
                    if contrast_enabled:
                        parts.append(f"contrast={vcp_writer.effective(VCP_CONTRAST)}")
                    if config.GAMMA_RAMP_ADJUSTMENTS:
                        parts.append(f"gamma_strength={config.TONE_CURVE_STRENGTH}")
                    parts.append(f"writes={vcp_writer.write_count()}")
                    print("  ".join(parts))
                    last_status_time = now

        except KeyboardInterrupt:
            print("\n[!] Interrupted.\n")
        finally:
            print("\n[!] Restoring default display settings.\n")
            restore_defaults()
            print("[!] Done.\n")
            time.sleep(1)
