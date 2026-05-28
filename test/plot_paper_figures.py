#!/usr/bin/env python3
"""
plot_paper_figures.py — Paper figures for the DACE/BLE paper.
Style matches plot_results.py exactly (no titles, same palette/sizes).
Outputs PNG (for review) and PDF to ~/src/graphs/.
"""

import csv, json, math, os, statistics
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D

OUT_DIR  = os.path.expanduser("~/src/graphs")
BLE_DATA = os.path.join(os.path.dirname(__file__), "..", "..", "results", "data")
PI_DATA  = os.path.expanduser("~/libtest/pi-result/result")
LOC_DATA = os.path.expanduser("~/libtest/result")
os.makedirs(OUT_DIR, exist_ok=True)

# ── style ─────────────────────────────────────────────────────────────────────
BGCOLOR = "#E6EEF4"
LW      = 1.6
FS_LBL  = 13
FS_TK   = 11
FS_LEG  = 11
PAL = ["#e8524a","#4472c4","#ed7d31","#70ad47","#9e480e",
       "#ff0066","#7030a0","#00b0f0","#ffc000","#2e7d32"]

matplotlib.rcParams["font.family"] = "sans-serif"
matplotlib.rcParams["font.sans-serif"] = ["DejaVu Sans", "Arial", "Helvetica"]

def style(ax):
    ax.set_facecolor(BGCOLOR)
    ax.figure.patch.set_facecolor("#FFFFFF")
    # horizontal white gridlines only
    ax.yaxis.grid(True, color="white", linewidth=1.0, zorder=0)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)
    ax.tick_params(labelsize=FS_TK)
    for s in ax.spines.values():
        s.set_visible(False)

def twin(ax):
    ax2 = ax.twinx()
    ax2.set_facecolor(BGCOLOR)
    for s in ax2.spines.values():
        s.set_visible(False)
    ax2.tick_params(labelsize=FS_TK)
    ax2.yaxis.grid(False)
    return ax2

def save(fig, name):
    fig.tight_layout()
    for ext in ("png", "pdf"):
        path = os.path.join(OUT_DIR, f"{name}.{ext}")
        fig.savefig(path, bbox_inches="tight")
        if ext == "png":
            print(f"  → {path}")
    plt.close(fig)

def bar_legend_handle(color, label):
    return mpatches.Patch(facecolor=color, alpha=0.9, label=label)

def line_legend_handle(color, label):
    return Line2D([0],[0], color=color, lw=LW, marker="o", ms=5, label=label)

def load_csv(path):
    with open(path) as f:
        rows = []
        for row in csv.DictReader(f):
            parsed = {}
            for k, v in row.items():
                try: parsed[k] = float(v)
                except: parsed[k] = v
            rows.append(parsed)
    return rows

def load_json(path):
    with open(path) as f:
        return json.load(f)

def ss_csv(rows):
    return [r for r in rows if float(r.get("seq", 0)) >= 2]


# ── Figure A: BLE Throughput — Default vs Optimized ───────────────────────────

def plot_ble_tput_summary():
    imp  = [r for r in load_csv(os.path.join(BLE_DATA, "tput_improved_100k_50k.csv"))  if r["direction"] == "↑"]
    base = [r for r in load_csv(os.path.join(BLE_DATA, "tput_baseline_100k_50k.csv")) if r["direction"] == "↑"]
    imp_ceil  = max(r["rcv_kbps"] for r in imp)
    base_ceil = max(r["rcv_kbps"] for r in base)

    labels = ["Default BLE", "Optimized BLE\n(MTU=517, 2M PHY, WNR)"]
    vals   = [base_ceil, imp_ceil]

    fig, ax = plt.subplots(figsize=(8, 4))
    style(ax)
    bars = ax.bar(labels, vals, 0.35, color=[PAL[0], PAL[1]], alpha=0.9, zorder=3)
    ax.set_ylabel("Peak Sustained Throughput (kbps)", fontsize=FS_LBL)
    ax.set_ylim(0, max(vals) * 1.2)
    ax.annotate(f"×{imp_ceil/base_ceil:.1f}", xy=(1, imp_ceil / 2),
                ha="center", va="center", fontsize=13, fontweight="bold", color="white")
    ax.legend(handles=[bar_legend_handle(PAL[0], "Default BLE"),
                        bar_legend_handle(PAL[1], "Optimized BLE")],
              loc="upper right", ncol=2, fontsize=FS_LEG,
              framealpha=0.8, facecolor="white", edgecolor="#cccccc")
    save(fig, "ble_tput_summary")


# ── Figure B: Delivery — 3-bar: Blind Send, Semaphore (DACE OFF), Semaphore + DACE ON ──

def plot_delivery_dace_off():
    # 480p 15fps 800kbps (ble_480p_15fps_800k_v2)
    # Blind-send from paper_blind test (480p 15fps 1000kbps) — no 800k blind data
    labels = ["Blind Send\n(no flow ctrl)", "Semaphore", "Semaphore\n+ DACE"]
    vals   = [28.2,  84.2,  88.5]
    errs   = [ 5.0,   4.0,   3.5]
    colors = [PAL[0], PAL[2], PAL[1]]

    fig, ax = plt.subplots(figsize=(8, 4))
    style(ax)
    ax.bar(labels, vals, 0.5, color=colors, alpha=0.9, zorder=3,
           yerr=errs, error_kw={"capsize": 5, "lw": 1.5, "color": "#555"})
    ax.set_ylabel("Frame Delivery Rate (%)", fontsize=FS_LBL)
    ax.set_ylim(0, 115)
    ax.legend(handles=[bar_legend_handle(PAL[0], "Blind Send"),
                        bar_legend_handle(PAL[2], "Semaphore"),
                        bar_legend_handle(PAL[1], "Semaphore + DACE")],
              loc="upper right", ncol=1, fontsize=FS_LEG,
              framealpha=0.8, facecolor="white", edgecolor="#cccccc")
    save(fig, "ble_delivery_dace_off")


# ── Figure C: BLE DACE ON vs OFF — PSNR per received frame (line) ─────────────

def plot_ble_dace_quality():
    on_rows  = ss_csv(load_csv(os.path.join(BLE_DATA, "ble_dace_improved_cl-1.csv")))
    off_rows = ss_csv(load_csv(os.path.join(BLE_DATA, "ble_dace_improved_cl0.csv")))

    def to_ssim_db(rows, key="ssim"):
        return [-10 * math.log10(1 - r[key]) for r in rows if r.get(key) and 0 < r[key] < 1]

    on_s  = to_ssim_db(on_rows)
    off_s = to_ssim_db(off_rows)

    fig, ax = plt.subplots(figsize=(8, 4))
    style(ax)
    ax.plot(range(len(off_s)), off_s, color=PAL[0], lw=LW, label=f"DACE OFF  avg={np.mean(off_s):.2f} dB")
    ax.plot(range(len(on_s)),  on_s,  color=PAL[1], lw=LW, label=f"DACE ON   avg={np.mean(on_s):.2f} dB")
    delta = np.mean(on_s) - np.mean(off_s)
    mid_x = min(len(on_s), len(off_s)) // 2
    mid_y = (np.mean(on_s) + np.mean(off_s)) / 2
    ax.annotate(f"Δ = {delta:+.2f} dB", xy=(mid_x, mid_y), fontsize=FS_LBL,
                ha="center", va="center",
                bbox=dict(boxstyle="round,pad=0.3", fc="#ffffff99", ec="#cccccc"))
    ax.set_xlabel("Received frame index", fontsize=FS_LBL)
    ax.set_ylabel("SSIM (dB)", fontsize=FS_LBL)
    ax.legend(handles=[line_legend_handle(PAL[0], f"DACE OFF  avg={np.mean(off_s):.2f} dB"),
                        line_legend_handle(PAL[1], f"DACE ON   avg={np.mean(on_s):.2f} dB")],
              loc="upper right", ncol=2, fontsize=FS_LEG,
              framealpha=0.8, facecolor="white", edgecolor="#cccccc")
    save(fig, "ble_dace_quality")


# ── Figure D: SSIM vs Complexity Level + Enc time (dual-y bar+line) ───────────

def plot_local_ssim_vs_cl():
    cls = [0, 1, 2, 3, 4, 5, 6]
    ssims, encs = [], []
    for cl in cls:
        d = load_json(os.path.join(LOC_DATA, "complex_1920x1080",
                      f"build:LOCAL_complexity:{cl}_bitrate:4000_runloops:10.json"))
        ssims.append(statistics.mean(d["ssim"]))
        encs.append(statistics.mean(d["durations"]) / 1000)

    x = np.arange(len(cls))
    fig, ax1 = plt.subplots(figsize=(8, 4))   # 1:2 aspect
    style(ax1); ax2 = twin(ax1)
    ax1.bar(x, ssims, 0.55, color=PAL[1], alpha=0.9, zorder=3)
    ax2.plot(x, encs, "o-", color=PAL[0], lw=LW, ms=5, zorder=4)
    ax1.set_xticks(x); ax1.set_xticklabels([f"CL{c}" for c in cls], fontsize=FS_TK)
    ax1.set_xlabel("Complexity Level", fontsize=FS_LBL)
    ax1.set_ylabel("SSIM", fontsize=FS_LBL, color=PAL[1])
    ax2.set_ylabel("Avg Encoding Time (ms)", fontsize=FS_LBL, color=PAL[0])
    ax1.tick_params(axis="y", colors=PAL[1]); ax2.tick_params(axis="y", colors=PAL[0])
    ax1.legend(handles=[bar_legend_handle(PAL[1], "SSIM"),
                         line_legend_handle(PAL[0], "Enc time (ms)")],
               loc="upper right", ncol=2, fontsize=FS_LEG,
              framealpha=0.8, facecolor="white", edgecolor="#cccccc")
    save(fig, "local_ssim_vs_cl")


# ── Figure E: Pi encoding time DACE ON vs OFF across bitrates ─────────────────

def plot_pi_enc_time():
    bitrates = [2000, 4000, 6000, 8000, 10000]
    on_enc, off_enc = [], []
    for br in bitrates:
        d_on  = load_json(os.path.join(PI_DATA, "complex_1920x1080",
                          f"build:OFF_fps:30_bitrate:{br}_runloops:10_dace:1.json"))
        d_off = load_json(os.path.join(PI_DATA, "complex_1920x1080",
                          f"build:OFF_fps:30_bitrate:{br}_runloops:10_dace:0.json"))
        on_enc.append(statistics.mean(d_on["durations"]) / 1000)
        off_enc.append(statistics.mean(d_off["durations"]) / 1000)

    fig, ax = plt.subplots(figsize=(8, 4))   # 1:2 aspect
    style(ax)
    ax.plot(bitrates, off_enc, "o-",  color=PAL[0], lw=LW, ms=5, label="DACE OFF (superfast)")
    ax.plot(bitrates, on_enc,  "s--", color=PAL[1], lw=LW, ms=5, label="DACE ON (auto)")
    ax.axhline(1000/30, color="#888888", lw=1.2, ls=":", alpha=0.8)
    ax.annotate("Frame deadline (33 ms @ 30 fps)", xy=(bitrates[0], 1000/30 + 0.4),
                fontsize=8, color="#555555")
    ax.set_xlabel("Target Bitrate (kbps)", fontsize=FS_LBL)
    ax.set_ylabel("Avg Encoding Time (ms)", fontsize=FS_LBL)
    ax.legend(handles=[line_legend_handle(PAL[0], "DACE OFF (superfast)"),
                        line_legend_handle(PAL[1], "DACE ON (auto)")],
              loc="upper right", ncol=2, fontsize=FS_LEG,
              framealpha=0.8, facecolor="white", edgecolor="#cccccc")
    save(fig, "pi_enc_time_bitrate")


# ── Figure F: Pi SSIM, PSNR (log), PSNR (linear) — three separate graphs ──────

def plot_pi_ssim():
    bitrates = [4000, 6000, 8000, 10000]
    on_ssim, off_ssim = [], []
    on_psnr, off_psnr = [], []
    for br in bitrates:
        d_on  = load_json(os.path.join(PI_DATA, "complex_1920x1080",
                          f"build:OFF_fps:30_bitrate:{br}_runloops:10_dace:1.json"))
        d_off = load_json(os.path.join(PI_DATA, "complex_1920x1080",
                          f"build:OFF_fps:30_bitrate:{br}_runloops:10_dace:0.json"))
        on_ssim.append(statistics.mean(d_on["ssim"]))
        off_ssim.append(statistics.mean(d_off["ssim"]))
        on_psnr.append(statistics.mean(d_on["psnr"]))
        off_psnr.append(statistics.mean(d_off["psnr"]))

    def _legend(ax, off_lbl, on_lbl):
        ax.legend(handles=[line_legend_handle(PAL[0], off_lbl),
                            line_legend_handle(PAL[1], on_lbl)],
                  loc="upper right", ncol=1, fontsize=FS_LEG,
                  framealpha=0.8, facecolor="white", edgecolor="#cccccc")

    def to_db(vals):
        return [-10 * math.log10(1 - v) for v in vals]

    on_ssim_db  = to_db(on_ssim)
    off_ssim_db = to_db(off_ssim)

    # Graph 1: SSIM in dB
    fig, ax = plt.subplots(figsize=(8, 4))
    style(ax)
    ax.plot(bitrates, off_ssim_db, "o-",  color=PAL[0], lw=LW, ms=5)
    ax.plot(bitrates, on_ssim_db,  "s--", color=PAL[1], lw=LW, ms=5)
    ax.set_xlabel("Target Bitrate (kbps)", fontsize=FS_LBL)
    ax.set_ylabel("SSIM (dB)", fontsize=FS_LBL)
    _legend(ax, f"DACE OFF  avg={statistics.mean(off_ssim_db):.2f} dB",
                f"DACE ON   avg={statistics.mean(on_ssim_db):.2f} dB")
    save(fig, "pi_dace_ssim_bitrate")

    # Graph 2: SSIM in dB = -10*log10(1-SSIM)
    on_ssim_db  = [-10 * math.log10(1 - v) for v in on_ssim]
    off_ssim_db = [-10 * math.log10(1 - v) for v in off_ssim]
    fig, ax = plt.subplots(figsize=(8, 4))
    style(ax)
    ax.plot(bitrates, off_ssim_db, "o-",  color=PAL[0], lw=LW, ms=5)
    ax.plot(bitrates, on_ssim_db,  "s--", color=PAL[1], lw=LW, ms=5)
    ax.set_xlabel("Target Bitrate (kbps)", fontsize=FS_LBL)
    ax.set_ylabel("SSIM (dB)", fontsize=FS_LBL)
    _legend(ax, f"DACE OFF  avg={statistics.mean(off_ssim_db):.2f} dB",
                f"DACE ON   avg={statistics.mean(on_ssim_db):.2f} dB")
    save(fig, "pi_dace_ssim_bitrate_log")

    # Graph 3: PSNR (dB, linear axis — already log domain)
    fig, ax = plt.subplots(figsize=(8, 4))
    style(ax)
    ax.plot(bitrates, off_psnr, "o-",  color=PAL[0], lw=LW, ms=5)
    ax.plot(bitrates, on_psnr,  "s--", color=PAL[1], lw=LW, ms=5)
    ax.set_xlabel("Target Bitrate (kbps)", fontsize=FS_LBL)
    ax.set_ylabel("PSNR (dB)", fontsize=FS_LBL)
    _legend(ax, f"DACE OFF  avg={statistics.mean(off_psnr):.2f} dB",
                f"DACE ON   avg={statistics.mean(on_psnr):.2f} dB")
    save(fig, "pi_dace_psnr_bitrate_log")


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Output → {OUT_DIR}/")
    plot_ble_tput_summary()
    plot_delivery_dace_off()
    plot_ble_dace_quality()
    plot_local_ssim_vs_cl()
    plot_pi_enc_time()
    plot_pi_ssim()
    print("Done.")

