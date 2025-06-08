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


GAMMA_QUEUE = Queue()
LUMA_QUEUE = Queue()
LOCK = threading.Lock()
FLAGS: list[threading.Event] = []


#
# Luminance calculating algorithms: https://stackoverflow.com/questions/596216/formula-to-determine-perceived-brightness-of-rgb-color
#


def get_average_luminance1(arr: np.ndarray):
    total_num_sum = np.prod(arr.shape[:-1])
    luminance_total = (arr / [2550.299, 2550.587, 1770.833]).sum()
    return (luminance_total / total_num_sum) * 255


@cache
def lum_to_0_100(luminance: float):
    return (luminance / 255) * 100


# ITU BT.709
def get_average_luminance2(arr: np.ndarray):
    mean_rgb = arr.reshape(-1, 3).mean(axis=0)
    luminance = (mean_rgb * [0.2126, 0.7152, 0.0722]).sum()
    return lum_to_0_100(luminance)


# ITU BT.601
def get_average_luminance3(arr: np.ndarray):
    mean_rgb = arr.reshape(-1, 3).mean(axis=0)
    luminance = (mean_rgb * [0.299, 0.587, 0.114]).sum()
    return lum_to_0_100(luminance)


@njit(cache=True)
def sum_to_0_100(summed: float, x: int, y: int):
    luminance = summed / (x * y)
    return (luminance / 255) * 100


# Fastest method when the source is grayscale
# Will not work in RGB mode
@njit(cache=True)
def get_average_luminance4(arr: np.ndarray):
    return sum_to_0_100(arr.sum(), arr.shape[0], arr.shape[1])


class PhysicalMonitor(Structure):
    _fields_ = [("handle", HANDLE), ("description", WCHAR * 128)]


def get_primary_monitor_handle():
    monitor_HMONITOR = windll.user32.MonitorFromPoint(0, 0, 1)
    physical_monitors = (PhysicalMonitor * 1)()
    windll.dxva2.GetPhysicalMonitorsFromHMONITOR(monitor_HMONITOR, 1, physical_monitors)
    return physical_monitors[0].handle


def vcp_set_luminance(handle, value):
    windll.dxva2.SetVCPFeature(HANDLE(handle), BYTE(0x10), DWORD(value))


def vcp_get_luminance(handle):
    feature_current = DWORD()
    feature_max = DWORD()

    windll.dxva2.GetVCPFeatureAndVCPFeatureReply(
        HANDLE(handle),
        BYTE(0x10),
        None,
        byref(feature_current),
        byref(feature_max),
    )

    return feature_current.value


def fade_luminance(
    flag: threading.Event,
    handle,
    luminance_map: dict[int, int],
    finish: int,
    interval: float,
    increment: int,
):
    luminanceFinishInt = round(finish)
    luminanceStartInt = vcp_get_luminance(handle)

    if luminanceStartInt > luminanceFinishInt:
        increment = -increment

    next_change_start_time = time.time()

    for luma in range(luminanceStartInt, luminanceFinishInt, increment):
        try:
            global_value = LUMA_QUEUE.get_nowait()
        except Exception:
            pass
        else:
            if (
                increment > 0
                and global_value >= luma
                or increment < 0
                and global_value <= luma
            ):
                # print(f"Global Luma already greater than {luma}: {global_value}")
                LUMA_QUEUE.put(global_value)
                continue

        if flag.is_set():
            # print(f"Luma changes breaking at {luma}... ")
            LUMA_QUEUE.put(luma)
            break

        vcp_set_luminance(handle, luminance_map[luma])

        next_change_start_time += interval
        sleep_time = next_change_start_time - time.time()

        if sleep_time > 0:
            time.sleep(sleep_time)


def clamp(d, minValue, maxValue):
    t = max(d, minValue)
    return min(t, maxValue)


def get_default_gamma_ramp(GetDeviceGammaRamp, hdc):
    default_gamma_ramp = np.empty((3, 256), dtype=np.uint16)
    if not GetDeviceGammaRamp(hdc, default_gamma_ramp.ctypes):
        raise RuntimeError("Can't get default gamma ramp")

    return default_gamma_ramp


def save_gamma_ramp(ramp, filename):
    np.save(filename, ramp)


def load_gamma_ramp(filename):
    return np.load(filename)


@njit(cache=True)
def mul_gamma_ramp(value, gamma_ramp):
    return np.round(np.multiply(value, gamma_ramp)).astype(np.uint16)


def get_gamma_allowed_values(SetDeviceGammaRamp, hdc, gamma_ramp):
    supported_values = []

    # Check 0.5 - 1.5 range
    for value in range(50, 150 + 1):
        value = value / 100
        NewRamps = mul_gamma_ramp(value, gamma_ramp)

        if not SetDeviceGammaRamp(hdc, NewRamps.ctypes):  # 0-1 = False/True
            continue
        else:
            supported_values.append(value)

    return supported_values


def set_gamma(SetDeviceGammaRamp, hdc, gamma_ramp, value):
    NewRamps = mul_gamma_ramp(value, gamma_ramp)

    if not SetDeviceGammaRamp(hdc, NewRamps.ctypes):  # 0-1 = False/True
        ValueError(f"Unable to set Gamma to {value}")

    return value


def fade_gamma(
    flag: threading.Event,
    SetDeviceGammaRamp,
    hdc,
    gamma_ramp: np.ndarray,
    gamma_map: dict[float, float],
    finish: float,
    start: float,
    interval: float,
    increment: float,
):
    finishInt = round(finish * 100)
    startInt = round(start * 100)
    incrementInt = round(abs(increment * 100))

    if startInt > finishInt:
        incrementInt = -incrementInt

    next_change_start_time = time.time()

    for valueInt in range(startInt, finishInt, incrementInt):
        value = valueInt / 100

        try:
            global_value = GAMMA_QUEUE.get_nowait()
        except Exception:
            pass
        else:
            if (
                incrementInt > 0
                and global_value >= value
                or incrementInt < 0
                and global_value <= value
            ):
                # print(f"Global Gamma already greater than {value}: {global_value}")
                GAMMA_QUEUE.put(global_value)
                continue
        if flag.is_set():
            # print(f"Gamma changes breaking at {value}... ")
            GAMMA_QUEUE.put(value)
            break

        set_gamma(SetDeviceGammaRamp, hdc, gamma_ramp, gamma_map[value])

        next_change_start_time += interval
        sleep_time = next_change_start_time - time.time()

        if sleep_time > 0:
            time.sleep(sleep_time)


def scale_number(unscaled, to_min, to_max, from_min, from_max):
    return (to_max - to_min) * (unscaled - from_min) / (from_max - from_min) + to_min


def scale_list(l, to_min, to_max):
    return [scale_number(i, to_min, to_max, min(l), max(l)) for i in l]


def fade_gamma_luminance_combined(
    flag: threading.Event,
    SetDeviceGammaRamp,
    hdc,
    gamma_ramp,
    handle,
    gamma_map: dict[float, float],
    luminance_map: dict[int, int],
    gammaFinish: float,
    gammaStart: float,
    luminanceFinish: int,
    gammaInterval: float,
    gammaIncrement: float,
    luminanceInterval: float,
    luminanceIncrement: int,
):
    fade_gamma(
        flag,
        SetDeviceGammaRamp,
        hdc,
        gamma_ramp,
        gamma_map,
        gammaFinish,
        gammaStart,
        gammaInterval,
        gammaIncrement,
    )

    fade_luminance(
        flag,
        handle,
        luminance_map,
        luminanceFinish,
        luminanceInterval,
        luminanceIncrement,
    )


@cache
def get_adjusted_gamma(
    log_mid_point: float,
    mean_luma: float,
    min_gamma_allowed: float,
    max_gamma_allowed: float,
):
    # All pixels are black (i.e., value of 0)
    # e.g., Black fullscreen wallpaper
    if mean_luma == 0:
        return max_gamma_allowed
    # All pixels are black (i.e., value of 255)
    # e.g., White fullscreen wallpaper
    elif mean_luma == 255:
        return min_gamma_allowed
    else:
        return log_mid_point / math.log(mean_luma)


@cache
def get_gamma_adjusted_mean_luma(
    mean_luma: float, adjusted_gamma: float, mid_point: float
):
    # Adjust average luma accordingly
    mean_luma *= adjusted_gamma

    # Increase monitor's luminance when gamma is lower to compensate the lower perceived luminance for darker colors
    if adjusted_gamma < (mid_point * 10):
        # Small exposure bias when gamma (i.e., content luminance) has been decreased to increase the perceived dynamic range
        mean_luma *= 1.20
    else:
        # Do the opposite for brighter/washed out colors
        mean_luma *= 0.80

    return abs(round(mean_luma))


if __name__ == "__main__":
    # Ignore annoying divide by zero and overflow warnings
    np.seterr(divide="ignore", over="ignore")

    if config.GAMMA_RAMP_ADJUSTMENTS:
        GetDC = windll.user32.GetDC
        SetDeviceGammaRamp = windll.gdi32.SetDeviceGammaRamp
        GetDeviceGammaRamp = windll.gdi32.GetDeviceGammaRamp

        hdc = HDC(GetDC(None))
        if not hdc:
            raise RuntimeError("No HDC")

        if os.path.exists("defaultgamma.npy"):
            default_gamma_ramp = load_gamma_ramp("defaultgamma.npy")
        else:
            default_gamma_ramp = get_default_gamma_ramp(GetDeviceGammaRamp, hdc)
            save_gamma_ramp(default_gamma_ramp, "defaultgamma")

        supported_values = get_gamma_allowed_values(
            SetDeviceGammaRamp, hdc, default_gamma_ramp
        )
        min_gamma_allowed, max_gamma_allowed = (
            min(supported_values),
            max(supported_values),
        )

        gamma_map: dict[float, float] = {}

        if config.GAMMA_CUSTOM_MAPPING:
            if not os.path.exists(config.GAMMA_CUSTOM_MAPPING):
                raise FileNotFoundError(
                    "Custom Gamma Mapping File is not present in the directory."
                )

            with open(config.GAMMA_CUSTOM_MAPPING) as f:
                luma = f.read()
                luma = list(luma.split("\n"))

                for line in luma:
                    s = [x.strip() for x in line.split("=")]
                    gamma_map[float(s[0])] = float(s[1])

        else:
            gamma_map = {
                y: round(x, 2)
                for x, y in zip(
                    scale_list(
                        supported_values,
                        max(min_gamma_allowed, config.MIN_DESIRED_GAMMA),
                        min(max_gamma_allowed, config.MAX_DESIRED_GAMMA),
                    ),
                    supported_values,
                )
            }

        min_gamma_allowed, max_gamma_allowed = list(gamma_map)[0], list(gamma_map)[-1]

        mid_point = (
            ((min_gamma_allowed + max_gamma_allowed) / 2) / 10
        ) + config.MID_POINT_BIAS

        log_mid_point = math.log(mid_point * 255)

        print(f"Min Desired Gamma: {config.MIN_DESIRED_GAMMA}")
        print(f"Max Desired Gamma: {config.MAX_DESIRED_GAMMA}")
        print(f"Gamma Values: {','.join(str(v) for v in gamma_map.values())}")
        print(f"Min Allowed Gamma: {min_gamma_allowed}")
        print(f"Max Allowed Gamma: {max_gamma_allowed}")
        print(f"Mid Point: {mid_point}")
        print(f"Log Mid Point: {log_mid_point}")
        print()

        # Start with a darkened image
        gamma = min_gamma_allowed
        set_gamma(SetDeviceGammaRamp, hdc, default_gamma_ramp, gamma)

    luminance_map: dict[int, int] = {}

    if config.MONITOR_LUMINANCE_CUSTOM_MAPPING:
        if not os.path.exists(config.MONITOR_LUMINANCE_CUSTOM_MAPPING):
            raise FileNotFoundError(
                "Custom Luma Mapping File is not present in the directory."
            )

        with open(config.MONITOR_LUMINANCE_CUSTOM_MAPPING) as f:
            luma = f.read()
            luma = list(luma.split("\n"))

            for line in luma:
                s = [x.strip() for x in line.split("=")]
                luminance_map[int(s[0])] = int(s[1])

    else:
        luminance_map = {
            y: int(x)
            for x, y in zip(
                scale_list(
                    list(range(101)),
                    config.MIN_DESIRED_MONITOR_LUMINANCE,
                    config.MAX_DESIRED_MONITOR_LUMINANCE,
                ),
                list(range(101)),
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

    monitor_luminance = adjusted_monitor_luminance = default_monitor_luminance

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
                    mean_luma = get_average_luminance4(frame)
                except ValueError as e:
                    raise RuntimeError(
                        "[!] Cannot calculate average luminance of the content."
                    ) from e

                meta_info = ""

                # Gamma ramp adjustments work together with monitor's luminance adjustments to make the perceived lumniance changes less aggressive
                if config.GAMMA_RAMP_ADJUSTMENTS:
                    # This method requires frame to be in grayscale (which we already have)
                    adjusted_gamma = get_adjusted_gamma(
                        log_mid_point, mean_luma, min_gamma_allowed, max_gamma_allowed
                    )

                    if config.MONITOR_LUMINANCE_ADJUSTMENTS:
                        # Use gamma correction information to decide the levels of monitor's luminance
                        mean_luma = get_gamma_adjusted_mean_luma(
                            mean_luma, adjusted_gamma, mid_point
                        )
                        adjusted_monitor_luminance = clamp(abs(mean_luma), 0, 100)
                        adjusted_monitor_luminance_map_value = luminance_map[
                            adjusted_monitor_luminance
                        ]

                    adjusted_gamma = clamp(
                        round(adjusted_gamma, 2), min_gamma_allowed, max_gamma_allowed
                    )
                    adjusted_gamma_map_value = gamma_map[adjusted_gamma]

                    change_in_gamma = round(abs(adjusted_gamma - gamma), 2)

                    if (
                        adjusted_gamma != gamma
                        and change_in_gamma > config.GAMMA_DIFFERENCE_THRESHOLD
                        and change_in_gamma != 0.01
                    ):
                        gammaIncrement = 0.01
                        gammaInterval = 0.01
                        luminanceInterval = 0.14
                        luminanceIncrement = 1

                        if config.MONITOR_LUMINANCE_ADJUSTMENTS:
                            if config.MONITOR_LUMINANCE_FORCE_INSTANT_ADJUSTMENTS:
                                fade_gamma(
                                    flag,
                                    SetDeviceGammaRamp,
                                    hdc,
                                    default_gamma_ramp,
                                    gamma_map,
                                    adjusted_gamma,
                                    gamma,
                                    gammaInterval,
                                    gammaIncrement,
                                )
                                if adjusted_monitor_luminance != monitor_luminance:
                                    vcp_set_luminance(
                                        handle,
                                        adjusted_monitor_luminance_map_value,
                                    )
                            else:
                                if FLAGS:
                                    FLAGS.pop().set()

                                flag = threading.Event()
                                FLAGS.append(flag)

                                thread = threading.Thread(
                                    target=fade_gamma_luminance_combined,
                                    args=(
                                        flag,
                                        SetDeviceGammaRamp,
                                        hdc,
                                        default_gamma_ramp,
                                        handle,
                                        gamma_map,
                                        luminance_map,
                                        adjusted_gamma,
                                        gamma,
                                        adjusted_monitor_luminance,
                                        gammaInterval,
                                        gammaIncrement,
                                        luminanceInterval,
                                        luminanceIncrement,
                                    ),
                                )
                                thread.start()

                        else:
                            if FLAGS:
                                FLAGS.pop().set()

                            flag = threading.Event()
                            FLAGS.append(flag)

                            thread = threading.Thread(
                                target=fade_gamma,
                                args=(
                                    flag,
                                    SetDeviceGammaRamp,
                                    hdc,
                                    default_gamma_ramp,
                                    gamma_map,
                                    adjusted_gamma,
                                    gamma,
                                    gammaInterval,
                                    gammaIncrement,
                                ),
                            )
                            thread.start()

                        info = f"Gamma: {adjusted_gamma_map_value}"
                        if meta_info:
                            info = f"{info}[ {meta_info} ]"

                        gamma = adjusted_gamma

                        if config.MONITOR_LUMINANCE_ADJUSTMENTS:
                            info = f"{info} -- Luminance: {adjusted_monitor_luminance_map_value}"

                        print(info)

                        if config.MONITOR_LUMINANCE_ADJUSTMENTS:
                            monitor_luminance = adjusted_monitor_luminance

                meta_info = ""

                if (
                    config.MONITOR_LUMINANCE_ADJUSTMENTS
                    and not config.GAMMA_RAMP_ADJUSTMENTS
                ):
                    mean_luma = round(mean_luma)

                    adjusted_monitor_luminance = clamp(abs(mean_luma), 0, 100)
                    adjusted_monitor_luminance_map_value = luminance_map[
                        adjusted_monitor_luminance
                    ]

                    # Skip if the adjusted luminance is same as current monitor's luminance
                    if adjusted_monitor_luminance == monitor_luminance:
                        continue

                    change_in_luma = abs(adjusted_monitor_luminance - monitor_luminance)

                    if change_in_luma == 1:
                        adjusted_monitor_luminance = monitor_luminance
                        continue

                    if change_in_luma > config.LUMA_DIFFERENCE_THRESHOLD:
                        if config.MONITOR_LUMINANCE_FORCE_INSTANT_ADJUSTMENTS:
                            vcp_set_luminance(
                                handle, adjusted_monitor_luminance_map_value
                            )
                        else:
                            if FLAGS:
                                FLAGS.pop().set()

                            flag = threading.Event()
                            FLAGS.append(flag)

                            thread = threading.Thread(
                                target=fade_luminance,
                                args=(
                                    flag,
                                    handle,
                                    luminance_map,
                                    adjusted_monitor_luminance,
                                    0.2,
                                    1,
                                ),
                            )
                            thread.start()
                            print(
                                f"Luminance: {adjusted_monitor_luminance_map_value} (from {monitor_luminance}) {f'[-- {meta_info} --]' if meta_info else ''}"
                            )
                            monitor_luminance = adjusted_monitor_luminance

    except KeyboardInterrupt:
        print("\n[!] Program is interrupted.\n")

        if config.GAMMA_RAMP_ADJUSTMENTS:
            ReleaseDC = windll.user32.ReleaseDC
            ReleaseDC(hdc)

        print("[!] Setting to default values... \n")

        # Gamma
        if config.GAMMA_RAMP_ADJUSTMENTS:
            default_gamma_ramp = load_gamma_ramp("defaultgamma.npy")
            set_gamma(SetDeviceGammaRamp, hdc, default_gamma_ramp, 1.0)

        # Luminance
        handle = get_primary_monitor_handle()
        vcp_set_luminance(handle, default_monitor_luminance)

        print("[!] Closing... \n")

        time.sleep(1)
