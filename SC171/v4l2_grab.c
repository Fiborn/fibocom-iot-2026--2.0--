/*
 * find_out.c — V4L2 双摄像头 1280×720 MJPEG 并行采集
 *
 * 用法:
 *   ./find_out_c                                          # 独立模式（倒计时，10秒）
 *   ./find_out_c <out_dir> [duration] [p0] [p1]           # pipe 模式
 *   ./find_out_c -c <out_dir> [p0] [p1]                   # 连续环缓冲模式
 *                                                         #    Ctrl+C 停止并dump到磁盘
 *   p0/p1: 前缀，如 cap_cam0 / cap_cam1
 *
 * 编译: gcc -O2 -o find_out_c find_out.c -lm
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <time.h>
#include <math.h>
#include <signal.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/poll.h>
#include <linux/videodev2.h>

/* ── 默认配置 ── */
#define CAM_W        1280
#define CAM_H        720
#define TARGET_FPS   160
#define DEFAULT_DUR  10
#define MAX_FRAME    3000
#define NBUF         4

#define DEV0         "/dev/video2"
#define DEV1         "/dev/video4"

/* ── 环形缓冲 ──
 * RING_CAPACITY = 160 fps × 30 s = 4800
 */
#define RING_CAPACITY  4800

typedef struct {
    int      valid;       /* 1 = 该槽有效 */
    char    *data;        /* JPEG 块（malloc） */
    int      size;        /* byteused */
    int      seq;         /* 帧序号（单调递增） */
} RingSlot;

typedef struct {
    RingSlot slots[RING_CAPACITY];
    int      head;        /* 下一个写入位置 */
    int      count;       /* 总捕获帧数 */
    int      wrapped;     /* 1 = 已环绕 */
    int      total_bytes; /* 累计字节数 */
} RingBuf;

/* ── 摄像头上下文 ── */
typedef struct {
    int         fd;
    void      **bufs;
    int        *buf_lens;
    int         nbufs;
    int         frame_count;
    const char *prefix;
    int         total_bytes;
    /* 环缓冲 */
    RingBuf     ring;
    int         using_ring;  /* 1 = ring mode */
} CamCtx;

/* ── 全局信号标记 ── */
static volatile int keep_running = 1;

static void sig_handler(int sig) {
    (void)sig;
    keep_running = 0;
}

/* ── 时间工具 ── */
static double now_sec(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static void sleep_sec(double s) {
    struct timespec ts = { (time_t)s, (long)((s - (time_t)s) * 1e9) };
    nanosleep(&ts, NULL);
}

/* ── 环形缓冲操作 ── */
static void ring_init(RingBuf *rb) {
    memset(rb, 0, sizeof(*rb));
    rb->head = 0;
}

static void ring_push(RingBuf *rb, const char *jpeg_data, int jpeg_size) {
    /* 若 slot 已有旧数据，先释放 */
    if (rb->slots[rb->head].valid) {
        free(rb->slots[rb->head].data);
    }
    rb->slots[rb->head].data  = malloc(jpeg_size);
    memcpy(rb->slots[rb->head].data, jpeg_data, jpeg_size);
    rb->slots[rb->head].size  = jpeg_size;
    rb->slots[rb->head].seq   = rb->count;
    rb->slots[rb->head].valid = 1;
    rb->total_bytes += jpeg_size;
    rb->count++;
    rb->head = (rb->head + 1) % RING_CAPACITY;
    if (rb->head == 0) rb->wrapped = 1;
}

static void ring_dump(RingBuf *rb, const char *out_dir, const char *prefix) {
    int start, n_valid, i, idx;

    if (!rb->wrapped) {
        /* 未环绕：0..count-1 全部有效 */
        start   = 0;
        n_valid = rb->count;
    } else {
        /* 已环绕：head..CAPACITY-1 最老，0..head-1 最新 */
        start   = rb->head;
        n_valid = RING_CAPACITY;
    }

    fprintf(stderr, "  [DUMP %s] %d frames ring buffer, writing %d...\n",
            prefix, rb->count, n_valid);

    char path[512];
    mkdir(out_dir, 0755);
    int written = 0;

    for (i = 0; i < n_valid; i++) {
        idx = (start + i) % RING_CAPACITY;
        if (!rb->slots[idx].valid)
            continue;
        snprintf(path, sizeof(path), "%s/%s_%06d.jpg",
                 out_dir, prefix, rb->slots[idx].seq);
        FILE *fp = fopen(path, "wb");
        if (fp) {
            fwrite(rb->slots[idx].data, 1, rb->slots[idx].size, fp);
            fclose(fp);
            written++;
        }
    }

    /* 清理释放内存 */
    for (i = 0; i < RING_CAPACITY; i++) {
        if (rb->slots[i].valid) {
            free(rb->slots[i].data);
            rb->slots[i].data  = NULL;
            rb->slots[i].valid = 0;
        }
    }
    fprintf(stderr, "  [DUMP %s] wrote %d files to %s/\n", prefix, written, out_dir);
}

/* ── V4L2 打开 + 设格式 ── */
static int v4l2_open(const char *dev) {
    struct v4l2_capability cap;
    struct v4l2_format fmt;
    struct v4l2_streamparm parm;

    int fd = open(dev, O_RDWR);
    if (fd < 0) { perror("open"); return -1; }

    ioctl(fd, VIDIOC_QUERYCAP, &cap);
    fprintf(stderr, "  [V4L2] %s: %s\n", dev, (char*)cap.card);

    memset(&fmt, 0, sizeof(fmt));
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    fmt.fmt.pix.width       = CAM_W;
    fmt.fmt.pix.height      = CAM_H;
    fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG;
    fmt.fmt.pix.field       = V4L2_FIELD_NONE;
    if (ioctl(fd, VIDIOC_S_FMT, &fmt) < 0) {
        perror("S_FMT"); close(fd); return -1;
    }
    fprintf(stderr, "  [V4L2] S_FMT: %dx%d MJPEG\n", fmt.fmt.pix.width, fmt.fmt.pix.height);

    memset(&parm, 0, sizeof(parm));
    parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    parm.parm.capture.timeperframe.numerator   = 1;
    parm.parm.capture.timeperframe.denominator = TARGET_FPS;
    ioctl(fd, VIDIOC_S_PARM, &parm);

    struct v4l2_requestbuffers req;
    memset(&req, 0, sizeof(req));
    req.count  = NBUF;
    req.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;
    if (ioctl(fd, VIDIOC_REQBUFS, &req) < 0) {
        perror("REQBUFS"); close(fd); return -1;
    }
    fprintf(stderr, "  [V4L2] Buffers: %d\n", req.count);
    return fd;
}

/* ── mmap ── */
static int v4l2_mmap(CamCtx *ctx) {
    ctx->nbufs    = NBUF;
    ctx->bufs     = calloc(ctx->nbufs, sizeof(void*));
    ctx->buf_lens = calloc(ctx->nbufs, sizeof(int));

    for (int i = 0; i < ctx->nbufs; i++) {
        struct v4l2_buffer buf;
        memset(&buf, 0, sizeof(buf));
        buf.index  = i;
        buf.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        if (ioctl(ctx->fd, VIDIOC_QUERYBUF, &buf) < 0) {
            perror("QUERYBUF"); return -1;
        }
        ctx->buf_lens[i] = buf.length;
        ctx->bufs[i] = mmap(NULL, buf.length, PROT_READ | PROT_WRITE,
                           MAP_SHARED, ctx->fd, buf.m.offset);
        if (ctx->bufs[i] == MAP_FAILED) {
            perror("mmap"); return -1;
        }
        memset(&buf, 0, sizeof(buf));
        buf.index  = i;
        buf.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        ioctl(ctx->fd, VIDIOC_QBUF, &buf);
    }
    return 0;
}

/* ── STREAMON ── */
static int v4l2_streamon(int fd) {
    int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    return ioctl(fd, VIDIOC_STREAMON, &type);
}

/* ── DQBUF → 直接写 .jpg 或推入环缓冲 → QBUF ── */
static int grab_and_write(CamCtx *ctx, const char *out_dir) {
    struct v4l2_buffer dqb, qb;

    memset(&dqb, 0, sizeof(dqb));
    dqb.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    dqb.memory = V4L2_MEMORY_MMAP;
    if (ioctl(ctx->fd, VIDIOC_DQBUF, &dqb) < 0)
        return -1;

    if (ctx->using_ring) {
        /* 环缓冲模式 → 推入内存 */
        ring_push(&ctx->ring, (const char*)ctx->bufs[dqb.index], dqb.bytesused);
    } else {
        /* 原模式 → 直接写盘 */
        char path[512];
        snprintf(path, sizeof(path), "%s/%s_%06d.jpg",
                 out_dir, ctx->prefix, ctx->frame_count);
        FILE *fp = fopen(path, "wb");
        if (fp) {
            fwrite(ctx->bufs[dqb.index], 1, dqb.bytesused, fp);
            fclose(fp);
        }
        ctx->total_bytes += dqb.bytesused;
    }
    ctx->frame_count++;

    memset(&qb, 0, sizeof(qb));
    qb.index  = dqb.index;
    qb.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    qb.memory = V4L2_MEMORY_MMAP;
    if (ioctl(ctx->fd, VIDIOC_QBUF, &qb) < 0) {
        perror("QBUF"); return -1;
    }
    return 0;
}

/* ── 关闭 ── */
static void cam_close(CamCtx *ctx) {
    int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    ioctl(ctx->fd, VIDIOC_STREAMOFF, &type);
    for (int i = 0; i < ctx->nbufs; i++) {
        if (ctx->bufs[i] && ctx->bufs[i] != MAP_FAILED)
            munmap(ctx->bufs[i], ctx->buf_lens[i]);
    }
    close(ctx->fd);
    free(ctx->bufs);
    free(ctx->buf_lens);
}

/* ── 打印用法 ── */
static void usage(const char *prog) {
    fprintf(stderr, "用法: %s [-c | <out_dir> [duration] [p0] [p1]]\n", prog);
    fprintf(stderr, "  无参数:       独立模式（倒计时，10秒，output_find/）\n");
    fprintf(stderr, "  <out_dir>...: pipe 模式（无倒计时），out_dir 必填\n");
    fprintf(stderr, "  -c <out_dir>: 连续环缓冲模式，Ctrl+C 停止并 dump 到磁盘\n");
}

/* ═══════════════════════════════════════════════════════════════
 * Main
 * ═══════════════════════════════════════════════════════════════ */
int main(int argc, char **argv) {
    int   continuous_mode = 0;
    int   pipe_mode       = 0;
    const char *out_dir   = "/home/fibo/WAYBACK/output_find";
    int   duration        = DEFAULT_DUR;
    const char *p0        = "cam0";
    const char *p1        = "cam1";

    setvbuf(stdout, NULL, _IONBF, 0);

    /* ── 参数解析 ── */
    if (argc >= 2 && strcmp(argv[1], "-c") == 0) {
        continuous_mode = 1;
        pipe_mode       = 1;
        if (argc >= 3) out_dir = argv[2];
        else           { usage(argv[0]); return 1; }
        if (argc >= 4) p0 = argv[3];
        if (argc >= 5) p1 = argv[4];
    } else if (argc >= 2) {
        pipe_mode = 1;
        out_dir   = argv[1];
        if (argc >= 3) duration = atoi(argv[2]);
        if (argc >= 4) p0 = argv[3];
        if (argc >= 5) p1 = argv[4];
        if (duration < 1 || duration > 60) duration = DEFAULT_DUR;
    }

    mkdir(out_dir, 0755);

    /* ── 打开摄像头 ── */
    CamCtx cam0 = {0}, cam1 = {0};
    cam0.prefix = p0;
    cam1.prefix = p1;

    cam0.fd = v4l2_open(DEV0);
    cam1.fd = v4l2_open(DEV1);
    if (cam0.fd < 0 || cam1.fd < 0) {
        fprintf(stderr, "[ERR] Camera open failed\n");
        return 1;
    }

    if (v4l2_mmap(&cam0) < 0 || v4l2_mmap(&cam1) < 0) {
        fprintf(stderr, "[ERR] mmap failed\n");
        return 1;
    }

    /* ── 环缓冲初始化 ── */
    if (continuous_mode) {
        cam0.using_ring = 1;
        cam1.using_ring = 1;
        ring_init(&cam0.ring);
        ring_init(&cam1.ring);
        signal(SIGINT,  sig_handler);
        signal(SIGTERM, sig_handler);
    }

    /* ── 独立模式倒计时 ── */
    if (!pipe_mode) {
        printf("====================================================\n");
        printf(" [FIND_OUT_C] V4L2 Dual-Camera RAW Capture\n");
        printf("====================================================\n");
        printf(" [CAM] 0: %s  [CAM] 1: %s\n", DEV0, DEV1);
        printf("\n [PREP] Get ready! 3..."); fflush(stdout);
        for (int i = 3; i >= 1; i--) {
            sleep_sec(1.0);
            if (i > 1) printf(" %d...", i-1);
            fflush(stdout);
        }
        sleep_sec(1.0);
        printf(" GO!\n\n"); fflush(stdout);
    }

    /* ── 连续模式前置提示 ── */
    if (continuous_mode) {
        printf("====================================================\n");
        printf(" [RING] Continuous capture (ring buffer %d frames)\n", RING_CAPACITY);
        printf(" [RING] Press Ctrl+C to stop and dump to %s\n", out_dir);
        printf("====================================================\n\n");
    }

    /* ── STREAMON ── */
    v4l2_streamon(cam0.fd);
    v4l2_streamon(cam1.fd);

    /* ── poll() 主循环 ── */
    struct pollfd pfds[2];
    pfds[0].fd     = cam0.fd;
    pfds[0].events = POLLIN;
    pfds[1].fd     = cam1.fd;
    pfds[1].events = POLLIN;

    double t_start  = now_sec();
    int    last_rep = 0;

    if (continuous_mode) {
        /* ── 连续模式：环缓冲，无时长限制 ── */
        while (keep_running) {
            int ret = poll(pfds, 2, 200);
            if (ret < 0) {
                if (errno == EINTR) continue;
                break;
            }

            if (pfds[0].revents & POLLIN)
                grab_and_write(&cam0, out_dir);
            if (pfds[1].revents & POLLIN)
                grab_and_write(&cam1, out_dir);

            if (cam0.frame_count - last_rep >= TARGET_FPS) {
                double el = now_sec() - t_start;
                int cam0_frames = cam0.ring.count;
                int cam1_frames = cam1.ring.count;
                fprintf(stderr, "\r [RING] %ds | cam0=%d frames (%d MB) | cam1=%d frames (%d MB)%s",
                       (int)el,
                       cam0_frames, cam0.ring.total_bytes / (1024*1024),
                       cam1_frames, cam1.ring.total_bytes / (1024*1024),
                       cam0.ring.wrapped ? " [WRAPPED]" : " [FILLING]");
                fflush(stderr);
                last_rep = cam0.frame_count;
            }
        }
        fprintf(stderr, "\n\n [RING] Stopping... dumping ring buffers to %s/\n", out_dir);
        ring_dump(&cam0.ring, out_dir, p0);
        ring_dump(&cam1.ring, out_dir, p1);
        fprintf(stderr, " [RING] Done! adb pull %s/ ./\n", out_dir);
    } else {
        /* ── 定时模式 ── */
        while (now_sec() - t_start < duration) {
            int ret = poll(pfds, 2, 100);
            if (ret < 0) break;

            if (pfds[0].revents & POLLIN)
                grab_and_write(&cam0, out_dir);
            if (pfds[1].revents & POLLIN)
                grab_and_write(&cam1, out_dir);

            if (cam0.frame_count >= MAX_FRAME || cam1.frame_count >= MAX_FRAME)
                break;

            if (cam0.frame_count - last_rep >= 480) {
                double el = now_sec() - t_start;
                fprintf(stderr, " [CAP] %d pairs (cam0=%d cam1=%d) @ %.0f p/s\n",
                       cam0.frame_count, cam0.frame_count, cam1.frame_count,
                       cam0.frame_count / el);
                last_rep = cam0.frame_count;
            }
        }
    }

    double elapsed = now_sec() - t_start;

    /* ── 关闭 ── */
    cam_close(&cam0);
    cam_close(&cam1);

    /* ── 输出结果到 stdout ── */
    if (!continuous_mode) {
        printf("%d %d %.1f\n", cam0.frame_count, cam1.frame_count, elapsed);
    }

    if (!pipe_mode && !continuous_mode) {
        printf("\n ---- FIND_OUT_C Complete ----\n");
        printf(" adb pull %s/ ./output_find/\n", out_dir);
        printf("====================================================\n");
    }
    return 0;
}
