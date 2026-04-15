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
    python3 test/ble_psnr_test.py --src /sdcard/Download/complex_1920x1080.yuv --w 1920 --h 1080
"""

import argparse, math, os, re, statistics, subprocess, sys, threading, time

SENDER_SERIAL   = "798f51f064cce0d1"   # Pi1 — has camera
RECEIVER_SERIAL = "f501a6221ec14252"   # Pi2
RECEIVER_PEER   = "ecd39f5b07a23a13"   # Pi2 peer ID (fixed identity)
PACKAGE         = "com.bitchat.droid"
ACTIVITY        = f"{PACKAGE}/com.bitchat.android.AdbActivity"
MAIN_ACTIVITY   = f"{PACKAGE}/com.bitchat.android.MainActivity"
LOG_TAG         = "latency"

# ── log patterns ──────────────────────────────────────────────────────────────
# SEND seq=93 cl=2 nal_b=708 psnr=24.31 ssim=0.8512 enc_us=8123 ts_us=1234567890000
# ssim field is optional (not present in older builds)
SEND_RE = re.compile(
    r"(\d\d-\d\d \d\d:\d\d:\d\d\.\d+).*SEND seq=(\d+) cl=(-?\d+) nal_b=(\d+) "
    r"psnr=([\d.]+)(?:\s+ssim=([\d.]+))?\s+enc_us=(\d+) ts_us=(\d+)"
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

def ensure_bluetooth_on(serial, timeout=25):
    """Ensure Bluetooth is ON; attempt to enable if OFF."""
    def is_on():
        r = adb(serial, "shell", "dumpsys", "bluetooth_manager", timeout=8)
        out = r.stdout or ""
        return ("state: ON" in out) and ("enabled: true" in out)

    if is_on():
        return True

    adb(serial, "shell", "cmd", "bluetooth_manager", "enable", timeout=8)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_on():
            return True
        time.sleep(1)
    return False

def ensure_mesh_service_running(serial, retries=6):
    """Ensure BluetoothMeshService exists by probing AdbActivity peer_id."""
    for _ in range(retries):
        adb(serial, "shell", "am", "start", "-n", MAIN_ACTIVITY, timeout=8)
        # MainActivity initializes mesh asynchronously; allow warm-up time.
        time.sleep(2.4)
        adb(serial, "shell", "logcat", "-c", timeout=5)
        adb_cmd(serial, "peer_id")
        time.sleep(1.2)
        r = adb(serial, "shell", "logcat", "-d", "-s", "ADB_CMD", timeout=5)
        out = r.stdout or ""
        if "PEER_ID " in out:
            return True
        time.sleep(1.6)
    return False

def get_peer_id(serial, retries=6):
    """Read PEER_ID from device logcat via AdbActivity."""
    for _ in range(retries):
        adb(serial, "shell", "am", "start", "-n", MAIN_ACTIVITY, timeout=8)
        time.sleep(2.0)
        adb(serial, "shell", "logcat", "-c", timeout=5)
        adb_cmd(serial, "peer_id")
        time.sleep(1.2)
        r = adb(serial, "shell", "logcat", "-d", "-s", "ADB_CMD", timeout=5)
        m = re.search(r"PEER_ID\s+([0-9a-fA-F]+)", r.stdout or "")
        if m:
            return m.group(1).lower()
        time.sleep(1.4)
    return None

def sync_clocks():
    ts = time.time()
    adb(SENDER_SERIAL,   "shell", f"date -s @{ts:.3f}", timeout=5)
    adb(RECEIVER_SERIAL, "shell", f"date -s @{ts:.3f}", timeout=5)

def measure_clock_delta(n=16):
    """Return sender_offset - receiver_offset in µs (for corrected latency)."""
    def offset(serial):
        samples = []
        host_now_us = int(time.time() * 1e6)
        for _ in range(n + 2):
            t1 = time.time()
            r  = adb(serial, "shell", "date +%s%6N", timeout=5)
            t2 = time.time()
            raw = r.stdout.strip()
            if r.returncode or not raw.isdigit():
                continue
            dev = int(raw)
            # Sanity: device time should be within 5 minutes of host
            if abs(dev - host_now_us) > 5 * 60 * 1_000_000:
                continue
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

def wait_for_ble_peer(receiver_peer, timeout=60):
    """Block until Pi1 has Pi2 in its verified peer list."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        adb(SENDER_SERIAL, "shell", "logcat", "-c", timeout=5)
        time.sleep(0.3)
        adb_cmd(SENDER_SERIAL, "peers")
        time.sleep(1)
        r = adb(SENDER_SERIAL, "shell", "logcat", "-d", "-s", "ADB_CMD", timeout=5)
        if receiver_peer in (r.stdout or ""):
            return True
        time.sleep(3)
    return False

def run_pass(cl, duration, clock_delta, receiver_peer,
             video_already_running=False, video_extras=None):
    """
    Set CL (or start video if not running), collect logs for `duration` s.
    Returns (send_rows, recv_rows, clock_delta_used).
    """
    label = "auto" if cl == -1 else f"cl{cl}"

    # Re-sync clocks before each pass
    sync_clocks()
    time.sleep(0.2)
    clock_delta = measure_clock_delta()
    print(f"  [{label}] cl={cl}  duration={duration}s  delta={clock_delta:+d}µs")

    # Clear logcat on both
    adb(SENDER_SERIAL,   "shell", "logcat", "-c", timeout=5)
    adb(RECEIVER_SERIAL, "shell", "logcat", "-c", timeout=5)
    time.sleep(0.4)

    send_lines, recv_lines = [], []
    stop_evt = threading.Event()
    t_send = threading.Thread(target=collect_logcat,
                               args=(SENDER_SERIAL,   stop_evt, send_lines), daemon=True)
    t_recv = threading.Thread(target=collect_logcat,
                               args=(RECEIVER_SERIAL, stop_evt, recv_lines), daemon=True)
    t_send.start(); t_recv.start()
    time.sleep(0.4)

    video_extras = video_extras or {}

    if video_already_running:
        # Just change CL — no stop/start, keeps BLE connection stable
        adb_cmd(SENDER_SERIAL, "set_complexity", extras={"cl": cl})
    else:
        extras = {
            "peer_id": receiver_peer,
            "cl": cl,
            "fps": int(video_extras.get("fps", 5)),
            "bitrate": int(video_extras.get("bitrate", 40000)),
        }
        src = video_extras.get("src")
        if src:
            extras["src"] = str(src)
            extras["w"] = int(video_extras.get("w", 320))
            extras["h"] = int(video_extras.get("h", 240))
        adb_cmd(SENDER_SERIAL, "start_video",
                extras=extras)

    time.sleep(duration)
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
                "ssim":   float(m.group(6)) if m.group(6) else None,
                "enc_us": int(m.group(7)),
                "ts_us":  int(m.group(8)),
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
    return send_rows, recv_rows, clock_delta


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
    lats, psnrs, ssims, enc_uss, nal_bs = [], [], [], [], []
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
        if sr.get("ssim") is not None and sr["ssim"] > 0:
            ssims.append(sr["ssim"])
        enc_uss.append(sr["enc_us"])
        nal_bs.append(sr["nal_b"])
    return {
        "n":       len(send_rows),
        "lost":    lost,
        "lat_us":  _stat(lats),
        "psnr":    _stat(psnrs),
        "ssim":    _stat(ssims),
        "enc_us":  _stat(enc_uss),
        "nal_b":   _stat(nal_bs),
    }


# ── save CSV ──────────────────────────────────────────────────────────────────

def save_csv(send_rows, recv_rows, clock_delta, path):
    recv_by_seq = {r["seq"]: r for r in recv_rows}
    with open(path, "w") as f:
        f.write("seq,cl,nal_b,psnr_db,ssim,enc_us,send_ts_us,recv_ts_us,lat_us\n")
        for sr in send_rows:
            rr = recv_by_seq.get(sr["seq"])
            recv_ts = rr["ts_us"] if rr else ""
            lat     = (rr["ts_us"] - sr["ts_us"] - clock_delta) if rr else ""
            ssim_v  = f"{sr['ssim']:.4f}" if sr.get("ssim") is not None else ""
            f.write(f"{sr['seq']},{sr['cl']},{sr['nal_b']},{sr['psnr']:.3f},{ssim_v},"
                    f"{sr['enc_us']},{sr['ts_us']},{recv_ts},{lat}\n")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="BLE BitChat video PSNR + latency test")
    ap.add_argument("--cls",      type=int, nargs="+", default=[-1,0,1,2,3,4,5,6,7,8,9],
                    help="DACE complexity levels to test (-1=auto, default: -1 0..9)")
    ap.add_argument("--duration", type=int, default=15,
                    help="seconds per run (default 15)")
    ap.add_argument("--fps",      type=int, default=3,
                    help="target fps for video source (default 3)")
    ap.add_argument("--bitrate",  type=int, default=40000,
                    help="encoder bitrate bps (default 40000)")
    ap.add_argument("--src",      default=None,
                    help="sender device path to planar YUV420 source file")
    ap.add_argument("--w",        type=int, default=320,
                    help="source width for --src (default 320)")
    ap.add_argument("--h",        type=int, default=240,
                    help="source height for --src (default 240)")
    ap.add_argument("--out",      default="/tmp/ble_psnr",
                    help="output file prefix (default /tmp/ble_psnr)")
    args = ap.parse_args()

    if args.fps <= 0:
        print("ERROR: --fps must be > 0")
        sys.exit(1)
    if args.src and (args.w <= 0 or args.h <= 0 or args.w % 2 != 0 or args.h % 2 != 0):
        print("ERROR: --w/--h must be positive even numbers when --src is used")
        sys.exit(1)

    print("=" * 65)
    print("  BLE BitChat mesh  —  PSNR + latency test")
    print(f"  sender  : {SENDER_SERIAL}  (Pi1, camera)")
    print(f"  receiver: {RECEIVER_SERIAL}  (Pi2)")
    print(f"  CLs     : {args.cls}")
    print(f"  duration: {args.duration}s per pass")
    if args.src:
        print(f"  source  : file {args.src} ({args.w}x{args.h}) @ {args.fps}fps")
    else:
        print(f"  source  : camera @ {args.fps}fps")
    print("=" * 65)

    print("\n[0] Preflight Bluetooth + app service …")
    if not ensure_bluetooth_on(SENDER_SERIAL):
        print("    sender Bluetooth is OFF and could not be enabled — aborting")
        sys.exit(1)
    if not ensure_bluetooth_on(RECEIVER_SERIAL):
        print("    receiver Bluetooth is OFF and could not be enabled — aborting")
        sys.exit(1)
    if not ensure_mesh_service_running(SENDER_SERIAL):
        print("    sender BluetoothMeshService not running — aborting")
        sys.exit(1)
    if not ensure_mesh_service_running(RECEIVER_SERIAL):
        print("    receiver BluetoothMeshService not running — aborting")
        sys.exit(1)

    receiver_peer = get_peer_id(RECEIVER_SERIAL) or RECEIVER_PEER
    print(f"    receiver peer = {receiver_peer}")

    print("\n[1] Syncing clocks (rooting if needed) …")
    for s in [SENDER_SERIAL, RECEIVER_SERIAL]:
        adb(s, "root", timeout=6)
    time.sleep(2)
    sync_clocks()
    time.sleep(0.5)

    print("[2] Measuring clock delta …")
    clock_delta = measure_clock_delta()
    print(f"    delta = {clock_delta:+d} µs")

    # Ensure BLE is up before starting
    print("\n[connecting] waiting for BLE peer …", end=" ", flush=True)
    if not wait_for_ble_peer(receiver_peer, timeout=60):
        print("TIMEOUT — aborting"); sys.exit(1)
    print("connected")

    video_extras = {"fps": args.fps, "bitrate": args.bitrate}
    if args.src:
        video_extras.update({"src": args.src, "w": args.w, "h": args.h})

    # Start video once on first CL, then only change CL between passes
    first_pass = True
    results = {}
    for i, cl in enumerate(args.cls):
        label = "auto" if cl == -1 else f"CL{cl}"
        print(f"\n[pass {label}]")
        time.sleep(2)

        send_rows, recv_rows, pass_delta = run_pass(
            cl,
            args.duration,
            clock_delta,
            receiver_peer,
            video_already_running=(not first_pass),
            video_extras=video_extras,
        )
        if len(send_rows) == 0:
            print(f"  [{label}] no SEND rows; retrying with fresh start_video")
            adb_cmd(SENDER_SERIAL, "stop_video")
            time.sleep(0.8)
            send_rows, recv_rows, pass_delta = run_pass(
                cl,
                args.duration,
                clock_delta,
                receiver_peer,
                video_already_running=False,
                video_extras=video_extras,
            )
        first_pass = False
        results[label] = analyse(send_rows, recv_rows, pass_delta)
        save_csv(send_rows, recv_rows, pass_delta, f"{args.out}_{label}.csv")

    # Stop video after all passes
    adb_cmd(SENDER_SERIAL, "stop_video")

    # ── summary table ──
    HDR = (f"\n{'Mode':<8} {'n':>4} {'lost':>4}  "
           f"{'PSNR avg':>9} {'PSNR min':>9}  {'SSIM avg':>9}  "
           f"{'NAL avg':>8}  "
           f"{'Enc avg':>9}  "
           f"{'Lat avg':>9} {'Lat p95':>9} {'Lat max':>9}")
    SEP = "─" * len(HDR)
    lines = ["\n" + "═"*len(HDR), "  Results", "═"*len(HDR), HDR, SEP]

    for label, s in results.items():
        if s["n"] == 0:
            lines.append(f"{label:<8}  (no data)")
            continue
        ssim_avg = s['ssim']['avg'] if s['ssim']['n'] > 0 else float('nan')
        ssim_str = f"{ssim_avg:.4f}" if not (ssim_avg != ssim_avg) else "  n/a  "
        lines.append(
            f"{label:<8} {s['n']:>4} {s['lost']:>4}  "
            f"{s['psnr']['avg']:>8.2f}dB {s['psnr']['min']:>8.2f}dB  {ssim_str:>9}  "
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
