#!/usr/bin/env python3
"""
clock_sync.py — Synchronise clocks between two Pi 5s using the host as reference.

Algorithm (NTP simplified, Cristian's method):
  For each sample on device D:
    t1      = host CLOCK_REALTIME (ns) before sending command
    t_dev   = device CLOCK_REALTIME (µs) from `date +%s%6N`
    t2      = host CLOCK_REALTIME (ns) after response
    offset  = (t1 + t2) / 2  -  t_dev * 1000   [ns units]

  Best estimate = sample with smallest RTT (most symmetric path).
  Final offset = weighted mean of best 25% samples by RTT.

  offset_us[D] = host_clock_us - device_clock_us
  =>  host_time_us = device_time_us + offset_us[D]

Cross-device latency (Pi1 → Pi2):
  delta_us = offset_us[pi2] - offset_us[pi1]
  =>  pi2_time_us = pi1_time_us + delta_us  (how much Pi2 is ahead of Pi1)
  one_way_latency = recv_time_pi2 - send_time_pi1 - delta_us
"""

import subprocess
import time
import statistics
import json
import argparse
import sys

SAMPLES = 40
WARMUP  = 5


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


def measure_clock_offset(serial: str, samples: int = SAMPLES, warmup: int = WARMUP):
    """
    Returns (offset_us, rtt_us_median, stdev_us) where
      offset_us = host_clock_us - device_clock_us
    """
    offsets = []
    rtts    = []

    for i in range(samples + warmup):
        t1_ns = time.time_ns()
        result = subprocess.run(
            adb_cmd("shell", "date +%s%6N", serial=serial),
            capture_output=True, text=True, timeout=5
        )
        t2_ns = time.time_ns()

        if result.returncode != 0 or not result.stdout.strip().isdigit():
            continue

        t_dev_us = int(result.stdout.strip())   # device µs since epoch
        t1_us    = t1_ns // 1000
        t2_us    = t2_ns // 1000
        rtt_us   = t2_us - t1_us
        mid_us   = (t1_us + t2_us) // 2
        offset   = mid_us - t_dev_us            # host - device (µs)

        if i >= warmup:
            offsets.append((rtt_us, offset))
            rtts.append(rtt_us)

    if not offsets:
        return None, None, None

    # Weight: use best 25% by RTT
    offsets.sort(key=lambda x: x[0])
    top_n   = max(1, len(offsets) // 4)
    best    = offsets[:top_n]
    best_offsets = [o for _, o in best]

    offset_us   = statistics.mean(best_offsets)
    stdev_us    = statistics.stdev(best_offsets) if len(best_offsets) > 1 else 0
    rtt_median  = statistics.median(rtts)

    return offset_us, rtt_median, stdev_us


def main():
    parser = argparse.ArgumentParser(description="Clock sync two Pi 5s via host reference")
    parser.add_argument("serials", nargs="*",
                        help="ADB serial(s); auto-detect if omitted (expects exactly 2)")
    parser.add_argument("--samples", type=int, default=SAMPLES)
    parser.add_argument("--out", default="clock_offsets.json")
    args = parser.parse_args()

    serials = args.serials or get_connected_serials()
    if len(serials) < 2:
        print(f"Need 2 devices, found {len(serials)}: {serials}")
        sys.exit(1)

    print(f"Clock synchronisation  ({args.samples} samples per device)\n")

    offsets = {}
    for serial in serials[:2]:
        print(f"Measuring clock offset for {serial} ...", flush=True)
        offset_us, rtt_us, stdev_us = measure_clock_offset(serial, samples=args.samples)
        if offset_us is None:
            print(f"  ERROR: could not measure offset for {serial}")
            sys.exit(1)
        offsets[serial] = round(offset_us)
        print(f"  host - device offset : {offset_us:+.0f} µs")
        print(f"  ADB RTT median       : {rtt_us:.0f} µs")
        print(f"  offset stdev (best25%): {stdev_us:.1f} µs\n")

    s1, s2 = serials[0], serials[1]
    # delta > 0 means Pi2 clock is behind Pi1 clock
    delta_us = offsets[s2] - offsets[s1]
    print(f"Clock delta ({s1} → {s2}): {delta_us:+.0f} µs")
    print(f"  (positive = Pi2 is behind Pi1; subtract delta from Pi2 recv_time for latency)\n")

    result = {
        "serials": serials[:2],
        "offsets_us": offsets,
        "delta_pi1_to_pi2_us": round(delta_us),
        "note": "one_way_latency = recv_time_pi2_us - send_time_pi1_us - delta_pi1_to_pi2_us"
    }

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Offsets written to {args.out}")


if __name__ == "__main__":
    main()
