#!/usr/bin/env python3
"""
analyze_video_log.py — parse raw logcat files from ble_video_test.py and print results.

Usage:
  python3 test/analyze_video_log.py
  python3 test/analyze_video_log.py --send /tmp/ble_video_send_T1AIOC656909KGK.log \
                                     --recv /tmp/ble_video_recv_8e27af28.log \
                                     --cl -1
  python3 test/analyze_video_log.py --csv /tmp/ble_video_cl-1.csv --cl -1
"""

import argparse
import csv
import math
import re
import statistics
import sys

SEND_RE      = re.compile(
    r"SEND seq=(\d+) cl=(-?\d+) nal_b=(\d+) psnr=([\d.]+) ssim=([\d.]+) "
    r"enc_us=(\d+) ts_us=(\d+)"
)
RECV_RE      = re.compile(r"RECV seq=(\d+) nal_b=(\d+) ts_us=(\d+)")
DECODE_RE    = re.compile(r"DECODE seq=(\d+) dec_us=(\d+) ts_us=(\d+)")
RECV_FAIL_RE = re.compile(r"RECV_FAIL seq=(\d+) nal_b=(\d+) ts_us=(\d+)")
TPUT_RECV_RE = re.compile(r"RECV_TPUT nal_b=(\d+) ts_us=(\d+)")
BLE_TPUT_RE  = re.compile(r"kbps=([\d.]+)")
FRAG_SEND_RE = re.compile(r"FRAG_SEND total=(\d+) size=(\d+)")
FRAG_RECV_RE = re.compile(r"FRAG_RECV idx=(\d+) total=(\d+)")
FRAG_DONE_RE = re.compile(r"FRAG_DONE total=(\d+)")


def stat(vals):
    if not vals:
        return {"n": 0, "avg": float("nan"), "min": float("nan"),
                "max": float("nan"), "p95": float("nan")}
    s = sorted(vals)
    return {"n": len(vals), "avg": statistics.mean(vals),
            "min": s[0], "max": s[-1], "p95": s[int(len(s) * 0.95)]}


def encoded_bitrate_kbps(send_rows):
    if len(send_rows) < 2:
        return 0.0
    ts = sorted(r["ts_us"] for r in send_rows)
    elapsed = (ts[-1] - ts[0]) / 1e6
    return sum(r["nal_b"] for r in send_rows) * 8 / elapsed / 1000.0 if elapsed > 0 else 0.0


def recv_bitrate_kbps(tput_rows):
    if len(tput_rows) < 2:
        return 0.0
    ts = sorted(t for _, t in tput_rows)
    elapsed = (ts[-1] - ts[0]) / 1e6
    return sum(nb for nb, _ in tput_rows) * 8 / elapsed / 1000.0 if elapsed > 0 else 0.0


def analyze_logs(send_text, recv_text, cl):
    seen_send = set()
    send_rows = []
    for m in SEND_RE.finditer(send_text):
        seq = int(m.group(1))
        if seq in seen_send:
            continue
        seen_send.add(seq)
        send_rows.append({
            "seq": seq, "cl": int(m.group(2)),
            "nal_b": int(m.group(3)), "psnr": float(m.group(4)),
            "ssim": float(m.group(5)), "enc_us": int(m.group(6)),
            "ts_us": int(m.group(7)),
        })

    seen = set()
    recv_rows = []
    for m in RECV_RE.finditer(recv_text):
        seq = int(m.group(1))
        if seq not in seen:
            seen.add(seq)
            recv_rows.append({"seq": seq, "nal_b": int(m.group(2)), "ts_us": int(m.group(3))})

    decode_rows = []
    for m in DECODE_RE.finditer(recv_text):
        decode_rows.append({"seq": int(m.group(1)), "dec_us": int(m.group(2)), "ts_us": int(m.group(3))})

    seen_fail = set()
    recv_fail_rows = []
    for m in RECV_FAIL_RE.finditer(recv_text):
        seq = int(m.group(1))
        if seq in seen_fail:
            continue
        seen_fail.add(seq)
        recv_fail_rows.append({"seq": seq, "nal_b": int(m.group(2)), "ts_us": int(m.group(3))})
    tput_recv_rows = [(int(m.group(1)), int(m.group(2))) for m in TPUT_RECV_RE.finditer(recv_text)]

    ble_kbps_vals = [float(m.group(1)) for m in BLE_TPUT_RE.finditer(send_text)]

    frags_sent     = sum(int(m.group(1)) for m in FRAG_SEND_RE.finditer(send_text))
    frags_recv_raw = len(FRAG_RECV_RE.findall(recv_text))
    frames_done_raw = len(FRAG_DONE_RE.findall(recv_text))
    dup_factor = max(1, round(frags_recv_raw / frags_sent)) if frags_sent > 0 else 1
    frags_recv  = frags_recv_raw  // dup_factor
    frames_done = frames_done_raw // dup_factor

    recv_by_seq = {r["seq"]: r for r in recv_rows}
    ss = [r for r in send_rows if r["seq"] >= 2]
    lats = []
    for r in send_rows:
        rr = recv_by_seq.get(r["seq"])
        if rr:
            lats.append(rr["ts_us"] - r["ts_us"])

    ble_avg = (statistics.mean(ble_kbps_vals) if ble_kbps_vals else 0.0) / dup_factor
    rcv_kbps = recv_bitrate_kbps(tput_recv_rows) / dup_factor

    label = "DACE OFF" if cl == 0 else ("DACE ON auto" if cl == -1 else f"DACE ON cl={cl}")

    return {
        "cl": cl, "label": label,
        "send": len(send_rows), "recv": len(recv_rows), "decoded": len(decode_rows),
        "recv_fail": len(recv_fail_rows),
        "psnr_ss": stat([r["psnr"] for r in ss]),
        "ssim_ss": stat([r["ssim"] for r in ss]),
        "nal_ss":  stat([r["nal_b"] for r in ss]),
        "enc_us":  stat([r["enc_us"] for r in send_rows]),
        "dec_us":  stat([r["dec_us"] for r in decode_rows]),
        "lat_us":  stat(lats),
        "enc_kbps": encoded_bitrate_kbps(send_rows),
        "ble_kbps": ble_avg,
        "recv_kbps": rcv_kbps,
        "frags_sent": frags_sent, "frags_recv": frags_recv,
        "frames_done": frames_done, "dup_factor": dup_factor,
    }


def analyze_csv(csv_path, cl):
    send_rows, lats = [], []
    recv_count = 0
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            seq = int(row["seq"])
            send_rows.append({
                "seq": seq, "cl": int(row["cl"]),
                "nal_b": int(row["nal_b"]), "psnr": float(row["psnr_db"]),
                "ssim": float(row["ssim"]), "enc_us": int(row["enc_us"]),
            })
            if row.get("lat_us"):
                lats.append(int(row["lat_us"]))
                recv_count += 1

    ss = [r for r in send_rows if r["seq"] >= 2]

    label = "DACE OFF" if cl == 0 else ("DACE ON auto" if cl == -1 else f"DACE ON cl={cl}")
    return {
        "cl": cl, "label": label,
        "send": len(send_rows), "recv": recv_count,
        "recv_fail": 0,
        "psnr_ss": stat([r["psnr"] for r in ss]),
        "ssim_ss": stat([r["ssim"] for r in ss]),
        "nal_ss":  stat([r["nal_b"] for r in ss]),
        "enc_us":  stat([r["enc_us"] for r in send_rows]),
        "lat_us":  stat(lats),
        "enc_kbps": 0.0,
        "ble_kbps": 0.0, "recv_kbps": 0.0,
        "frags_sent": 0, "frags_recv": 0, "frames_done": 0, "dup_factor": 1,
    }


def print_result(s):
    if s["dup_factor"] > 1:
        print(f"  [warn] duplicate BLE connections ×{s['dup_factor']} — counts divided")

    frame_pct = 100.0 * s["recv"] / s["send"] if s["send"] > 0 else 0.0
    decode_pct = 100.0 * s.get("decoded", 0) / s["send"] if s["send"] > 0 else 0.0
    frag_pct  = (100.0 * s["frags_recv"] / s["frags_sent"]
                 if s.get("frags_sent", 0) > 0 else float("nan"))
    frag_str  = f"{frag_pct:5.1f}%" if not math.isnan(frag_pct) else "   n/a"
    psnr = s["psnr_ss"]["avg"]
    ssim = s["ssim_ss"]["avg"]
    enc  = s["enc_us"]["avg"] / 1000
    dec  = s.get("dec_us", {}).get("avg", float("nan")) / 1000
    lat  = s["lat_us"]["avg"] / 1000 if s["lat_us"]["n"] > 0 else float("nan")

    print("═" * 90)
    print(f"  {s['label']}  (cl={s['cl']})")
    print("─" * 90)
    print(f"  SEND={s['send']}  RECV={s['recv']}  DECODED={s.get('decoded', 0)}  FAIL={s['recv_fail']}")
    print(f"  frame recv: {frame_pct:.1f}%  decode: {decode_pct:.1f}%  frag delivery: {frag_str}")
    print(f"  BLE link: {s['ble_kbps']:.1f} kbps  recv: {s['recv_kbps']:.1f} kbps"
          f"  enc: {s['enc_kbps']:.1f} kbps")
    print(f"  PSNR ss: {psnr:.2f} dB  (n={s['psnr_ss']['n']}, "
          f"min={s['psnr_ss']['min']:.2f}, max={s['psnr_ss']['max']:.2f})")
    print(f"  SSIM ss: {ssim:.4f}  (n={s['ssim_ss']['n']})")
    print(f"  enc avg: {enc:.1f} ms  p95: {s['enc_us']['p95']/1000:.1f} ms")
    if not math.isnan(dec):
        print(f"  dec avg: {dec:.1f} ms  p95: {s['dec_us']['p95']/1000:.1f} ms")
    if not math.isnan(lat):
        print(f"  e2e lat: {lat:.1f} ms  (n={s['lat_us']['n']})")
    print("═" * 90)


def main():
    ap = argparse.ArgumentParser(description="Analyze ble_video_test logcat files")
    ap.add_argument("--send", default="/tmp/ble_video_send_T1AIOC656909KGK.log")
    ap.add_argument("--recv", default="/tmp/ble_video_recv_8e27af28.log")
    ap.add_argument("--csv",  default=None, help="Use CSV instead of raw logs")
    ap.add_argument("--cl",   type=int, default=-1, help="CL value for labeling")
    ap.add_argument("--all-cls", action="store_true",
                    help="Auto-detect all /tmp/ble_video_cl*.csv files")
    args = ap.parse_args()

    import glob as _glob
    import os

    if args.all_cls:
        csv_files = sorted(_glob.glob("/tmp/ble_video_cl*.csv"))
        if not csv_files:
            print("No /tmp/ble_video_cl*.csv files found.")
            sys.exit(1)
        results = []
        for fp in csv_files:
            m = re.search(r"cl(-?\d+)\.csv$", fp)
            cl = int(m.group(1)) if m else 0
            print(f"\nAnalyzing {fp} (cl={cl}) …")
            s = analyze_csv(fp, cl)
            print_result(s)
            results.append(s)

        if len(results) >= 2:
            on  = next((r for r in results if r["cl"] == -1), None)
            off = next((r for r in results if r["cl"] == 0), None)
            if on and off:
                d_psnr = on["psnr_ss"]["avg"] - off["psnr_ss"]["avg"]
                d_ssim = on["ssim_ss"]["avg"] - off["ssim_ss"]["avg"]
                ratio  = (on["enc_us"]["avg"] / off["enc_us"]["avg"]
                          if off["enc_us"]["avg"] > 0 else float("nan"))
                print(f"\n  DACE ON vs OFF:  ΔPSNR={d_psnr:+.2f}dB  "
                      f"ΔSSIM={d_ssim:+.4f}  enc ratio={ratio:.1f}×")
        return

    if args.csv:
        s = analyze_csv(args.csv, args.cl)
        print_result(s)
        return

    for path in [args.send, args.recv]:
        if not os.path.exists(path):
            print(f"ERROR: {path} not found. Run ble_video_test.py first.")
            sys.exit(1)

    with open(args.send) as f:
        send_text = f.read()
    with open(args.recv) as f:
        recv_text = f.read()

    s = analyze_logs(send_text, recv_text, args.cl)
    print_result(s)


if __name__ == "__main__":
    main()
