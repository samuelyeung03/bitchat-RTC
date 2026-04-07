#!/usr/bin/env python3
"""
dace_compare.py  —  DACE on vs off (and per-CL sweep) comparison test.

Runs two back-to-back passes over the same camera stream:
  Pass A: DACE ON,  auto-complexity   (--complexity -1)
  Pass B: DACE OFF, plain x264        (--dace-off)

Optionally also sweeps fixed CL 0-9 for per-level data (--sweep).

Measures per-frame:
  • NAL size (bytes)             — bitstream size
  • PSNR estimate (dB)          — encoding quality via bpp proxy
  • DACE encoding time (µs)     — time spent inside x264_encoder_encode
  • One-way latency (µs)        — corrected for clock offset

Output: CSV files + printed summary table.

────────────────────────────────────────────────────────────────
HOW TO RERUN
────────────────────────────────────────────────────────────────
Prerequisites (one-time):
  1. Both Pi 5s connected via USB/ADB.
  2. Pi 5 #1 (SENDER_SERIAL) has Razer Kiyo X on /dev/video0.
  3. dace_hw_bench built and pushed (run build_and_push.sh or see below).
  4. Both Pi 5s in root mode: adb -s <serial> root

Build & push (if binary is missing):
  cd test/
  make hw
  adb -s 798f51f064cce0d1 push /tmp/dace_hw_bench /data/local/tmp/dace_hw_bench
  adb -s f501a6221ec14252 push /tmp/dace_hw_bench /data/local/tmp/dace_hw_bench

Run the comparison:
  python3 test/dace_compare.py                    # DACE on vs off (default)
  python3 test/dace_compare.py --sweep            # + per-CL 0-9 sweep
  python3 test/dace_compare.py --frames 300       # longer run (10 s @ 30fps)
  python3 test/dace_compare.py --out /tmp/mytest  # custom output prefix

Results are written to:
  <out>_dace_on.csv
  <out>_dace_off.csv
  <out>_cl_sweep.csv   (if --sweep)
  <out>_summary.txt
────────────────────────────────────────────────────────────────
"""

import subprocess, threading, time, argparse, math, os, sys

# ── device serials ─────────────────────────────────────────────────────────────
SENDER_SERIAL   = "798f51f064cce0d1"   # Pi1 — has Razer Kiyo X on /dev/video0
RECEIVER_SERIAL = "f501a6221ec14252"   # Pi2
PORT            = 5555
DEVICE_BIN      = "/data/local/tmp/dace_hw_bench"


# ── helpers ────────────────────────────────────────────────────────────────────
def adb(serial, *args, capture=True, timeout=30):
    cmd = ["adb", "-s", serial] + list(args)
    return subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)

def sync_clock(serial):
    ts = time.time()
    adb(serial, "shell", f"date -s @{ts:.3f}", timeout=10)

def measure_offset(serial, n=20):
    offsets = []
    for _ in range(n + 3):
        t1 = time.time()
        r = adb(serial, "shell", "date +%s%6N", timeout=5)
        t2 = time.time()
        if r.returncode != 0 or not r.stdout.strip().isdigit():
            continue
        dev = int(r.stdout.strip())
        mid = int(((t1 + t2) / 2) * 1e6)
        rtt = int((t2 - t1) * 1e6)
        offsets.append((rtt, mid - dev))
    offsets.sort()
    best = offsets[:max(1, len(offsets) // 4)]
    vals = [o[1] for o in best]
    return int(sum(vals) / len(vals)) if vals else 0

def setup_forwarding():
    subprocess.run(["adb", "-s", SENDER_SERIAL,   "forward", f"tcp:{PORT}", f"tcp:{PORT}"])
    subprocess.run(["adb", "-s", RECEIVER_SERIAL, "reverse", f"tcp:{PORT}", f"tcp:{PORT}"])

def run_pass(label, sender_args, clock_delta, n_frames, out_csv):
    """Run one sender+receiver pair, return parsed rows."""
    sender_cmd = (
        f"{DEVICE_BIN} --sender --cam /dev/video0 "
        f"--frames {n_frames} {sender_args} "
        f">/data/local/tmp/sender_{label}.csv 2>/data/local/tmp/sender_{label}.log"
    )
    receiver_cmd = (
        f"{DEVICE_BIN} --receiver --sender-ip 127.0.0.1 "
        f"--clock-delta {clock_delta} --save-frames 0 "
        f">/data/local/tmp/receiver_{label}.csv 2>/data/local/tmp/receiver_{label}.log"
    )

    sender_done = []; receiver_done = []

    def _sender():
        r = subprocess.run(["adb","-s",SENDER_SERIAL,"shell",sender_cmd],
                           capture_output=True, text=True, timeout=120)
        sender_done.append(r)

    def _receiver():
        r = subprocess.run(["adb","-s",RECEIVER_SERIAL,"shell",receiver_cmd],
                           capture_output=True, text=True, timeout=120)
        receiver_done.append(r)

    t_s = threading.Thread(target=_sender);   t_s.start()
    time.sleep(2)
    t_r = threading.Thread(target=_receiver); t_r.start()
    t_s.join(); t_r.join()

    # Pull receiver CSV
    subprocess.run(["adb","-s",RECEIVER_SERIAL,"pull",
                    f"/data/local/tmp/receiver_{label}.csv", out_csv],
                   capture_output=True)

    rows = []
    try:
        with open(out_csv) as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) >= 8 and parts[0].isdigit():
                    rows.append({
                        "seq":       int(parts[0]),
                        "nal_b":     int(parts[1]),
                        "psnr":      float(parts[2]),
                        "dace_us":   int(parts[3]),
                        "send_us":   int(parts[4]),
                        "recv_us":   int(parts[5]),
                        "corr_lat":  int(parts[7]),
                    })
    except Exception as e:
        print(f"  [warn] Could not parse {out_csv}: {e}")
    return rows

def stats(vals):
    if not vals: return {"avg": 0, "min": 0, "max": 0, "std": 0}
    avg = sum(vals) / len(vals)
    std = math.sqrt(sum((v-avg)**2 for v in vals) / len(vals))
    return {"avg": avg, "min": min(vals), "max": max(vals), "std": std}

def summarise(label, rows):
    if not rows:
        print(f"  {label}: no data")
        return {}
    lats  = [r["corr_lat"] for r in rows]
    psnrs = [r["psnr"] for r in rows if r["psnr"] > 0]
    nals  = [r["nal_b"]  for r in rows]
    daces = [r["dace_us"] for r in rows if r["dace_us"] > 0]
    ls = stats(lats); ps = stats(psnrs); ns = stats(nals); ds = stats(daces)
    return {"n": len(rows), "lat": ls, "psnr": ps, "nal": ns, "dace": ds}


# ── main ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="DACE on/off comparison test")
    ap.add_argument("--frames",  type=int, default=150, help="frames per pass (default 150 = 5s)")
    ap.add_argument("--sweep",   action="store_true",    help="also run CL 0-9 sweep")
    ap.add_argument("--out",     default="/tmp/dace_compare", help="output file prefix")
    args = ap.parse_args()

    print("=" * 60)
    print("  DACE comparison test")
    print(f"  frames/pass={args.frames}  sweep={args.sweep}")
    print(f"  sender:   {SENDER_SERIAL[:8]}… (Pi1, Razer Kiyo X)")
    print(f"  receiver: {RECEIVER_SERIAL[:8]}… (Pi2)")
    print("=" * 60)

    # ── Setup ──
    print("\n[1] Syncing clocks …")
    sync_clock(SENDER_SERIAL);   time.sleep(0.3)
    sync_clock(RECEIVER_SERIAL)

    print("[2] Measuring clock offsets …")
    off1 = measure_offset(SENDER_SERIAL)
    off2 = measure_offset(RECEIVER_SERIAL)
    clock_delta = off1 - off2   # corrected_lat = raw_lat - clock_delta
    print(f"    off1={off1:+d} µs  off2={off2:+d} µs  delta={clock_delta:+d} µs")

    setup_forwarding()
    print("[3] ADB forwarding ready\n")

    results = {}

    # ── Pass A: DACE ON, auto ──
    print("[4] Pass A: DACE ON (auto-complexity) …")
    rows_on = run_pass("on", "--complexity -1",
                       clock_delta, args.frames,
                       f"{args.out}_dace_on.csv")
    results["DACE-ON(auto)"] = summarise("DACE-ON(auto)", rows_on)
    print(f"    → {len(rows_on)} frames received")

    # ── Pass B: DACE OFF ──
    print("[5] Pass B: DACE OFF (plain x264) …")
    # Re-sync before second pass
    ts = time.time()
    adb(SENDER_SERIAL,   "shell", f"date -s @{ts:.3f}", timeout=5)
    adb(RECEIVER_SERIAL, "shell", f"date -s @{ts:.3f}", timeout=5)
    off1b = measure_offset(SENDER_SERIAL)
    off2b = measure_offset(RECEIVER_SERIAL)
    clock_delta_b = off1b - off2b
    setup_forwarding()

    rows_off = run_pass("off", "--dace-off",
                        clock_delta_b, args.frames,
                        f"{args.out}_dace_off.csv")
    results["DACE-OFF(x264)"] = summarise("DACE-OFF(x264)", rows_off)
    print(f"    → {len(rows_off)} frames received")

    # ── Optional CL sweep ──
    if args.sweep:
        print("\n[6] CL sweep (0-9) …")
        all_sweep_rows = []
        for cl in range(10):
            ts = time.time()
            adb(SENDER_SERIAL,   "shell", f"date -s @{ts:.3f}", timeout=5)
            adb(RECEIVER_SERIAL, "shell", f"date -s @{ts:.3f}", timeout=5)
            o1 = measure_offset(SENDER_SERIAL)
            o2 = measure_offset(RECEIVER_SERIAL)
            cd = o1 - o2
            setup_forwarding()
            out_cl = f"{args.out}_cl{cl}.csv"
            rows_cl = run_pass(f"cl{cl}", f"--complexity {cl}",
                               cd, args.frames, out_cl)
            key = f"CL{cl}"
            results[key] = summarise(key, rows_cl)
            # tag rows with CL
            for r in rows_cl: r["cl"] = cl
            all_sweep_rows.extend(rows_cl)
            print(f"    CL{cl}: {len(rows_cl)} frames  "
                  f"psnr={results[key]['psnr']['avg']:.1f}dB  "
                  f"lat={results[key]['lat']['avg']:.0f}µs  "
                  f"enc={results[key]['dace']['avg']:.0f}µs")

        # Write combined sweep CSV
        sweep_csv = f"{args.out}_cl_sweep.csv"
        with open(sweep_csv, "w") as f:
            f.write("cl,seq,nal_b,psnr_db,dace_enc_us,corr_lat_us\n")
            for r in all_sweep_rows:
                f.write(f"{r.get('cl','?')},{r['seq']},{r['nal_b']},{r['psnr']:.3f},"
                        f"{r['dace_us']},{r['corr_lat']}\n")
        print(f"    Sweep CSV: {sweep_csv}")

    # ── Summary ──
    summary_lines = []
    summary_lines.append("\n" + "=" * 78)
    summary_lines.append(f"{'Mode':<22} {'n':>4} {'PSNR avg':>10} {'NAL avg':>10} "
                         f"{'Enc avg':>10} {'Lat avg':>10} {'Lat min':>10} {'Lat max':>10}")
    summary_lines.append("-" * 78)
    for key, s in results.items():
        if not s: continue
        summary_lines.append(
            f"{key:<22} {s['n']:>4} "
            f"{s['psnr']['avg']:>9.2f}dB "
            f"{s['nal']['avg']:>8.0f}B "
            f"{s['dace']['avg']:>8.0f}µs "
            f"{s['lat']['avg']:>8.0f}µs "
            f"{s['lat']['min']:>8.0f}µs "
            f"{s['lat']['max']:>8.0f}µs"
        )
    summary_lines.append("=" * 78)
    summary_lines.append(f"\nFiles: {args.out}_dace_on.csv  {args.out}_dace_off.csv")
    if args.sweep:
        summary_lines.append(f"       {args.out}_cl_sweep.csv")
    summary_text = "\n".join(summary_lines)
    print(summary_text)

    with open(f"{args.out}_summary.txt", "w") as f:
        f.write(summary_text + "\n")
    print(f"\nSummary saved: {args.out}_summary.txt")


if __name__ == "__main__":
    main()
