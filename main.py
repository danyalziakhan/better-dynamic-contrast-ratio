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

import math
import os
import threading
import time

from ctypes import Structure, byref, windll
from ctypes.wintypes import BYTE, DWORD, HANDLE, HDC, WCHAR
from functools import cache
from queue import Queue

import cv2
import numpy as np

from numba import njit
from zbl import Capture

import config


# Queues used by fade threads to communicate the last applied value when
# interrupted mid-transition, so the next transition starts from the right point.
gamma_progress_queue: Queue = Queue()
luma_progress_queue: Queue = Queue()

# Holds the stop-event of the currently active fade thread (at most one entry).
# The main loop signals it before spawning a replacement fade.
active_flags: list[threading.Event] = []


# ---------------------------------------------------------------------------
# Luminance calculation
# Reference: https://stackoverflow.com/questions/596216/formula-to-determine-perceived-brightness-of-rgb-color
# ---------------------------------------------------------------------------
# Several formulations are provided as alternatives. Only `luminance_from_grayscale`
# is used in the main loop because it is the fastest (requires a pre-converted grayscale frame).


def luminance_from_rgb_weighted(arr: np.ndarray) -> float:
    """Single-pass weighted sum. Avoids per-pixel normalization by folding
    the 1/255 factor into the divisors."""
    total_pixels = np.prod(arr.shape[:-1])
    luminance_sum = (arr / [2550.299, 2550.587, 1770.833]).sum()
    return (luminance_sum / total_pixels) * 255


@cache
def _normalize_to_0_100(luminance: float) -> float:
    return (luminance / 255) * 100


def luminance_bt709(arr: np.ndarray) -> float:
    """ITU BT.709 (HDTV) luma coefficients."""
    mean_rgb = arr.reshape(-1, 3).mean(axis=0)
    return _normalize_to_0_100((mean_rgb * [0.2126, 0.7152, 0.0722]).sum())


def luminance_bt601(arr: np.ndarray) -> float:
    """ITU BT.601 (SDTV) luma coefficients."""
    mean_rgb = arr.reshape(-1, 3).mean(axis=0)
    return _normalize_to_0_100((mean_rgb * [0.299, 0.587, 0.114]).sum())


@njit(cache=True)
def _sum_to_0_100(total: float, width: int, height: int) -> float:
    return ((total / (width * height)) / 255) * 100


@njit(cache=True)
def luminance_from_grayscale(arr: np.ndarray) -> float:
    """Fastest method. Requires the frame to be in grayscale (single channel)."""
    return _sum_to_0_100(arr.sum(), arr.shape[0], arr.shape[1])


# ---------------------------------------------------------------------------
# Monitor handle / VCP (DDC/CI) interface
# ---------------------------------------------------------------------------


class PhysicalMonitor(Structure):
    _fields_ = [("handle", HANDLE), ("description", WCHAR * 128)]


def get_primary_monitor_handle() -> HANDLE:
    monitor_hmonitor = windll.user32.MonitorFromPoint(0, 0, 1)
    physical_monitors = (PhysicalMonitor * 1)()
    windll.dxva2.GetPhysicalMonitorsFromHMONITOR(monitor_hmonitor, 1, physical_monitors)
    return physical_monitors[0].handle


def vcp_set_luminance(handle, value: int):
    windll.dxva2.SetVCPFeature(HANDLE(handle), BYTE(0x10), DWORD(value))


def vcp_get_luminance(handle) -> int:
    current = DWORD()
    maximum = DWORD()
    windll.dxva2.GetVCPFeatureAndVCPFeatureReply(
        HANDLE(handle), BYTE(0x10), None, byref(current), byref(maximum)
    )
    return current.value


# ---------------------------------------------------------------------------
# Gamma ramp helpers
# ---------------------------------------------------------------------------


def get_default_gamma_ramp(GetDeviceGammaRamp, hdc) -> np.ndarray:
    ramp = np.empty((3, 256), dtype=np.uint16)
    if not GetDeviceGammaRamp(hdc, ramp.ctypes):
        raise RuntimeError(
            "Failed to read the current gamma ramp from the display driver."
        )
    return ramp


def save_gamma_ramp(ramp: np.ndarray, filename: str):
    np.save(filename, ramp)


def load_gamma_ramp(filename: str) -> np.ndarray:
    return np.load(filename)


@njit(cache=True)
def _scale_gamma_ramp(multiplier: float, ramp: np.ndarray) -> np.ndarray:
    """Scale every entry in the ramp by `multiplier` and clamp to uint16."""
    return np.round(np.multiply(multiplier, ramp)).astype(np.uint16)


def probe_supported_gamma_values(
    SetDeviceGammaRamp, hdc, base_ramp: np.ndarray
) -> list[float]:
    """
    Test which multipliers in the 0.50-1.50 range the driver accepts.
    The supported range varies by GPU driver and active color profile.
    """
    accepted = []
    for raw in range(50, 151):
        multiplier = raw / 100
        if SetDeviceGammaRamp(hdc, _scale_gamma_ramp(multiplier, base_ramp).ctypes):
            accepted.append(multiplier)
    return accepted


def apply_gamma(
    SetDeviceGammaRamp, hdc, base_ramp: np.ndarray, multiplier: float
) -> float:
    if not SetDeviceGammaRamp(hdc, _scale_gamma_ramp(multiplier, base_ramp).ctypes):
        raise ValueError(f"Display driver rejected gamma multiplier: {multiplier}")
    return multiplier


def reset_gamma_to_default(SetDeviceGammaRamp, base_ramp: np.ndarray):
    """
    Restore the saved default gamma ramp using a fresh device context.
    A new DC is acquired here rather than reusing the long-running one, because
    the main DC can be in an inconsistent state when called during shutdown.
    """
    fresh_hdc = HDC(windll.user32.GetDC(None))
    try:
        SetDeviceGammaRamp(fresh_hdc, _scale_gamma_ramp(1.0, base_ramp).ctypes)
    finally:
        # ReleaseDC requires both the window handle (NULL for a screen DC) and the DC itself.
        windll.user32.ReleaseDC(None, fresh_hdc)


# ---------------------------------------------------------------------------
# Mapping file loader (shared by gamma and luminance config parsing)
# ---------------------------------------------------------------------------


def load_key_value_mapping(filepath: str) -> dict:
    """
    Parse a plain-text file with lines in `key = value` format.
    Keys and values are cast to int if whole numbers, otherwise float.
    """
    mapping = {}
    with open(filepath) as f:
        for line in f.read().split("\n"):
            if not line.strip():
                continue
            parts = [x.strip() for x in line.split("=")]
            raw_key, raw_val = parts[0], parts[1]
            key = int(raw_key) if "." not in raw_key else float(raw_key)
            val = int(raw_val) if "." not in raw_val else float(raw_val)
            mapping[key] = val
    return mapping


# ---------------------------------------------------------------------------
# Math utilities
# ---------------------------------------------------------------------------


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


def scale_value(
    value: float, src_min: float, src_max: float, dst_min: float, dst_max: float
) -> float:
    return (dst_max - dst_min) * (value - src_min) / (src_max - src_min) + dst_min


def scale_list(values: list, dst_min: float, dst_max: float) -> list:
    src_min, src_max = min(values), max(values)
    return [scale_value(v, src_min, src_max, dst_min, dst_max) for v in values]


# ---------------------------------------------------------------------------
# Gamma / luminance logic
# ---------------------------------------------------------------------------


@cache
def compute_target_gamma(
    log_mid_point: float,
    mean_luma: float,
    min_gamma: float,
    max_gamma: float,
) -> float:
    """
    Map screen average luminance to a gamma multiplier via a log curve anchored
    at `log_mid_point`. Edge cases: fully black -> max gamma (brighten),
    fully white -> min gamma (darken).
    """
    if mean_luma == 0:
        return max_gamma
    elif mean_luma == 255:
        return min_gamma
    else:
        return log_mid_point / math.log(mean_luma)


@cache
def gamma_adjusted_luma(
    mean_luma: float, adjusted_gamma: float, mid_point: float
) -> float:
    """
    Derive the target monitor luminance from gamma-adjusted luma.
    Dark content gets a small exposure boost to preserve shadow detail;
    bright content gets a slight reduction to avoid blown highlights.
    """
    adjusted = mean_luma * adjusted_gamma

    if adjusted_gamma < (mid_point * 10):
        adjusted *= 1.20  # Boost: dark content, gamma was lowered
    else:
        adjusted *= 0.80  # Reduce: bright content, gamma was raised

    return abs(round(adjusted))


# ---------------------------------------------------------------------------
# Fade thread management
# ---------------------------------------------------------------------------


def _cancel_active_fade() -> threading.Event:
    """
    Signal the currently running fade to stop, then return a fresh Event
    to hand to the replacement thread.
    """
    if active_flags:
        active_flags.pop().set()
    new_flag = threading.Event()
    active_flags.append(new_flag)
    return new_flag


def _stop_all_fades():
    """Signal every tracked fade thread to stop. Used during shutdown."""
    for flag in active_flags:
        flag.set()
    active_flags.clear()


def fade_gamma(
    stop_flag: threading.Event,
    SetDeviceGammaRamp,
    hdc,
    base_ramp: np.ndarray,
    gamma_map: dict[float, float],
    target_gamma: float,
    current_gamma: float,
    step_interval: float,
    step_size: float,
):
    target_int = round(target_gamma * 100)
    current_int = round(current_gamma * 100)
    step_int = round(abs(step_size * 100))

    if current_int > target_int:
        step_int = -step_int

    next_step_time = time.time()

    for value_int in range(current_int, target_int, step_int):
        value = value_int / 100

        # Skip steps the main loop has already superseded with a newer target.
        try:
            queued = gamma_progress_queue.get_nowait()
        except Exception:
            pass
        else:
            already_past = (step_int > 0 and queued >= value) or (
                step_int < 0 and queued <= value
            )
            if already_past:
                gamma_progress_queue.put(queued)
                continue

        if stop_flag.is_set():
            gamma_progress_queue.put(value)
            break

        apply_gamma(SetDeviceGammaRamp, hdc, base_ramp, gamma_map[value])

        next_step_time += step_interval
        sleep_time = next_step_time - time.time()
        if sleep_time > 0:
            time.sleep(sleep_time)


def fade_luminance(
    stop_flag: threading.Event,
    handle,
    luminance_map: dict[int, int],
    target_luminance: int,
    step_interval: float,
    step_size: int,
):
    target_int = round(target_luminance)
    current_int = vcp_get_luminance(handle)

    if current_int > target_int:
        step_size = -step_size

    next_step_time = time.time()

    for luma in range(current_int, target_int, step_size):
        try:
            queued = luma_progress_queue.get_nowait()
        except Exception:
            pass
        else:
            already_past = (step_size > 0 and queued >= luma) or (
                step_size < 0 and queued <= luma
            )
            if already_past:
                luma_progress_queue.put(queued)
                continue

        if stop_flag.is_set():
            luma_progress_queue.put(luma)
            break

        vcp_set_luminance(handle, luminance_map[luma])

        next_step_time += step_interval
        sleep_time = next_step_time - time.time()
        if sleep_time > 0:
            time.sleep(sleep_time)


def fade_gamma_and_luminance(
    stop_flag: threading.Event,
    SetDeviceGammaRamp,
    hdc,
    base_ramp: np.ndarray,
    handle,
    gamma_map: dict[float, float],
    luminance_map: dict[int, int],
    target_gamma: float,
    current_gamma: float,
    target_luminance: int,
    gamma_interval: float,
    gamma_step: float,
    luminance_interval: float,
    luminance_step: int,
):
    fade_gamma(
        stop_flag,
        SetDeviceGammaRamp,
        hdc,
        base_ramp,
        gamma_map,
        target_gamma,
        current_gamma,
        gamma_interval,
        gamma_step,
    )
    fade_luminance(
        stop_flag,
        handle,
        luminance_map,
        target_luminance,
        luminance_interval,
        luminance_step,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Suppress divide-by-zero and overflow warnings that can occur during
    # per-frame luminance calculations on edge-case frames (e.g. pure black).
    np.seterr(divide="ignore", over="ignore")

    # -- Gamma ramp setup -----------------------------------------------------
    if config.GAMMA_RAMP_ADJUSTMENTS:
        GetDC = windll.user32.GetDC
        SetDeviceGammaRamp = windll.gdi32.SetDeviceGammaRamp
        GetDeviceGammaRamp = windll.gdi32.GetDeviceGammaRamp

        hdc = HDC(GetDC(None))
        if not hdc:
            raise RuntimeError("Failed to obtain a device context (HDC).")

        # Save the default ramp on first run so it can be restored on exit.
        # Subsequent runs reuse the file to avoid drift from repeated read/write cycles.
        if os.path.exists("defaultgamma.npy"):
            default_gamma_ramp = load_gamma_ramp("defaultgamma.npy")
        else:
            default_gamma_ramp = get_default_gamma_ramp(GetDeviceGammaRamp, hdc)
            save_gamma_ramp(default_gamma_ramp, "defaultgamma")

        supported_gamma_values = probe_supported_gamma_values(
            SetDeviceGammaRamp, hdc, default_gamma_ramp
        )
        min_gamma_supported = min(supported_gamma_values)
        max_gamma_supported = max(supported_gamma_values)

        if config.GAMMA_CUSTOM_MAPPING:
            if not os.path.exists(config.GAMMA_CUSTOM_MAPPING):
                raise FileNotFoundError("Custom gamma mapping file not found.")
            gamma_map: dict[float, float] = load_key_value_mapping(
                config.GAMMA_CUSTOM_MAPPING
            )
        else:
            gamma_map = {
                raw: round(mapped, 2)
                for raw, mapped in zip(
                    supported_gamma_values,
                    scale_list(
                        supported_gamma_values,
                        max(min_gamma_supported, config.MIN_DESIRED_GAMMA),
                        min(max_gamma_supported, config.MAX_DESIRED_GAMMA),
                    ),
                )
            }

        min_gamma_allowed = list(gamma_map)[0]
        max_gamma_allowed = list(gamma_map)[-1]

        mid_point = (
            (min_gamma_allowed + max_gamma_allowed) / 2
        ) / 10 + config.MID_POINT_BIAS
        log_mid_point = math.log(mid_point * 255)

        print(f"Min Desired Gamma: {config.MIN_DESIRED_GAMMA}")
        print(f"Max Desired Gamma: {config.MAX_DESIRED_GAMMA}")
        print(f"Gamma Values: {','.join(str(v) for v in gamma_map.values())}")
        print(f"Min Allowed Gamma: {min_gamma_allowed}")
        print(f"Max Allowed Gamma: {max_gamma_allowed}")
        print(f"Mid Point: {mid_point}")
        print(f"Log Mid Point: {log_mid_point}")
        print()

        # Start from the darkest allowed gamma so the first transition is visible
        current_gamma = min_gamma_allowed
        apply_gamma(SetDeviceGammaRamp, hdc, default_gamma_ramp, current_gamma)

    # -- Monitor luminance (VCP/DDC-CI) setup ---------------------------------
    if config.MONITOR_LUMINANCE_CUSTOM_MAPPING:
        if not os.path.exists(config.MONITOR_LUMINANCE_CUSTOM_MAPPING):
            raise FileNotFoundError("Custom luminance mapping file not found.")
        luminance_map: dict[int, int] = load_key_value_mapping(
            config.MONITOR_LUMINANCE_CUSTOM_MAPPING
        )
    else:
        luminance_map = {
            raw: int(mapped)
            for raw, mapped in zip(
                range(101),
                scale_list(
                    list(range(101)),
                    config.MIN_DESIRED_MONITOR_LUMINANCE,
                    config.MAX_DESIRED_MONITOR_LUMINANCE,
                ),
            )
        }
        print(
            f"Min Monitor's Desired Luminance: {config.MIN_DESIRED_MONITOR_LUMINANCE}"
        )
        print(
            f"Max Monitor's Desired Luminance: {config.MAX_DESIRED_MONITOR_LUMINANCE}"
        )

    print(
        f"Monitor's Luminance Values: {','.join(str(v) for v in luminance_map.values())}"
    )
    print()

    handle = get_primary_monitor_handle()
    default_monitor_luminance = vcp_get_luminance(handle)
    print(f"Default Monitor's Luminance: {default_monitor_luminance}")
    print()

    current_monitor_luminance = default_monitor_luminance

    # -- Main capture/adjust loop ---------------------------------------------
    try:
        with Capture(
            display_id=config.MONITOR_INDEX,
            is_cursor_capture_enabled=False,
            is_border_required=False,
        ) as cap:
            frames = cap.frames()

            while True:
                frame = next(frames)
                frame = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

                try:
                    mean_luma = luminance_from_grayscale(frame)
                except ValueError as e:
                    raise RuntimeError(
                        "Cannot calculate average luminance of the frame."
                    ) from e

                # -- Gamma ramp adjustments -----------------------------------
                if config.GAMMA_RAMP_ADJUSTMENTS:
                    target_gamma = compute_target_gamma(
                        log_mid_point, mean_luma, min_gamma_allowed, max_gamma_allowed
                    )

                    if config.MONITOR_LUMINANCE_ADJUSTMENTS:
                        # Feed the gamma-corrected luma back into the luminance calculation
                        # so both controls react to perceived brightness, not raw luma.
                        luma_for_luminance = gamma_adjusted_luma(
                            mean_luma, target_gamma, mid_point
                        )
                        target_monitor_luminance = clamp(
                            abs(luma_for_luminance), 0, 100
                        )
                        target_luminance_map_value = luminance_map[
                            target_monitor_luminance
                        ]

                    target_gamma = clamp(
                        round(target_gamma, 2), min_gamma_allowed, max_gamma_allowed
                    )
                    target_gamma_map_value = gamma_map[target_gamma]
                    gamma_delta = round(abs(target_gamma - current_gamma), 2)

                    # Only trigger a fade if the change exceeds the noise threshold
                    # and is not just floating-point rounding jitter (0.01 step).
                    if (
                        target_gamma != current_gamma
                        and gamma_delta > config.GAMMA_DIFFERENCE_THRESHOLD
                        and gamma_delta != 0.01
                    ):
                        gamma_step = 0.01
                        gamma_interval = 0.01
                        luminance_interval = 0.14
                        luminance_step = 1

                        if config.MONITOR_LUMINANCE_ADJUSTMENTS:
                            if config.MONITOR_LUMINANCE_FORCE_INSTANT_ADJUSTMENTS:
                                fade_gamma(
                                    _cancel_active_fade(),
                                    SetDeviceGammaRamp,
                                    hdc,
                                    default_gamma_ramp,
                                    gamma_map,
                                    target_gamma,
                                    current_gamma,
                                    gamma_interval,
                                    gamma_step,
                                )
                                if (
                                    target_monitor_luminance
                                    != current_monitor_luminance
                                ):
                                    vcp_set_luminance(
                                        handle, target_luminance_map_value
                                    )
                            else:
                                thread = threading.Thread(
                                    target=fade_gamma_and_luminance,
                                    args=(
                                        _cancel_active_fade(),
                                        SetDeviceGammaRamp,
                                        hdc,
                                        default_gamma_ramp,
                                        handle,
                                        gamma_map,
                                        luminance_map,
                                        target_gamma,
                                        current_gamma,
                                        target_monitor_luminance,
                                        gamma_interval,
                                        gamma_step,
                                        luminance_interval,
                                        luminance_step,
                                    ),
                                )
                                thread.start()
                        else:
                            thread = threading.Thread(
                                target=fade_gamma,
                                args=(
                                    _cancel_active_fade(),
                                    SetDeviceGammaRamp,
                                    hdc,
                                    default_gamma_ramp,
                                    gamma_map,
                                    target_gamma,
                                    current_gamma,
                                    gamma_interval,
                                    gamma_step,
                                ),
                            )
                            thread.start()

                        info = f"Gamma: {target_gamma_map_value}"
                        if config.MONITOR_LUMINANCE_ADJUSTMENTS:
                            info += f" -- Luminance: {target_luminance_map_value}"
                        print(info)

                        current_gamma = target_gamma
                        if config.MONITOR_LUMINANCE_ADJUSTMENTS:
                            current_monitor_luminance = target_monitor_luminance

                # -- Standalone luminance adjustments (gamma disabled) --------
                if (
                    config.MONITOR_LUMINANCE_ADJUSTMENTS
                    and not config.GAMMA_RAMP_ADJUSTMENTS
                ):
                    target_monitor_luminance = clamp(abs(round(mean_luma)), 0, 100)
                    target_luminance_map_value = luminance_map[target_monitor_luminance]

                    if target_monitor_luminance == current_monitor_luminance:
                        continue

                    luma_delta = abs(
                        target_monitor_luminance - current_monitor_luminance
                    )

                    # Skip single-step changes to avoid flickering on sensor noise
                    if luma_delta == 1:
                        continue

                    if luma_delta > config.LUMA_DIFFERENCE_THRESHOLD:
                        if config.MONITOR_LUMINANCE_FORCE_INSTANT_ADJUSTMENTS:
                            vcp_set_luminance(handle, target_luminance_map_value)
                        else:
                            thread = threading.Thread(
                                target=fade_luminance,
                                args=(
                                    _cancel_active_fade(),
                                    handle,
                                    luminance_map,
                                    target_monitor_luminance,
                                    0.2,
                                    1,
                                ),
                            )
                            thread.start()
                            print(
                                f"Luminance: {target_luminance_map_value} (from {current_monitor_luminance})"
                            )

                        current_monitor_luminance = target_monitor_luminance

    except KeyboardInterrupt:
        print("\n[!] Interrupted. Restoring default display settings...\n")

        # Stop all active fade threads BEFORE touching the gamma ramp.
        # Without this a running fade can overwrite the reset immediately after it is applied.
        _stop_all_fades()
        time.sleep(0.15)  # Give threads time to observe the stop flag and exit

        if config.GAMMA_RAMP_ADJUSTMENTS:
            # Use a fresh DC for the reset. The main `hdc` acquired at startup
            # can be in an inconsistent state after being interrupted mid-fade.
            # Also uses the already-loaded `default_gamma_ramp` rather than reloading from disk.
            reset_gamma_to_default(SetDeviceGammaRamp, default_gamma_ramp)

        # Re-acquire the DDC/CI handle; it can time out during long sessions.
        handle = get_primary_monitor_handle()
        vcp_set_luminance(handle, default_monitor_luminance)

        print("[!] Done. Closing...\n")
        time.sleep(1)
