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
VCP luminance call latency and cadence tester.

Measures how long vcp_set_luminance takes per call on your monitor, whether
no-op writes (same value again) are cheaper than real changes, and the
fastest inter-write interval at which the monitor still reliably applies
every value (verified with read-backs). The result feeds
MONITOR_LUMINANCE_MIN_WRITE_INTERVAL_MS in config.py.

Uses time.perf_counter() (sub-microsecond resolution on Windows) for accuracy.

Note: this measures the DDC/CI write round-trip time as seen by the CPU.
The monitor's panel response (how fast it *applies* the brightness) is a
separate, hardware-side delay that cannot be measured from software.

The whole run performs a few hundred DDC/CI writes; see the EEPROM warning
in the README before running it repeatedly.
"""

import statistics
import time

from main import (
    destroy_physical_monitor_handle,
    get_primary_monitor_handle,
    vcp_get_luminance,
    vcp_set_luminance,
)

# Brightness values to cycle through. Spread across the full range so the
# monitor actually has to do work each call rather than ignoring no-op writes.
TEST_VALUES = [10, 30, 50, 70, 90, 70, 50, 30, 10, 50]

# How many full passes through TEST_VALUES to run.
PASSES = 5

# Milliseconds to wait between calls so the monitor's DDC/CI receiver has time
# to process each command. Most monitors need 40-50 ms minimum.
INTER_CALL_DELAY_MS = 50


# Inter-write delays (ms) to sweep, fastest-reliable-cadence search. Swept
# downward; the sweep stops at the first unreliable delay.
SWEEP_DELAYS_MS = [50, 40, 30, 20, 15, 10, 5]

# Writes per trial and trials per delay during the sweep.
SWEEP_WRITES = 6
SWEEP_TRIALS = 2


def print_stats(label: str, timings_ms: list[float]) -> None:
    print(f"  {label}")
    print(f"    Calls   : {len(timings_ms)}")
    print(f"    Mean    : {statistics.mean(timings_ms):.3f} ms")
    print(f"    Median  : {statistics.median(timings_ms):.3f} ms")
    if len(timings_ms) > 1:
        print(f"    Std dev : {statistics.stdev(timings_ms):.3f} ms")
    print(f"    Min/Max : {min(timings_ms):.3f} / {max(timings_ms):.3f} ms")


def measure_write_latency(handle) -> None:
    print("-- Write latency (changing values) " + "-" * 30)
    timings_ms: list[float] = []
    for pass_num in range(1, PASSES + 1):
        for value in TEST_VALUES:
            t0 = time.perf_counter()
            vcp_set_luminance(handle, value)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            timings_ms.append(elapsed_ms)
            print(f"  Pass {pass_num}  value={value:>3}  {elapsed_ms:.3f} ms")
            time.sleep(INTER_CALL_DELAY_MS / 1000.0)
    print()
    print_stats("Changing-value writes:", timings_ms)
    print()


def measure_noop_writes(handle) -> None:
    # Whether writing the value the monitor already has is cheaper than a real
    # change. If it is, redundant writes are cheap; if not, deduplication in
    # the writer (already in place) is what saves the time.
    print("-- No-op writes (same value repeatedly) " + "-" * 25)
    value = 50
    vcp_set_luminance(handle, value)
    time.sleep(0.1)

    timings_ms: list[float] = []
    for _ in range(10):
        t0 = time.perf_counter()
        vcp_set_luminance(handle, value)
        timings_ms.append((time.perf_counter() - t0) * 1000.0)
        time.sleep(INTER_CALL_DELAY_MS / 1000.0)
    print_stats("Same-value writes:", timings_ms)
    print()


def find_min_reliable_interval(handle) -> int | None:
    # Sweep the inter-write delay downward. A delay is reliable when every
    # SetVCPFeature call reports success AND a read-back after the burst
    # confirms the monitor holds the last written value. Read-back is the
    # ground truth: some monitors ack writes they then drop when pushed too
    # fast.
    print("-- Fastest reliable write cadence " + "-" * 31)
    values = [30, 70]
    reliable: int | None = None

    for delay_ms in SWEEP_DELAYS_MS:
        ok = True
        for _ in range(SWEEP_TRIALS):
            last_value = values[0]
            for i in range(SWEEP_WRITES):
                last_value = values[i % 2]
                if not vcp_set_luminance(handle, last_value):
                    ok = False
                time.sleep(delay_ms / 1000.0)
            time.sleep(0.15)  # let the monitor settle before the read-back
            if vcp_get_luminance(handle) != last_value:
                ok = False
            if not ok:
                break
        print(f"  {delay_ms:>3} ms between writes: {'reliable' if ok else 'UNRELIABLE'}")
        if not ok:
            break
        reliable = delay_ms

    print()
    return reliable


def main() -> None:
    handle = get_primary_monitor_handle()
    default_luminance = vcp_get_luminance(handle)
    print(f"Default luminance: {default_luminance}")
    print(f"Test values:       {TEST_VALUES}")
    print(f"Passes:            {PASSES}")
    print(f"Inter-call delay:  {INTER_CALL_DELAY_MS} ms")
    print()

    reliable: int | None = None
    try:
        measure_write_latency(handle)
        measure_noop_writes(handle)
        reliable = find_min_reliable_interval(handle)
    finally:
        print("Restoring default luminance...")
        vcp_set_luminance(handle, default_luminance)
        destroy_physical_monitor_handle(handle)

    print()
    if reliable is not None:
        # Keep a margin over the fastest reliable delay; a short sweep can look
        # cleaner than sustained real-world bursts.
        suggested = max(reliable, 10)
        print(f"Fastest reliable inter-write interval: {reliable} ms")
        print(f"Suggested config: MONITOR_LUMINANCE_MIN_WRITE_INTERVAL_MS = {suggested}")
        print("(Keep some margin: the sweep is short; sustained bursts may need more.)")
        # Translate the cadence into a sustained-rate ceiling for the EEPROM
        # guard: the peak is 60000/interval writes per minute; the guard should
        # sit comfortably below that.
        peak_per_min = int(60000 / suggested)
        print(
            f"At that interval the peak is ~{peak_per_min} writes/min; set "
            f"MONITOR_LUMINANCE_MAX_WRITES_PER_MINUTE below it (default 600) "
            "to bound sustained wear."
        )
    else:
        print("No reliable interval found even at the slowest sweep delay;")
        print("keep MONITOR_LUMINANCE_MIN_WRITE_INTERVAL_MS at 50 or higher.")
    print()
    print("Note: these timings reflect the DDC/CI write round-trip as seen by")
    print("the CPU (i.e. how long the OS call blocks). The monitor's actual")
    print("panel brightness transition happens independently after that.")


if __name__ == "__main__":
    main()
