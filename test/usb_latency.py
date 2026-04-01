#!/usr/bin/env python3
"""
usb_latency.py — Estimate ADB/USB one-way latency for each connected Pi 5.

Method (Cristian's algorithm, single-hop):
  For each round-trip:
    t1  = host monotonic clock before adb shell command
    t_d = device monotonic clock reported by the shell command
    t2  = host monotonic clock after response received
    RTT = t2 - t1
    usb_one_way ≈ RTT / 2   (assumes symmetric USB path)

Runs SAMPLES round-trips per device, discards top 5% outliers, reports stats.
"""

import subprocess
import time
import statistics
import sys
import json
import argparse

SAMPLES = 50
WARMUP  = 5   # discard first N samples (JIT / adb daemon startup)

# Shell command that returns device monotonic microseconds
DEVICE_CMD = "cat /proc/uptime"  # gives uptime in seconds with high res


def adb(*args, serial=None):
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


def measure_rtt(serial: str, samples: int = SAMPLES, warmup: int = WARMUP):
    """
    Returns list of (rtt_us, device_uptime_s) tuples.
    Uses `cat /proc/uptime` on the device as the timing echo:
    we don't use device time directly for RTT — we just measure host-side RTT.
    """
    rtts = []
    for i in range(samples + warmup):
        t1 = time.monotonic()
        result = subprocess.run(
            adb("shell", "cat /proc/uptime", serial=serial),
            capture_output=True, text=True, timeout=5
        )
        t2 = time.monotonic()
        if result.returncode != 0:
            continue
        rtt_us = (t2 - t1) * 1e6
        if i >= warmup:
            rtts.append(rtt_us)
    return rtts


def analyse(rtts: list, label: str):
    if not rtts:
        print(f"  {label}: no data")
        return {}

    # Drop top 5% outliers
    cutoff = sorted(rtts)[int(len(rtts) * 0.95)]
    clean  = [r for r in rtts if r <= cutoff]

    mean   = statistics.mean(clean)
    median = statistics.median(clean)
    stdev  = statistics.stdev(clean) if len(clean) > 1 else 0
    mn     = min(clean)
    mx     = max(clean)
    usb_ow = median / 2   # one-way estimate

    print(f"  {label}:")
    print(f"    samples        : {len(clean)} (of {len(rtts)}, {len(rtts)-len(clean)} outliers removed)")
    print(f"    RTT  mean      : {mean:7.0f} µs")
    print(f"    RTT  median    : {median:7.0f} µs")
    print(f"    RTT  min/max   : {mn:7.0f} / {mx:7.0f} µs")
    print(f"    RTT  stddev    : {stdev:7.0f} µs")
    print(f"    USB one-way est: {usb_ow:7.0f} µs  (= median/2)")

    return {
        "serial":       label,
        "rtt_mean_us":  round(mean),
        "rtt_median_us":round(median),
        "rtt_min_us":   round(mn),
        "rtt_max_us":   round(mx),
        "rtt_stdev_us": round(stdev),
        "usb_oneway_us":round(usb_ow),
    }


def main():
    parser = argparse.ArgumentParser(description="Estimate USB/ADB latency for Pi 5 devices")
    parser.add_argument("serials", nargs="*", help="ADB device serial(s); auto-detect if omitted")
    parser.add_argument("--samples", type=int, default=SAMPLES)
    parser.add_argument("--out", default="usb_latency.json", help="Output JSON file")
    args = parser.parse_args()

    serials = args.serials or get_connected_serials()
    if not serials:
        print("No ADB devices found.")
        sys.exit(1)

    print(f"USB/ADB latency measurement  ({args.samples} samples per device)\n")

    results = {}
    for serial in serials:
        print(f"Measuring {serial} ...", flush=True)
        rtts = measure_rtt(serial, samples=args.samples)
        r = analyse(rtts, serial)
        if r:
            results[serial] = r
        print()

    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Results written to {args.out}")


if __name__ == "__main__":
    main()
