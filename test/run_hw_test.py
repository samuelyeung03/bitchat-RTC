#!/usr/bin/env python3
"""
run_hw_test.py — Orchestrate the DACE end-to-end hardware test.

Steps:
  1. Sync both Pi 5 CLOCK_REALTIME to host time (root required).
  2. Measure residual clock offset between the two devices.
  3. Build dace_hw_bench for arm64-android and push to both devices.
  4. Set up adb forward/reverse so Pi2 can TCP-connect to Pi1.
  5. Start receiver on Pi2, then sender on Pi1.
  6. Pull CSV results and print summary.
"""

import subprocess, sys, time, json, os, math, threading, argparse, shutil

# ── device serials ────────────────────────────────────────────────────────────
SENDER_SERIAL   = "798f51f064cce0d1"   # Pi1 — has Razer Kiyo X
RECEIVER_SERIAL = "f501a6221ec14252"   # Pi2
PORT            = 5555
DEVICE_BIN      = "/data/local/tmp/dace_hw_bench"
SAMPLES         = 30    # clock sync samples per device
WARMUP          = 5

# ── paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC        = os.path.join(SCRIPT_DIR, "dace_hw_bench.c")
BIN        = "/tmp/dace_hw_bench"

NDK_ROOT   = os.path.expanduser("~/Android/Sdk/ndk/27.2.12479018")
TC         = os.path.join(NDK_ROOT,
             "toolchains/llvm/prebuilt/linux-x86_64/bin")
X264       = "/tmp/x264-dace-arm64"

# ── helpers ───────────────────────────────────────────────────────────────────
def adb(*args, serial=None, capture=True, timeout=30):
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    r = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)
    return r

def adb_out(*args, serial=None):
    return adb(*args, serial=serial).stdout.strip()

def adb_shell(serial, cmd, **kw):
    return adb("shell", cmd, serial=serial, **kw)

# ── step 1 : sync clocks via root ─────────────────────────────────────────────
def sync_clock(serial):
    """Set CLOCK_REALTIME on device to current host time."""
    ts = time.time()
    r  = adb_shell(serial, f"date -s @{ts:.3f}")
    if r.returncode != 0:
        print(f"  [warn] date -s failed on {serial}: {r.stderr.strip()}", flush=True)
        return False
    print(f"  [{serial[:8]}] clock set to {ts:.3f}", flush=True)
    return True

# ── step 2 : measure residual offset (NTP-style, CLOCK_REALTIME) ──────────────
def measure_offset(serial, samples=SAMPLES, warmup=WARMUP):
    """
    Returns (offset_us, rtt_median_us, stdev_us).
    offset_us = host_realtime_us - device_realtime_us
    """
    offsets = []
    for i in range(samples + warmup):
        t1 = time.time()
        r  = adb_shell(serial, "date +%s%6N")
        t2 = time.time()
        if r.returncode != 0 or not r.stdout.strip().isdigit():
            continue
        t_dev = int(r.stdout.strip())   # device µs since epoch
        t1_us = int(t1 * 1e6)
        t2_us = int(t2 * 1e6)
        rtt   = t2_us - t1_us
        mid   = (t1_us + t2_us) // 2
        off   = mid - t_dev
        if i >= warmup:
            offsets.append((rtt, off))

    if not offsets:
        return 0, 0, 0

    offsets.sort()
    best = offsets[:max(1, len(offsets) // 4)]   # best 25 % by RTT
    rtts    = [o[0] for o in offsets]
    values  = [o[1] for o in best]
    mean    = sum(values) / len(values)
    var     = sum((v - mean)**2 for v in values) / len(values)
    rtt_med = sorted(rtts)[len(rtts) // 2]
    return int(mean), rtt_med, math.sqrt(var)

# ── step 3 : build ────────────────────────────────────────────────────────────
def build():
    cc = os.path.join(TC, "aarch64-linux-android26-clang")
    cmd = [
        cc, "-O2", "-o", BIN, SRC,
        f"-I{X264}/include",
        f"{X264}/lib/libx264.a",
        "-lmediandk", "-lm",
        "-Wl,-z,max-page-size=16384",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("Build failed:\n" + r.stderr)
        sys.exit(1)
    print(f"  Built: {BIN}  ({os.path.getsize(BIN)//1024} KB)", flush=True)

def push_bin(serial):
    r = adb("push", BIN, DEVICE_BIN, serial=serial)
    adb_shell(serial, f"chmod +x {DEVICE_BIN}")
    print(f"  [{serial[:8]}] pushed {DEVICE_BIN}", flush=True)

# ── step 4 : adb forward/reverse ─────────────────────────────────────────────
def setup_forwarding():
    # host:PORT → Pi1:PORT  (host connects TO Pi1's server)
    adb("forward", f"tcp:{PORT}", f"tcp:{PORT}", serial=SENDER_SERIAL)
    # Pi2:PORT  → host:PORT  (Pi2 connects TO host which proxies to Pi1)
    adb("reverse", f"tcp:{PORT}", f"tcp:{PORT}", serial=RECEIVER_SERIAL)
    print(f"  ADB forward:  host:{PORT} → Pi1:{PORT}", flush=True)
    print(f"  ADB reverse:  Pi2:{PORT}  → host:{PORT}", flush=True)

def teardown_forwarding():
    adb("forward", "--remove", f"tcp:{PORT}", serial=SENDER_SERIAL)
    adb("reverse", "--remove", f"tcp:{PORT}", serial=RECEIVER_SERIAL)

# ── step 5 : run test ─────────────────────────────────────────────────────────
def run_test(complexity, n_frames, clock_delta_us, save_frames):
    sender_csv   = "/data/local/tmp/sender.csv"
    receiver_csv = "/data/local/tmp/receiver.csv"

    sender_cmd = (
        f"{DEVICE_BIN} --sender "
        f"--complexity {complexity} --frames {n_frames} "
        f"2>/data/local/tmp/sender.log"
    )
    receiver_cmd = (
        f"{DEVICE_BIN} --receiver "
        f"--sender-ip 127.0.0.1 "
        f"--clock-delta {clock_delta_us} "
        f"--save-frames {save_frames} "
        f"2>/data/local/tmp/receiver.log"
    )

    sender_out   = []
    receiver_out = []
    sender_err   = []
    receiver_err = []

    def run_sender():
        r = adb_shell(SENDER_SERIAL, sender_cmd, timeout=120)
        sender_out.extend(r.stdout.splitlines())
        sender_err.extend(r.stderr.splitlines())

    def run_receiver():
        r = adb_shell(RECEIVER_SERIAL, receiver_cmd, timeout=120)
        receiver_out.extend(r.stdout.splitlines())
        receiver_err.extend(r.stderr.splitlines())

    # Start receiver first (it blocks on connect), then sender
    t_recv = threading.Thread(target=run_receiver, daemon=True)
    t_recv.start()
    time.sleep(1.0)   # give receiver time to start before sender connects

    t_send = threading.Thread(target=run_sender, daemon=True)
    t_send.start()

    t_send.join()
    t_recv.join()

    return sender_out, receiver_out, sender_err, receiver_err

# ── step 6 : summary ──────────────────────────────────────────────────────────
def summarise(recv_lines, clock_delta_us):
    rows = []
    for line in recv_lines:
        parts = line.split(",")
        if len(parts) < 10 or not parts[0].isdigit():
            continue
        rows.append({
            "seq":        int(parts[0]),
            "nal_b":      int(parts[1]),
            "psnr_enc":   float(parts[2]),
            "psnr_dec":   float(parts[3]),
            "dace_us":    int(parts[4]),
            "decode_us":  int(parts[5]),
            "corr_lat":   int(parts[9]),
        })
    if not rows:
        print("  No data rows.")
        return

    def avg(k): return sum(r[k] for r in rows) / len(rows)
    def mn(k):  return min(r[k] for r in rows)
    def mx(k):  return max(r[k] for r in rows)

    print(f"\n  Frames decoded  : {len(rows)}")
    print(f"  NAL size        : avg={avg('nal_b'):.0f} B  "
          f"min={mn('nal_b')} B  max={mx('nal_b')} B")
    print(f"  PSNR (encoder)  : avg={avg('psnr_enc'):.2f} dB")
    print(f"  PSNR (decoder)  : avg={avg('psnr_dec'):.2f} dB")
    print(f"  DACE enc time   : avg={avg('dace_us'):.0f} µs  "
          f"max={mx('dace_us')} µs")
    print(f"  AMediaCodec dec : avg={avg('decode_us'):.0f} µs  "
          f"max={mx('decode_us')} µs")
    print(f"  One-way latency : avg={avg('corr_lat'):.0f} µs  "
          f"min={mn('corr_lat')} µs  max={mx('corr_lat')} µs")
    print(f"  (clock delta applied: {clock_delta_us:+d} µs)")

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--complexity",   type=int, default=2)
    ap.add_argument("--frames",       type=int, default=150)
    ap.add_argument("--save-frames",  type=int, default=10)
    ap.add_argument("--skip-build",   action="store_true")
    ap.add_argument("--skip-sync",    action="store_true")
    args = ap.parse_args()

    print("═" * 60)
    print("  DACE hardware test")
    print(f"  sender  : {SENDER_SERIAL[:8]}…  (Pi1, Razer Kiyo X)")
    print(f"  receiver: {RECEIVER_SERIAL[:8]}…  (Pi2)")
    print("═" * 60)

    # ── Step 1: sync clocks ──
    if not args.skip_sync:
        print("\n[1] Syncing clocks via root …")
        sync_clock(SENDER_SERIAL)
        time.sleep(0.2)
        sync_clock(RECEIVER_SERIAL)
    else:
        print("\n[1] Skipping clock sync (--skip-sync)")

    # ── Step 2: measure residual offset ──
    print("\n[2] Measuring residual clock offset …")
    off1, rtt1, std1 = measure_offset(SENDER_SERIAL)
    off2, rtt2, std2 = measure_offset(RECEIVER_SERIAL)
    clock_delta_us   = off2 - off1   # host_us - Pi2 - (host_us - Pi1) = Pi1 - Pi2
    print(f"  Pi1 offset: {off1:+d} µs  RTT_med={rtt1} µs  ±{std1:.0f} µs")
    print(f"  Pi2 offset: {off2:+d} µs  RTT_med={rtt2} µs  ±{std2:.0f} µs")
    print(f"  Clock delta Pi1→Pi2: {clock_delta_us:+d} µs")

    # ── Step 3: build ──
    if not args.skip_build:
        print(f"\n[3] Building dace_hw_bench …")
        build()
        print(f"\n[4] Pushing binary …")
        push_bin(SENDER_SERIAL)
        push_bin(RECEIVER_SERIAL)
    else:
        print("\n[3/4] Skipping build/push (--skip-build)")

    # ── Step 4: adb forwarding ──
    print(f"\n[5] Setting up ADB TCP forwarding …")
    setup_forwarding()

    # ── Step 5: run ──
    print(f"\n[6] Running test  "
          f"(complexity={args.complexity}  frames={args.frames}) …")
    t0 = time.time()
    sender_out, receiver_out, sender_err, receiver_err = run_test(
        args.complexity, args.frames, clock_delta_us, args.save_frames)
    elapsed = time.time() - t0
    print(f"  Done in {elapsed:.1f} s")

    # ── Step 6: summary ──
    print("\n[7] Results summary:")
    summarise(receiver_out, clock_delta_us)

    # Print sender/receiver stderr for debug
    if sender_err:
        print("\n  -- sender log --")
        for l in sender_err: print("  " + l)
    if receiver_err:
        print("\n  -- receiver log --")
        for l in receiver_err: print("  " + l)

    # Save full CSV
    csv_path = "/tmp/dace_hw_results.csv"
    with open(csv_path, "w") as f:
        f.write("\n".join(receiver_out))
    print(f"\n  Full CSV: {csv_path}")

    teardown_forwarding()

if __name__ == "__main__":
    main()
