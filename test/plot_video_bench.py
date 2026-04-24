#!/usr/bin/env python3
"""
plot_video_bench.py — Plot latency breakdown and delivery rate from ble_video_test.py JSON output.

Usage:
  # Latency breakdown + single-mode delivery:
  python3 test/plot_video_bench.py --json /tmp/ble_video_averaged.json

  # Delivery comparison: backpressure vs blind-send:
  python3 test/plot_video_bench.py --bp-json /tmp/ble_video_bp_averaged.json \
                                   --blind-json /tmp/ble_video_blind_averaged.json

  # Both charts at once:
  python3 test/plot_video_bench.py --json /tmp/ble_video_averaged.json \
                                   --bp-json /tmp/ble_video_bp_averaged.json \
                                   --blind-json /tmp/ble_video_blind_averaged.json

  --out  output directory (default: test/results/)
"""

import argparse
import json
import math
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

OUT_DIR = os.path.join(os.path.dirname(__file__), "results")

# ── style (matches plot_results.py) ──────────────────────────────────────────
BGCOLOR = "#dce6f4"
GRID    = "#c0cfe8"
LW      = 1.6
FS_LBL  = 13
FS_TK   = 11
FS_LEG  = 11
PAL = ["#e8524a", "#4472c4", "#ed7d31", "#70ad47", "#9e480e",
       "#ff0066", "#7030a0", "#00b0f0", "#ffc000", "#2e7d32"]

ENC_COL = PAL[2]   # orange
NET_COL = PAL[1]   # blue
DEC_COL = PAL[3]   # green


def style(ax):
    ax.set_facecolor(BGCOLOR)
    ax.figure.patch.set_facecolor(BGCOLOR)
    ax.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.tick_params(labelsize=FS_TK)
    for s in ax.spines.values():
        s.set_visible(False)


def load_json(path):
    with open(path) as f:
        return json.load(f)


def get_result(data, cl_key):
    """Return result dict for a CL key (int or str)."""
    r = data["results"].get(str(cl_key))
    if r is None:
        return None
    return r


def safe(val, default=0.0):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return default
    return val


def expected_frames(meta):
    fps = safe(meta.get("fps"), 0)
    duration = safe(meta.get("duration"), 0)
    return fps * duration if fps > 0 and duration > 0 else 0


def corrected_send_stats(result, meta):
    """Return (send, send_std, dup_factor) with legacy-send auto-correction.

    Old JSONs can contain duplicated SEND rows (~2x expected frame count).
    Detect this from meta fps*duration and scale send/send_std down for plotting.
    """
    send = safe(result.get("send"), 0)
    send_std = safe(result.get("send_std"), 0)
    exp = expected_frames(meta)
    if exp <= 0 or send <= 0:
        return send, send_std, 1

    ratio = send / exp
    dup = int(round(ratio))
    if dup >= 2:
        corrected = send / dup
        # Accept correction when corrected count is close to expected.
        if abs(corrected - exp) <= 0.25 * exp:
            return corrected, send_std / dup, dup
    return send, send_std, 1


# ── Chart 1: Latency breakdown horizontal stacked bar ────────────────────────

def plot_latency_breakdown(data, out_dir):
    """Horizontal stacked bar: Enc | Net | Dec for DACE ON and DACE OFF."""
    on  = get_result(data, -1)
    off = get_result(data, 0)
    if not on or not off:
        print("[latency] Need both cl=-1 and cl=0 in JSON, skipping.")
        return

    labels = ["DACE OFF (cl=0)", "DACE ON auto (cl=-1)"]
    rows   = [off, on]

    enc_ms  = [safe(r["enc_us"]["avg"]) / 1000 for r in rows]
    net_ms  = [safe(r["net_us"]["avg"]) / 1000 for r in rows]
    dec_ms  = [safe(r["dec_us"]["avg"]) / 1000 for r in rows]
    e2e_ms  = [safe(r["lat_us"]["avg"]) / 1000 for r in rows]
    e2e_std = [safe(r["lat_us"].get("std", 0)) / 1000 for r in rows]

    fig, ax = plt.subplots(figsize=(10, 3.5))
    style(ax)

    y = np.arange(len(labels))
    h = 0.45

    bars_enc = ax.barh(y, enc_ms, h, color=ENC_COL, alpha=0.9, label="Encode", zorder=3)
    bars_net = ax.barh(y, net_ms, h, left=enc_ms, color=NET_COL, alpha=0.9, label="Network", zorder=3)
    left_dec = [e + n for e, n in zip(enc_ms, net_ms)]
    bars_dec = ax.barh(y, dec_ms, h, left=left_dec, color=DEC_COL, alpha=0.9, label="Decode", zorder=3)

    # E2E error bars
    for i, (e2e, std) in enumerate(zip(e2e_ms, e2e_std)):
        ax.errorbar(e2e, y[i], xerr=std, fmt="none", color="#333333", capsize=5, lw=1.5, zorder=5)

    # Value labels inside bars
    for i, (e, n, d) in enumerate(zip(enc_ms, net_ms, dec_ms)):
        if e > 20:
            ax.text(e / 2, y[i], f"{e:.0f}ms", ha="center", va="center",
                    fontsize=9, color="white", fontweight="bold")
        if n > 20:
            ax.text(e + n / 2, y[i], f"{n:.0f}ms", ha="center", va="center",
                    fontsize=9, color="white", fontweight="bold")
        if d > 5:
            ax.text(e + n + d / 2, y[i], f"{d:.0f}ms", ha="center", va="center",
                    fontsize=9, color="white", fontweight="bold")

    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=FS_TK)
    ax.set_xlabel("Latency (ms)", fontsize=FS_LBL)
    ax.set_title(f"E2E Latency Breakdown  ({data['meta']['w']}×{data['meta']['h']} "
                 f"@ {data['meta']['fps']}fps  {data['meta']['bitrate']//1000}kbps  "
                 f"n={data['meta']['runs']} runs)",
                 fontsize=FS_LBL, pad=8)
    ax.legend(loc="lower right", fontsize=FS_LEG, framealpha=0.7, edgecolor=GRID)
    fig.tight_layout()

    path = os.path.join(out_dir, "latency_breakdown.pdf")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  → {path}")


# ── Chart 2: Delivery rate comparison (backpressure vs blind-send) ────────────

def plot_delivery_comparison(bp_data, blind_data, out_dir):
    """Grouped bar chart: frame delivery % for BP vs blind, DACE ON and OFF."""
    groups = [
        ("DACE ON\n(cl=-1)",  -1, PAL[1], PAL[0]),
        ("DACE OFF\n(cl=0)",   0, PAL[3], PAL[2]),
    ]

    fig, ax = plt.subplots(figsize=(8, 4.5))
    style(ax)

    x      = np.arange(len(groups))
    w      = 0.32
    offset = [-w/2, w/2]
    labels_legend = ["Backpressure (app-level flow ctrl)", "Blind send (no flow ctrl)"]
    datasets = [bp_data, blind_data]
    bar_colors = [PAL[1], PAL[0]]
    meta = bp_data.get("meta", {})

    for di, (dset, lbl, col) in enumerate(zip(datasets, labels_legend, bar_colors)):
        vals = []
        errs = []
        for _, cl, _, _ in groups:
            r = get_result(dset, cl)
            if r:
                send, _, dup = corrected_send_stats(r, meta)
                if dup > 1:
                    print(f"[delivery] cl={cl}: auto-corrected send by /{dup} (legacy duplicated SEND logs)")
                recv = safe(r.get("recv"), 0)
                recv_std = safe(r.get("recv_std", 0), 0)
                if send > 0:
                    pct = 100.0 * recv / send
                    std = 100.0 * recv_std / send
                else:
                    pct, std = 0.0, 0.0
            else:
                pct, std = 0.0, 0.0
            vals.append(pct)
            errs.append(std)

        bars = ax.bar(x + offset[di], vals, w, color=col, alpha=0.9,
                      label=lbl, zorder=3, yerr=errs,
                      error_kw={"capsize": 4, "lw": 1.5, "color": "#333333"})
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1.5,
                    f"{v:.1f}%", ha="center", va="bottom", fontsize=9)

    ax.set_xticks(x)
    ax.set_xticklabels([g[0] for g in groups], fontsize=FS_TK)
    ax.set_ylabel("Frame delivery rate (%)", fontsize=FS_LBL)
    ax.set_ylim(0, 115)

    ax.set_title(f"Frame Delivery: Backpressure vs Blind Send  "
                 f"({meta['w']}×{meta['h']} @ {meta['fps']}fps  {meta['bitrate']//1000}kbps)",
                 fontsize=FS_LBL, pad=8)
    ax.legend(loc="upper right", fontsize=FS_LEG, framealpha=0.7, edgecolor=GRID)
    fig.tight_layout()

    path = os.path.join(out_dir, "delivery_comparison.pdf")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  → {path}")


# ── Chart 3: Delivery rate — DACE OFF only, backpressure vs blind-send ────────

def plot_delivery_dace_off(bp_data, blind_data, out_dir):
    """Single-group bar: DACE OFF delivery % for backpressure vs blind-send."""
    meta = bp_data.get("meta", {})
    datasets     = [bp_data,                          blind_data]
    labels       = ["Backpressure\n(app-level flow ctrl)", "Blind send\n(no flow ctrl)"]
    bar_colors   = [PAL[1], PAL[0]]

    vals, errs = [], []
    for dset in datasets:
        r = get_result(dset, 0)  # cl=0 = DACE OFF
        if r:
            send, _, dup = corrected_send_stats(r, meta)
            recv     = safe(r.get("recv"), 0)
            recv_std = safe(r.get("recv_std", 0), 0)
            pct = 100.0 * recv / send if send > 0 else 0.0
            std = 100.0 * recv_std / send if send > 0 else 0.0
        else:
            pct, std = 0.0, 0.0
        vals.append(pct)
        errs.append(std)

    fig, ax = plt.subplots(figsize=(5, 4.5))
    style(ax)

    x = np.arange(len(labels))
    bars = ax.bar(x, vals, 0.45, color=bar_colors, alpha=0.9, zorder=3,
                  yerr=errs, error_kw={"capsize": 5, "lw": 1.5, "color": "#333333"})
    for bar, v in zip(bars, vals):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1.5,
                f"{v:.1f}%", ha="center", va="bottom", fontsize=11, fontweight="bold")

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=FS_TK)
    ax.set_ylabel("Frame delivery rate (%)", fontsize=FS_LBL)
    ax.set_ylim(0, 115)
    ax.set_title(f"DACE OFF — Delivery: Backpressure vs Blind Send\n"
                 f"({meta.get('w')}×{meta.get('h')} @ {meta.get('fps')}fps  "
                 f"{meta.get('bitrate', 0)//1000}kbps  n={meta.get('runs')} runs)",
                 fontsize=FS_LBL, pad=8)
    fig.tight_layout()

    path = os.path.join(out_dir, "delivery_dace_off.pdf")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  → {path}")



def main():
    ap = argparse.ArgumentParser(description="Plot video bench results from averaged JSON")
    ap.add_argument("--json",       default=None, help="Averaged JSON (latency + single delivery)")
    ap.add_argument("--bp-json",    default=None, help="Backpressure averaged JSON")
    ap.add_argument("--blind-json", default=None, help="Blind-send averaged JSON")
    ap.add_argument("--out",        default=OUT_DIR, help="Output directory")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    if not args.json and not args.bp_json and not args.blind_json:
        ap.print_help()
        sys.exit(1)

    print(f"Output → {args.out}/")

    if args.json:
        data = load_json(args.json)
        plot_latency_breakdown(data, args.out)

    if args.bp_json and args.blind_json:
        bp_data    = load_json(args.bp_json)
        blind_data = load_json(args.blind_json)
        plot_delivery_comparison(bp_data, blind_data, args.out)
        plot_delivery_dace_off(bp_data, blind_data, args.out)
    elif args.bp_json or args.blind_json:
        print("[delivery] Need both --bp-json and --blind-json to plot delivery comparison.")


if __name__ == "__main__":
    main()
