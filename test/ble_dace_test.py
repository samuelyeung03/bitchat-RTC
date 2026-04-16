#!/usr/bin/env python3
"""
ble_dace_test.py — Run DACE ON vs OFF PSNR/SSIM comparison over BLE mesh.

Usage:
  python3 test/ble_dace_test.py                        # DACE ON then OFF, 60s each
  python3 test/ble_dace_test.py --duration 30          # 30s each
  python3 test/ble_dace_test.py --cls -1 0 1 2         # custom CL list
  python3 test/ble_dace_test.py --fps 5 --bitrate 60000
  python3 test/ble_dace_test.py --sender T1AIOC656909KGK --receiver 8e27af28

Devices (defaults):
  Sender  : T1AIOC656909KGK  (ASUS ROG9)
  Receiver: 8e27af28          (Xiaomi 12, peer fea25dd05ccc26a6)

CL mapping:
  cl=0   → DACE OFF (param.dace=0, plain x264)
  cl=-1  → DACE ON auto (self-regulates complexity)
  cl=1-9 → DACE ON fixed complexity level
"""

import argparse
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
    """Ensure MainActivity (and thus BluetoothMeshService) is running."""
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
    """Read peer ID via broadcast (stays in same process)."""
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


# ── stats ─────────────────────────────────────────────────────────────────────

SEND_RE = re.compile(
    r"SEND seq=(\d+) cl=(-?\d+) nal_b=(\d+) psnr=([\d.]+) ssim=([\d.]+) "
    r"enc_us=(\d+) ts_us=(\d+)"
)
RECV_RE = re.compile(r"RECV seq=(\d+) nal_b=(\d+) ts_us=(\d+)")


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


def stat(vals):
    if not vals:
        return {"n": 0, "avg": float("nan"), "min": float("nan"),
                "max": float("nan")}
    return {"n": len(vals), "avg": statistics.mean(vals),
            "min": min(vals), "max": max(vals)}


def summarise(send_rows, recv_rows):
    """Merge by seq, compute steady-state (skip first 2 frames)."""
    recv_by_seq = {r["seq"]: r for r in recv_rows}
    ss = [r for r in send_rows if r["seq"] >= 2]  # skip IDR + correction
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
        "send":     len(send_rows),
        "recv":     len(recv_rows),
        "psnr_ss":  stat(psnrs),
        "ssim_ss":  stat(ssims),
        "nal_ss":   stat(nals),
        "enc_us":   stat(enc_uss),
        "lat_us":   stat(lats),
    }


# ── test runner ───────────────────────────────────────────────────────────────

def run_pass(sender, receiver, receiver_peer, cl, fps, bitrate, duration, src, w, h):
    label = "DACE OFF" if cl == 0 else (f"DACE ON auto" if cl == -1 else f"DACE ON cl={cl}")
    print(f"\n  [{label}] cl={cl}  {duration}s  {fps}fps  {bitrate}bps")

    # Clear logcat
    adb(sender,   "shell", "logcat", "-c", timeout=5)
    adb(receiver, "shell", "logcat", "-c", timeout=5)
    time.sleep(0.3)

    # Start video
    bring_to_foreground(sender)
    extras = dict(cmd="start_video", peer_id=receiver_peer,
                  cl=cl, fps=fps, bitrate=bitrate, w=w, h=h)
    if src:
        extras["src"] = src
    broadcast(sender, **extras)

    # Sample progress every 10s
    t0 = time.time()
    while time.time() - t0 < duration:
        time.sleep(10)
        wake_screen(sender)
        elapsed = time.time() - t0
        send_now = adb(sender, "shell", "logcat", "-d", "-s", "latency:I",
                       timeout=5).count("SEND seq")
        tput_line = adb(sender, "shell", "logcat", "-d", "-s", "BLE_THROUGHPUT:I",
                        timeout=5).strip().splitlines()
        kbps = "n/a"
        if tput_line:
            m = re.search(r"kbps=([\d.]+)", tput_line[-1])
            if m:
                kbps = m.group(1)
        print(f"    {elapsed:4.0f}s  SEND={send_now}  tput={kbps}kbps", flush=True)

    # Stop
    broadcast(sender, cmd="stop_video")
    time.sleep(2)

    send_log = adb(sender,   "shell", "logcat", "-d", "-s", "latency:I", timeout=8)
    recv_log = adb(receiver, "shell", "logcat", "-d", "-s", "latency:I", timeout=8)
    tput_log = adb(sender,   "shell", "logcat", "-d", "-s", "BLE_THROUGHPUT:I", timeout=5)

    send_rows = parse_send(send_log)
    recv_rows = parse_recv(recv_log)
    kbps_vals = [float(m.group(1)) for m in re.finditer(r"kbps=([\d.]+)", tput_log)]
    tput_avg  = statistics.mean(kbps_vals) if kbps_vals else 0.0

    s = summarise(send_rows, recv_rows)
    s["tput_kbps"] = tput_avg
    s["cl"] = cl
    s["label"] = label
    return s, send_rows, recv_rows


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="BLE DACE ON vs OFF PSNR/SSIM test")
    ap.add_argument("--sender",   default=SENDER,   help="ADB serial of sender")
    ap.add_argument("--receiver", default=RECEIVER, help="ADB serial of receiver")
    ap.add_argument("--peer",     default=None,
                    help="Receiver peer ID hex (auto-detected if omitted)")
    ap.add_argument("--cls",   type=int, nargs="+", default=DEFAULT_CLS,
                    help="CL values: 0=DACE OFF, -1=DACE ON auto, 1-9=fixed (default: -1 0)")
    ap.add_argument("--duration", type=int, default=DEFAULT_DUR,
                    help=f"Seconds per pass (default {DEFAULT_DUR})")
    ap.add_argument("--fps",      type=int, default=DEFAULT_FPS)
    ap.add_argument("--bitrate",  type=int, default=DEFAULT_BITRATE, help="bps")
    ap.add_argument("--src",      default=SRC_PATH,
                    help="YUV420 source path on sender device")
    ap.add_argument("--w",        type=int, default=SRC_W)
    ap.add_argument("--h",        type=int, default=SRC_H)
    ap.add_argument("--out",      default="/tmp/ble_dace",
                    help="Output CSV prefix (default /tmp/ble_dace)")
    ap.add_argument("--no-push",  action="store_true",
                    help="Skip pushing YUV file to sender")
    args = ap.parse_args()

    sender   = args.sender
    receiver = args.receiver

    print("═" * 62)
    print(f"  BLE DACE test  sender={sender}  receiver={receiver}")
    print(f"  CLs={args.cls}  duration={args.duration}s  fps={args.fps}  bitrate={args.bitrate}bps")
    print("═" * 62)

    # Push YUV file
    if not args.no_push and args.src == SRC_PATH:
        local_yuv = "test/media/complex_320x240.yuv"
        import os
        if os.path.exists(local_yuv):
            print(f"\n[setup] Pushing {local_yuv} → {SRC_PATH} on sender…")
            subprocess.run(["adb", "-s", sender, "push", local_yuv, SRC_PATH],
                           check=True, timeout=120)
        else:
            print(f"[setup] YUV file not found locally, assuming already on device.")

    # Grant permissions
    print("[setup] Granting BT permissions on both devices…")
    grant_bt_permissions(sender)
    grant_bt_permissions(receiver)

    # Start apps
    print("[setup] Starting apps…")
    for s in [sender, receiver]:
        adb(s, "shell", "am", "force-stop", PACKAGE, capture=False, timeout=8)
        adb(s, "shell", "settings", "put", "system", "screen_off_timeout", "600000",
            capture=False, timeout=5)
    time.sleep(1)
    for s in [sender, receiver]:
        adb(s, "shell", "am", "start", "-n", MAIN_ACTIVITY,
            capture=False, timeout=8)
        wake_screen(s)

    # Wait for BLE mesh + peer discovery
    print("[setup] Waiting 30s for BLE peer discovery…", end=" ", flush=True)
    time.sleep(30)

    # Get receiver peer ID
    receiver_peer = args.peer
    if not receiver_peer:
        print("\n[setup] Discovering receiver peer ID…")
        peers = get_connected_peers(sender)
        if peers:
            receiver_peer = peers[0]
            print(f"  Found: {receiver_peer}")
        else:
            # Fall back to asking the receiver directly
            bring_to_foreground(receiver)
            time.sleep(2)
            receiver_peer = get_peer_id(receiver)
            if receiver_peer:
                print(f"  Receiver self-reports: {receiver_peer}")
            else:
                print("\nERROR: Cannot find receiver peer ID. Are both apps running?")
                sys.exit(1)
    else:
        print(f"connected\n  Using provided peer: {receiver_peer}")

    # Verify sender sees receiver
    bring_to_foreground(sender)
    peers = get_connected_peers(sender)
    if receiver_peer not in peers:
        print(f"\nWARNING: Sender doesn't see receiver peer {receiver_peer} yet.")
        print("  Waiting 15 more seconds…")
        time.sleep(15)
        peers = get_connected_peers(sender)
        if receiver_peer not in peers:
            print("  Still not connected — proceeding anyway (may get 0 RECV).")

    # Run each pass
    results = {}
    all_send = {}
    all_recv = {}

    for cl in args.cls:
        s, send_rows, recv_rows = run_pass(
            sender, receiver, receiver_peer,
            cl, args.fps, args.bitrate, args.duration,
            args.src, args.w, args.h
        )
        results[cl] = s
        all_send[cl] = send_rows
        all_recv[cl] = recv_rows

        # Save CSV
        csv_path = f"{args.out}_cl{cl}.csv"
        with open(csv_path, "w") as f:
            f.write("seq,cl,nal_b,psnr_db,ssim,enc_us\n")
            for r in send_rows:
                recv_ts = next((rr["ts_us"] for rr in recv_rows if rr["seq"] == r["seq"]), "")
                f.write(f"{r['seq']},{r['cl']},{r['nal_b']},{r['psnr']:.3f},"
                        f"{r['ssim']:.4f},{r['enc_us']}\n")
        print(f"  CSV → {csv_path}")

        # Brief gap between passes
        if cl != args.cls[-1]:
            time.sleep(5)
            bring_to_foreground(sender)
            time.sleep(3)

    # ── summary table ──────────────────────────────────────────────────────────
    print("\n" + "═" * 72)
    print("  RESULTS  (ss = steady-state, skip first 2 frames = IDR + correction)")
    print("═" * 72)
    hdr = f"{'Mode':<18} {'SEND':>5} {'RECV':>5} {'tput':>7}  {'PSNR ss':>9} {'SSIM ss':>9}  {'NAL ss':>8}  {'enc avg':>8}"
    print(hdr)
    print("─" * 72)

    for cl in args.cls:
        s = results[cl]
        label = s["label"]
        psnr  = s["psnr_ss"]["avg"]
        ssim  = s["ssim_ss"]["avg"]
        nal   = s["nal_ss"]["avg"]
        enc   = s["enc_us"]["avg"]
        tput  = s["tput_kbps"]
        print(
            f"{label:<18} {s['send']:>5} {s['recv']:>5} {tput:>6.1f}kbps  "
            f"{psnr:>8.2f}dB {ssim:>9.4f}  {nal:>7.0f}B  {enc/1000:>7.1f}ms"
        )

    # Delta (DACE ON vs OFF) if both present
    if -1 in results and 0 in results:
        on  = results[-1]
        off = results[0]
        d_psnr = on["psnr_ss"]["avg"] - off["psnr_ss"]["avg"]
        d_ssim = on["ssim_ss"]["avg"] - off["ssim_ss"]["avg"]
        enc_on  = on["enc_us"]["avg"]  / 1000
        enc_off = off["enc_us"]["avg"] / 1000
        ratio   = enc_on / enc_off if enc_off > 0 else float("nan")
        print("─" * 72)
        print(f"  DACE ON vs OFF:  ΔPSNR={d_psnr:+.2f}dB  ΔSSIM={d_ssim:+.4f}"
              f"  enc ratio={ratio:.1f}×")

    print("═" * 72)
    print(f"\nCSV files: {args.out}_cl<N>.csv")
    summary_path = f"{args.out}_summary.txt"
    with open(summary_path, "w") as f:
        f.write(f"BLE DACE test — sender={sender} receiver={receiver}\n")
        f.write(f"CLs={args.cls} duration={args.duration}s fps={args.fps} "
                f"bitrate={args.bitrate}bps\n\n")
        f.write(f"{'Mode':<18} {'SEND':>5} {'RECV':>5} {'tput':>7}  "
                f"{'PSNR ss':>9} {'SSIM ss':>9}  {'NAL ss':>8}  {'enc avg':>8}\n")
        for cl in args.cls:
            s = results[cl]
            f.write(f"{s['label']:<18} {s['send']:>5} {s['recv']:>5} "
                    f"{s['tput_kbps']:>6.1f}kbps  "
                    f"{s['psnr_ss']['avg']:>8.2f}dB {s['ssim_ss']['avg']:>9.4f}  "
                    f"{s['nal_ss']['avg']:>7.0f}B  {s['enc_us']['avg']/1000:>7.1f}ms\n")
    print(f"Summary → {summary_path}")


if __name__ == "__main__":
    main()
