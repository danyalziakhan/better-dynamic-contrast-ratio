# MIT License
#
# Copyright (c) 2025 Danyal Zia
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

import math
import threading
import time

from ctypes import Structure, byref, windll
from ctypes.wintypes import BYTE, DWORD, HANDLE, HDC, WCHAR

import cv2
import numpy as np

from numba import njit
from zbl import Capture

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


class PhysicalMonitor(Structure):
    _fields_ = [("handle", HANDLE), ("description", WCHAR * 128)]


def get_primary_monitor_handle() -> HANDLE:
    hmonitor = windll.user32.MonitorFromPoint(0, 0, 1)
    physical_monitors = (PhysicalMonitor * 1)()
    windll.dxva2.GetPhysicalMonitorsFromHMONITOR(hmonitor, 1, physical_monitors)
    return physical_monitors[0].handle


def vcp_set_luminance(handle, value: int) -> None:
    windll.dxva2.SetVCPFeature(HANDLE(handle), BYTE(0x10), DWORD(value))


def vcp_get_luminance(handle) -> int:
    current = DWORD()
    maximum = DWORD()
    windll.dxva2.GetVCPFeatureAndVCPFeatureReply(
        HANDLE(handle), BYTE(0x10), None, byref(current), byref(maximum)
    )
    return current.value


def get_default_gamma_ramp(GetDeviceGammaRamp, hdc) -> np.ndarray:
    ramp = np.empty((3, 256), dtype=np.uint16)
    if not GetDeviceGammaRamp(hdc, ramp.ctypes):
        raise RuntimeError(
            "Failed to read the current gamma ramp from the display driver."
        )
    return ramp


@njit(cache=True)
def scale_gamma_ramp(multiplier: float, ramp: np.ndarray) -> np.ndarray:
    return np.round(np.multiply(multiplier, ramp)).astype(np.uint16)


def probe_supported_gamma_range(
    SetDeviceGammaRamp, hdc, base_ramp: np.ndarray
) -> tuple[float, float]:
    # Try every 0.01 step from 0.50 to 1.50 and record which ones the driver accepts.
    # The result varies by GPU driver and active color profile.
    accepted = []
    for raw in range(50, 151):
        multiplier = raw / 100
        if SetDeviceGammaRamp(hdc, scale_gamma_ramp(multiplier, base_ramp).ctypes):
            accepted.append(multiplier)
    if not accepted:
        raise RuntimeError(
            "Driver accepted no gamma multipliers in the 0.50-1.50 range."
        )
    return min(accepted), max(accepted)


def apply_gamma_ramp(SetDeviceGammaRamp, hdc, ramp: np.ndarray) -> None:
    if not SetDeviceGammaRamp(hdc, ramp.ctypes):
        raise ValueError("Display driver rejected the gamma ramp.")


def reset_gamma_to_default(SetDeviceGammaRamp, base_ramp: np.ndarray) -> None:
    # Acquire a fresh DC rather than reusing the one from the main loop, since
    # that one may be in an inconsistent state after an abrupt interrupt.
    # ReleaseDC needs the window handle (NULL for a screen DC) alongside the DC.
    fresh_hdc = HDC(windll.user32.GetDC(None))
    try:
        SetDeviceGammaRamp(fresh_hdc, base_ramp.ctypes)
    finally:
        windll.user32.ReleaseDC(None, fresh_hdc)


def vig_scale(v: float, scene_mean: float) -> float:
    # Sigmoid-shaped offset centred at scene_mean. Generates the per-level
    # scaling factor used in the virtual illumination step.
    r = 1.0 - scene_mean * 0.999999
    return r * (1.0 / (1.0 + math.exp(-(v - scene_mean))) - 0.5)


def build_tone_curve_ramp(
    base_ramp: np.ndarray,
    scene_mean_norm: float,
    strength: float,
    min_multiplier: float,
    max_multiplier: float,
) -> np.ndarray:
    # Clamp and scale strength from [0.1, 1.0] down to [0.01, 0.1] internally.
    internal_strength = max(0.1, min(strength, 1.0)) * 0.1

    scene_mean = max(scene_mean_norm, 1e-5)

    # Build the intensity array for all 256 ramp entries.
    # Index 0 would hit log(0), so clamp it to a small positive value.
    L = np.arange(256, dtype=np.float64) / 255.0
    L[0] = 1e-5

    # The guided filter base layer (local illumination estimate) is approximated
    # by the global scene mean. This makes the effect a global tone curve rather
    # than per-pixel local adaptation, but still reacts meaningfully to scene content.
    R_val = np.log(L) - math.log(scene_mean)

    # Selective Reflectance Scaling: amplify the reflectance component for
    # pixels brighter than the scene average.
    brighter = L > scene_mean
    factor = np.where(brighter, np.sqrt(np.maximum(L / scene_mean, 0.0)), 1.0)
    R_new = R_val * factor

    # Virtual Illumination: generate five illumination levels spanning the luma
    # range, anchored around the scene mean.
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

    # Remap the [0, 3] tone curve output into the driver's accepted multiplier
    # window so the ramp is always valid, then blend with the identity by strength.
    remapped = min_multiplier + (raw_ratio / 3.0) * (max_multiplier - min_multiplier)
    effective_ratio = 1.0 + (remapped - 1.0) * internal_strength

    new_ramp = np.clip(
        np.round(base_ramp.astype(np.float64) * effective_ratio[np.newaxis, :]),
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


if __name__ == "__main__":
    np.seterr(divide="ignore", over="ignore")

    SetDeviceGammaRamp = None
    hdc: HDC | None = None
    default_gamma_ramp: np.ndarray | None = None
    prev_scene_mean_norm: float = -1.0
    min_gamma_multiplier: float = 0.5
    max_gamma_multiplier: float = 1.5

    if config.GAMMA_RAMP_ADJUSTMENTS:
        GetDC = windll.user32.GetDC
        SetDeviceGammaRamp = windll.gdi32.SetDeviceGammaRamp
        GetDeviceGammaRamp = windll.gdi32.GetDeviceGammaRamp

        hdc = HDC(GetDC(None))
        if not hdc:
            raise RuntimeError("Failed to obtain a device context (HDC).")

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

    luminance_map: dict[int, int] = {
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
    print(f"Min monitor luminance: {config.MIN_DESIRED_MONITOR_LUMINANCE}")
    print(f"Max monitor luminance: {config.MAX_DESIRED_MONITOR_LUMINANCE}")
    print(
        f"Monitor luminance values: {', '.join(str(v) for v in luminance_map.values())}"
    )
    print()

    handle = get_primary_monitor_handle()
    default_monitor_luminance = vcp_get_luminance(handle)
    print(f"Default monitor luminance: {default_monitor_luminance}")
    print()

    current_monitor_luminance = default_monitor_luminance

    # Separate EMA accumulators for gamma and luminance. They react at different
    # speeds and have independent alphas in config.
    smoothed_luma_gamma: float = -1.0
    smoothed_luma_luminance: float = -1.0

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
                    raw_luma = luminance_from_grayscale(frame)
                except ValueError as e:
                    raise RuntimeError(
                        "Cannot compute average luminance of the frame."
                    ) from e

                # Gamma always uses EMA when smoothing is on, giving the eye
                # adaptation effect.
                #
                # For luminance: when FORCE_INSTANT is True, brightness changes
                # are applied immediately with no smoothing. When False, temporal
                # smoothing is applied so each brightness update is a smoothed
                # value rather than a raw frame reading. If both FORCE_INSTANT
                # and TEMPORAL_SMOOTHING are False, brightness is still applied
                # instantly, just without any smoothing.
                if config.TEMPORAL_SMOOTHING:
                    if smoothed_luma_gamma < 0:
                        smoothed_luma_gamma = raw_luma
                    else:
                        smoothed_luma_gamma = ema(
                            raw_luma,
                            smoothed_luma_gamma,
                            config.TEMPORAL_SMOOTHING_GAMMA_ALPHA,
                        )
                    luma_for_gamma = smoothed_luma_gamma

                    if config.MONITOR_LUMINANCE_FORCE_INSTANT_ADJUSTMENTS:
                        luma_for_luminance = raw_luma
                    else:
                        if smoothed_luma_luminance < 0:
                            smoothed_luma_luminance = raw_luma
                        else:
                            smoothed_luma_luminance = ema(
                                raw_luma,
                                smoothed_luma_luminance,
                                config.TEMPORAL_SMOOTHING_LUMINANCE_ALPHA,
                            )
                        luma_for_luminance = smoothed_luma_luminance
                else:
                    luma_for_gamma = raw_luma
                    luma_for_luminance = raw_luma

                if config.GAMMA_RAMP_ADJUSTMENTS:
                    assert SetDeviceGammaRamp is not None
                    assert hdc is not None
                    assert default_gamma_ramp is not None

                    scene_mean_norm = luma_for_gamma / 100.0
                    luma_delta = abs(scene_mean_norm - prev_scene_mean_norm)

                    if prev_scene_mean_norm < 0 or luma_delta > (
                        config.GAMMA_DIFFERENCE_THRESHOLD / 100.0
                    ):
                        tone_ramp = build_tone_curve_ramp(
                            default_gamma_ramp,
                            scene_mean_norm,
                            config.TONE_CURVE_STRENGTH,
                            min_gamma_multiplier,
                            max_gamma_multiplier,
                        )
                        apply_gamma_ramp(SetDeviceGammaRamp, hdc, tone_ramp)
                        prev_scene_mean_norm = scene_mean_norm
                        print(
                            f"Tone curve: scene_mean={scene_mean_norm:.3f}  strength={config.TONE_CURVE_STRENGTH}"
                        )

                if config.MONITOR_LUMINANCE_ADJUSTMENTS:
                    target_monitor_luminance = int(
                        clamp(abs(round(luma_for_luminance)), 0, 100)
                    )
                    target_luminance_map_value = luminance_map[target_monitor_luminance]

                    if target_monitor_luminance == current_monitor_luminance:
                        continue

                    luma_delta = abs(
                        target_monitor_luminance - current_monitor_luminance
                    )

                    if luma_delta == 1:
                        continue

                    if luma_delta > config.LUMA_DIFFERENCE_THRESHOLD:
                        # All brightness changes are now instant. Temporal smoothing
                        # (when enabled and FORCE_INSTANT is False) already provides
                        # gradual-feeling transitions by smoothing the target value
                        # itself, so a separate fade mechanism is not needed.
                        threading.Thread(
                            target=vcp_set_luminance,
                            args=(handle, target_luminance_map_value),
                            daemon=True,
                        ).start()
                        print(
                            f"Luminance: {target_luminance_map_value} (from {current_monitor_luminance})"
                        )

                        current_monitor_luminance = target_monitor_luminance

    except KeyboardInterrupt:
        print("\n[!] Interrupted. Restoring default display settings.\n")

        if (
            config.GAMMA_RAMP_ADJUSTMENTS
            and SetDeviceGammaRamp is not None
            and default_gamma_ramp is not None
        ):
            reset_gamma_to_default(SetDeviceGammaRamp, default_gamma_ramp)

        handle = get_primary_monitor_handle()
        vcp_set_luminance(handle, default_monitor_luminance)

        print("[!] Done.\n")
        time.sleep(1)
