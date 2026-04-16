#!/usr/bin/env python3
"""
ble_dace_test.py — Run DACE ON vs OFF PSNR/SSIM + throughput test over BLE mesh.

Usage:
  python3 test/ble_dace_test.py                        # DACE ON then OFF, 60s each
  python3 test/ble_dace_test.py --duration 30          # 30s each
  python3 test/ble_dace_test.py --cls -1 0 1 2         # custom CL list
  python3 test/ble_dace_test.py --fps 5 --bitrate 60000
  python3 test/ble_dace_test.py --throughput           # throughput-only test
  python3 test/ble_dace_test.py --cls -1 0 --throughput  # PSNR + throughput
  python3 test/ble_dace_test.py --sender T1AIOC656909KGK --receiver 8e27af28

Devices (defaults):
  Sender  : T1AIOC656909KGK  (ASUS ROG9)
  Receiver: 8e27af28          (Xiaomi 12, peer fea25dd05ccc26a6)

CL mapping:
  cl=0   → DACE OFF (param.dace=0, plain x264)
  cl=-1  → DACE ON auto (self-regulates complexity)
  cl=1-9 → DACE ON fixed complexity level

Metrics reported separately:
  Encoded bitrate  : x264 output kbps  (nal_b × 8 / duration)
  BLE link sender  : GATT write kbps   (BLE_THROUGHPUT:I tag)
  BLE link receiver: GATT recv kbps    (BLE_TPUT_RECV tag)
"""

import argparse
import collections
import os
import re
import statistics
import subprocess
import sys
import time

# ── defaults ──────────────────────────────────────────────────────────────────
SENDER          = "T1AIOC656909KGK"
RECEIVER        = "8e27af28"
RECEIVER_PEER   = "fea25dd05ccc26a6"   # Xiaomi 12 peer ID
SRC_PATH        = "/data/local/tmp/complex_320x240.yuv"
SRC_W, SRC_H    = 320, 240
DEFAULT_FPS     = 3
DEFAULT_BITRATE = 40000
DEFAULT_DUR     = 60
DEFAULT_CLS     = [-1, 0]              # DACE ON auto, DACE OFF
PACKAGE         = "com.bitchat.droid"
MAIN_ACTIVITY   = f"{PACKAGE}/com.bitchat.android.MainActivity"
CMD_ACTION      = "com.bitchat.droid.CMD"

# ── helpers ───────────────────────────────────────────────────────────────────

def adb(serial, *args, check=False, capture=True, timeout=15):
    cmd = ["adb", "-s", serial] + list(args)
    r = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"adb {args} on {serial} failed:\n{r.stderr.strip()}")
    return r.stdout.strip() if capture else None


def broadcast(serial, **extras):
    """Send am broadcast to the running mesh service (no process spawn)."""
    cmd = ["shell", "am", "broadcast", "-a", CMD_ACTION]
    for k, v in extras.items():
        if isinstance(v, bool):
            cmd += ["--ez", k, "true" if v else "false"]
        elif isinstance(v, int):
            cmd += ["--ei", k, str(v)]
        else:
            cmd += ["--es", k, str(v)]
    adb(serial, *cmd, capture=False, timeout=10)


def wake_screen(serial):
    adb(serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP",
        capture=False, timeout=5)


def bring_to_foreground(serial):
    wake_screen(serial)
    adb(serial, "shell", "am", "start", "-n", MAIN_ACTIVITY,
        capture=False, timeout=8)
    time.sleep(0.8)
    wake_screen(serial)


def grant_bt_permissions(serial):
    perms = [
        "BLUETOOTH_ADVERTISE", "BLUETOOTH_CONNECT", "BLUETOOTH_SCAN",
        "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION",
        "POST_NOTIFICATIONS", "RECORD_AUDIO", "CAMERA",
    ]
    for p in perms:
        subprocess.run(
            ["adb", "-s", serial, "shell", "pm", "grant", PACKAGE,
             f"android.permission.{p}"],
            capture_output=True, timeout=10
        )


def get_peer_id(serial, retries=6):
    for _ in range(retries):
        adb(serial, "shell", "logcat", "-c", timeout=5)
        broadcast(serial, cmd="peer_id")
        time.sleep(2.5)
        out = adb(serial, "shell", "logcat", "-d", "-s", "ADB_CMD:I", timeout=5)
        m = re.search(r"PEER_ID\s+([0-9a-fA-F]+)", out)
        if m:
            return m.group(1).lower()
    return None


def get_connected_peers(serial, retries=6):
    for _ in range(retries):
        adb(serial, "shell", "logcat", "-c", timeout=5)
        broadcast(serial, cmd="peers")
        time.sleep(2.5)
        out = adb(serial, "shell", "logcat", "-d", "-s", "ADB_CMD:I", timeout=5)
        peers = re.findall(r"PEER id=([0-9a-fA-F]+)", out)
        if peers:
            return peers
    return []


# ── log patterns ──────────────────────────────────────────────────────────────

SEND_RE = re.compile(
    r"SEND seq=(\d+) cl=(-?\d+) nal_b=(\d+) psnr=([\d.]+) ssim=([\d.]+) "
    r"enc_us=(\d+) ts_us=(\d+)"
)
RECV_RE      = re.compile(r"RECV seq=(\d+) nal_b=(\d+) ts_us=(\d+)")
RECV_FAIL_RE = re.compile(r"RECV_FAIL seq=(\d+) nal_b=(\d+) ts_us=(\d+)")
TPUT_RECV_RE = re.compile(r"RECV_TPUT nal_b=(\d+) ts_us=(\d+)")
BLE_TPUT_RE  = re.compile(r"kbps=([\d.]+)")


def parse_send(logcat_text):
    rows = []
    for m in SEND_RE.finditer(logcat_text):
        rows.append({
            "seq":    int(m.group(1)),
            "cl":     int(m.group(2)),
            "nal_b":  int(m.group(3)),
            "psnr":   float(m.group(4)),
            "ssim":   float(m.group(5)),
            "enc_us": int(m.group(6)),
            "ts_us":  int(m.group(7)),
        })
    return rows


def parse_recv(logcat_text):
    rows = []
    for m in RECV_RE.finditer(logcat_text):
        rows.append({"seq": int(m.group(1)), "nal_b": int(m.group(2)),
                     "ts_us": int(m.group(3))})
    return rows


def parse_recv_fail(logcat_text):
    return [{"seq": int(m.group(1)), "nal_b": int(m.group(2)),
             "ts_us": int(m.group(3))}
            for m in RECV_FAIL_RE.finditer(logcat_text)]


def parse_tput_recv(logcat_text):
    """Returns list of (nal_b, ts_us) for receiver-side throughput calculation."""
    return [(int(m.group(1)), int(m.group(2)))
            for m in TPUT_RECV_RE.finditer(logcat_text)]


def encoded_bitrate_kbps(send_rows):
    """Compute actual encoded bitrate from nal_b sum over elapsed time."""
    if len(send_rows) < 2:
        return 0.0
    ts_sorted = sorted(r["ts_us"] for r in send_rows)
    elapsed_s = (ts_sorted[-1] - ts_sorted[0]) / 1e6
    if elapsed_s <= 0:
        return 0.0
    total_bits = sum(r["nal_b"] for r in send_rows) * 8
    return total_bits / elapsed_s / 1000.0  # kbps


def recv_bitrate_kbps(tput_recv_rows):
    """Compute receiver-side bitrate from RECV_TPUT log entries."""
    if len(tput_recv_rows) < 2:
        return 0.0
    ts_sorted = sorted(ts for _, ts in tput_recv_rows)
    elapsed_s = (ts_sorted[-1] - ts_sorted[0]) / 1e6
    if elapsed_s <= 0:
        return 0.0
    total_bits = sum(nb for nb, _ in tput_recv_rows) * 8
    return total_bits / elapsed_s / 1000.0


def stat(vals):
    if not vals:
        return {"n": 0, "avg": float("nan"), "min": float("nan"), "max": float("nan")}
    s = sorted(vals)
    return {"n": len(vals), "avg": statistics.mean(vals), "min": s[0], "max": s[-1],
            "p95": s[int(len(s) * 0.95)]}


def summarise(send_rows, recv_rows, recv_fail_rows=None, tput_recv_rows=None,
              ble_tput_kbps=0.0):
    recv_by_seq = {r["seq"]: r for r in recv_rows}
    ss = [r for r in send_rows if r["seq"] >= 2]  # skip IDR + post-IDR correction
    psnrs   = [r["psnr"]   for r in ss]
    ssims   = [r["ssim"]   for r in ss]
    nals    = [r["nal_b"]  for r in ss]
    enc_uss = [r["enc_us"] for r in send_rows]
    lats    = []
    for r in send_rows:
        rr = recv_by_seq.get(r["seq"])
        if rr:
            lats.append(rr["ts_us"] - r["ts_us"])
    return {
        "send":         len(send_rows),
        "recv":         len(recv_rows),
        "recv_fail":    len(recv_fail_rows) if recv_fail_rows else 0,
        "psnr_ss":      stat(psnrs),
        "ssim_ss":      stat(ssims),
        "nal_ss":       stat(nals),
        "enc_us":       stat(enc_uss),
        "lat_us":       stat(lats),
        "enc_kbps":     encoded_bitrate_kbps(send_rows),   # x264 encoded output
        "ble_kbps":     ble_tput_kbps,                      # BLE link (sender GATT writes)
        "recv_kbps":    recv_bitrate_kbps(tput_recv_rows) if tput_recv_rows else 0.0,  # receiver side
    }


# ── test runner ───────────────────────────────────────────────────────────────

def run_pass(sender, receiver, receiver_peer, cl, fps, bitrate, duration, src, w, h,
             throughput_mode=False):
    label = "DACE OFF" if cl == 0 else ("DACE ON auto" if cl == -1 else f"DACE ON cl={cl}")
    if throughput_mode:
        label = f"THROUGHPUT {label}"
    print(f"\n  [{label}] cl={cl}  {duration}s  {fps}fps  {bitrate}bps")

    adb(sender,   "shell", "logcat", "-c", timeout=5)
    adb(receiver, "shell", "logcat", "-c", timeout=5)
    time.sleep(0.3)

    bring_to_foreground(sender)
    extras = dict(cmd="start_video", peer_id=receiver_peer,
                  cl=cl, fps=fps, bitrate=bitrate, w=w, h=h)
    if src:
        extras["src"] = src
    broadcast(sender, **extras)

    t0 = time.time()
    while time.time() - t0 < duration:
        time.sleep(10)
        wake_screen(sender)
        elapsed = time.time() - t0
        send_now = adb(sender, "shell", "logcat", "-d", "-s", "latency:I",
                       timeout=5).count("SEND seq")
        tput_lines = adb(sender, "shell", "logcat", "-d", "-s", "BLE_THROUGHPUT:I",
                         timeout=5).strip().splitlines()
        kbps = "n/a"
        if tput_lines:
            m = BLE_TPUT_RE.search(tput_lines[-1])
            if m:
                kbps = m.group(1)
        print(f"    {elapsed:4.0f}s  SEND={send_now}  ble_link={kbps}kbps", flush=True)

    broadcast(sender, cmd="stop_video")
    time.sleep(2)

    send_log      = adb(sender,   "shell", "logcat", "-d", "-s", "latency:I",      timeout=8)
    recv_log      = adb(receiver, "shell", "logcat", "-d", "-s", "latency:I",      timeout=8)
    tput_send_log = adb(sender,   "shell", "logcat", "-d", "-s", "BLE_THROUGHPUT:I", timeout=5)
    tput_recv_log = adb(receiver, "shell", "logcat", "-d", "-s", "BLE_TPUT_RECV:I",  timeout=5)

    send_rows      = parse_send(send_log)
    recv_rows      = parse_recv(recv_log)
    recv_fail_rows = parse_recv_fail(recv_log)
    tput_recv_rows = parse_tput_recv(tput_recv_log)
    ble_kbps_vals  = [float(m.group(1)) for m in BLE_TPUT_RE.finditer(tput_send_log)]
    ble_tput_avg   = statistics.mean(ble_kbps_vals) if ble_kbps_vals else 0.0

    s = summarise(send_rows, recv_rows, recv_fail_rows, tput_recv_rows, ble_tput_avg)
    s["cl"] = cl
    s["label"] = label
    return s, send_rows, recv_rows


# ── throughput summary printer ────────────────────────────────────────────────

def print_throughput_summary(results_list):
    print("\n" + "═" * 72)
    print("  THROUGHPUT RESULTS")
    print("═" * 72)
    hdr = (f"{'Mode':<18} {'SEND':>5} {'RECV':>5} {'deliv%':>7}  "
           f"{'enc kbps':>9} {'ble snd':>8} {'ble rcv':>8}  {'lat avg':>8} {'lat p95':>8}")
    print(hdr)
    print("─" * 72)
    for s in results_list:
        delivery = 100.0 * s["recv"] / s["send"] if s["send"] > 0 else 0.0
        lat_avg  = s["lat_us"]["avg"] / 1000 if s["lat_us"]["n"] > 0 else float("nan")
        lat_p95  = s["lat_us"].get("p95", float("nan")) / 1000 if s["lat_us"]["n"] > 0 else float("nan")
        print(
            f"{s['label']:<18} {s['send']:>5} {s['recv']:>5} {delivery:>6.1f}%  "
            f"{s['enc_kbps']:>8.1f} {s['ble_kbps']:>8.1f} {s['recv_kbps']:>8.1f}  "
            f"{lat_avg:>7.1f}ms {lat_p95:>7.1f}ms"
        )
    print("═" * 72)
    print("  enc kbps = x264 encoded output (from nal_b sum)")
    print("  ble snd  = BLE link sender kbps (GATT write bytes)")
    print("  ble rcv  = BLE link receiver kbps (received bytes)")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="BLE DACE ON vs OFF PSNR/SSIM + throughput test")
    ap.add_argument("--sender",      default=SENDER)
    ap.add_argument("--receiver",    default=RECEIVER)
    ap.add_argument("--peer",        default=None,
                    help="Receiver peer ID hex (auto-detected if omitted)")
    ap.add_argument("--cls",   type=int, nargs="+", default=DEFAULT_CLS,
                    help="CL values: 0=DACE OFF, -1=auto, 1-9=fixed (default: -1 0)")
    ap.add_argument("--duration",    type=int, default=DEFAULT_DUR,
                    help=f"Seconds per PSNR pass (default {DEFAULT_DUR})")
    ap.add_argument("--fps",         type=int, default=DEFAULT_FPS)
    ap.add_argument("--bitrate",     type=int, default=DEFAULT_BITRATE, help="bps")
    ap.add_argument("--src",         default=SRC_PATH)
    ap.add_argument("--w",           type=int, default=SRC_W)
    ap.add_argument("--h",           type=int, default=SRC_H)
    ap.add_argument("--out",         default="/tmp/ble_dace",
                    help="Output CSV/txt prefix (default /tmp/ble_dace)")
    ap.add_argument("--no-push",     action="store_true",
                    help="Skip pushing YUV file to sender")
    ap.add_argument("--throughput",  action="store_true",
                    help="Also run a dedicated throughput pass (cl=0, fps=15, bitrate=200kbps)")
    ap.add_argument("--tput-duration", type=int, default=30,
                    help="Duration of throughput pass in seconds (default 30)")
    args = ap.parse_args()

    sender   = args.sender
    receiver = args.receiver

    print("═" * 66)
    print(f"  BLE DACE test  sender={sender}  receiver={receiver}")
    print(f"  CLs={args.cls}  dur={args.duration}s  fps={args.fps}  bitrate={args.bitrate}bps"
          + ("  +throughput" if args.throughput else ""))
    print("═" * 66)

    # Push YUV file
    if not args.no_push and args.src == SRC_PATH:
        local_yuv = "test/media/complex_320x240.yuv"
        if os.path.exists(local_yuv):
            print(f"\n[setup] Pushing {local_yuv} → {SRC_PATH} …")
            subprocess.run(["adb", "-s", sender, "push", local_yuv, SRC_PATH],
                           check=True, timeout=120)
        else:
            print("[setup] YUV not found locally, assuming already on device.")

    # Permissions + start
    print("[setup] Granting BT permissions…")
    grant_bt_permissions(sender)
    grant_bt_permissions(receiver)

    print("[setup] Starting apps…")
    for s in [sender, receiver]:
        adb(s, "shell", "am", "force-stop", PACKAGE, capture=False, timeout=8)
        adb(s, "shell", "settings", "put", "system", "screen_off_timeout", "600000",
            capture=False, timeout=5)
    time.sleep(1)
    for s in [sender, receiver]:
        adb(s, "shell", "am", "start", "-n", MAIN_ACTIVITY, capture=False, timeout=8)
        wake_screen(s)

    print("[setup] Waiting 30s for BLE peer discovery…", end=" ", flush=True)
    time.sleep(30)

    # Peer ID
    receiver_peer = args.peer
    if not receiver_peer:
        print("\n[setup] Discovering receiver peer ID…")
        peers = get_connected_peers(sender)
        if peers:
            receiver_peer = peers[0]
            print(f"  Found: {receiver_peer}")
        else:
            bring_to_foreground(receiver)
            time.sleep(2)
            receiver_peer = get_peer_id(receiver)
            if receiver_peer:
                print(f"  Receiver self-reports: {receiver_peer}")
            else:
                print("\nERROR: Cannot find receiver peer ID. Are both apps running?")
                sys.exit(1)
    else:
        print(f"connected\n  Using: {receiver_peer}")

    bring_to_foreground(sender)
    peers = get_connected_peers(sender)
    if receiver_peer not in peers:
        print(f"\nWARNING: Sender doesn't see {receiver_peer} yet. Waiting 15s…")
        time.sleep(15)

    # ── PSNR/SSIM passes ──────────────────────────────────────────────────────
    results   = {}
    tput_results = []

    for cl in args.cls:
        s, send_rows, recv_rows = run_pass(
            sender, receiver, receiver_peer,
            cl, args.fps, args.bitrate, args.duration,
            args.src, args.w, args.h
        )
        results[cl] = s

        csv_path = f"{args.out}_cl{cl}.csv"
        recv_by_seq = {r["seq"]: r for r in recv_rows}
        with open(csv_path, "w") as f:
            f.write("seq,cl,nal_b,psnr_db,ssim,enc_us,recv_ts_us,lat_us\n")
            for r in send_rows:
                rr = recv_by_seq.get(r["seq"])
                recv_ts = rr["ts_us"] if rr else ""
                lat     = (rr["ts_us"] - r["ts_us"]) if rr else ""
                f.write(f"{r['seq']},{r['cl']},{r['nal_b']},{r['psnr']:.3f},"
                        f"{r['ssim']:.4f},{r['enc_us']},{recv_ts},{lat}\n")
        print(f"  CSV → {csv_path}")

        if cl != args.cls[-1]:
            time.sleep(5)
            bring_to_foreground(sender)
            time.sleep(3)

    # ── Throughput pass ───────────────────────────────────────────────────────
    if args.throughput:
        time.sleep(5)
        bring_to_foreground(sender)
        time.sleep(3)
        s, _, _ = run_pass(
            sender, receiver, receiver_peer,
            cl=0, fps=15, bitrate=200000, duration=args.tput_duration,
            src=args.src, w=args.w, h=args.h, throughput_mode=True
        )
        tput_results.append(s)

    # ── PSNR/SSIM summary table ───────────────────────────────────────────────
    if results:
        print("\n" + "═" * 80)
        print("  PSNR/SSIM RESULTS  (ss = steady-state, skip IDR + correction frames)")
        print("═" * 80)
        hdr = (f"{'Mode':<18} {'SEND':>5} {'RECV':>5} {'fail':>5}  "
               f"{'enc kbps':>9} {'ble kbps':>9} {'rcv kbps':>9}  "
               f"{'PSNR ss':>8} {'SSIM ss':>8}  {'enc ms':>7}")
        print(hdr)
        print("─" * 80)

        for cl in args.cls:
            s     = results[cl]
            label = s["label"]
            psnr  = s["psnr_ss"]["avg"]
            ssim  = s["ssim_ss"]["avg"]
            enc   = s["enc_us"]["avg"] / 1000
            fail  = s["recv_fail"]
            print(
                f"{label:<18} {s['send']:>5} {s['recv']:>5} {fail:>5}  "
                f"{s['enc_kbps']:>8.1f} {s['ble_kbps']:>9.1f} {s['recv_kbps']:>9.1f}  "
                f"{psnr:>7.2f}dB {ssim:>8.4f}  {enc:>6.1f}ms"
            )

        if -1 in results and 0 in results:
            on  = results[-1]
            off = results[0]
            d_psnr = on["psnr_ss"]["avg"] - off["psnr_ss"]["avg"]
            d_ssim = on["ssim_ss"]["avg"] - off["ssim_ss"]["avg"]
            ratio  = (on["enc_us"]["avg"] / off["enc_us"]["avg"]
                      if off["enc_us"]["avg"] > 0 else float("nan"))
            print("─" * 80)
            print(f"  DACE ON vs OFF:  ΔPSNR={d_psnr:+.2f}dB  ΔSSIM={d_ssim:+.4f}"
                  f"  enc ratio={ratio:.1f}×")
        print("═" * 80)
        print("  enc kbps = x264 encoded bitrate (nal_b sum)  |  ble/rcv kbps = BLE link bytes")
        print("  fail     = RECV_FAIL (decode error, not missing packet)")

    # ── Throughput summary ────────────────────────────────────────────────────
    if tput_results:
        print_throughput_summary(tput_results)

    # Save summary txt
    summary_path = f"{args.out}_summary.txt"
    with open(summary_path, "w") as f:
        f.write(f"BLE DACE test — sender={sender} receiver={receiver}\n")
        f.write(f"CLs={args.cls} duration={args.duration}s fps={args.fps} "
                f"bitrate={args.bitrate}bps\n\n")
        if results:
            f.write(f"{'Mode':<18} {'SEND':>5} {'RECV':>5} {'fail':>5}  "
                    f"{'enc_kbps':>9} {'ble_kbps':>9} {'rcv_kbps':>9}  "
                    f"{'PSNR_ss':>8} {'SSIM_ss':>8}  {'enc_ms':>7}\n")
            for cl in args.cls:
                s = results[cl]
                f.write(
                    f"{s['label']:<18} {s['send']:>5} {s['recv']:>5} {s['recv_fail']:>5}  "
                    f"{s['enc_kbps']:>8.1f} {s['ble_kbps']:>9.1f} {s['recv_kbps']:>9.1f}  "
                    f"{s['psnr_ss']['avg']:>7.2f}dB {s['ssim_ss']['avg']:>8.4f}  "
                    f"{s['enc_us']['avg']/1000:>6.1f}ms\n"
                )
    print(f"\nSummary → {summary_path}  |  CSVs: {args.out}_cl<N>.csv")


if __name__ == "__main__":
    main()
