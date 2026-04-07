/*
 * dace_psnr_bench.c — DACE encoding benchmark with decode-side PSNR + latency.
 *
 * SENDER MODE  (Pi 5 #1):
 *   Encodes synthetic YUV420 frames at each DACE complexity level (0-5),
 *   records per-frame PSNR from x264's reconstructed picture, and writes
 *   binary packets to --out file.
 *
 * RECEIVER MODE  (Pi 5 #2):
 *   Reads packet file, decodes each H.264 NAL via Android's AMediaCodec
 *   (hardware-accelerated), computes PSNR against the locally-regenerated
 *   reference frame, and measures one-way latency (corrected by clock delta
 *   from clock_sync.py).
 *
 * Packet layout (little-endian, HDR_SIZE = 40 bytes):
 *   [0..3]   magic       uint32   0x45434144 ('DACE')
 *   [4..7]   seq         uint32   global frame index
 *   [8..11]  complexity  uint32   DACE level (0-5)
 *   [12..15] frame_pts   uint32   local PTS within complexity level
 *   [16..23] send_us     int64    CLOCK_MONOTONIC at encode finish (µs)
 *   [24..31] psnr_y_enc  double   sender-side luma PSNR from x264 b_psnr (dB)
 *   [32..35] dace_enc_us uint32   x264 DACE_encoding_time (µs)
 *   [36..39] nal_size    uint32   bytes of Annex-B H.264 following
 *   [40..]   nal_data    uint8[]
 *
 * Receiver CSV columns:
 *   seq, complexity, frame_pts, nal_size_b,
 *   psnr_y_enc_db (sender),  psnr_y_dec_db (receiver, decoded),
 *   dace_enc_us, decode_us,
 *   send_us, recv_us, raw_latency_us, corrected_latency_us
 *
 * Build:
 *   aarch64-linux-android26-clang -O2 -o dace_psnr_bench dace_psnr_bench.c
 *     -I<x264>/include <x264>/lib/libx264.a -lmediandk -lm
 *     -Wl,-z,max-page-size=16384
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <math.h>
#include <errno.h>

/* x264 only needed for sender (encoder) */
#include "x264.h"

/* AMediaCodec for receiver (decoder) */
#include <media/NdkMediaCodec.h>
#include <media/NdkMediaFormat.h>
#include <android/looper.h>

/* ── constants ──────────────────────────────────────────────────────────────── */
#define WIDTH     320
#define HEIGHT    240
#define FPS       15
#define BITRATE   100      /* kbps */
#define FRAMES    60       /* frames per complexity level */
#define MAGIC     0x45434144u
#define HDR_SIZE  40

#define DECODE_TIMEOUT_US  200000  /* 200 ms dequeue timeout */
#define DECODE_DRAIN_RETRIES 10    /* retries waiting for output */

/* ── helpers ────────────────────────────────────────────────────────────────── */
/* Use CLOCK_REALTIME so send_us on Pi1 and recv_us on Pi2 share the same
   epoch after clock_sync.py has set both devices to the same wall time. */
static int64_t now_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (int64_t)ts.tv_sec * 1000000LL + ts.tv_nsec / 1000LL;
}

static int64_t mono_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000LL + ts.tv_nsec / 1000LL;
}

/* write helpers */
static void w32(FILE *f, uint32_t v) {
    uint8_t b[4] = { v&0xFF, (v>>8)&0xFF, (v>>16)&0xFF, (v>>24)&0xFF };
    fwrite(b, 1, 4, f);
}
static void w64(FILE *f, int64_t v) {
    uint64_t u = (uint64_t)v;
    uint8_t b[8]; for (int i=0;i<8;i++){b[i]=u&0xFF;u>>=8;}
    fwrite(b, 1, 8, f);
}
static void wf64(FILE *f, double v) {
    uint8_t b[8]; memcpy(b, &v, 8); fwrite(b, 1, 8, f);
}

/* read helpers */
static uint32_t r32(const uint8_t *p) {
    return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);
}
static int64_t r64(const uint8_t *p) {
    uint64_t v=0; for(int i=7;i>=0;i--) v=(v<<8)|p[i]; return (int64_t)v;
}
static double rf64(const uint8_t *p) { double v; memcpy(&v,p,8); return v; }

/* Synthesize Y plane for a given frame PTS (must match sender's fill_yuv420) */
static void fill_ref_y(uint8_t *y, int w, int h, int pts) {
    for (int row = 0; row < h; row++)
        for (int col = 0; col < w; col++)
            y[row * w + col] = (uint8_t)((col + row + pts * 3) & 0xFF);
}

/* Y-plane PSNR. Returns 100.0 dB for identical frames. */
static double psnr_y(const uint8_t *ref, const uint8_t *dec, int n) {
    double mse = 0.0;
    for (int i = 0; i < n; i++) {
        double d = (double)ref[i] - (double)dec[i];
        mse += d * d;
    }
    mse /= n;
    return (mse < 1e-10) ? 100.0 : 10.0 * log10(255.0 * 255.0 / mse);
}

/* x264 YUV fill (sender only) */
static void fill_yuv420(x264_picture_t *pic, int pts) {
    int w = WIDTH, h = HEIGHT;
    for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++)
            pic->img.plane[0][y * pic->img.i_stride[0] + x] =
                (uint8_t)((x + y + pts * 3) & 0xFF);
    for (int y = 0; y < h/2; y++)
        for (int x = 0; x < w/2; x++) {
            pic->img.plane[1][y*pic->img.i_stride[1]+x] = (uint8_t)((x*2+pts)&0xFF);
            pic->img.plane[2][y*pic->img.i_stride[2]+x] = (uint8_t)((y*2+pts)&0xFF);
        }
}

/* ── SENDER ─────────────────────────────────────────────────────────────────── */
static int run_sender(const char *outpath) {
    FILE *fp = fopen(outpath, "wb");
    if (!fp) { perror("fopen"); return 1; }

    fprintf(stderr, "[sender] %s  %dx%d @%dfps %dkbps  %d frames/cl\n",
            outpath, WIDTH, HEIGHT, FPS, BITRATE, FRAMES);

    uint32_t global_seq = 0;

    for (int cl = 0; cl <= 5; cl++) {
        x264_param_t param;
        x264_param_default(&param);
        x264_param_apply_profile(&param, "baseline");

        param.i_width             = WIDTH;
        param.i_height            = HEIGHT;
        param.i_fps_num           = FPS;
        param.i_fps_den           = 1;
        param.rc.i_rc_method      = X264_RC_ABR;
        param.rc.i_bitrate        = BITRATE;
        param.rc.i_vbv_max_bitrate = BITRATE;
        param.rc.i_vbv_buffer_size = BITRATE * 2;
        param.i_bframe            = 0;
        param.b_sliced_threads    = 0;
        param.i_sync_lookahead    = 0;
        param.rc.b_mb_tree        = 0;
        param.i_lookahead_threads = 0;
        param.b_vfr_input         = 0;
        param.b_repeat_headers    = 1;
        param.b_annexb            = 1;
        param.i_threads           = 1;
        /* LOG_INFO required — b_psnr is silently forced to 0 when level < INFO */
        param.i_log_level         = X264_LOG_INFO;
        param.dace                = 1;
        param.dace_complexity_level = cl;
        param.analyse.b_psnr      = 1;

        x264_t *enc = x264_encoder_open(&param);
        if (!enc) {
            fprintf(stderr, "[sender] open failed cl=%d\n", cl); fclose(fp); return 1;
        }

        x264_picture_t pic_in, pic_out;
        x264_picture_alloc(&pic_in, X264_CSP_I420, WIDTH, HEIGHT);

        int n = 0;
        for (int i = 0; i < FRAMES; i++) {
            fill_yuv420(&pic_in, i);
            pic_in.i_type = (i == 0) ? X264_TYPE_IDR : X264_TYPE_AUTO;
            pic_in.i_pts  = i;

            x264_nal_t *nals; int nal_count;
            int64_t send_us = now_us();
            int frame_size  = x264_encoder_encode(enc, &nals, &nal_count, &pic_in, &pic_out);
            if (frame_size <= 0) continue;

            uint32_t pts     = (uint32_t)pic_out.i_pts;
            double   psnr_enc = pic_out.prop.f_psnr[0];
            uint32_t dace_et  = (uint32_t)(pic_out.prop.DACE_encoding_time > 0
                                           ? pic_out.prop.DACE_encoding_time : 0);

            w32(fp, MAGIC);
            w32(fp, global_seq);
            w32(fp, (uint32_t)cl);
            w32(fp, pts);
            w64(fp, send_us);
            wf64(fp, psnr_enc);
            w32(fp, dace_et);
            w32(fp, (uint32_t)frame_size);
            fwrite(nals[0].p_payload, 1, frame_size, fp);

            global_seq++;
            n++;
        }

        while (x264_encoder_delayed_frames(enc)) {
            x264_nal_t *nals; int nc;
            x264_encoder_encode(enc, &nals, &nc, NULL, &pic_out);
        }
        x264_picture_clean(&pic_in);
        x264_encoder_close(enc);
        fprintf(stderr, "[sender] cl=%d  %d packets\n", cl, n);
    }

    fclose(fp);
    fprintf(stderr, "[sender] done  total=%u packets\n", global_seq);
    return 0;
}

/* ── RECEIVER (with AMediaCodec decode + PSNR) ──────────────────────────────── */

/* Feed one NAL buffer into the decoder input queue. */
static int decoder_feed(AMediaCodec *codec, const uint8_t *nal, size_t nal_sz, int64_t pts) {
    ssize_t idx = AMediaCodec_dequeueInputBuffer(codec, DECODE_TIMEOUT_US);
    if (idx < 0) {
        fprintf(stderr, "[recv] dequeueInputBuffer timeout (pts=%lld)\n", (long long)pts);
        return -1;
    }
    size_t cap;
    uint8_t *buf = AMediaCodec_getInputBuffer(codec, (size_t)idx, &cap);
    if (!buf || nal_sz > cap) {
        fprintf(stderr, "[recv] input buffer too small (%zu > %zu)\n", nal_sz, cap);
        AMediaCodec_queueInputBuffer(codec, (size_t)idx, 0, 0, pts, 0);
        return -1;
    }
    memcpy(buf, nal, nal_sz);
    AMediaCodec_queueInputBuffer(codec, (size_t)idx, 0, nal_sz, pts, 0);
    return 0;
}

/*
 * Drain one decoded output frame.
 * Returns 1 if a frame was obtained, 0 if none yet, -1 on error.
 * Copies the Y plane (first WIDTH*HEIGHT bytes) into out_y.
 * Sets *out_pts to the presentation timestamp.
 */
static int decoder_drain(AMediaCodec *codec, uint8_t *out_y, int64_t *out_pts) {
    AMediaCodecBufferInfo info;
    ssize_t idx = AMediaCodec_dequeueOutputBuffer(codec, &info, DECODE_TIMEOUT_US);

    if (idx == AMEDIACODEC_INFO_OUTPUT_FORMAT_CHANGED ||
        idx == AMEDIACODEC_INFO_OUTPUT_BUFFERS_CHANGED) {
        return 0;   /* format change, no frame yet */
    }
    if (idx < 0) return 0;  /* no output yet */

    size_t out_sz;
    const uint8_t *out_buf = AMediaCodec_getOutputBuffer(codec, (size_t)idx, &out_sz);
    if (out_buf && out_sz >= (size_t)(WIDTH * HEIGHT)) {
        /* Y plane is always the first WIDTH*HEIGHT bytes */
        memcpy(out_y, out_buf + info.offset, WIDTH * HEIGHT);
    }
    *out_pts = info.presentationTimeUs;
    AMediaCodec_releaseOutputBuffer(codec, (size_t)idx, false);
    return 1;
}

static int run_receiver(const char *inpath, int64_t clock_delta_us) {
    FILE *fp = fopen(inpath, "rb");
    if (!fp) { perror("fopen"); return 1; }

    fprintf(stderr, "[recv] %s  clock_delta=%+lld µs\n",
            inpath, (long long)clock_delta_us);

    /* ── Prepare a Looper for this thread (required by AMediaCodec in shell) ── */
    ALooper *looper = ALooper_prepare(ALOOPER_PREPARE_ALLOW_NON_CALLBACKS);
    (void)looper;

    /* ── Init AMediaCodec H.264 decoder ── */
    /* Use explicit codec name — createDecoderByType may select a decoder that
       requires a Surface or app context when run from an adb shell binary. */
    AMediaCodec *codec = AMediaCodec_createCodecByName("c2.android.avc.decoder");
    if (!codec) {
        fprintf(stderr, "[recv] c2.android.avc.decoder not found, trying ffmpeg\n");
        codec = AMediaCodec_createCodecByName("c2.ffmpeg.h264.decoder");
    }
    if (!codec) {
        fprintf(stderr, "[recv] no H.264 decoder available\n"); return 1;
    }

    AMediaFormat *fmt = AMediaFormat_new();
    AMediaFormat_setString(fmt, AMEDIAFORMAT_KEY_MIME,   "video/avc");
    AMediaFormat_setInt32 (fmt, AMEDIAFORMAT_KEY_WIDTH,  WIDTH);
    AMediaFormat_setInt32 (fmt, AMEDIAFORMAT_KEY_HEIGHT, HEIGHT);
    /* Prefer software decoder — more reliable in shell context */
    AMediaFormat_setInt32 (fmt, AMEDIAFORMAT_KEY_COLOR_FORMAT, 0x13 /* COLOR_FormatYUV420Planar */);

    media_status_t cfg_status = AMediaCodec_configure(codec, fmt, NULL, NULL, 0);
    AMediaFormat_delete(fmt);
    if (cfg_status != AMEDIA_OK) {
        fprintf(stderr, "[recv] AMediaCodec_configure failed: %d\n", cfg_status); return 1;
    }

    media_status_t start_status = AMediaCodec_start(codec);
    if (start_status != AMEDIA_OK) {
        fprintf(stderr, "[recv] AMediaCodec_start failed: %d\n", start_status); return 1;
    }

    /* Give codec time to spin up its internal thread */
    struct timespec ts = {0, 100000000L}; /* 100 ms */
    nanosleep(&ts, NULL);
    fprintf(stderr, "[recv] AMediaCodec decoder started\n");

    /* ── Allocate buffers ── */
    uint8_t *nal_buf  = NULL;
    size_t   nal_cap  = 0;
    uint8_t *ref_y    = malloc(WIDTH * HEIGHT);   /* reference Y plane */
    uint8_t *dec_y    = malloc(WIDTH * HEIGHT);   /* decoded  Y plane  */

    /* ── CSV header ── */
    printf("seq,complexity,frame_pts,nal_size_b,"
           "psnr_y_enc_db,psnr_y_dec_db,"
           "dace_enc_us,decode_us,"
           "send_us,recv_us,raw_latency_us,corrected_latency_us\n");

    uint8_t  hdr[HDR_SIZE];
    int      count = 0;

    while (fread(hdr, 1, HDR_SIZE, fp) == (size_t)HDR_SIZE) {

        /* Timestamp the moment the header arrives — transfer latency */
        int64_t recv_us = now_us();

        if (r32(hdr) != MAGIC) {
            fprintf(stderr, "[recv] bad magic at packet %d\n", count); break;
        }

        uint32_t seq        = r32(hdr +  4);
        uint32_t complexity = r32(hdr +  8);
        uint32_t frame_pts  = r32(hdr + 12);
        int64_t  send_us    = r64(hdr + 16);
        double   psnr_enc   = rf64(hdr + 24);
        uint32_t dace_enc   = r32(hdr + 32);
        uint32_t nal_size   = r32(hdr + 36);

        /* Read NAL data */
        if (nal_size > nal_cap) {
            nal_buf = realloc(nal_buf, nal_size);
            nal_cap = nal_size;
        }
        if (fread(nal_buf, 1, nal_size, fp) != nal_size) break;

        /* ── Decode ── */
        int64_t dec_start = now_us();

        decoder_feed(codec, nal_buf, nal_size, (int64_t)frame_pts);

        /* Retry drain up to DECODE_DRAIN_RETRIES × DECODE_TIMEOUT_US */
        int decoded = 0;
        int64_t out_pts = 0;
        for (int attempt = 0; attempt < DECODE_DRAIN_RETRIES && !decoded; attempt++) {
            decoded = decoder_drain(codec, dec_y, &out_pts);
        }

        int64_t decode_us = now_us() - dec_start;

        /* ── PSNR from decoded output ── */
        double psnr_dec = -1.0;
        if (decoded) {
            fill_ref_y(ref_y, WIDTH, HEIGHT, (int)out_pts);
            psnr_dec = psnr_y(ref_y, dec_y, WIDTH * HEIGHT);
        } else {
            fprintf(stderr, "[recv] seq=%u: no decoded frame available\n", seq);
        }

        /* ── Latency ── */
        int64_t raw_lat  = recv_us - send_us;
        int64_t corr_lat = recv_us - send_us - clock_delta_us;

        printf("%u,%u,%u,%u,%.3f,%.3f,%u,%lld,%lld,%lld,%lld,%lld\n",
               seq, complexity, frame_pts, nal_size,
               psnr_enc, psnr_dec,
               dace_enc,
               (long long)decode_us,
               (long long)send_us,   (long long)recv_us,
               (long long)raw_lat,   (long long)corr_lat);

        count++;
    }

    /* ── Flush decoder ── */
    {
        ssize_t idx = AMediaCodec_dequeueInputBuffer(codec, DECODE_TIMEOUT_US);
        if (idx >= 0)
            AMediaCodec_queueInputBuffer(codec, (size_t)idx, 0, 0, 0,
                                         AMEDIACODEC_BUFFER_FLAG_END_OF_STREAM);
    }

    AMediaCodec_stop(codec);
    AMediaCodec_delete(codec);
    free(nal_buf);
    free(ref_y);
    free(dec_y);
    fclose(fp);

    fprintf(stderr, "[recv] done  %d packets decoded\n", count);
    return 0;
}

/* ── main ───────────────────────────────────────────────────────────────────── */
int main(int argc, char **argv) {
    const char *mode    = NULL;
    const char *inpath  = NULL;
    const char *outpath = NULL;
    int64_t     delta   = 0;

    for (int i = 1; i < argc; i++) {
        if      (!strcmp(argv[i], "--sender"))   mode    = "sender";
        else if (!strcmp(argv[i], "--receiver")) mode    = "receiver";
        else if (!strcmp(argv[i], "--mono-us"))  mode    = "mono";
        else if (!strcmp(argv[i], "--real-us"))  mode    = "real";
        else if (!strcmp(argv[i], "--out") && i+1<argc) outpath = argv[++i];
        else if (!strcmp(argv[i], "--in")  && i+1<argc) inpath  = argv[++i];
        else if (!strcmp(argv[i], "--clock-delta") && i+1<argc) delta = atoll(argv[++i]);
    }

    if (!mode) {
        fprintf(stderr,
            "Usage:\n"
            "  %s --sender   --out <file>\n"
            "  %s --receiver --in  <file> [--clock-delta <µs>]\n"
            "  %s --mono-us           (print CLOCK_MONOTONIC µs and exit)\n"
            "  %s --real-us           (print CLOCK_REALTIME  µs and exit)\n",
            argv[0], argv[0], argv[0], argv[0]);
        return 1;
    }

    if (!strcmp(mode, "mono")) {
        printf("%lld\n", (long long)mono_us());
        return 0;
    }
    if (!strcmp(mode, "real")) {
        /* Used by clock_sync.py: print CLOCK_REALTIME µs and exit */
        printf("%lld\n", (long long)now_us());
        return 0;
    }
    if (!strcmp(mode, "sender")) {
        if (!outpath) { fprintf(stderr, "--out required\n"); return 1; }
        return run_sender(outpath);
    } else {
        if (!inpath) { fprintf(stderr, "--in required\n"); return 1; }
        return run_receiver(inpath, delta);
    }
}
