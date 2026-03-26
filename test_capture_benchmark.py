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
Screen capture library benchmark.

Compares zbl, mss, dxcam, and windows-capture on per-frame latency
and frame pacing (jitter). Each library captures FRAME_COUNT frames converted
to grayscale, which matches the workload of the main program.

Metrics per library:
  - Capture latency: time from "request frame" to "frame ready + grayscale done"
  - Frame pacing: std dev of inter-frame intervals (jitter), lower is better for games
  - Effective FPS derived from mean inter-frame interval

Note on windows-capture: it is callback-driven, so latency here measures the
processing time inside the callback rather than a request-to-frame round trip.
The inter-frame interval is still a valid pacing measurement.

Note on dxcam: comtypes (a dependency) calls CoInitializeEx on import,
which conflicts with the COM apartment already set up by our ctypes windll calls
on the main thread. Both libraries are therefore run inside a dedicated thread
that has its own COM context, avoiding the WinError -2147417850 conflict.
"""

import contextlib
import statistics
import threading
import time

import cv2
import numpy as np

import config

FRAME_COUNT = 300
WARMUP_FRAMES = 60  # discarded so JIT/driver startup doesn't skew results
MONITOR_INDEX = config.MONITOR_INDEX


def print_results(name: str, latencies: list[float], intervals: list[float]) -> None:
    if not latencies:
        print(f"  {name}: no data collected\n")
        return

    mean_lat = statistics.mean(latencies)
    median_lat = statistics.median(latencies)
    stdev_lat = statistics.stdev(latencies) if len(latencies) > 1 else 0.0
    min_lat = min(latencies)
    max_lat = max(latencies)
    mean_itvl = statistics.mean(intervals) if intervals else 0.0
    stdev_itvl = statistics.stdev(intervals) if len(intervals) > 1 else 0.0
    fps = 1000.0 / mean_itvl if mean_itvl > 0 else 0.0

    print(
        f"  Latency     mean={mean_lat:6.2f}ms  median={median_lat:6.2f}ms  "
        f"min={min_lat:5.2f}ms  max={max_lat:6.2f}ms  stdev={stdev_lat:5.2f}ms"
    )
    print(f"  Pacing      mean={mean_itvl:6.2f}ms  stdev={stdev_itvl:5.2f}ms  =>  {fps:.1f} FPS")
    print()


def section(name: str) -> None:
    print(f"\n{'─' * 56}")
    print(f"  {name}")
    print(f"{'─' * 56}")


def benchmark_zbl() -> tuple[list[float], list[float]]:
    try:
        from zbl import Capture
    except ImportError:
        print("  [skip] zbl not installed")
        return [], []

    latencies: list[float] = []
    timestamps: list[float] = []

    try:
        with Capture(
            display_id=MONITOR_INDEX,
            is_cursor_capture_enabled=False,
            is_border_required=False,
        ) as cap:
            frames = cap.frames()
            for i in range(FRAME_COUNT + WARMUP_FRAMES):
                t0 = time.perf_counter()
                frame = next(frames)
                cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                t1 = time.perf_counter()
                if i >= WARMUP_FRAMES:
                    latencies.append((t1 - t0) * 1000.0)
                    timestamps.append(t0)
    except Exception as e:
        print(f"  [error] zbl: {e}")
        return [], []

    intervals = [(timestamps[i] - timestamps[i - 1]) * 1000.0 for i in range(1, len(timestamps))]
    return latencies, intervals


def benchmark_mss() -> tuple[list[float], list[float]]:
    try:
        import mss
    except ImportError:
        print("  [skip] mss not installed")
        return [], []

    latencies: list[float] = []
    timestamps: list[float] = []

    try:
        with mss.mss() as sct:
            monitor = sct.monitors[MONITOR_INDEX + 1]  # mss uses 1-based monitor indexing
            for i in range(FRAME_COUNT + WARMUP_FRAMES):
                t0 = time.perf_counter()
                shot = sct.grab(monitor)
                frame = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
                cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
                t1 = time.perf_counter()
                if i >= WARMUP_FRAMES:
                    latencies.append((t1 - t0) * 1000.0)
                    timestamps.append(t0)
    except Exception as e:
        print(f"  [error] mss: {e}")
        return [], []

    intervals = [(timestamps[i] - timestamps[i - 1]) * 1000.0 for i in range(1, len(timestamps))]
    return latencies, intervals


def run_dxcam_in_thread(result: dict) -> None:
    # dxcam uses comtypes which calls CoInitializeEx on import. Running it in a
    # dedicated thread gives it a fresh COM apartment, avoiding the WinError
    # -2147417850 conflict with the ctypes windll calls already made on the
    # main thread. output_color="GRAY" lets dxcam do the conversion natively.
    try:
        import dxcam
    except ImportError:
        result["skip"] = "dxcam not installed"
        return

    latencies: list[float] = []
    timestamps: list[float] = []
    camera = None

    try:
        camera = dxcam.create(output_idx=MONITOR_INDEX, output_color="GRAY")
        total = FRAME_COUNT + WARMUP_FRAMES
        captured = 0

        while captured < total:
            t0 = time.perf_counter()
            # new_frame_only=False always returns the latest frame rather than
            # None when the screen hasn't changed, which is what we want here.
            frame = camera.grab(new_frame_only=False)
            t1 = time.perf_counter()

            if frame is None:
                continue

            if captured >= WARMUP_FRAMES:
                latencies.append((t1 - t0) * 1000.0)
                timestamps.append(t0)
            captured += 1
    except Exception as e:
        result["error"] = str(e)
        return
    finally:
        if camera is not None:
            with contextlib.suppress(Exception):
                camera.release()

    result["latencies"] = latencies
    result["intervals"] = [
        (timestamps[i] - timestamps[i - 1]) * 1000.0 for i in range(1, len(timestamps))
    ]


def benchmark_dxcam() -> tuple[list[float], list[float]]:
    result: dict = {}
    t = threading.Thread(target=run_dxcam_in_thread, args=(result,))
    t.start()
    t.join()

    if "skip" in result:
        print(f"  [skip] {result['skip']}")
        return [], []
    if "error" in result:
        print(f"  [error] dxcam: {result['error']}")
        return [], []

    return result.get("latencies", []), result.get("intervals", [])


def benchmark_windows_capture() -> tuple[list[float], list[float]]:
    try:
        from windows_capture import Frame, InternalCaptureControl, WindowsCapture
    except ImportError:
        print("  [skip] windows-capture not installed")
        return [], []

    latencies: list[float] = []
    timestamps: list[float] = []
    frame_count = [0]
    done = threading.Event()
    errors: list[str] = []

    capture = WindowsCapture(
        cursor_capture=None,
        draw_border=False,
        monitor_index=None,
        window_name=None,
    )

    @capture.event
    def on_frame_arrived(frame: Frame, capture_control: InternalCaptureControl) -> None:
        t0 = time.perf_counter()
        try:
            # 1. Use .frame_buffer instead of .buffer()
            raw = frame.frame_buffer

            # 2. Access .width and .height as attributes, not functions
            arr = np.frombuffer(raw, dtype=np.uint8).reshape(frame.height, frame.width, 4)

            cv2.cvtColor(arr, cv2.COLOR_BGRA2GRAY)
        except Exception as e:
            errors.append(str(e))
            capture_control.stop()
            done.set()
            return
        t1 = time.perf_counter()

        idx = frame_count[0]
        frame_count[0] += 1

        if idx >= WARMUP_FRAMES:
            latencies.append((t1 - t0) * 1000.0)
            timestamps.append(t0)

        if frame_count[0] >= FRAME_COUNT + WARMUP_FRAMES:
            capture_control.stop()
            done.set()

    @capture.event
    def on_closed() -> None:
        done.set()

    try:
        ctrl = capture.start_free_threaded()
        done.wait(timeout=60.0)
        with contextlib.suppress(Exception):
            ctrl.stop()
    except Exception as e:
        print(f"  [error] windows-capture: {e}")
        return [], []

    if errors:
        print(f"  [warning] frame errors (first 3): {errors[:3]}")

    intervals = [(timestamps[i] - timestamps[i - 1]) * 1000.0 for i in range(1, len(timestamps))]
    return latencies, intervals


LIBRARIES = [
    ("zbl", benchmark_zbl),
    ("mss", benchmark_mss),
    ("dxcam", benchmark_dxcam),
    ("windows-capture", benchmark_windows_capture),
]


def main() -> None:
    print("\nCapture library benchmark")
    print(
        f"Monitor : {MONITOR_INDEX}  |  Frames : {FRAME_COUNT}  (+{WARMUP_FRAMES} warmup discarded)"
    )
    print("Workload: grayscale conversion per frame (matches main program)")

    all_results: dict[str, tuple[list[float], list[float]]] = {}

    for name, fn in LIBRARIES:
        section(name)
        lats, itvls = fn()
        all_results[name] = (lats, itvls)
        print_results(name, lats, itvls)

    ranked = []
    for name, (lats, itvls) in all_results.items():
        if not lats:
            continue
        mean_lat = statistics.mean(lats)
        stdev_lat = statistics.stdev(lats) if len(lats) > 1 else 0.0
        mean_itvl = statistics.mean(itvls) if itvls else 0.0
        stdev_itvl = statistics.stdev(itvls) if len(itvls) > 1 else 0.0
        fps = 1000.0 / mean_itvl if mean_itvl > 0 else 0.0
        ranked.append((name, mean_lat, stdev_lat, stdev_itvl, fps))

    ranked.sort(key=lambda x: x[1])

    print(f"\n{'=' * 56}")
    print("  Summary  (sorted by mean latency, lower is better)")
    print(f"{'=' * 56}")
    print(f"  {'Library':<18}  {'Mean':>7}  {'Stdev':>7}  {'Jitter':>7}  {'FPS':>6}")
    print(f"  {'-' * 18}  {'-' * 7}  {'-' * 7}  {'-' * 7}  {'-' * 6}")
    for name, mean_lat, stdev_lat, jitter, fps in ranked:
        print(
            f"  {name:<18}  {mean_lat:>6.2f}ms  {stdev_lat:>6.2f}ms  {jitter:>6.2f}ms  {fps:>5.1f}"
        )

    if ranked:
        print()
        print(f"  Fastest (mean latency) : {ranked[0][0]}  ({ranked[0][1]:.2f} ms)")
        by_jitter = sorted(ranked, key=lambda x: x[3])
        print(f"  Best frame pacing      : {by_jitter[0][0]}  ({by_jitter[0][3]:.2f} ms jitter)")
    print()


if __name__ == "__main__":
    main()
