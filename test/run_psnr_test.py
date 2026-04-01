#!/usr/bin/env python3
"""
run_psnr_test.py — Orchestrate the full DACE PSNR + latency test across two Pi 5s.

Steps:
  1. Detect / validate the two ADB devices
  2. (Optionally) cross-compile dace_psnr_bench for arm64-android
  3. Run clock_sync.py to get Pi1→Pi2 clock delta
  4. Push binary to both devices
  5. Run sender on Pi 5 #1  → /data/local/tmp/dace_encoded.bin
  6. Pull encoded.bin to host
  7. Push encoded.bin to Pi 5 #2
  8. Run receiver on Pi 5 #2  → CSV on stdout
  9. Parse CSV, compute aggregate PSNR + latency stats, print report

Usage:
  python3 run_psnr_test.py [--sender <serial>] [--receiver <serial>] [--no-build]
"""

import argparse
import csv
import io
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT   = os.path.dirname(SCRIPT_DIR)
C_SRC       = os.path.join(SCRIPT_DIR, "dace_psnr_bench.c")
BENCH_BIN   = "/tmp/dace_psnr_bench"
X264_INC    = "/tmp/x264-dace-arm64/include"
X264_LIB    = "/tmp/x264-dace-arm64/lib/libx264.a"
NDK_BIN     = os.path.expanduser(
    "~/Android/Sdk/ndk/27.2.12479018/toolchains/llvm/prebuilt/linux-x86_64/bin"
)
CLANG       = os.path.join(NDK_BIN, "aarch64-linux-android26-clang")
DEVICE_BIN  = "/data/local/tmp/dace_psnr_bench"
DEVICE_ENC  = "/data/local/tmp/dace_encoded.bin"
CLOCK_JSON  = "/tmp/clock_offsets.json"


# ── helpers ────────────────────────────────────────────────────────────────────

def run(cmd, **kwargs):
    print(f"  $ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=True, **kwargs)


def adb(serial, *args, **kwargs):
    return run(["adb", "-s", serial] + list(args), **kwargs)


def adb_out(serial, *args):
    r = subprocess.run(["adb", "-s", serial] + list(args),
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def get_connected_serials():
    out = subprocess.check_output(["adb", "devices"], text=True)
    serials = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if line and "device" in line and not line.startswith("*"):
            serials.append(line.split()[0])
    return serials


# ── build ──────────────────────────────────────────────────────────────────────

def build_binary():
    print("\n[1/5] Compiling dace_psnr_bench for arm64-android …")
    run([
        CLANG, "-O2", "-o", BENCH_BIN,
        C_SRC,
        f"-I{X264_INC}",
        X264_LIB,
        "-lm",
        "-Wl,-z,max-page-size=16384",
    ])
    print(f"      → {BENCH_BIN}")


# ── clock sync ─────────────────────────────────────────────────────────────────

def sync_clocks(s1: str, s2: str):
    print("\n[2/5] Synchronising clocks …")
    run([
        sys.executable,
        os.path.join(SCRIPT_DIR, "clock_sync.py"),
        s1, s2,
        "--out", CLOCK_JSON,
    ])
    with open(CLOCK_JSON) as f:
        data = json.load(f)
    delta = data["delta_pi1_to_pi2_us"]
    print(f"      Pi1→Pi2 delta = {delta:+d} µs")
    return delta


# ── push binary ────────────────────────────────────────────────────────────────

def push_binary(serial: str):
    adb(serial, "push", BENCH_BIN, DEVICE_BIN)
    adb(serial, "shell", "chmod", "+x", DEVICE_BIN)


# ── sender ─────────────────────────────────────────────────────────────────────

def run_sender(serial: str):
    print(f"\n[3/5] Running sender on {serial} …")
    # Run directly; stderr (progress) shown, stdout goes to device file
    r = subprocess.run(
        ["adb", "-s", serial, "shell",
         f"{DEVICE_BIN} --sender --out {DEVICE_ENC}"],
        check=True
    )
    return r.returncode == 0


# ── transfer ───────────────────────────────────────────────────────────────────

def transfer_encoded(s_sender: str, s_receiver: str):
    print(f"\n[4/5] Transferring encoded.bin  {s_sender} → host → {s_receiver} …")
    local = "/tmp/dace_encoded.bin"
    adb(s_sender,   "pull", DEVICE_ENC, local)
    adb(s_receiver, "push", local, DEVICE_ENC)
    return local


# ── receiver ───────────────────────────────────────────────────────────────────

def run_receiver(serial: str, clock_delta_us: int) -> str:
    print(f"\n[5/5] Running receiver on {serial}  (clock_delta={clock_delta_us:+d} µs) …")
    r = subprocess.run(
        ["adb", "-s", serial, "shell",
         f"{DEVICE_BIN} --receiver --in {DEVICE_ENC} --clock-delta {clock_delta_us}"],
        capture_output=True, text=True, check=True
    )
    # stderr = progress messages
    if r.stderr:
        for line in r.stderr.strip().splitlines():
            print(f"  {line}")
    return r.stdout


# ── stats ──────────────────────────────────────────────────────────────────────

def print_stats(csv_text: str):
    print("\n" + "═" * 72)
    print("DACE PSNR + One-Way Latency Report")
    print("═" * 72)

    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        print("No data.")
        return

    # Save raw CSV
    csv_path = "/tmp/dace_psnr_results.csv"
    with open(csv_path, "w") as f:
        f.write(csv_text)
    print(f"Raw CSV saved to {csv_path}\n")

    # Group by complexity level
    from collections import defaultdict
    by_cl = defaultdict(list)
    for row in rows:
        by_cl[int(row["complexity"])].append(row)

    hdr = (f"{'cl':>3}  {'frames':>6}  "
           f"{'PSNR_Y mean':>11}  {'PSNR_Y min':>10}  "
           f"{'lat mean':>9}  {'lat min':>8}  {'lat max':>8}  {'lat p95':>8}  "
           f"{'NAL mean':>8}")
    print(hdr)
    print("─" * len(hdr))

    for cl in sorted(by_cl.keys()):
        data = by_cl[cl]
        psnrs   = [float(r["psnr_y_db"])          for r in data if float(r["psnr_y_db"]) > 0]
        lats    = [int(r["corrected_latency_us"])  for r in data]
        nals    = [int(r["nal_size_b"])            for r in data]

        psnr_mean = statistics.mean(psnrs)   if psnrs else float("nan")
        psnr_min  = min(psnrs)               if psnrs else float("nan")
        lat_mean  = statistics.mean(lats)    if lats  else 0
        lat_min   = min(lats)                if lats  else 0
        lat_max   = max(lats)                if lats  else 0
        lat_p95   = sorted(lats)[int(len(lats)*0.95)] if lats else 0
        nal_mean  = statistics.mean(nals)    if nals  else 0

        print(f"{cl:>3}  {len(data):>6}  "
              f"{psnr_mean:>11.2f}  {psnr_min:>10.2f}  "
              f"{lat_mean:>9.0f}  {lat_min:>8}  {lat_max:>8}  {lat_p95:>8}  "
              f"{nal_mean:>8.0f}")

    print()
    all_lats  = [int(r["corrected_latency_us"]) for r in rows]
    all_psnrs = [float(r["psnr_y_db"]) for r in rows if float(r["psnr_y_db"]) > 0]
    print(f"Overall  PSNR_Y  mean: {statistics.mean(all_psnrs):.2f} dB   "
          f"min: {min(all_psnrs):.2f} dB")
    print(f"Overall  latency mean: {statistics.mean(all_lats):.0f} µs   "
          f"median: {statistics.median(all_lats):.0f} µs   "
          f"p95: {sorted(all_lats)[int(len(all_lats)*0.95)]:.0f} µs")
    print()


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="DACE PSNR + latency test across two Pi 5s")
    parser.add_argument("--sender",   help="ADB serial of sender Pi 5")
    parser.add_argument("--receiver", help="ADB serial of receiver Pi 5")
    parser.add_argument("--no-build", action="store_true",
                        help="Skip recompilation (use existing /tmp/dace_psnr_bench)")
    parser.add_argument("--clock-delta", type=int, default=None,
                        help="Override clock delta µs (skip clock_sync)")
    args = parser.parse_args()

    # Detect devices
    serials = get_connected_serials()
    if len(serials) < 2:
        print(f"ERROR: Need 2 ADB devices, found {len(serials)}: {serials}")
        sys.exit(1)

    s_sender   = args.sender   or serials[0]
    s_receiver = args.receiver or serials[1]
    print(f"Sender  : {s_sender}")
    print(f"Receiver: {s_receiver}")

    # Build
    if not args.no_build:
        build_binary()

    # Clock sync
    if args.clock_delta is not None:
        clock_delta = args.clock_delta
        print(f"\n[2/5] Using provided clock_delta={clock_delta:+d} µs (skipping clock_sync)")
    else:
        clock_delta = sync_clocks(s_sender, s_receiver)

    # Push binary to both
    print(f"\nPushing binary to both devices …")
    push_binary(s_sender)
    push_binary(s_receiver)

    # Sender
    run_sender(s_sender)

    # Transfer
    transfer_encoded(s_sender, s_receiver)

    # Receiver
    csv_out = run_receiver(s_receiver, clock_delta)

    # Report
    print_stats(csv_out)


if __name__ == "__main__":
    main()
