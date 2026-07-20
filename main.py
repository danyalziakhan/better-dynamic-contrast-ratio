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

import atexit
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


def scene_statistics(frame: np.ndarray, highlight_percentile: float) -> tuple[float, float]:
    """Mean luma (0-100) and highlight level (0-1) of a grayscale frame.

    Both come from one 256-bin histogram pass. The highlight percentile is a
    much better backlight driver than the mean: a night scene with a small
    bright moon and a flat grey scene can share the same mean, but only the
    percentile tells the backlight that the night scene still has highlights
    worth preserving.
    """
    hist = np.bincount(frame.ravel(), minlength=256)
    mean_luma = float(np.dot(hist, np.arange(256))) / frame.size / 255.0 * 100.0
    highlight = percentile_from_histogram(hist, highlight_percentile / 100.0)
    return mean_luma, highlight / 255.0


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


def vcp_set_luminance(handle, value: int) -> bool:
    return bool(windll.dxva2.SetVCPFeature(HANDLE(handle), BYTE(0x10), DWORD(value)))


def vcp_get_luminance_range(handle) -> tuple[int, int]:
    """Read the monitor's current and maximum luminance. The maximum is not
    always 100; the VCP spec only guarantees the value is in [0, maximum]."""
    current = DWORD()
    maximum = DWORD()
    if not windll.dxva2.GetVCPFeatureAndVCPFeatureReply(
        HANDLE(handle), BYTE(0x10), None, byref(current), byref(maximum)
    ):
        raise RuntimeError(
            "Failed to read the monitor's luminance over DDC/CI. "
            "Make sure DDC/CI is enabled in the monitor's OSD."
        )
    return current.value, maximum.value


def vcp_get_luminance(handle) -> int:
    return vcp_get_luminance_range(handle)[0]


class VcpLuminanceWriter:
    """Serializes DDC/CI brightness writes on a single dedicated thread.

    Targets go through a one-slot mailbox: submitting while a write is in
    flight replaces any not-yet-written value, so the monitor always receives
    the newest target and writes can never pile up or land out of order --
    which could happen with a thread spawned per change, since concurrent
    SetVCPFeature calls on the same I2C bus have no ordering guarantee.
    The pause after each write both gives the DDC/CI receiver time to settle
    and caps the total write rate (see the EEPROM warning in the README).

    The writer is also the source of truth for the monitor's luminance state:
    it only records a value once the driver confirms the write, so a failed
    write leaves the recorded state at the old value and the caller's next
    submit of the still-differing target acts as a natural, rate-limited retry.
    """

    def __init__(self, handle, min_write_interval_s: float, initial_value: int) -> None:
        self._handle = handle
        self._min_write_interval_s = min_write_interval_s
        self._cond = threading.Condition()
        self._target: int | None = None
        self._last_written = initial_value
        self._write_failing = False
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, value: int) -> bool:
        """Queue a new target. Returns False if it is already the effective
        state (pending or confirmed written) and nothing was queued."""
        with self._cond:
            if self._stopped:
                return False
            effective = self._target if self._target is not None else self._last_written
            if value == effective:
                return False
            self._target = value
            self._cond.notify()
            return True

    def effective_luminance(self) -> int:
        """The pending target if one is queued, else the last value the
        driver confirmed as written."""
        with self._cond:
            return self._target if self._target is not None else self._last_written

    def stop(self) -> None:
        # Pending unwritten targets are dropped; the caller decides what the
        # final state should be (e.g. restoring the default luminance).
        with self._cond:
            self._stopped = True
            self._cond.notify()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._target is None and not self._stopped:
                    self._cond.wait()
                # The is-None check is unreachable while running (the wait loop
                # guarantees a target); it doubles as type narrowing.
                if self._stopped or self._target is None:
                    return
                value = self._target
                self._target = None
            ok = vcp_set_luminance(self._handle, value)
            with self._cond:
                if ok:
                    self._last_written = value
                    self._write_failing = False
                elif not self._write_failing:
                    self._write_failing = True
                    print("[!] DDC/CI brightness write failed; retrying with the newest target.")
            # Sleep outside the lock so newer targets coalesce in the mailbox
            # while the bus settles.
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


def vig_scale(v: float, scene_mean: float) -> float:
    # Sigmoid-shaped offset centred at scene_mean. Generates the per-level
    # scaling factor used in the virtual illumination step.
    r = 1.0 - scene_mean * 0.999999
    return r * (1.0 / (1.0 + math.exp(-(v - scene_mean))) - 0.5)


def remap_ratio_to_multiplier(
    raw_ratio: np.ndarray, min_multiplier: float, max_multiplier: float
) -> np.ndarray:
    # Piecewise-linear remap of the [0, 3] tone curve output into the driver's
    # accepted multiplier window, anchored so a ratio of 1.0 (no change) maps
    # to a multiplier of exactly 1.0. A plain linear remap of the whole [0, 3]
    # range would move the identity point and put a constant darkening bias on
    # neutral scenes.
    return np.where(
        raw_ratio <= 1.0,
        min_multiplier + np.clip(raw_ratio, 0.0, 1.0) * (1.0 - min_multiplier),
        1.0 + (np.clip(raw_ratio, 1.0, 3.0) - 1.0) * ((max_multiplier - 1.0) / 2.0),
    )


def build_tone_curve_ramp(
    base_ramp: np.ndarray,
    scene_mean_norm: float,
    strength: float,
    min_multiplier: float,
    max_multiplier: float,
    backlight_ratio: float = 1.0,
    compensation_strength: float = 0.0,
) -> np.ndarray:
    # Clamp and scale strength from [0.1, 1.0] down to [0.01, 0.1] internally.
    internal_strength = max(0.1, min(strength, 1.0)) * 0.1

    scene_mean = max(scene_mean_norm, 1e-5)

    # Build the intensity array for all 256 ramp entries.
    # Index 0 would hit log(0), so clamp it to a small positive value.
    L = np.arange(256, dtype=np.float64) / 255.0
    L[0] = 1e-5

    # Keep numeric-error suppression local to this math instead of a global
    # np.seterr, so real numeric problems elsewhere still surface.
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        # The guided filter base layer (local illumination estimate) is
        # approximated by the global scene mean. This makes the effect a global
        # tone curve rather than per-pixel local adaptation, but still reacts
        # meaningfully to scene content.
        R_val = np.log(L) - math.log(scene_mean)

        # Selective Reflectance Scaling: amplify the reflectance component for
        # pixels brighter than the scene average.
        brighter = scene_mean < L
        factor = np.where(brighter, np.sqrt(np.maximum(L / scene_mean, 0.0)), 1.0)
        R_new = R_val * factor

        # Virtual Illumination: generate five illumination levels spanning the
        # luma range, anchored around the scene mean.
        inv_L = 1.0 - L
        v1 = 0.2
        v3 = scene_mean
        v2 = 0.5 * (v1 + v3)
        v5 = 0.8
        v4 = 0.5 * (v3 + v5)

        exp_R_new = np.exp(R_new)
        A = np.zeros(256, dtype=np.float64)
        B = np.zeros(256, dtype=np.float64)

        for i, vk in enumerate((v1, v2, v3, v4, v5)):
            fvk = vig_scale(vk, scene_mean)
            I_k = (1.0 + fvk) * (L + fvk * inv_L)
            Lk = exp_R_new * I_k
            # Lower levels weight by intensity; upper levels weight by complement.
            wk = np.where(i < 3, I_k, 0.5 * (1.0 - I_k))
            wk = np.clip(wk, 0.001, 1.0)
            A += Lk * wk
            B += wk

        L_final = A / (B + 1e-6)
        raw_ratio = np.clip(L_final / L, 0.0, 3.0)

    # Remap into the driver's accepted multiplier window (identity-anchored,
    # so the ramp is always valid), then blend with the identity by strength.
    remapped = remap_ratio_to_multiplier(raw_ratio, min_multiplier, max_multiplier)
    effective_ratio = 1.0 + (remapped - 1.0) * internal_strength

    # Backlight compensation: nudge the curve using the backlight actually
    # applied to the hardware so the two systems cooperate -- when the backlight
    # dims below the reference (ratio < 1) the shadows and mids are lifted a
    # little back toward the perceived brightness the content had at the
    # reference backlight, and pulled down when it brightens above it.
    #
    # The raw (1/ratio) gain explodes at low backlight (e.g. 7.7x at ratio
    # 0.13), which -- multiplied onto the near-identity tone curve and clipped
    # to the driver max -- flattens into a full-strength brightness boost that
    # blows out dark scenes. So the gain is hard-capped to a narrow band around
    # 1.0 that only widens with the strength: the compensation stays a gentle
    # correction, never an override of the tone curve.
    if compensation_strength > 0.0:
        gain = (1.0 / max(backlight_ratio, 0.05)) ** compensation_strength
        gain_span = 0.3 * compensation_strength
        gain = min(max(gain, 1.0 - gain_span), 1.0 + gain_span)
        effective_ratio = effective_ratio * gain

    effective_ratio = np.clip(effective_ratio, min_multiplier, max_multiplier)

    # White-anchored rolloff: fade the deviation from identity to zero toward
    # the top of the ramp. A flat multiplier > 1 saturates at 65535 and
    # hard-clips every highlight above the saturation point; shaping the boost
    # into the shadows and mids keeps the top of the curve (and the white
    # point) intact.
    rolloff = 1.0 - L**3
    final_ratio = 1.0 + (effective_ratio - 1.0) * rolloff

    new_ramp = np.clip(
        np.round(base_ramp.astype(np.float64) * final_ratio[np.newaxis, :]),
        0,
        65535,
    ).astype(np.uint16)

    return new_ramp


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
    SetDeviceGammaRamp = None
    hdc: HDC | None = None
    default_gamma_ramp: np.ndarray | None = None
    prev_scene_mean_norm: float = -1.0
    min_gamma_multiplier: float = 0.5
    max_gamma_multiplier: float = 1.5

    # Separate EMA accumulators for gamma and luminance. They react at different
    # speeds and have independent time constants in config.
    smoothed_luma_gamma: float = -1.0
    smoothed_luma_luminance: float = -1.0

    last_frame_time: float | None = None
    last_gamma_write_time: float = 0.0
    last_status_time: float = 0.0
    deadband_since: float | None = None
    prev_backlight_ratio: float = 1.0
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

        luminance_writer = VcpLuminanceWriter(
            handle,
            config.MONITOR_LUMINANCE_MIN_WRITE_INTERVAL_MS / 1000.0,
            default_monitor_luminance,
        )

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
                luminance_writer.stop()

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

                raw_mean_luma, highlight_norm = scene_statistics(
                    frame[::sample_stride, ::sample_stride], config.LUMINANCE_SCENE_PERCENTILE
                )

                # Perceptual backlight target (0-100): the highlight percentile
                # raised to the mapping exponent. The exponent approximates the
                # display transfer, so the backlight tracks the linear light
                # the scene's highlights actually need instead of spending most
                # of its travel on bright-ish content.
                raw_backlight_target = 100.0 * (highlight_norm**config.LUMINANCE_MAPPING_EXPONENT)

                # Gamma always uses EMA when smoothing is on, giving the eye
                # adaptation effect. The EMA weight is derived from the elapsed
                # wall-clock time between frames, so adaptation speed does not
                # depend on the capture frame rate.
                #
                # For luminance: when FORCE_INSTANT is True, brightness changes
                # are applied immediately with no smoothing. When False, temporal
                # smoothing is applied so each brightness update is a smoothed
                # value rather than a raw frame reading. If both FORCE_INSTANT
                # and TEMPORAL_SMOOTHING are False, brightness is still applied
                # instantly, just without any smoothing.
                if config.TEMPORAL_SMOOTHING:
                    if smoothed_luma_gamma < 0:
                        smoothed_luma_gamma = raw_mean_luma
                    else:
                        smoothed_luma_gamma = ema(
                            raw_mean_luma,
                            smoothed_luma_gamma,
                            ema_alpha(dt, config.TEMPORAL_SMOOTHING_GAMMA_TAU),
                        )
                    luma_for_gamma = smoothed_luma_gamma

                    if config.MONITOR_LUMINANCE_FORCE_INSTANT_ADJUSTMENTS:
                        luma_for_luminance = raw_backlight_target
                    else:
                        if smoothed_luma_luminance < 0:
                            smoothed_luma_luminance = raw_backlight_target
                        else:
                            smoothed_luma_luminance = ema(
                                raw_backlight_target,
                                smoothed_luma_luminance,
                                ema_alpha(dt, config.TEMPORAL_SMOOTHING_LUMINANCE_TAU),
                            )
                        luma_for_luminance = smoothed_luma_luminance
                else:
                    luma_for_gamma = raw_mean_luma
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
                    if (
                        commanded_luminance < 0
                        or config.MONITOR_LUMINANCE_MAX_CHANGE_PER_SECOND <= 0
                        or config.MONITOR_LUMINANCE_FORCE_INSTANT_ADJUSTMENTS
                        or dt <= 0
                    ):
                        commanded_luminance = luma_for_luminance
                    else:
                        max_step = config.MONITOR_LUMINANCE_MAX_CHANGE_PER_SECOND * dt
                        commanded_luminance += clamp(
                            luma_for_luminance - commanded_luminance, -max_step, max_step
                        )

                    target_monitor_luminance = int(clamp(round(commanded_luminance), 0, 100))
                    target_luminance_map_value = luminance_map[target_monitor_luminance]

                    # Compare against the writer's view of the hardware state
                    # (pending target, else last driver-confirmed write) so a
                    # failed write is retried instead of being assumed applied.
                    current_monitor_luminance = luminance_writer.effective_luminance()

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
                        luminance_writer.submit(target_luminance_map_value)

                if config.GAMMA_RAMP_ADJUSTMENTS:
                    assert SetDeviceGammaRamp is not None
                    assert hdc is not None
                    assert default_gamma_ramp is not None

                    scene_mean_norm = luma_for_gamma / 100.0
                    luma_delta = abs(scene_mean_norm - prev_scene_mean_norm)

                    # Ratio of the backlight actually applied to the hardware
                    # vs. the user's default backlight. Feeding the applied
                    # value (not the target) into the tone curve keeps the two
                    # systems in lockstep even while DDC/CI writes lag behind.
                    if config.MONITOR_LUMINANCE_ADJUSTMENTS and config.GAMMA_BACKLIGHT_COMPENSATION:
                        backlight_ratio = luminance_writer.effective_luminance() / max(
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

                    ramp_inputs_changed = (
                        luma_delta > (config.GAMMA_DIFFERENCE_THRESHOLD / 100.0)
                        or abs(backlight_ratio - prev_backlight_ratio) > 0.01
                    )

                    if prev_scene_mean_norm < 0 or (gamma_write_due and ramp_inputs_changed):
                        tone_ramp = build_tone_curve_ramp(
                            default_gamma_ramp,
                            scene_mean_norm,
                            config.TONE_CURVE_STRENGTH,
                            min_gamma_multiplier,
                            max_gamma_multiplier,
                            backlight_ratio,
                            compensation_strength,
                        )
                        apply_gamma_ramp(SetDeviceGammaRamp, hdc, tone_ramp)
                        prev_scene_mean_norm = scene_mean_norm
                        prev_backlight_ratio = backlight_ratio
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
                            f"backlight={luminance_writer.effective_luminance()}"
                            f"->{int(clamp(round(luma_for_luminance), 0, 100))}"
                        )
                    if config.GAMMA_RAMP_ADJUSTMENTS:
                        parts.append(f"gamma_strength={config.TONE_CURVE_STRENGTH}")
                    print("  ".join(parts))
                    last_status_time = now

        except KeyboardInterrupt:
            print("\n[!] Interrupted.\n")
        finally:
            print("\n[!] Restoring default display settings.\n")
            restore_defaults()
            print("[!] Done.\n")
            time.sleep(1)
