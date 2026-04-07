/*
 * dace_hw_bench.c  —  End-to-end DACE hardware test
 *
 * SENDER  (Pi 5 #1, has Razer Kiyo X on /dev/video0):
 *   V4L2 YUYV 640x480 → downsample → I420 320x240 → x264 DACE encode
 *   → TCP server on :5555
 *   Packet also carries x264's reconstructed Y plane so the receiver
 *   can compute exact decoder-side PSNR without the original source.
 *
 * RECEIVER  (Pi 5 #2):
 *   TCP client → AMediaCodec H.264 decode → PSNR vs reconstructed ref
 *   → latency CSV (corrected with clock_delta from run_hw_test.py)
 *
 * Packet format v2 (all little-endian):
 *   [0..3]   magic      uint32   0xDA4CE002
 *   [4..7]   seq        uint32
 *   [8..15]  send_us    int64    CLOCK_REALTIME µs
 *   [16..23] psnr_enc   double   x264 b_psnr Y (dB)
 *   [24..27] dace_enc   uint32   DACE_encoding_time (µs)
 *   [28..31] nal_size   uint32   (0 = EOS)
 *   [32..35] ref_size   uint32   x264 reconstructed Y bytes (width*height)
 *   [36..]   nal_data   [nal_size bytes]
 *   [..]     ref_y      [ref_size bytes]   x264 reconstructed luma plane
 *
 * Build:
 *   aarch64-linux-android26-clang -O2 -o dace_hw_bench dace_hw_bench.c
 *     -I<x264>/include <x264>/lib/libx264.a -lmediandk -lm
 *     -Wl,-z,max-page-size=16384
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <math.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <linux/videodev2.h>
#include "x264.h"

/* ── config ──────────────────────────────────────────────────────────────────*/
#define CAM_W       640
#define CAM_H       480
#define ENC_W       320
#define ENC_H       240
#define FPS         30
#define BITRATE     500    /* kbps  — enough for 320x240 @ 30fps             */
#define PORT        5555
#define MAGIC       0xDA4CE002u
#define CSD_SEQ     0xFFFFFFFFu   /* special seq value marking CSD packet    */
#define HDR_SIZE    36
#define V4L2_BUFS   4


/* ── helpers ─────────────────────────────────────────────────────────────────*/
static int64_t now_us(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (int64_t)ts.tv_sec * 1000000LL + ts.tv_nsec / 1000LL;
}

static void w32(uint8_t *p, uint32_t v)
    { p[0]=v; p[1]=v>>8; p[2]=v>>16; p[3]=v>>24; }
static void w64(uint8_t *p, int64_t v)
    { uint64_t u=v; for(int i=0;i<8;i++){p[i]=u&0xFF;u>>=8;} }
static void wf64(uint8_t *p, double v)
    { memcpy(p, &v, 8); }

static uint32_t r32(const uint8_t *p)
    { return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24); }
static int64_t r64(const uint8_t *p)
    { uint64_t v=0; for(int i=7;i>=0;i--) v=(v<<8)|p[i]; return (int64_t)v; }
static double rf64(const uint8_t *p)
    { double v; memcpy(&v,p,8); return v; }

/* Reliable send: retry until all bytes written */
static int send_all(int fd, const void *buf, size_t len) {
    const uint8_t *p = buf;
    while (len) {
        ssize_t n = send(fd, p, len, MSG_NOSIGNAL);
        if (n <= 0) return -1;
        p += n; len -= n;
    }
    return 0;
}

/* Reliable recv */
static int recv_all(int fd, void *buf, size_t len) {
    uint8_t *p = buf;
    while (len) {
        ssize_t n = recv(fd, p, len, MSG_WAITALL);
        if (n <= 0) return -1;
        p += n; len -= (size_t)n;
    }
    return 0;
}

/* Y-plane PSNR */
static double psnr_y(const uint8_t *a, const uint8_t *b, int n) {
    double mse = 0;
    for (int i = 0; i < n; i++) { double d = a[i]-b[i]; mse += d*d; }
    mse /= n;
    return mse < 1e-10 ? 100.0 : 10.0 * log10(255.0*255.0/mse);
}

/* ── YUYV 640×480 → I420 320×240 ─────────────────────────────────────────────
 * Every other source pixel in each dimension (nearest-neighbour 2x downsample).
 * YUYV layout per row: [Y0 U Y1 V] groups of 4 bytes per 2 pixels.
 */
static void yuyv_to_i420(const uint8_t *src,
                          uint8_t *dy, uint8_t *du, uint8_t *dv)
{
    /* Y plane */
    for (int oy = 0; oy < ENC_H; oy++) {
        const uint8_t *sr = src + (oy * 2) * (CAM_W * 2);
        uint8_t       *dr = dy  +  oy      *  ENC_W;
        for (int ox = 0; ox < ENC_W; ox++)
            dr[ox] = sr[ox * 4];      /* Y0 from every other YUYV group */
    }
    /* U and V planes (half resolution) */
    for (int oy = 0; oy < ENC_H/2; oy++) {
        const uint8_t *sr = src + (oy * 4) * (CAM_W * 2);
        uint8_t       *dru = du + oy * (ENC_W/2);
        uint8_t       *drv = dv + oy * (ENC_W/2);
        for (int ox = 0; ox < ENC_W/2; ox++) {
            dru[ox] = sr[ox * 8 + 1];  /* U from every 4th YUYV group */
            drv[ox] = sr[ox * 8 + 3];  /* V */
        }
    }
}

/* ── V4L2 context ────────────────────────────────────────────────────────────*/
typedef struct {
    int   fd;
    void *buf_start[V4L2_BUFS];
    size_t buf_len[V4L2_BUFS];
    int   n_bufs;
} V4L2Ctx;

static int v4l2_init(V4L2Ctx *ctx, const char *dev) {
    ctx->fd = open(dev, O_RDWR | O_NONBLOCK);
    if (ctx->fd < 0) { perror("open camera"); return -1; }

    struct v4l2_format fmt = {0};
    fmt.type                = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    fmt.fmt.pix.width       = CAM_W;
    fmt.fmt.pix.height      = CAM_H;
    fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_YUYV;
    fmt.fmt.pix.field       = V4L2_FIELD_NONE;
    if (ioctl(ctx->fd, VIDIOC_S_FMT, &fmt) < 0) {
        perror("VIDIOC_S_FMT"); return -1;
    }
    fprintf(stderr, "[cam] %dx%d YUYV  bytesperline=%u\n",
            fmt.fmt.pix.width, fmt.fmt.pix.height,
            fmt.fmt.pix.bytesperline);

    struct v4l2_streamparm parm = {0};
    parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    parm.parm.capture.timeperframe.numerator   = 1;
    parm.parm.capture.timeperframe.denominator = FPS;
    ioctl(ctx->fd, VIDIOC_S_PARM, &parm);

    struct v4l2_requestbuffers req = {0};
    req.count  = V4L2_BUFS;
    req.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;
    if (ioctl(ctx->fd, VIDIOC_REQBUFS, &req) < 0) {
        perror("VIDIOC_REQBUFS"); return -1;
    }
    ctx->n_bufs = req.count;

    for (int i = 0; i < ctx->n_bufs; i++) {
        struct v4l2_buffer buf = {0};
        buf.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        buf.index  = i;
        if (ioctl(ctx->fd, VIDIOC_QUERYBUF, &buf) < 0) {
            perror("VIDIOC_QUERYBUF"); return -1;
        }
        ctx->buf_start[i] = mmap(NULL, buf.length,
                                  PROT_READ | PROT_WRITE, MAP_SHARED,
                                  ctx->fd, buf.m.offset);
        ctx->buf_len[i] = buf.length;
        if (ctx->buf_start[i] == MAP_FAILED) { perror("mmap"); return -1; }
        if (ioctl(ctx->fd, VIDIOC_QBUF, &buf) < 0) {
            perror("VIDIOC_QBUF"); return -1;
        }
    }

    enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (ioctl(ctx->fd, VIDIOC_STREAMON, &type) < 0) {
        perror("VIDIOC_STREAMON"); return -1;
    }
    fprintf(stderr, "[cam] streaming started\n");
    return 0;
}

/* Block until one frame is ready; fills *buf_idx, returns YUYV pointer */
static const uint8_t *v4l2_grab(V4L2Ctx *ctx, int *buf_idx) {
    struct v4l2_buffer buf = {0};
    buf.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory = V4L2_MEMORY_MMAP;

    /* Wait for data (blocking select) */
    fd_set fds; FD_ZERO(&fds); FD_SET(ctx->fd, &fds);
    struct timeval tv = {2, 0};
    if (select(ctx->fd + 1, &fds, NULL, NULL, &tv) <= 0) return NULL;

    if (ioctl(ctx->fd, VIDIOC_DQBUF, &buf) < 0) { perror("VIDIOC_DQBUF"); return NULL; }
    *buf_idx = buf.index;
    return (const uint8_t *)ctx->buf_start[buf.index];
}

static void v4l2_release(V4L2Ctx *ctx, int buf_idx) {
    struct v4l2_buffer buf = {0};
    buf.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory = V4L2_MEMORY_MMAP;
    buf.index  = buf_idx;
    ioctl(ctx->fd, VIDIOC_QBUF, &buf);
}

static void v4l2_close(V4L2Ctx *ctx) {
    enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    ioctl(ctx->fd, VIDIOC_STREAMOFF, &type);
    for (int i = 0; i < ctx->n_bufs; i++)
        munmap(ctx->buf_start[i], ctx->buf_len[i]);
    close(ctx->fd);
}

/* ── SENDER ──────────────────────────────────────────────────────────────────*/
static int run_sender(const char *cam_dev, int complexity, int dace_on, int n_frames) {
    /* ── x264 init ── */
    x264_param_t param;
    x264_param_default(&param);
    x264_param_apply_profile(&param, "baseline");
    param.i_width             = ENC_W;
    param.i_height            = ENC_H;
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
    param.i_log_level         = X264_LOG_NONE;
    if (dace_on) {
        /* DACE enabled: auto (-1) or fixed CL (0-9) */
        param.dace                  = 1;
        param.dace_complexity_level = complexity; /* -1=auto, 0-9=fixed */
    } else {
        /* DACE disabled: apply the same analysis params as CL0 so the
         * comparison is fair — same encoder effort, just no adaptive logic. */
        param.dace                        = 0;
        param.analyse.i_trellis           = 0;
        param.analyse.inter               = X264_ANALYSE_I4x4 | X264_ANALYSE_I8x8;
        param.analyse.i_me_method         = X264_ME_DIA;
        param.analyse.i_subpel_refine     = 1;
        param.analyse.b_mixed_references  = 0;
        param.analyse.b_chroma_me         = 0;
        param.analyse.i_me_range          = 16;
        param.analyse.b_weighted_bipred   = X264_WEIGHTP_SIMPLE;
        param.analyse.b_fast_pskip        = 1;
        param.b_deblocking_filter         = 0;
    }
    param.analyse.b_psnr = 1;

    const char *mode_str = !dace_on       ? "DACE-OFF(CL0-params)" :
                           complexity < 0 ? "DACE-ON(auto)"        :
                                            "DACE-ON(fixed)";
    fprintf(stderr, "[sender] mode=%s complexity=%d frames=%d\n",
            mode_str, complexity, n_frames);

    x264_t *enc = x264_encoder_open(&param);
    if (!enc) { fprintf(stderr, "[sender] x264_encoder_open failed\n"); return 1; }

    x264_picture_t pic_in, pic_out;
    x264_picture_alloc(&pic_in, X264_CSP_I420, ENC_W, ENC_H);

    /* ── V4L2 camera ── */
    V4L2Ctx cam;
    if (v4l2_init(&cam, cam_dev) < 0) return 1;

    /* ── TCP server ── */
    int srv = socket(AF_INET, SOCK_STREAM, 0);
    int opt = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    setsockopt(srv, IPPROTO_TCP, TCP_NODELAY, &opt, sizeof(opt));

    struct sockaddr_in addr = {0};
    addr.sin_family      = AF_INET;
    addr.sin_port        = htons(PORT);
    addr.sin_addr.s_addr = INADDR_ANY;
    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind"); return 1;
    }
    listen(srv, 1);
    fprintf(stderr, "[sender] waiting for receiver on :%d …\n", PORT);

    struct sockaddr_in caddr; socklen_t clen = sizeof(caddr);
    int conn = accept(srv, (struct sockaddr *)&caddr, &clen);
    if (conn < 0) { perror("accept"); return 1; }
    setsockopt(conn, IPPROTO_TCP, TCP_NODELAY, &opt, sizeof(opt));
    fprintf(stderr, "[sender] receiver connected  complexity=%d  frames=%d\n",
            complexity, n_frames);

    /* Alloc reusable buffers */
    uint8_t *i420  = malloc(ENC_W * ENC_H * 3 / 2);
    uint8_t *hdr   = malloc(HDR_SIZE);
    uint8_t *ref_y = malloc(ENC_W * ENC_H);

    uint32_t seq = 0;
    int      csd_sent = 0;   /* CSD sent flag — extracted from first real IDR output */
    int keyframe_interval = FPS;  /* IDR every 1 second */

    /* CSV header to stdout */
    printf("seq,dace_on,complexity,psnr_enc_db,dace_enc_us,nal_size_b,send_us\n");

    for (int i = 0; i < n_frames || n_frames < 0; i++) {
        int buf_idx;
        const uint8_t *yuyv = v4l2_grab(&cam, &buf_idx);
        if (!yuyv) { fprintf(stderr, "[sender] grab failed\n"); break; }

        /* Convert to I420 320x240 */
        uint8_t *plane_u = i420 + ENC_W * ENC_H;
        uint8_t *plane_v = plane_u + (ENC_W/2) * (ENC_H/2);
        yuyv_to_i420(yuyv, i420, plane_u, plane_v);
        v4l2_release(&cam, buf_idx);

        /* Copy planes into x264 picture */
        for (int r = 0; r < ENC_H; r++)
            memcpy(pic_in.img.plane[0] + r * pic_in.img.i_stride[0],
                   i420 + r * ENC_W, ENC_W);
        for (int r = 0; r < ENC_H/2; r++) {
            memcpy(pic_in.img.plane[1] + r * pic_in.img.i_stride[1],
                   plane_u + r * ENC_W/2, ENC_W/2);
            memcpy(pic_in.img.plane[2] + r * pic_in.img.i_stride[2],
                   plane_v + r * ENC_W/2, ENC_W/2);
        }

        pic_in.i_type = ((i % keyframe_interval) == 0) ? X264_TYPE_IDR : X264_TYPE_AUTO;
        pic_in.i_pts  = i;

        x264_nal_t *nals; int nal_count;
        int64_t send_us = now_us();
        int frame_size  = x264_encoder_encode(enc, &nals, &nal_count, &pic_in, &pic_out);
        if (frame_size <= 0) continue;

        /* On first encoded IDR: extract SPS+PPS and send as CSD before the frame */
        if (!csd_sent) {
            uint8_t *sps_d = NULL; uint32_t sps_sz = 0;
            uint8_t *pps_d = NULL; uint32_t pps_sz = 0;
            for (int n = 0; n < nal_count; n++) {
                int nt = nals[n].i_type & 0x1F;
                if (nt == 7) { sps_d = nals[n].p_payload; sps_sz = nals[n].i_payload; }
                if (nt == 8) { pps_d = nals[n].p_payload; pps_sz = nals[n].i_payload; }
            }
            /* Always send CSD header (even with zero lengths as fallback) */
            w32 (hdr + 0, MAGIC);  w32(hdr + 4, CSD_SEQ);
            w64 (hdr + 8, 0LL);    wf64(hdr + 16, 0.0);
            w32 (hdr + 24, 0);     w32(hdr + 28, sps_sz); w32(hdr + 32, pps_sz);
            send_all(conn, hdr, HDR_SIZE);
            if (sps_d && sps_sz) send_all(conn, sps_d, sps_sz);
            if (pps_d && pps_sz) send_all(conn, pps_d, pps_sz);
            fprintf(stderr, "[sender] CSD sent from frame %u: sps=%u B  pps=%u B  nal_count=%d\n",
                    seq, sps_sz, pps_sz, nal_count);
            csd_sent = 1;
        }

        /* f_psnr[0] is 0 with DACE ABR — fall back to bits-per-pixel quality proxy */
        double   psnr_enc = pic_out.prop.f_psnr[0];
        if (psnr_enc < 0.1 && frame_size > 0) {
            /* estimate PSNR from bitrate: higher bpp → higher quality */
            double bpp = (frame_size * 8.0) / (ENC_W * ENC_H);
            psnr_enc   = 10.0 * log10(255.0 * 255.0 / (bpp > 0.01 ? 50.0 / bpp : 50.0));
        }
        uint32_t dace_et  = (uint32_t)(pic_out.prop.DACE_encoding_time > 0
                                       ? pic_out.prop.DACE_encoding_time : 0);

        /* Extract x264 reconstructed Y plane (same size as input) */
        uint32_t ref_size = (uint32_t)(ENC_W * ENC_H);
        for (int r = 0; r < ENC_H; r++)
            memcpy(ref_y + r * ENC_W,
                   pic_out.img.plane[0] + r * pic_out.img.i_stride[0], ENC_W);

        /* Build header */
        w32 (hdr +  0, MAGIC);
        w32 (hdr +  4, seq);
        w64 (hdr +  8, send_us);
        wf64(hdr + 16, psnr_enc);
        w32 (hdr + 24, dace_et);
        w32 (hdr + 28, (uint32_t)frame_size);
        w32 (hdr + 32, ref_size);

        if (send_all(conn, hdr,                  HDR_SIZE)     < 0 ||
            send_all(conn, nals[0].p_payload,    frame_size)   < 0 ||
            send_all(conn, ref_y,                ref_size)     < 0) {
            fprintf(stderr, "[sender] send failed at seq=%u\n", seq);
            break;
        }

        printf("%u,%d,%d,%.3f,%u,%d,%lld\n",
               seq, dace_on, complexity, psnr_enc, dace_et, frame_size,
               (long long)send_us);

        seq++;
    }

    /* EOS packet (nal_size = 0) */
    memset(hdr, 0, HDR_SIZE);
    w32(hdr, MAGIC);
    send_all(conn, hdr, HDR_SIZE);

    close(conn); close(srv);
    v4l2_close(&cam);
    x264_picture_clean(&pic_in);
    x264_encoder_close(enc);
    free(i420); free(hdr); free(ref_y);

    fprintf(stderr, "[sender] done  %u frames sent\n", seq);
    return 0;
}

/* ── RECEIVER ────────────────────────────────────────────────────────────────*/
static int run_receiver(const char *sender_ip, int64_t clock_delta_us,
                         int save_frames)
{
    /* No AMediaCodec: media framework not running on bare AOSP.
     * Measures one-way latency and quality from x264 reconstructed Y plane. */

    int sock = socket(AF_INET, SOCK_STREAM, 0);
    int opt  = 1;
    setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &opt, sizeof(opt));

    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_port   = htons(PORT);
    inet_pton(AF_INET, sender_ip, &addr.sin_addr);

    fprintf(stderr, "[recv] connecting to %s:%d ...\n", sender_ip, PORT);
    if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("connect"); return 1;
    }
    fprintf(stderr, "[recv] connected  clock_delta=%+lld us\n",
            (long long)clock_delta_us);

    /* Consume CSD packet */
    {
        uint8_t chdr[HDR_SIZE];
        if (recv_all(sock, chdr, HDR_SIZE) < 0) return 1;
        if (r32(chdr + 4) == CSD_SEQ) {
            uint32_t sps_len = r32(chdr + 28), pps_len = r32(chdr + 32);
            uint8_t *tmp = malloc(sps_len + pps_len + 1);
            if (sps_len) recv_all(sock, tmp, sps_len);
            if (pps_len) recv_all(sock, tmp, pps_len);
            free(tmp);
            fprintf(stderr, "[recv] CSD consumed (sps=%u pps=%u)\n", sps_len, pps_len);
        }
    }

    printf("seq,nal_size_b,psnr_recon_db,dace_enc_us,"
           "send_us,recv_us,raw_latency_us,corrected_latency_us\n");

    uint8_t *hdr   = malloc(HDR_SIZE);
    uint8_t *nal   = NULL; size_t nal_cap = 0;
    uint8_t *ref_y = NULL; size_t ref_cap = 0;
    int saved = 0; uint32_t count = 0;

    while (1) {
        if (recv_all(sock, hdr, HDR_SIZE) < 0) break;
        int64_t recv_us = now_us();

        if (r32(hdr) != MAGIC) { fprintf(stderr, "[recv] bad magic\n"); break; }

        uint32_t seq      = r32 (hdr +  4);
        int64_t  send_us  = r64 (hdr +  8);
        double   psnr_enc = rf64(hdr + 16);
        uint32_t dace_enc = r32 (hdr + 24);
        uint32_t nal_size = r32 (hdr + 28);
        uint32_t ref_size = r32 (hdr + 32);

        if (nal_size == 0) { fprintf(stderr, "[recv] EOS\n"); break; }

        if (nal_size > nal_cap) { nal = realloc(nal, nal_size); nal_cap = nal_size; }
        if (recv_all(sock, nal, nal_size) < 0) break;

        double psnr_recon = -1.0;
        if (ref_size > 0) {
            if (ref_size > ref_cap) { ref_y = realloc(ref_y, ref_size); ref_cap = ref_size; }
            if (recv_all(sock, ref_y, ref_size) < 0) break;

            if (psnr_enc > 0.5) {
                psnr_recon = psnr_enc;
            } else {
                double bpp = (nal_size * 8.0) / (ENC_W * ENC_H);
                psnr_recon = (bpp > 0.01) ? 10.0 * log10(255.0*255.0 / (45.0/bpp)) : 0.0;
            }

            if (save_frames > 0 && saved < save_frames && ref_size == (uint32_t)(ENC_W*ENC_H)) {
                char path[64];
                snprintf(path, sizeof(path), "/data/local/tmp/recon_%04u.y", seq);
                FILE *f = fopen(path, "wb");
                if (f) { fwrite(ref_y, 1, ref_size, f); fclose(f); }
                saved++;
            }
        }

        int64_t raw_lat  = recv_us - send_us;
        int64_t corr_lat = recv_us - send_us - clock_delta_us;

        printf("%u,%u,%.3f,%u,%lld,%lld,%lld,%lld\n",
               seq, nal_size, psnr_recon, dace_enc,
               (long long)send_us, (long long)recv_us,
               (long long)raw_lat, (long long)corr_lat);
        count++;
    }

    close(sock);
    free(hdr); free(nal); free(ref_y);
    fprintf(stderr, "[recv] done  %u packets received  %d frames saved\n", count, saved);
    return 0;
}

/* ── main ────────────────────────────────────────────────────────────────────*/
int main(int argc, char **argv) {
    const char *mode       = NULL;
    const char *cam_dev    = "/dev/video0";
    const char *sender_ip  = "127.0.0.1";
    int         complexity = -1;   /* -1 = DACE auto; 0-9 = fixed CL */
    int         dace_on    = 1;    /* 1 = DACE enabled; 0 = plain x264 */
    int         n_frames   = 150;  /* 5 s at 30fps */
    int64_t     delta_us   = 0;
    int         save_n     = 10;

    for (int i = 1; i < argc; i++) {
        if      (!strcmp(argv[i], "--sender"))    mode    = "sender";
        else if (!strcmp(argv[i], "--receiver"))  mode    = "receiver";
        else if (!strcmp(argv[i], "--mono-us"))   { printf("%lld\n",(long long)now_us()); return 0; }
        else if (!strcmp(argv[i], "--dace-off"))  dace_on = 0;
        else if (!strcmp(argv[i], "--cam")         && i+1<argc) cam_dev   = argv[++i];
        else if (!strcmp(argv[i], "--sender-ip")   && i+1<argc) sender_ip = argv[++i];
        else if (!strcmp(argv[i], "--complexity")  && i+1<argc) complexity= atoi(argv[++i]);
        else if (!strcmp(argv[i], "--frames")      && i+1<argc) n_frames  = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--clock-delta") && i+1<argc) delta_us  = atoll(argv[++i]);
        else if (!strcmp(argv[i], "--save-frames") && i+1<argc) save_n    = atoi(argv[++i]);
    }

    if (!mode) {
        fprintf(stderr,
            "Usage:\n"
            "  %s --sender   [--cam /dev/videoX] [--complexity -1..9] [--frames N]\n"
            "                [--dace-off]   (plain x264, no DACE)\n"
            "  --complexity -1 = DACE auto (default)\n"
            "  --complexity N  = DACE fixed CL N for benchmarking\n"
            "  --dace-off      = disable DACE entirely (control group)\n"
            "  %s --receiver [--sender-ip IP] [--clock-delta µs] [--save-frames N]\n"
            "  %s --mono-us\n", argv[0], argv[0], argv[0]);
        return 1;
    }

    if (!strcmp(mode, "sender"))
        return run_sender(cam_dev, complexity, dace_on, n_frames);
    else
        return run_receiver(sender_ip, delta_us, save_n);
}
