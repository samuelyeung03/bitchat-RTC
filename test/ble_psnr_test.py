#!/usr/bin/env python3
"""
ble_psnr_test.py  —  BLE BitChat mesh video PSNR + latency test

Runs one pass per complexity level (DACE auto + CL 0-5) over the real BLE
mesh between two Pi 5s, collects logcat, and produces a summary table.

Log format produced by RTCConnectionManager (tag: latency):
  Sender: SEND seq=N cl=N nal_b=N psnr=XX.XX enc_us=N ts_us=N
  Recv:   RECV seq=N nal_b=N ts_us=N

Usage:
  python3 test/ble_psnr_test.py                    # default: auto + CL0-5
  python3 test/ble_psnr_test.py --duration 20      # seconds per run
  python3 test/ble_psnr_test.py --cls -1 0 2 4     # specific CLs only
  python3 test/ble_psnr_test.py --out /tmp/myrun   # output prefix
"""

import argparse, math, os, re, statistics, subprocess, sys, threading, time

SENDER_SERIAL   = "798f51f064cce0d1"   # Pi1 — has camera
RECEIVER_SERIAL = "f501a6221ec14252"   # Pi2
RECEIVER_PEER   = "ecd39f5b07a23a13"   # Pi2 peer ID (fixed identity)
PACKAGE         = "com.bitchat.droid"
ACTIVITY        = f"{PACKAGE}/com.bitchat.android.AdbActivity"
LOG_TAG         = "latency"

# ── log patterns ──────────────────────────────────────────────────────────────
# 04-07 19:14:42.775  5735  5880 I latency : SEND seq=93 cl=2 nal_b=708 psnr=24.31 enc_us=8123 ts_us=1234567890000
SEND_RE = re.compile(
    r"(\d\d-\d\d \d\d:\d\d:\d\d\.\d+).*SEND seq=(\d+) cl=(-?\d+) nal_b=(\d+) "
    r"psnr=([\d.]+) enc_us=(\d+) ts_us=(\d+)"
)
RECV_RE = re.compile(
    r"(\d\d-\d\d \d\d:\d\d:\d\d\.\d+).*RECV seq=(\d+) nal_b=(\d+) ts_us=(\d+)"
)


# ── helpers ───────────────────────────────────────────────────────────────────

def adb(serial, *args, capture=True, timeout=15):
    return subprocess.run(["adb", "-s", serial] + list(args),
                          capture_output=capture, text=True, timeout=timeout)

def adb_cmd(serial, cmd, extras=None, timeout=10):
    args = ["shell", "am", "start", "-n", ACTIVITY, "--es", "cmd", cmd]
    for k, v in (extras or {}).items():
        flag = "--ei" if isinstance(v, int) else "--es"
        args += [flag, k, str(v)]
    adb(serial, *args, capture=True, timeout=timeout)

def sync_clocks():
    ts = time.time()
    adb(SENDER_SERIAL,   "shell", f"date -s @{ts:.3f}", timeout=5)
    adb(RECEIVER_SERIAL, "shell", f"date -s @{ts:.3f}", timeout=5)

def measure_clock_delta(n=16):
    """Return sender_offset - receiver_offset in µs (for corrected latency)."""
    def offset(serial):
        samples = []
        for _ in range(n + 2):
            t1 = time.time()
            r  = adb(serial, "shell", "date +%s%6N", timeout=5)
            t2 = time.time()
            if r.returncode or not r.stdout.strip().isdigit():
                continue
            dev = int(r.stdout.strip())
            mid = int(((t1 + t2) / 2) * 1e6)
            samples.append((int((t2 - t1) * 1e6), mid - dev))
        samples.sort()
        vals = [s[1] for s in samples[:max(1, len(samples)//4)]]
        return int(sum(vals)/len(vals)) if vals else 0
    return offset(SENDER_SERIAL) - offset(RECEIVER_SERIAL)


# ── logcat collection ─────────────────────────────────────────────────────────

def collect_logcat(serial, stop_evt, lines_out):
    proc = subprocess.Popen(
        ["adb", "-s", serial, "logcat", "-s", LOG_TAG],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    try:
        while not stop_evt.is_set():
            line = proc.stdout.readline()
            if line:
                lines_out.append(line)
            else:
                time.sleep(0.02)
    finally:
        proc.terminate()


# ── run one pass ──────────────────────────────────────────────────────────────

def wait_for_ble_mesh(timeout=30):
    """Block until both devices are peered (BluetoothMeshService is active)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = adb(SENDER_SERIAL, "shell", "logcat", "-d", "-s", "BluetoothMeshService",
                timeout=5)
        if "Sending broadcast announce" in r.stdout:
            r2 = adb(RECEIVER_SERIAL, "shell", "logcat", "-d", "-s", "BluetoothMeshService",
                     timeout=5)
            if "Sending broadcast announce" in r2.stdout:
                return True
        time.sleep(2)
    return False

def run_pass(cl, duration, clock_delta):
    """
    Start video with given CL, collect logs for `duration` seconds, stop.
    Returns (send_rows, recv_rows).
    """
    label = "auto" if cl == -1 else f"cl{cl}"

    # Re-sync clocks before each pass for accurate per-pass latency
    sync_clocks()
    time.sleep(0.3)
    clock_delta = measure_clock_delta()
    print(f"  [{label}] cl={cl}  duration={duration}s  delta={clock_delta:+d}µs")

    # Clear logcat on both
    adb(SENDER_SERIAL,   "shell", "logcat", "-c", timeout=5)
    adb(RECEIVER_SERIAL, "shell", "logcat", "-c", timeout=5)
    time.sleep(0.5)

    send_lines, recv_lines = [], []
    stop_evt = threading.Event()
    t_send = threading.Thread(target=collect_logcat,
                               args=(SENDER_SERIAL,   stop_evt, send_lines), daemon=True)
    t_recv = threading.Thread(target=collect_logcat,
                               args=(RECEIVER_SERIAL, stop_evt, recv_lines), daemon=True)
    t_send.start(); t_recv.start()
    time.sleep(0.5)

    # Start video — give BLE a few seconds to warm up before counting frames
    adb_cmd(SENDER_SERIAL, "start_video",
            extras={"peer_id": RECEIVER_PEER, "cl": cl})
    time.sleep(duration)

    # Stop video
    adb_cmd(SENDER_SERIAL, "stop_video")
    time.sleep(2.5)
    stop_evt.set()
    t_send.join(3); t_recv.join(3)

    # Parse
    send_rows, recv_rows = [], []
    for line in send_lines:
        m = SEND_RE.search(line)
        if m:
            send_rows.append({
                "seq":    int(m.group(2)),
                "cl":     int(m.group(3)),
                "nal_b":  int(m.group(4)),
                "psnr":   float(m.group(5)),
                "enc_us": int(m.group(6)),
                "ts_us":  int(m.group(7)),
            })
    for line in recv_lines:
        m = RECV_RE.search(line)
        if m:
            recv_rows.append({
                "seq":   int(m.group(2)),
                "nal_b": int(m.group(3)),
                "ts_us": int(m.group(4)),
            })

    print(f"  [{label}] parsed: {len(send_rows)} SEND  {len(recv_rows)} RECV")
    return send_rows, recv_rows


# ── stats ─────────────────────────────────────────────────────────────────────

def _stat(vals):
    vals = [v for v in vals if not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return {"n": 0, "avg": float("nan"), "min": float("nan"),
                "max": float("nan"), "p95": float("nan")}
    s = sorted(vals)
    return {"n": len(vals), "avg": statistics.mean(vals),
            "min": s[0], "max": s[-1],
            "p95": s[int(len(s)*0.95)]}

def analyse(send_rows, recv_rows, clock_delta):
    recv_by_seq = {r["seq"]: r for r in recv_rows}
    lats, psnrs, enc_uss, nal_bs = [], [], [], []
    lost = 0
    for sr in send_rows:
        rr = recv_by_seq.get(sr["seq"])
        if rr:
            lat = rr["ts_us"] - sr["ts_us"] - clock_delta
            lats.append(lat)
        else:
            lost += 1
        if sr["psnr"] > 0:
            psnrs.append(sr["psnr"])
        enc_uss.append(sr["enc_us"])
        nal_bs.append(sr["nal_b"])
    return {
        "n":       len(send_rows),
        "lost":    lost,
        "lat_us":  _stat(lats),
        "psnr":    _stat(psnrs),
        "enc_us":  _stat(enc_uss),
        "nal_b":   _stat(nal_bs),
    }


# ── save CSV ──────────────────────────────────────────────────────────────────

def save_csv(send_rows, recv_rows, clock_delta, path):
    recv_by_seq = {r["seq"]: r for r in recv_rows}
    with open(path, "w") as f:
        f.write("seq,cl,nal_b,psnr_db,enc_us,send_ts_us,recv_ts_us,lat_us\n")
        for sr in send_rows:
            rr = recv_by_seq.get(sr["seq"])
            recv_ts = rr["ts_us"] if rr else ""
            lat     = (rr["ts_us"] - sr["ts_us"] - clock_delta) if rr else ""
            f.write(f"{sr['seq']},{sr['cl']},{sr['nal_b']},{sr['psnr']:.3f},"
                    f"{sr['enc_us']},{sr['ts_us']},{recv_ts},{lat}\n")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="BLE BitChat video PSNR + latency test")
    ap.add_argument("--cls",      type=int, nargs="+", default=[-1,0,1,2,3,4,5],
                    help="DACE complexity levels to test (-1=auto, default: -1 0 1 2 3 4 5)")
    ap.add_argument("--duration", type=int, default=15,
                    help="seconds per run (default 15)")
    ap.add_argument("--out",      default="/tmp/ble_psnr",
                    help="output file prefix (default /tmp/ble_psnr)")
    args = ap.parse_args()

    print("=" * 65)
    print("  BLE BitChat mesh  —  PSNR + latency test")
    print(f"  sender  : {SENDER_SERIAL}  (Pi1, camera)")
    print(f"  receiver: {RECEIVER_SERIAL}  (Pi2)")
    print(f"  CLs     : {args.cls}")
    print(f"  duration: {args.duration}s per pass")
    print("=" * 65)

    print("\n[1] Syncing clocks …")
    sync_clocks()
    time.sleep(0.3)

    print("[2] Measuring clock delta …")
    clock_delta = measure_clock_delta()
    print(f"    delta = {clock_delta:+d} µs")

    results = {}
    for i, cl in enumerate(args.cls):
        label = "auto" if cl == -1 else f"CL{cl}"
        print(f"\n[pass {label}]")

        # Wait for BLE mesh to be stable before each pass (longer for later passes)
        settle = 5 if i == 0 else 10
        print(f"  settling {settle}s …")
        time.sleep(settle)

        send_rows, recv_rows = run_pass(cl, args.duration, clock_delta)
        results[label] = analyse(send_rows, recv_rows, clock_delta)
        save_csv(send_rows, recv_rows, clock_delta, f"{args.out}_{label}.csv")

    # ── summary table ──
    HDR = (f"\n{'Mode':<8} {'n':>4} {'lost':>4}  "
           f"{'PSNR avg':>9} {'PSNR min':>9}  "
           f"{'NAL avg':>8}  "
           f"{'Enc avg':>9}  "
           f"{'Lat avg':>9} {'Lat p95':>9} {'Lat max':>9}")
    SEP = "─" * len(HDR)
    lines = ["\n" + "═"*len(HDR), "  Results", "═"*len(HDR), HDR, SEP]

    for label, s in results.items():
        if s["n"] == 0:
            lines.append(f"{label:<8}  (no data)")
            continue
        lines.append(
            f"{label:<8} {s['n']:>4} {s['lost']:>4}  "
            f"{s['psnr']['avg']:>8.2f}dB {s['psnr']['min']:>8.2f}dB  "
            f"{s['nal_b']['avg']:>7.0f}B  "
            f"{s['enc_us']['avg']:>8.0f}µs  "
            f"{s['lat_us']['avg']:>8.0f}µs "
            f"{s['lat_us']['p95']:>8.0f}µs "
            f"{s['lat_us']['max']:>8.0f}µs"
        )

    lines += [SEP, f"\nCSVs: {args.out}_<label>.csv"]
    summary = "\n".join(lines)
    print(summary)

    summary_path = f"{args.out}_summary.txt"
    with open(summary_path, "w") as f:
        f.write(summary + "\n")
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
