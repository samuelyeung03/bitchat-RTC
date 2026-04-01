/*
 * dace_psnr_bench.c — DACE encoding benchmark with PSNR and one-way latency.
 *
 * SENDER MODE  (runs on Pi 5 #1):
 *   Encodes synthetic YUV420 frames, extracts per-frame PSNR from x264's
 *   reconstructed picture, and writes binary packets to --out file.
 *
 *   Packet layout (little-endian):
 *     [0..3]   magic     uint32  0x45434144 ('DACE')
 *     [4..7]   seq       uint32  frame index (global across all complexity levels)
 *     [8..11]  complexity uint32 DACE complexity level for this frame
 *     [12..19] send_us   int64   CLOCK_MONOTONIC at encode finish (µs)
 *     [20..27] psnr_y    double  luma PSNR (dB)  from x264 b_psnr
 *     [28..31] enc_time  uint32  x264 DACE_encoding_time (µs internal)
 *     [32..35] nal_size  uint32  bytes of NAL data following
 *     [36..]   nal_data  uint8[] Annex-B H.264 bitstream
 *
 * RECEIVER MODE  (runs on Pi 5 #2):
 *   Reads the packet file written by the sender, records receive timestamp
 *   for each packet, and prints a CSV report.
 *
 *   Output CSV columns:
 *     seq, complexity, psnr_y_db, enc_time_us, nal_size_b,
 *     send_us, recv_us, raw_latency_us, corrected_latency_us
 *
 *   corrected_latency = recv_us - send_us - clock_delta_us
 *   where clock_delta_us is provided via --clock-delta (from clock_sync.py).
 *
 * Build (cross-compiled for arm64-android, done by run_psnr_test.py):
 *   aarch64-linux-android26-clang -O2 -o dace_psnr_bench dace_psnr_bench.c
 *     -I<x264_install>/include <x264_install>/lib/libx264.a -lm
 *     -Wl,-z,max-page-size=16384
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <math.h>
#include <errno.h>
#include "x264.h"

/* ── constants ─────────────────────────────────────────────────────────────── */
#define WIDTH    320
#define HEIGHT   240
#define FPS      15
#define BITRATE  100    /* kbps */
#define FRAMES   60     /* frames per complexity level */
#define MAGIC    0x45434144u   /* 'DACE' LE */

/* ── helpers ────────────────────────────────────────────────────────────────── */
static int64_t now_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000LL + ts.tv_nsec / 1000LL;
}

static void write_le32(FILE *f, uint32_t v) {
    uint8_t b[4] = { v & 0xFF, (v>>8)&0xFF, (v>>16)&0xFF, (v>>24)&0xFF };
    fwrite(b, 1, 4, f);
}
static void write_le64(FILE *f, int64_t v) {
    uint64_t u = (uint64_t)v;
    uint8_t b[8];
    for (int i = 0; i < 8; i++) { b[i] = u & 0xFF; u >>= 8; }
    fwrite(b, 1, 8, f);
}
static void write_f64(FILE *f, double v) {
    uint8_t b[8]; memcpy(b, &v, 8); fwrite(b, 1, 8, f);
}

static uint32_t read_le32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1]<<8) | ((uint32_t)p[2]<<16) | ((uint32_t)p[3]<<24);
}
static int64_t read_le64(const uint8_t *p) {
    uint64_t v = 0;
    for (int i = 7; i >= 0; i--) v = (v << 8) | p[i];
    return (int64_t)v;
}
static double read_f64(const uint8_t *p) {
    double v; memcpy(&v, p, 8); return v;
}

static void fill_yuv420(x264_picture_t *pic, int frame_idx) {
    int w = WIDTH, h = HEIGHT;
    for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++)
            pic->img.plane[0][y * pic->img.i_stride[0] + x] =
                (uint8_t)((x + y + frame_idx * 3) & 0xFF);
    for (int y = 0; y < h/2; y++)
        for (int x = 0; x < w/2; x++) {
            pic->img.plane[1][y * pic->img.i_stride[1] + x] = (uint8_t)((x*2 + frame_idx) & 0xFF);
            pic->img.plane[2][y * pic->img.i_stride[2] + x] = (uint8_t)((y*2 + frame_idx) & 0xFF);
        }
}

/* ── SENDER ─────────────────────────────────────────────────────────────────── */
static int run_sender(const char *outpath) {
    FILE *fp = fopen(outpath, "wb");
    if (!fp) { perror("fopen output"); return 1; }

    fprintf(stderr, "[sender] Writing to %s\n", outpath);
    fprintf(stderr, "[sender] %dx%d @ %dfps  %dkbps  %d frames/complexity\n\n",
            WIDTH, HEIGHT, FPS, BITRATE, FRAMES);

    uint32_t global_seq = 0;

    for (int cl = 0; cl <= 5; cl++) {
        x264_param_t param;
        x264_param_default(&param);
        x264_param_apply_profile(&param, "baseline");

        param.i_width    = WIDTH;
        param.i_height   = HEIGHT;
        param.i_fps_num  = FPS;
        param.i_fps_den  = 1;

        param.rc.i_rc_method     = X264_RC_ABR;
        param.rc.i_bitrate       = BITRATE;
        param.rc.i_vbv_max_bitrate = BITRATE;
        param.rc.i_vbv_buffer_size = BITRATE * 2;

        param.i_bframe           = 0;
        param.b_sliced_threads   = 0;
        param.i_sync_lookahead   = 0;
        param.rc.b_mb_tree       = 0;
        param.i_lookahead_threads = 0;
        param.b_vfr_input        = 0;
        param.b_repeat_headers   = 1;
        param.b_annexb           = 1;
        param.i_threads          = 1;
        param.i_log_level        = X264_LOG_NONE;

        /* DACE */
        param.dace                   = 1;
        param.dace_complexity_level  = cl;

        /* Enable per-frame PSNR output */
        param.analyse.b_psnr         = 1;

        x264_t *enc = x264_encoder_open(&param);
        if (!enc) {
            fprintf(stderr, "[sender] x264_encoder_open failed at cl=%d\n", cl);
            fclose(fp);
            return 1;
        }

        x264_picture_t pic_in, pic_out;
        x264_picture_alloc(&pic_in, X264_CSP_I420, WIDTH, HEIGHT);

        int encoded_count = 0;
        for (int i = 0; i < FRAMES; i++) {
            fill_yuv420(&pic_in, i);
            pic_in.i_type = (i == 0) ? X264_TYPE_IDR : X264_TYPE_AUTO;
            pic_in.i_pts  = i;

            x264_nal_t *nals;
            int nal_count;

            int64_t send_us   = now_us();
            int frame_size = x264_encoder_encode(enc, &nals, &nal_count, &pic_in, &pic_out);

            if (frame_size <= 0) continue;

            double psnr_y   = pic_out.prop.f_psnr[0];   /* luma PSNR, dB */
            int    dace_et  = pic_out.prop.DACE_encoding_time; /* µs */
            int    dace_cl  = pic_out.prop.DACE_complexity;

            /* Write packet */
            write_le32(fp, MAGIC);
            write_le32(fp, global_seq);
            write_le32(fp, (uint32_t)cl);
            write_le64(fp, send_us);
            write_f64 (fp, psnr_y);
            write_le32(fp, (uint32_t)(dace_et > 0 ? dace_et : 0));
            write_le32(fp, (uint32_t)frame_size);
            fwrite(nals[0].p_payload, 1, frame_size, fp);

            global_seq++;
            encoded_count++;
        }

        /* Flush */
        while (x264_encoder_delayed_frames(enc)) {
            x264_nal_t *nals; int nc;
            x264_encoder_encode(enc, &nals, &nc, NULL, &pic_out);
        }

        x264_picture_clean(&pic_in);
        x264_encoder_close(enc);

        fprintf(stderr, "[sender] complexity=%d  frames=%d\n", cl, encoded_count);
    }

    fclose(fp);
    fprintf(stderr, "[sender] Done. %u total packets written.\n", global_seq);
    return 0;
}

/* ── RECEIVER ───────────────────────────────────────────────────────────────── */
#define HDR_SIZE  36   /* bytes before nal_data */

static int run_receiver(const char *inpath, int64_t clock_delta_us) {
    FILE *fp = fopen(inpath, "rb");
    if (!fp) { perror("fopen input"); return 1; }

    fprintf(stderr, "[receiver] Reading %s  clock_delta=%+lld µs\n",
            inpath, (long long)clock_delta_us);
    fprintf(stderr, "[receiver] one_way = recv - send - clock_delta\n\n");

    /* CSV header */
    printf("seq,complexity,psnr_y_db,dace_enc_us,nal_size_b,"
           "send_us,recv_us,raw_latency_us,corrected_latency_us\n");

    uint8_t hdr[HDR_SIZE];
    uint8_t *nal_buf = NULL;
    size_t   nal_cap = 0;
    int      count   = 0;

    while (fread(hdr, 1, HDR_SIZE, fp) == HDR_SIZE) {
        int64_t recv_us = now_us();   /* timestamp as early as possible */

        uint32_t magic      = read_le32(hdr +  0);
        uint32_t seq        = read_le32(hdr +  4);
        uint32_t complexity = read_le32(hdr +  8);
        int64_t  send_us    = read_le64(hdr + 12);
        double   psnr_y     = read_f64 (hdr + 20);
        uint32_t dace_enc   = read_le32(hdr + 28);
        uint32_t nal_size   = read_le32(hdr + 32);

        if (magic != MAGIC) {
            fprintf(stderr, "[receiver] bad magic at packet %d, aborting\n", count);
            break;
        }

        /* Read (and discard) NAL data */
        if (nal_size > nal_cap) {
            nal_buf = realloc(nal_buf, nal_size);
            nal_cap = nal_size;
        }
        if (fread(nal_buf, 1, nal_size, fp) != nal_size) break;

        int64_t raw_lat       = recv_us - send_us;
        int64_t corrected_lat = recv_us - send_us - clock_delta_us;

        printf("%u,%u,%.3f,%u,%u,%lld,%lld,%lld,%lld\n",
               seq, complexity, psnr_y, dace_enc, nal_size,
               (long long)send_us, (long long)recv_us,
               (long long)raw_lat, (long long)corrected_lat);

        count++;
    }

    free(nal_buf);
    fclose(fp);
    fprintf(stderr, "[receiver] %d packets processed.\n", count);
    return 0;
}

/* ── main ───────────────────────────────────────────────────────────────────── */
static void usage(const char *prog) {
    fprintf(stderr,
        "Usage:\n"
        "  %s --sender   --out <file>\n"
        "  %s --receiver --in  <file> [--clock-delta <µs>]\n"
        "\n"
        "  --clock-delta  host_us - device_us offset from clock_sync.py\n"
        "                 positive = receiver Pi clock is behind sender Pi clock\n",
        prog, prog);
}

int main(int argc, char **argv) {
    const char *mode       = NULL;
    const char *inpath     = NULL;
    const char *outpath    = NULL;
    int64_t     delta_us   = 0;

    for (int i = 1; i < argc; i++) {
        if      (!strcmp(argv[i], "--sender"))       mode    = "sender";
        else if (!strcmp(argv[i], "--receiver"))     mode    = "receiver";
        else if (!strcmp(argv[i], "--out")  && i+1 < argc) outpath  = argv[++i];
        else if (!strcmp(argv[i], "--in")   && i+1 < argc) inpath   = argv[++i];
        else if (!strcmp(argv[i], "--clock-delta") && i+1 < argc)
                                                     delta_us = atoll(argv[++i]);
    }

    if (!mode) { usage(argv[0]); return 1; }

    if (!strcmp(mode, "sender")) {
        if (!outpath) { fprintf(stderr, "--out required\n"); return 1; }
        return run_sender(outpath);
    } else {
        if (!inpath) { fprintf(stderr, "--in required\n"); return 1; }
        return run_receiver(inpath, delta_us);
    }
}
