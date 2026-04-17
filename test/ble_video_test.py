#!/usr/bin/env python3
"""
ble_video_test.py — DACE ON vs OFF PSNR/SSIM quality test over BLE mesh.

Usage:
  python3 test/ble_video_test.py                        # DACE ON then OFF, 60s each
  python3 test/ble_video_test.py --duration 30          # 30s each
  python3 test/ble_video_test.py --cls -1 0 1 2         # custom CL list
  python3 test/ble_video_test.py --fps 5 --bitrate 60000
  python3 test/ble_video_test.py --sender T1AIOC656909KGK --receiver 8e27af28

Devices (defaults):
  Sender  : T1AIOC656909KGK  (ASUS ROG9)
  Receiver: 8e27af28          (Xiaomi 12, peer fea25dd05ccc26a6)

CL mapping:
  cl=0   → DACE OFF (param.dace=0, plain x264)
  cl=-1  → DACE ON auto (self-regulates complexity)
  cl=1-9 → DACE ON fixed complexity level

Metrics:
  PSNR/SSIM  : steady-state (skip IDR + seq1 correction frames)
  BLE link   : GATT write kbps (BLE_THROUGHPUT:I tag)
  Receiver   : received kbps  (BLE_TPUT_RECV tag)
"""

import argparse
import os
import re
import statistics
import subprocess
import sys
import time

# ── defaults ──────────────────────────────────────────────────────────────────
SENDER          = "T1AIOC656909KGK"
RECEIVER        = "8e27af28"
RECEIVER_PEER   = "fea25dd05ccc26a6"
SRC_PATH        = "/data/local/tmp/complex_320x240.yuv"
SRC_W, SRC_H    = 320, 240
DEFAULT_FPS     = 3
DEFAULT_BITRATE = 40000
DEFAULT_DUR     = 60
DEFAULT_CLS     = [-1, 0]
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
    cmd = ["shell", "am", "broadcast", "-a", CMD_ACTION]
    for k, v in extras.items():
        if isinstance(v, bool):
            cmd += ["--ez", k, "true" if v else "false"]
        elif k == "delay_ms":
            cmd += ["--el", k, str(v)]
        elif isinstance(v, int):
            cmd += ["--ei", k, str(v)]
        else:
            cmd += ["--es", k, str(v)]
    adb(serial, *cmd, capture=False, timeout=10)


def wake_screen(serial):
    adb(serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP", capture=False, timeout=5)


def bring_to_foreground(serial):
    wake_screen(serial)
    adb(serial, "shell", "am", "start", "-n", MAIN_ACTIVITY, capture=False, timeout=8)
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


# ── log streaming ─────────────────────────────────────────────────────────────

def start_logcat_stream(serial, tags, filepath):
    tag_args = []
    for t in tags:
        tag_args += ["-s", t]
    f = open(filepath, "w+")
    proc = subprocess.Popen(
        ["adb", "-s", serial, "shell", "logcat"] + tag_args,
        stdout=f, stderr=subprocess.DEVNULL, text=True
    )
    return proc, f


def stop_logcat_stream(proc, fh):
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    fh.flush()
    fh.seek(0)
    content = fh.read()
    fh.close()
    return content


# ── log patterns ──────────────────────────────────────────────────────────────

SEND_RE = re.compile(
    r"SEND seq=(\d+) cl=(-?\d+) nal_b=(\d+) psnr=([\d.]+) ssim=([\d.]+) "
    r"enc_us=(\d+) ts_us=(\d+)"
)
RECV_RE       = re.compile(r"RECV seq=(\d+) nal_b=(\d+) ts_us=(\d+)")
RECV_FAIL_RE  = re.compile(r"RECV_FAIL seq=(\d+) nal_b=(\d+) ts_us=(\d+)")
TPUT_RECV_RE  = re.compile(r"RECV_TPUT nal_b=(\d+) ts_us=(\d+)")
BLE_TPUT_RE   = re.compile(r"kbps=([\d.]+)")
FRAG_SEND_RE  = re.compile(r"FRAG_SEND total=(\d+) size=(\d+)")
FRAG_RECV_RE  = re.compile(r"FRAG_RECV idx=(\d+) total=(\d+)")
FRAG_DONE_RE  = re.compile(r"FRAG_DONE total=(\d+)")


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
    seen = set()
    rows = []
    for m in RECV_RE.finditer(logcat_text):
        seq = int(m.group(1))
        if seq not in seen:
            seen.add(seq)
            rows.append({"seq": seq, "nal_b": int(m.group(2)), "ts_us": int(m.group(3))})
    return rows


def parse_recv_fail(logcat_text):
    return [{"seq": int(m.group(1)), "nal_b": int(m.group(2)), "ts_us": int(m.group(3))}
            for m in RECV_FAIL_RE.finditer(logcat_text)]


def parse_tput_recv(logcat_text):
    return [(int(m.group(1)), int(m.group(2))) for m in TPUT_RECV_RE.finditer(logcat_text)]


def parse_frag_stats(sender_log, receiver_log):
    frags_sent      = sum(int(m.group(1)) for m in FRAG_SEND_RE.finditer(sender_log))
    frags_recv_raw  = len(FRAG_RECV_RE.findall(receiver_log))
    frames_done_raw = len(FRAG_DONE_RE.findall(receiver_log))
    dup_factor = max(1, round(frags_recv_raw / frags_sent)) if frags_sent > 0 else 1
    frags_recv  = frags_recv_raw  // dup_factor
    frames_done = frames_done_raw // dup_factor
    return frags_sent, frags_recv, frames_done, dup_factor


def encoded_bitrate_kbps(send_rows):
    if len(send_rows) < 2:
        return 0.0
    ts_sorted = sorted(r["ts_us"] for r in send_rows)
    elapsed_s = (ts_sorted[-1] - ts_sorted[0]) / 1e6
    if elapsed_s <= 0:
        return 0.0
    return sum(r["nal_b"] for r in send_rows) * 8 / elapsed_s / 1000.0


def recv_bitrate_kbps(tput_recv_rows):
    if len(tput_recv_rows) < 2:
        return 0.0
    ts_sorted = sorted(ts for _, ts in tput_recv_rows)
    elapsed_s = (ts_sorted[-1] - ts_sorted[0]) / 1e6
    if elapsed_s <= 0:
        return 0.0
    return sum(nb for nb, _ in tput_recv_rows) * 8 / elapsed_s / 1000.0


def stat(vals):
    if not vals:
        return {"n": 0, "avg": float("nan"), "min": float("nan"), "max": float("nan")}
    s = sorted(vals)
    return {"n": len(vals), "avg": statistics.mean(vals), "min": s[0], "max": s[-1],
            "p95": s[int(len(s) * 0.95)]}


def summarise(send_rows, recv_rows, recv_fail_rows=None, tput_recv_rows=None,
              ble_tput_kbps=0.0):
    recv_by_seq = {r["seq"]: r for r in recv_rows}
    ss      = [r for r in send_rows if r["seq"] >= 2]
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
        "send":      len(send_rows),
        "recv":      len(recv_rows),
        "recv_fail": len(recv_fail_rows) if recv_fail_rows else 0,
        "psnr_ss":   stat(psnrs),
        "ssim_ss":   stat(ssims),
        "nal_ss":    stat(nals),
        "enc_us":    stat(enc_uss),
        "lat_us":    stat(lats),
        "enc_kbps":  encoded_bitrate_kbps(send_rows),
        "ble_kbps":  ble_tput_kbps,
        "recv_kbps": recv_bitrate_kbps(tput_recv_rows) if tput_recv_rows else 0.0,
    }


# ── test runner ───────────────────────────────────────────────────────────────

def run_pass(sender, receiver, receiver_peer, cl, fps, bitrate, duration, src, w, h):
    label = "DACE OFF" if cl == 0 else ("DACE ON auto" if cl == -1 else f"DACE ON cl={cl}")
    print(f"\n  [{label}] cl={cl}  {duration}s  {fps}fps  {bitrate}bps")

    adb(sender,   "shell", "logcat", "-c", timeout=5)
    adb(receiver, "shell", "logcat", "-c", timeout=5)
    time.sleep(0.3)

    send_log_path = f"/tmp/ble_video_send_{sender}.log"
    recv_log_path = f"/tmp/ble_video_recv_{receiver}.log"
    sender_proc, sender_fh = start_logcat_stream(
        sender,   ["latency:I", "BLE_THROUGHPUT:I", "BLE_FRAG:I"], send_log_path)
    receiver_proc, receiver_fh = start_logcat_stream(
        receiver, ["latency:I", "BLE_TPUT_RECV:I",  "BLE_FRAG:I"], recv_log_path)
    time.sleep(0.2)

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
        try:
            with open(send_log_path) as f:
                cur = f.read()
            send_now = cur.count("SEND seq")
            tput_lines = [l for l in cur.splitlines() if "kbps=" in l]
            kbps = "n/a"
            if tput_lines:
                m = BLE_TPUT_RE.search(tput_lines[-1])
                if m:
                    kbps = m.group(1)
        except OSError:
            send_now, kbps = "?", "n/a"
        print(f"    {elapsed:4.0f}s  SEND={send_now}  ble_link={kbps}kbps", flush=True)

    broadcast(sender, cmd="stop_video")
    time.sleep(2)

    send_log = stop_logcat_stream(sender_proc,   sender_fh)
    recv_log = stop_logcat_stream(receiver_proc, receiver_fh)

    send_rows      = parse_send(send_log)
    recv_rows      = parse_recv(recv_log)
    recv_fail_rows = parse_recv_fail(recv_log)
    tput_recv_rows = parse_tput_recv(recv_log)
    ble_kbps_vals  = [float(m.group(1)) for m in BLE_TPUT_RE.finditer(send_log)]
    ble_tput_avg   = statistics.mean(ble_kbps_vals) if ble_kbps_vals else 0.0

    frags_sent, frags_recv, frames_done, dup_factor = parse_frag_stats(send_log, recv_log)
    if dup_factor > 1:
        print(f"  [warn] duplicate BLE connections detected (×{dup_factor}) — "
              f"frag/frame counts divided by {dup_factor}")

    s = summarise(send_rows, recv_rows, recv_fail_rows, tput_recv_rows,
                  ble_tput_avg / dup_factor)
    s["recv_kbps"] = s["recv_kbps"] / dup_factor
    s["cl"]          = cl
    s["label"]       = label
    s["frags_sent"]  = frags_sent
    s["frags_recv"]  = frags_recv
    s["frames_done"] = frames_done
    s["dup_factor"]  = dup_factor
    return s, send_rows, recv_rows


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="BLE DACE ON vs OFF PSNR/SSIM quality test")
    ap.add_argument("--sender",    default=SENDER)
    ap.add_argument("--receiver",  default=RECEIVER)
    ap.add_argument("--peer",      default=None,
                    help="Receiver peer ID hex (auto-detected if omitted)")
    ap.add_argument("--cls",       type=int, nargs="+", default=DEFAULT_CLS,
                    help="CL values: 0=DACE OFF, -1=auto, 1-9=fixed (default: -1 0)")
    ap.add_argument("--duration",  type=int, default=DEFAULT_DUR,
                    help=f"Seconds per pass (default {DEFAULT_DUR})")
    ap.add_argument("--fps",       type=int, default=DEFAULT_FPS)
    ap.add_argument("--bitrate",   type=int, default=DEFAULT_BITRATE, help="bps")
    ap.add_argument("--src",       default=SRC_PATH)
    ap.add_argument("--w",         type=int, default=SRC_W)
    ap.add_argument("--h",         type=int, default=SRC_H)
    ap.add_argument("--out",       default="/tmp/ble_video",
                    help="Output CSV/txt prefix (default /tmp/ble_video)")
    ap.add_argument("--no-push",   action="store_true",
                    help="Skip pushing YUV file to sender")
    args = ap.parse_args()

    sender   = args.sender
    receiver = args.receiver
    cls      = args.cls

    print("═" * 66)
    print(f"  BLE video test  sender={sender}  receiver={receiver}")
    print(f"  CLs={cls}  dur={args.duration}s  fps={args.fps}  bitrate={args.bitrate}bps")
    print("═" * 66)

    if not args.no_push and args.src == SRC_PATH:
        local_yuv = "test/media/complex_320x240.yuv"
        if os.path.exists(local_yuv):
            print(f"\n[setup] Pushing {local_yuv} → {SRC_PATH} …")
            subprocess.run(["adb", "-s", sender, "push", local_yuv, SRC_PATH],
                           check=True, timeout=120)
        else:
            print("[setup] YUV not found locally, assuming already on device.")

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

    results = {}

    for cl in cls:
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

        if cl != cls[-1]:
            time.sleep(5)
            bring_to_foreground(sender)
            time.sleep(3)

    # ── Summary ───────────────────────────────────────────────────────────────
    if results:
        print("\n" + "═" * 90)
        print("  PSNR/SSIM RESULTS  (ss = steady-state, skip IDR + correction frames)")
        print("═" * 90)
        hdr = (f"{'Mode':<18} {'SEND':>5} {'RECV':>5} {'frm%':>6} {'frag%':>6}  "
               f"{'ble kbps':>9} {'rcv kbps':>9}  "
               f"{'PSNR ss':>8} {'SSIM ss':>8}  {'enc ms':>7}")
        print(hdr)
        print("─" * 90)

        for cl in cls:
            s     = results[cl]
            psnr  = s["psnr_ss"]["avg"]
            ssim  = s["ssim_ss"]["avg"]
            enc   = s["enc_us"]["avg"] / 1000
            frame_pct = 100.0 * s["recv"] / s["send"] if s["send"] > 0 else 0.0
            frag_pct  = (100.0 * s["frags_recv"] / s["frags_sent"]
                         if s.get("frags_sent", 0) > 0 else float("nan"))
            frag_str  = f"{frag_pct:5.1f}%" if frag_pct == frag_pct else "   n/a"
            print(
                f"{s['label']:<18} {s['send']:>5} {s['recv']:>5} {frame_pct:>5.1f}% {frag_str:>6}  "
                f"{s['ble_kbps']:>9.1f} {s['recv_kbps']:>9.1f}  "
                f"{psnr:>7.2f}dB {ssim:>8.4f}  {enc:>6.1f}ms"
            )

        if -1 in results and 0 in results:
            on  = results[-1]
            off = results[0]
            d_psnr = on["psnr_ss"]["avg"] - off["psnr_ss"]["avg"]
            d_ssim = on["ssim_ss"]["avg"] - off["ssim_ss"]["avg"]
            ratio  = (on["enc_us"]["avg"] / off["enc_us"]["avg"]
                      if off["enc_us"]["avg"] > 0 else float("nan"))
            print("─" * 90)
            print(f"  DACE ON vs OFF:  ΔPSNR={d_psnr:+.2f}dB  ΔSSIM={d_ssim:+.4f}"
                  f"  enc ratio={ratio:.1f}×")
        print("═" * 90)
        print("  frm% = frame delivery (RECV/SEND) | frag% = BLE fragment delivery")
        print("  ble/rcv kbps = BLE link bytes | enc ms = encoder latency")

    summary_path = f"{args.out}_summary.txt"
    with open(summary_path, "w") as f:
        f.write(f"BLE video test — sender={sender} receiver={receiver}\n")
        f.write(f"CLs={cls} duration={args.duration}s fps={args.fps} "
                f"bitrate={args.bitrate}bps\n\n")
        if results:
            f.write(f"{'Mode':<18} {'SEND':>5} {'RECV':>5} {'fail':>5}  "
                    f"{'enc_kbps':>9} {'ble_kbps':>9} {'rcv_kbps':>9}  "
                    f"{'PSNR_ss':>8} {'SSIM_ss':>8}  {'enc_ms':>7}\n")
            for cl in cls:
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
