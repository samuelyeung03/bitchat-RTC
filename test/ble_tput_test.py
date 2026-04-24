#!/usr/bin/env python3
"""
ble_tput_test.py — BLE raw throughput ramp test.

Finds the max sustainable bitrate by sending sequential packets (one at a time,
gated on BLE write completion) and measuring actual wire throughput.

Usage:
  python3 test/ble_tput_test.py                         # ramp 50k→ceiling, step=50k
  python3 test/ble_tput_test.py --start 25000 --step 25000
  python3 test/ble_tput_test.py --dur 20 --threshold 95
  python3 test/ble_tput_test.py --sender T1AIOC656909KGK --receiver 8e27af28

How it works:
  - Sends start_tput ADB command — app sends 450B pseudo-random packets sequentially,
    waiting for BLE write completion before each send (no queue overflow)
  - delay_ms paces the send rate to the target bitrate
  - Measures snd_kbps (TPUT_SND logs) and rcv_kbps (RECV logs on receiver)
  - delivery% = rcv/snd — should be ~100% when link is healthy
  - Ramp stops when snd plateaus (BLE hardware ceiling) or delivery% < threshold

Devices (defaults):
  Sender  : T1AIOC656909KGK  (ASUS ROG9)
  Receiver: 8e27af28          (Xiaomi 12, peer fea25dd05ccc26a6)
"""

import argparse
import csv
import os
import re
import statistics
import subprocess
import sys
import time

# ── defaults ──────────────────────────────────────────────────────────────────
SENDER        = "T1AIOC656909KGK"
RECEIVER      = "8e27af28"
RECEIVER_PEER = "fea25dd05ccc26a6"
PACKAGE       = "com.bitchat.droid"
MAIN_ACTIVITY = f"{PACKAGE}/com.bitchat.android.MainActivity"
CMD_ACTION    = "com.bitchat.droid.CMD"

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
            if v < 0:
                cmd += ["--el", k, str(v)]
            else:
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
        "POST_NOTIFICATIONS",
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

RECV_RE      = re.compile(r"RECV seq=(\d+) nal_b=(\d+) ts_us=(\d+)")
TPUT_SND_RE  = re.compile(r"TPUT_SND seq=\d+ bytes=(\d+) ts_us=(\d+)")


def _kbps_from_rows(rows):
    """Compute kbps from list of (bytes, ts_us) tuples."""
    if len(rows) < 2:
        return 0.0
    ts = sorted(t for _, t in rows)
    elapsed_s = (ts[-1] - ts[0]) / 1e6
    if elapsed_s <= 0:
        return 0.0
    return sum(b for b, _ in rows) * 8 / elapsed_s / 1000.0


# ── ramp step ─────────────────────────────────────────────────────────────────

def _ramp_step(sender, receiver, receiver_peer, target_bps, payload_bytes, dur):
    """Send for dur seconds at target_bps; return (snd_kbps, rcv_kbps, dup_factor, snd_pkts, rcv_pkts).

    rcv_kbps may be 0 on old builds that lack handleIncomingVideo logging.
    Ceiling detection uses snd_kbps plateau regardless of rcv.
    """
    bits_per_pkt = payload_bytes * 8
    delay_ms     = max(0, round(bits_per_pkt * 1000 / target_bps) - 1)

    adb(sender,   "shell", "logcat", "-c", timeout=5)
    adb(receiver, "shell", "logcat", "-c", timeout=5)
    sp, sf = start_logcat_stream(sender,   ["BLE_TPUT:I"],  "/tmp/ble_tput_send.log")
    rp, rf = start_logcat_stream(receiver, ["latency:I"],   "/tmp/ble_tput_recv.log")
    time.sleep(0.2)

    broadcast(sender, cmd="start_tput", peer_id=receiver_peer,
              bytes=payload_bytes, delay_ms=delay_ms)
    time.sleep(dur)
    broadcast(sender, cmd="stop_tput")
    time.sleep(1)

    snd_log = stop_logcat_stream(sp, sf)
    rcv_log = stop_logcat_stream(rp, rf)

    snd_rows = [(int(m.group(1)), int(m.group(2))) for m in TPUT_SND_RE.finditer(snd_log)]
    rcv_rows = [(int(m.group(2)), int(m.group(3))) for m in RECV_RE.finditer(rcv_log)]

    snd_pkts = len(snd_rows)
    rcv_pkts = len(rcv_rows)

    snd = _kbps_from_rows(snd_rows)
    rcv = _kbps_from_rows(rcv_rows)

    # Correct for duplicate connections (rcv ≈ 2× snd)
    dup = max(1, round(rcv / snd)) if snd > 1.0 and rcv > 0 else 1
    rcv_pkts_corrected = rcv_pkts // dup
    return snd, rcv / dup, dup, snd_pkts, rcv_pkts_corrected


# ── ramp ──────────────────────────────────────────────────────────────────────

# ── ramp ──────────────────────────────────────────────────────────────────────

def run_ramp(sender, receiver, receiver_peer, start, dur, threshold,
             payload_bytes=450, min_step_bps=500, out_csv=None):
    """Slow-start (exponential doubling) to find ceiling, then binary search to pin it."""
    print(f"\n{'═'*62}")
    print(f"  BITRATE RAMP  payload={payload_bytes}B/pkt (sequential)")
    print(f"  start={start//1000}k  dur={dur}s  threshold={threshold}%")
    print(f"  Phase 1: exponential doubling  |  Phase 2: binary search")
    print(f"{'═'*62}\n")

    csv_rows = []

    def _row(arrow, target, snd, rcv, dup, ok, deliv, snd_pkts, rcv_pkts):
        csv_rows.append({
            "direction":    arrow,
            "target_kbps":  target // 1000,
            "snd_kbps":     round(snd, 2),
            "rcv_kbps":     round(rcv, 2),
            "delivery_pct": round(deliv, 1),
            "snd_pkts":     snd_pkts,
            "rcv_pkts":     rcv_pkts,
        })
        deliv_str = f"{deliv:5.1f}%({'rcv' if rcv > 0 else 'snd'})"
        status = "OK  " if ok else "LOSS"
        dup_s = f" ×{dup}" if dup > 1 else "   "
        pkt_str = f"{rcv_pkts}/{snd_pkts}pkts({100*rcv_pkts/snd_pkts:.0f}%)" if snd_pkts > 0 else "0/0pkts"
        print(f"  {arrow} {target//1000:>4}k  snd={snd:6.1f}  rcv={rcv:6.1f}"
              f"  deliv={deliv_str}{dup_s}  {pkt_str}  [{status}]", flush=True)

    def _measure(target):
        snd, rcv, dup, snd_pkts, rcv_pkts = _ramp_step(
            sender, receiver, receiver_peer, target, payload_bytes, dur)
        if rcv_pkts > 0 and snd_pkts > 0:
            deliv = 100.0 * rcv_pkts / snd_pkts
        elif rcv > 0 and snd > 0:
            deliv = 100.0 * rcv / snd
        else:
            deliv = 100.0 * snd / (target / 1000) if target > 0 else 0.0
        ok = deliv >= threshold
        return snd, rcv, dup, snd_pkts, rcv_pkts, deliv, ok

    def _between():
        time.sleep(2)
        bring_to_foreground(sender)
        bring_to_foreground(receiver)
        time.sleep(1)
        peers = get_connected_peers(sender)
        if receiver_peer not in peers:
            print("  [ramp] waiting for reconnect…", flush=True)
            for _ in range(4):
                time.sleep(5)
                bring_to_foreground(sender)
                if receiver_peer in get_connected_peers(sender):
                    break

    # ── Phase 1: exponential doubling ────────────────────────────────────────
    print("  Phase 1: exponential doubling…")
    low  = 0
    high = None
    target = start
    while True:
        snd, rcv, dup, snd_pkts, rcv_pkts, deliv, ok = _measure(target)
        _row("↑", target, snd, rcv, dup, ok, deliv, snd_pkts, rcv_pkts)
        if ok:
            low = target
            target *= 2
        else:
            high = target
            break
        _between()

    # ── Phase 2: binary search ────────────────────────────────────────────────
    print(f"\n  Phase 2: binary search [{low//1000}k … {high//1000}k]…")
    while high - low > min_step_bps:
        target = (low + high) // 2
        snd, rcv, dup, snd_pkts, rcv_pkts, deliv, ok = _measure(target)
        _row("→", target, snd, rcv, dup, ok, deliv, snd_pkts, rcv_pkts)
        if ok:
            low = target
        else:
            high = target
        if high - low <= min_step_bps:
            break
        _between()

    print(f"\n{'═'*62}")
    print(f"  ★  Max sustainable: ~{low//1000} kbps  (loss at {high//1000}k)")
    print(f"{'═'*62}")

    if out_csv and csv_rows:
        os.makedirs(os.path.dirname(os.path.abspath(out_csv)), exist_ok=True)
        with open(out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=csv_rows[0].keys())
            w.writeheader()
            w.writerows(csv_rows)
        print(f"  CSV → {out_csv}")

    return low



# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="BLE raw throughput ramp test")
    ap.add_argument("--sender",    default=SENDER)
    ap.add_argument("--receiver",  default=RECEIVER)
    ap.add_argument("--peer",      default=None,
                    help="Receiver peer ID hex (auto-detected if omitted)")
    ap.add_argument("--start",     type=int, default=50000,
                    help="Start bitrate bps (default 50000)")
    ap.add_argument("--dur",       type=int, default=15,
                    help="Seconds per step (default 15)")
    ap.add_argument("--threshold", type=float, default=90.0,
                    help="Delivery %% threshold for 'OK' (default 90.0)")
    ap.add_argument("--payload",   type=int, default=450,
                    help="Packet payload bytes (default 450)")
    ap.add_argument("--min_step",  type=float, default=0.5,
                    help="Binary search resolution in kbps (default 0.5)")
    ap.add_argument("--out",       default=None,
                    help="Path to save CSV results (e.g. results/data/tput_run.csv)")
    args = ap.parse_args()

    if args.min_step <= 0:
        print("ERROR: --min_step must be > 0", file=sys.stderr)
        sys.exit(2)

    sender   = args.sender
    receiver = args.receiver
    min_step_bps = max(1, int(round(args.min_step * 1000)))

    print("═" * 62)
    print(f"  BLE throughput ramp  sender={sender}  receiver={receiver}")
    print("═" * 62)

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
                print("\nERROR: Cannot find receiver peer ID.")
                sys.exit(1)
    else:
        print(f"connected\n  Using: {receiver_peer}")

    bring_to_foreground(sender)
    if receiver_peer not in get_connected_peers(sender):
        print(f"\nWARNING: Sender doesn't see {receiver_peer} yet. Waiting 15s…")
        time.sleep(15)

    run_ramp(sender, receiver, receiver_peer,
             start=args.start,
             dur=args.dur, threshold=args.threshold,
             payload_bytes=args.payload,
             min_step_bps=min_step_bps,
             out_csv=args.out)


if __name__ == "__main__":
    main()
