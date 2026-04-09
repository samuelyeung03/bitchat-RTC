#!/usr/bin/env python3
"""
encode_bench.py  —  DACE encode-only benchmark (sender only, no BLE needed)

Starts the BitChat app on Pi1, runs one encode pass per complexity level,
and reports PSNR + encode time from the sender's logcat. No receiver or BLE
connectivity required.

Log format produced by RTCConnectionManager (tag: latency):
  SEND seq=N cl=N nal_b=N psnr=XX.XX enc_us=N ts_us=N

Usage:
  python3 test/encode_bench.py                              # default: auto + CL0-5
  python3 test/encode_bench.py --cls -1 0 1 2 3 4 5 6 7 8 9
  python3 test/encode_bench.py --duration 20
  python3 test/encode_bench.py --src /data/local/tmp/complex_320x240.yuv --w 320 --h 240
  python3 test/encode_bench.py --fps 15
  python3 test/encode_bench.py --cls -1 0 1 2 3 4 5 6 7 8 9  # full sweep
"""

import argparse, math, re, statistics, subprocess, sys, threading, time

SENDER_SERIAL = "798f51f064cce0d1"   # Pi1 — has camera + YUV files
PACKAGE       = "com.bitchat.droid"
ACTIVITY      = f"{PACKAGE}/com.bitchat.android.AdbActivity"
MAIN_ACTIVITY = f"{PACKAGE}/com.bitchat.android.MainActivity"
LOG_TAG       = "latency"
# Use receiver peer ID as destination; frames won't be delivered, that's fine.
DUMMY_PEER    = "ecd39f5b07a23a13"

SEND_RE = re.compile(
    r"SEND seq=(\d+) cl=(-?\d+) nal_b=(\d+) psnr=([\d.]+) enc_us=(\d+) ts_us=(\d+)"
)


def adb(*args, capture=True, timeout=15):
    return subprocess.run(
        ["adb", "-s", SENDER_SERIAL] + list(args),
        capture_output=capture, text=True, timeout=timeout,
    )


def adb_cmd(cmd, extras=None, timeout=10):
    args = ["shell", "am", "start", "-n", ACTIVITY, "--es", "cmd", cmd]
    for k, v in (extras or {}).items():
        flag = "--ei" if isinstance(v, int) else "--es"
        args += [flag, k, str(v)]
    adb(*args, capture=True, timeout=timeout)


def ensure_bluetooth_on(timeout=25):
    def is_on():
        r = adb("shell", "dumpsys", "bluetooth_manager", timeout=8)
        out = r.stdout or ""
        return ("state: ON" in out) and ("enabled: true" in out)

    if is_on():
        return True
    adb("shell", "cmd", "bluetooth_manager", "enable", timeout=8)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_on():
            return True
        time.sleep(1)
    return False


def ensure_service_running(retries=6):
    for _ in range(retries):
        adb("shell", "am", "start", "-n", MAIN_ACTIVITY, timeout=8)
        time.sleep(2.4)
        adb("shell", "logcat", "-c", timeout=5)
        adb_cmd("peer_id")
        time.sleep(1.2)
        r = adb("shell", "logcat", "-d", "-s", "ADB_CMD", timeout=5)
        if "PEER_ID " in (r.stdout or ""):
            return True
        time.sleep(1.6)
    return False


def collect_logcat(stop_evt, lines_out):
    proc = subprocess.Popen(
        ["adb", "-s", SENDER_SERIAL, "logcat", "-s", LOG_TAG],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
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


def run_pass(cl, duration, video_already_running, video_extras):
    label = "auto" if cl == -1 else f"cl{cl}"
    print(f"  [{label}] cl={cl}  duration={duration}s")

    adb("shell", "logcat", "-c", timeout=5)
    time.sleep(0.3)

    lines = []
    stop_evt = threading.Event()
    t = threading.Thread(target=collect_logcat, args=(stop_evt, lines), daemon=True)
    t.start()
    time.sleep(0.3)

    if video_already_running:
        adb_cmd("set_complexity", extras={"cl": cl})
    else:
        extras = {
            "peer_id": DUMMY_PEER,
            "cl": cl,
            "fps": int(video_extras.get("fps", 5)),
        }
        src = video_extras.get("src")
        if src:
            extras["src"] = str(src)
            extras["w"]   = int(video_extras.get("w", 320))
            extras["h"]   = int(video_extras.get("h", 240))
        adb_cmd("start_video", extras=extras)

    time.sleep(duration)
    stop_evt.set()
    t.join(3)

    rows = []
    for line in lines:
        m = SEND_RE.search(line)
        if m:
            rows.append({
                "seq":    int(m.group(1)),
                "cl":     int(m.group(2)),
                "nal_b":  int(m.group(3)),
                "psnr":   float(m.group(4)),
                "enc_us": int(m.group(5)),
            })
    print(f"  [{label}] parsed: {len(rows)} frames")
    return rows


def _stat(vals):
    vals = [v for v in vals if not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return {"n": 0, "avg": float("nan"), "min": float("nan"), "p95": float("nan")}
    s = sorted(vals)
    return {
        "n":   len(vals),
        "avg": statistics.mean(vals),
        "min": s[0],
        "p95": s[int(len(s) * 0.95)],
    }


def analyse(rows):
    psnrs  = [r["psnr"]   for r in rows if r["psnr"] > 0]
    enc_us = [r["enc_us"] for r in rows]
    nal_b  = [r["nal_b"]  for r in rows]
    return {
        "n":      len(rows),
        "psnr":   _stat(psnrs),
        "enc_us": _stat(enc_us),
        "nal_b":  _stat(nal_b),
    }


def save_csv(rows, path):
    with open(path, "w") as f:
        f.write("seq,cl,nal_b,psnr_db,enc_us\n")
        for r in rows:
            f.write(f"{r['seq']},{r['cl']},{r['nal_b']},{r['psnr']:.3f},{r['enc_us']}\n")


def main():
    ap = argparse.ArgumentParser(description="DACE encode-only benchmark (sender only)")
    ap.add_argument("--cls",      type=int, nargs="+", default=[-1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
                    help="DACE complexity levels (-1=auto, default: -1 0..9)")
    ap.add_argument("--duration", type=int, default=15,
                    help="seconds per pass (default 15)")
    ap.add_argument("--fps",      type=int, default=5,
                    help="target fps (default 5)")
    ap.add_argument("--src",      default=None,
                    help="device path to planar YUV420 source (default: live camera)")
    ap.add_argument("--w",        type=int, default=320,
                    help="source width when --src is used (default 320)")
    ap.add_argument("--h",        type=int, default=240,
                    help="source height when --src is used (default 240)")
    ap.add_argument("--out",      default="/tmp/encode_bench",
                    help="output file prefix (default /tmp/encode_bench)")
    args = ap.parse_args()

    print("=" * 60)
    print("  DACE encode-only benchmark")
    print(f"  sender  : {SENDER_SERIAL}  (Pi1)")
    print(f"  CLs     : {args.cls}")
    print(f"  duration: {args.duration}s per pass")
    if args.src:
        print(f"  source  : file {args.src} ({args.w}x{args.h}) @ {args.fps}fps")
    else:
        print(f"  source  : camera @ {args.fps}fps")
    print("=" * 60)

    print("\n[0] Preflight …")
    if not ensure_bluetooth_on():
        print("    Bluetooth OFF and could not be enabled — aborting")
        sys.exit(1)
    if not ensure_service_running():
        print("    BluetoothMeshService not running — aborting")
        sys.exit(1)

    video_extras = {"fps": args.fps}
    if args.src:
        video_extras.update({"src": args.src, "w": args.w, "h": args.h})

    first_pass = True
    results = {}
    for cl in args.cls:
        label = "auto" if cl == -1 else f"CL{cl}"
        print(f"\n[pass {label}]")
        time.sleep(1)

        rows = run_pass(cl, args.duration, not first_pass, video_extras)
        if not rows:
            print(f"  [{label}] no frames; retrying with fresh start_video")
            adb_cmd("stop_video")
            time.sleep(0.8)
            rows = run_pass(cl, args.duration, False, video_extras)
        first_pass = False
        results[label] = analyse(rows)
        save_csv(rows, f"{args.out}_{label}.csv")

    adb_cmd("stop_video")

    HDR = (f"\n{'Mode':<8} {'n':>5}  "
           f"{'PSNR avg':>9} {'PSNR min':>9}  "
           f"{'Enc avg':>9} {'Enc p95':>9}  "
           f"{'NAL avg':>8}")
    SEP = "─" * len(HDR)
    lines = ["\n" + "═" * len(HDR), "  Results (encode only)", "═" * len(HDR), HDR, SEP]

    for label, s in results.items():
        if s["n"] == 0:
            lines.append(f"{label:<8}  (no data)")
            continue
        lines.append(
            f"{label:<8} {s['n']:>5}  "
            f"{s['psnr']['avg']:>8.2f}dB {s['psnr']['min']:>8.2f}dB  "
            f"{s['enc_us']['avg']:>8.0f}µs {s['enc_us']['p95']:>8.0f}µs  "
            f"{s['nal_b']['avg']:>7.0f}B"
        )

    lines += [SEP, f"\nCSVs: {args.out}_<label>.csv"]
    summary = "\n".join(lines)
    print(summary)

    with open(f"{args.out}_summary.txt", "w") as f:
        f.write(summary + "\n")
    print(f"Summary: {args.out}_summary.txt")


if __name__ == "__main__":
    main()
