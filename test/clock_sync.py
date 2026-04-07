#!/usr/bin/env python3
"""
clock_sync.py — Synchronise two Pi 5 clocks then measure residual offset.

Strategy (root mode):
  1. Set CLOCK_REALTIME on both devices to the current host time via
       adb shell date -s @<epoch>
     This collapses the large (~hours) raw offset to a small residual.

  2. Measure the residual offset on each device with the NTP/Cristian
     method, calling `dace_psnr_bench --real-us` for µs-precision
     CLOCK_REALTIME readings (much better than /proc/uptime's 10 ms).

  3. Compute the Pi1→Pi2 delta for use as --clock-delta in the benchmark.

One-way latency formula:
  corrected_latency = recv_time_pi2_us - send_time_pi1_us - delta_us
  where delta_us = offset_pi2 - offset_pi1
  and   offset   = host_realtime_us - device_realtime_us  (residual only)
"""

import subprocess
import time
import statistics
import json
import argparse
import sys

SAMPLES    = 40
WARMUP     = 5
DEVICE_BIN = "/data/local/tmp/dace_psnr_bench"


def adb_cmd(*args, serial=None):
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    return cmd


def get_connected_serials():
    out = subprocess.check_output(["adb", "devices"], text=True)
    serials = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if line and "device" in line and not line.startswith("*"):
            serials.append(line.split()[0])
    return serials


def set_device_clock(serial: str):
    """Set device CLOCK_REALTIME to current host time (requires root)."""
    host_ts = time.time()
    result = subprocess.run(
        adb_cmd("shell", f"date -s @{host_ts:.6f}", serial=serial),
        capture_output=True, text=True, timeout=5
    )
    if result.returncode != 0:
        print(f"  WARNING: date -s failed ({result.stderr.strip()})")
    else:
        print(f"  Clock set to @{host_ts:.3f}")


def probe_realtime_us(serial: str) -> int | None:
    """Call dace_psnr_bench --real-us; returns device CLOCK_REALTIME in µs."""
    result = subprocess.run(
        adb_cmd("shell", f"{DEVICE_BIN} --real-us", serial=serial),
        capture_output=True, text=True, timeout=5
    )
    if result.returncode != 0 or not result.stdout.strip().lstrip("-").isdigit():
        return None
    return int(result.stdout.strip())


def measure_residual_offset(serial: str, samples: int = SAMPLES, warmup: int = WARMUP):
    """
    Returns (offset_us, rtt_median_us, stdev_us) where
      offset_us = host_REALTIME_us - device_REALTIME_us  (residual after set_device_clock)

    Uses dace_psnr_bench --real-us for µs-precision CLOCK_REALTIME on device.
    """
    offsets = []
    rtts    = []

    for i in range(samples + warmup):
        t1_us = time.time_ns() // 1000
        t_dev = probe_realtime_us(serial)
        t2_us = time.time_ns() // 1000

        if t_dev is None:
            continue

        rtt_us = t2_us - t1_us
        mid_us = (t1_us + t2_us) // 2
        offset = mid_us - t_dev          # host - device (µs)

        if i >= warmup:
            offsets.append((rtt_us, offset))
            rtts.append(rtt_us)

    if not offsets:
        return None, None, None

    offsets.sort(key=lambda x: x[0])
    top_n        = max(1, len(offsets) // 4)
    best_offsets = [o for _, o in offsets[:top_n]]

    offset_us  = statistics.mean(best_offsets)
    stdev_us   = statistics.stdev(best_offsets) if len(best_offsets) > 1 else 0.0
    rtt_median = statistics.median(rtts)

    return offset_us, rtt_median, stdev_us


def main():
    parser = argparse.ArgumentParser(description="Clock sync two Pi 5s (root) then measure residual offset")
    parser.add_argument("serials", nargs="*",
                        help="ADB serial(s); auto-detect if omitted (expects exactly 2)")
    parser.add_argument("--samples", type=int, default=SAMPLES,
                        help=f"NTP samples per device (default {SAMPLES})")
    parser.add_argument("--out", default="clock_offsets.json",
                        help="JSON output path")
    parser.add_argument("--no-set", action="store_true",
                        help="Skip setting device clocks (just measure residual)")
    args = parser.parse_args()

    serials = args.serials or get_connected_serials()
    if len(serials) < 2:
        print(f"Need 2 devices, found {len(serials)}: {serials}")
        sys.exit(1)
    s1, s2 = serials[0], serials[1]

    # ── Step 1: set both clocks ──────────────────────────────────────────────
    if not args.no_set:
        print("Setting CLOCK_REALTIME on both devices to host time ...\n")
        for serial in (s1, s2):
            print(f"  [{serial}]", end=" ")
            set_device_clock(serial)
        print()

    # ── Step 2: measure residual offsets ────────────────────────────────────
    print(f"Measuring residual offset ({args.samples} samples, µs precision) ...\n")

    offsets = {}
    for serial in (s1, s2):
        print(f"  [{serial}]", flush=True)
        offset_us, rtt_us, stdev_us = measure_residual_offset(serial, samples=args.samples)
        if offset_us is None:
            print(f"    ERROR: probe failed — is {DEVICE_BIN} on device?")
            sys.exit(1)
        offsets[serial] = round(offset_us)
        print(f"    residual offset  : {offset_us:+.1f} µs  (host − device)")
        print(f"    ADB RTT median   : {rtt_us:.0f} µs")
        print(f"    stdev (best 25%) : {stdev_us:.1f} µs\n")

    # ── Step 3: compute Pi1→Pi2 delta ───────────────────────────────────────
    # delta > 0 means Pi2 clock is behind Pi1 (Pi2's REALTIME is smaller)
    delta_us = offsets[s2] - offsets[s1]
    print(f"Clock delta ({s1} → {s2}): {delta_us:+d} µs")
    print(f"  one_way = recv_pi2_us - send_pi1_us - {delta_us:+d}")
    print(f"  (positive delta = Pi2 clock is behind Pi1)\n")

    result = {
        "serials":             [s1, s2],
        "offsets_us":          offsets,
        "delta_pi1_to_pi2_us": delta_us,
        "note":                "one_way_us = recv_pi2_us - send_pi1_us - delta_pi1_to_pi2_us"
    }
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Written to {args.out}")


if __name__ == "__main__":
    main()
