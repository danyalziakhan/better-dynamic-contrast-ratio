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
VCP luminance call latency tester.

Measures how long vcp_set_luminance takes per call on your monitor.
Uses time.perf_counter() (sub-microsecond resolution on Windows) for accuracy.

Note: this measures the DDC/CI write round-trip time as seen by the CPU.
The monitor's panel response (how fast it *applies* the brightness) is a
separate, hardware-side delay that cannot be measured from software.
"""

import statistics
import time

from main import get_primary_monitor_handle, vcp_get_luminance, vcp_set_luminance

# Brightness values to cycle through. Spread across the full range so the
# monitor actually has to do work each call rather than ignoring no-op writes.
TEST_VALUES = [10, 30, 50, 70, 90, 70, 50, 30, 10, 50]

# How many full passes through TEST_VALUES to run.
PASSES = 5

# Milliseconds to wait between calls so the monitor's DDC/CI receiver has time
# to process each command. Most monitors need 40-50 ms minimum.
INTER_CALL_DELAY_MS = 50


def main() -> None:
    handle = get_primary_monitor_handle()
    default_luminance = vcp_get_luminance(handle)
    print(f"Default luminance: {default_luminance}")
    print(f"Test values:       {TEST_VALUES}")
    print(f"Passes:            {PASSES}")
    print(f"Inter-call delay:  {INTER_CALL_DELAY_MS} ms")
    print(f"Total calls:       {len(TEST_VALUES) * PASSES}")
    print()

    timings_ms: list[float] = []

    try:
        for pass_num in range(1, PASSES + 1):
            for value in TEST_VALUES:
                t0 = time.perf_counter()
                vcp_set_luminance(handle, value)
                t1 = time.perf_counter()

                elapsed_ms = (t1 - t0) * 1000.0
                timings_ms.append(elapsed_ms)
                print(f"  Pass {pass_num}  value={value:>3}  {elapsed_ms:.3f} ms")

                time.sleep(INTER_CALL_DELAY_MS / 1000.0)

    finally:
        print()
        print("Restoring default luminance...")
        vcp_set_luminance(handle, default_luminance)

    print()
    print("=" * 44)
    print(f"  Calls measured : {len(timings_ms)}")
    print(f"  Mean           : {statistics.mean(timings_ms):.3f} ms")
    print(f"  Median         : {statistics.median(timings_ms):.3f} ms")
    print(f"  Std dev        : {statistics.stdev(timings_ms):.3f} ms")
    print(f"  Min            : {min(timings_ms):.3f} ms")
    print(f"  Max            : {max(timings_ms):.3f} ms")
    print("=" * 44)
    print()
    print("Note: these timings reflect the DDC/CI write round-trip as seen by")
    print("the CPU (i.e. how long the OS call blocks). The monitor's actual")
    print("panel brightness transition happens independently after that.")


if __name__ == "__main__":
    main()
