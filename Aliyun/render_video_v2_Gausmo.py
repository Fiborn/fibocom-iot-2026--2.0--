#!/usr/bin/env python3
"""
render_video_v2_Gausmo.py — 2D 轨迹动画视频渲染（3D 轨迹俯视图，逐段绘制，含高斯平滑）

处理 SC171 三角化后的 3D 轨迹（analysis.py 处理后的干净 JSON）。
俯视图：取 X 坐标（球场横向）和 Z 坐标（球场纵向/深度），忽略 Y（高度）。
  - 画布横轴 = 世界 X（米）
  - 画布纵轴 = 世界 Z（米）
  - 球网位于 Z=0，绘制为水平线（横跨 X 方向）

对轨迹点做一维高斯平滑（X、Z 各自平滑），击球点作为硬锚点不动，
非击球区域更连贯平滑，击球点的尖锐转折保留。

用法: python render_video_v2_Gausmo.py <proc_traj.json> [--output out.mp4] [--sigma 1.5]
输出: <同目录>/anim_<文件名>.mp4
"""

import json, sys, os, math
import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analysis

SEGMENT_GAP = 5
CANVAS_W, CANVAS_H = 1280, 720   # 俯视图画布（固定）
FPS = 60
DURATION_PER_POINT = 0.05   # 每个点显示秒数
HOLD_SECONDS = 1.5          # 段绘制完停留
TRANSITION_SECONDS = 0.3    # 段间清空过渡
SUMMARY_SECONDS = 3.0       # 总结画面停留

# ── 球场世界坐标范围（米）与画布映射 ──
# 羽毛球场：横向 x ∈ [-3.05, 3.05]（半宽 3.05m），纵向 z ∈ [-6.7, 6.7]（半场 6.7m）
# 取略大的范围容纳检测误差
X_MIN, X_MAX = -3.5, 3.5
Z_MIN, Z_MAX = -7.0, 7.0
PAD = 50                      # 画布边距（像素）

# 等比例缩放（保证球场纵横比不失真），居中
_avail_w = CANVAS_W - 2 * PAD
_avail_h = CANVAS_H - 2 * PAD
SCALE = min(_avail_w / (X_MAX - X_MIN), _avail_h / (Z_MAX - Z_MIN))
CX = CANVAS_W / 2.0
CY = CANVAS_H / 2.0


def world_to_canvas(x, z):
    """世界坐标（米）→ 画布像素。z 正向朝下。"""
    px = CX + x * SCALE
    py = CY + z * SCALE
    return int(round(px)), int(round(py))


COLORS = [
    (0, 130, 255),    # 橙
    (0, 80, 255),     # 橙红
    (0, 200, 0),      # 绿
    (255, 200, 0),    # 青
    (255, 0, 150),    # 粉
    (200, 0, 200),    # 紫
    (0, 255, 255),    # 黄
    (150, 100, 255),  # 浅蓝
    (0, 165, 255),    # 橙黄
    (200, 200, 0),    # 黄绿
]

# ═══════════════════════════════════════════════════════════════
# Data
# ═══════════════════════════════════════════════════════════════
def load_traj(path):
    """读取 analysis.process_trajectory 输出的干净 3D JSON。
    返回 (xs, zs, fnums, nb, frames) —— xs=世界X, zs=世界Z（2D 纵轴）。
    """
    with open(path) as f:
        data = json.load(f)
    frames = sorted(data.get('frames', []), key=lambda x: x.get('frame', 0))
    xs = [f['xyz'][0] for f in frames]   # 世界 X（米）
    zs = [f['xyz'][2] for f in frames]   # 世界 Z（米）
    fnums = [f['frame'] for f in frames]
    nb = data.get('net_band', None)
    return xs, zs, fnums, nb, frames


def parse_best_hit(s):
    """Parse --best-hit frame,x,z,score[,is_foul]. Returns dict or None."""
    if not s:
        return None
    parts = s.split(",")
    if len(parts) not in (4, 5):
        return None
    try:
        r = {
            "frame": int(parts[0]),
            "xyz": (float(parts[1]), 0.0, float(parts[2])),
            "score": float(parts[3]),
            "is_foul": False,
        }
        if len(parts) >= 5:
            r["is_foul"] = bool(int(parts[4]))
        return r
    except ValueError:
        return None


def parse_all_hits(s):
    """Parse --hits f1,x1,z1,s1[,];f2,... Returns list of dicts."""
    if not s:
        return []
    hits = []
    for part in s.split(";"):
        pieces = part.split(",")
        if len(pieces) not in (4, 5):
            continue
        try:
            h = {
                "frame": int(pieces[0]),
                "xyz": (float(pieces[1]), 0.0, float(pieces[2])),
                "score": float(pieces[3]),
                "is_foul": False,
            }
            if len(pieces) >= 5:
                h["is_foul"] = bool(int(pieces[4]))
            hits.append(h)
        except ValueError:
            continue
    return hits


# ═══════════════════════════════════════════════════════════════
# Net band helpers（俯视图：网带是 z=0 的水平线）
# ═══════════════════════════════════════════════════════════════
def get_net_poly(nb):
    """返回网带在画布上的折线点。俯视图下网带沿 X 方向、位于 Z=z。"""
    if nb:
        half = float(nb.get('half_width', 3.05))
        z = float(nb.get('z', 0.0))
    else:
        half, z = 3.05, 0.0
    p0 = world_to_canvas(-half, z)
    p1 = world_to_canvas(half, z)
    return [p0, p1]


# ═══════════════════════════════════════════════════════════════
# Gaussian smoothing（击球点作硬锚点）
# ═══════════════════════════════════════════════════════════════
def gaussian_smooth_1d(arr, sigma):
    """一维高斯卷积，nearest 边界，返回同长度 numpy 数组。"""
    arr = np.asarray(arr, dtype=np.float64)
    n = len(arr)
    if n < 5:
        return arr
    half = max(1, int(math.ceil(3 * sigma)))
    kernel_size = 2 * half + 1
    if kernel_size >= n:
        kernel_size = n if n % 2 == 1 else n - 1
        half = kernel_size // 2
        if kernel_size < 3:
            return arr
    k = np.exp(-0.5 * (np.arange(kernel_size) - half) ** 2 / sigma ** 2)
    k /= k.sum()
    padded = np.concatenate([np.full(half, arr[0]), arr, np.full(half, arr[-1])])
    return np.convolve(padded, k, mode='valid')


def smooth_segment(seg, hit_frame_set, sigma):
    """对 segment 做高斯平滑（X、Z 各自平滑），击球点作为硬锚点不动。

    seg: [(x, z, frame), ...]
    返回平滑后的 seg（击球点坐标保持原样，其余点平滑）。
    """
    n = len(seg)
    if n < 5:
        return seg
    anchor_idx = sorted([i for i, p in enumerate(seg) if p[2] in hit_frame_set])
    anchor_xz = {i: (seg[i][0], seg[i][1]) for i in anchor_idx}

    # 子段区间（锚点作为相邻子段的共享端点）
    if not anchor_idx:
        ranges = [(0, n)]
    else:
        ranges = []
        prev = 0
        for ai in anchor_idx:
            ranges.append((prev, ai + 1))
            prev = ai
        ranges.append((prev, n))

    out = [None] * n
    for lo, hi in ranges:
        if hi - lo < 1:
            continue
        pts = seg[lo:hi]
        if len(pts) < 5:
            for i in range(lo, hi):
                out[i] = seg[i]
            continue
        xs = gaussian_smooth_1d([p[0] for p in pts], sigma)
        zs = gaussian_smooth_1d([p[1] for p in pts], sigma)
        for k in range(len(pts)):
            i = lo + k
            out[i] = (float(xs[k]), float(zs[k]), pts[k][2])

    # 锚点强制回原值（保持尖锐转折不偏移）
    for i, (ax, az) in anchor_xz.items():
        out[i] = (ax, az, seg[i][2])
    # 兜底（理论上不会发生）
    for i in range(n):
        if out[i] is None:
            out[i] = seg[i]
    return out


# ═══════════════════════════════════════════════════════════════
# Drawing helpers
# ═══════════════════════════════════════════════════════════════
def draw_bg(canvas):
    """Dark background."""
    canvas[:] = (25, 25, 30)


def draw_court(canvas):
    """俯视图球场示意（边界线 + 网带线）。"""
    # 球场边线（横向 ±3.05，纵向 ±6.7）
    top_l = world_to_canvas(-3.05, -6.7)
    top_r = world_to_canvas(3.05, -6.7)
    bot_l = world_to_canvas(-3.05, 6.7)
    bot_r = world_to_canvas(3.05, 6.7)
    line_color = (60, 60, 70)
    cv2.line(canvas, top_l, top_r, line_color, 1, cv2.LINE_AA)
    cv2.line(canvas, bot_l, bot_r, line_color, 1, cv2.LINE_AA)
    cv2.line(canvas, top_l, bot_l, line_color, 1, cv2.LINE_AA)
    cv2.line(canvas, top_r, bot_r, line_color, 1, cv2.LINE_AA)


def draw_net_polyline(canvas, poly):
    """绘制网带（俯视图水平线，绿色）。"""
    if len(poly) >= 2:
        cv2.line(canvas, poly[0], poly[1], (80, 180, 80), 3, cv2.LINE_AA)
        cv2.putText(canvas, "NET (z=0)", (poly[0][0] + 6, poly[0][1] - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80, 180, 80), 1)


def draw_segment_fade(canvas, seg, up_to, color, label):
    """
    Draw segment points [0..up_to]（seg 为世界坐标 (x,z,frame)，映射到画布）。
    Fading from older to newer.
    """
    for pi in range(0, min(up_to + 1, len(seg))):
        x, z, _ = seg[pi]
        px, py = world_to_canvas(x, z)

        # Trail line to next point
        if pi < len(seg) - 1:
            nx, nz, _ = seg[pi + 1]
            npx, npy = world_to_canvas(nx, nz)
            cv2.line(canvas, (px, py), (npx, npy), color, 2, cv2.LINE_AA)

        # Fade: newer points brighter
        age_ratio = pi / max(len(seg) - 1, 1)
        alpha = 0.4 + 0.6 * age_ratio
        c = tuple(int(v * alpha + 30 * (1 - alpha)) for v in color)

        radius = 4
        cv2.circle(canvas, (px, py), radius, c, -1)
        cv2.circle(canvas, (px, py), radius, (255, 255, 255), 1)

    # Label (segment number)
    sx, sz, _ = seg[0]
    spx, spy = world_to_canvas(sx, sz)
    cv2.putText(canvas, label, (spx - 10, spy - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)


def draw_hit_mark(canvas, hit_xyz, is_best=False):
    """
    Draw hit point marker（hit_xyz 为世界坐标 (x,y,z)，用 x/z 映射到画布）。
    - is_best=True (过网击球): red X, larger
    - is_best=False (其他): white cross, small
    """
    px, py = world_to_canvas(hit_xyz[0], hit_xyz[2])
    if is_best:
        size = 12
        cv2.line(canvas, (px-size, py-size), (px+size, py+size), (0, 0, 255), 3)
        cv2.line(canvas, (px+size, py-size), (px-size, py+size), (0, 0, 255), 3)
        cv2.circle(canvas, (px, py), 4, (0, 0, 255), -1)
    else:
        size = 10
        cv2.line(canvas, (px-size, py), (px+size, py), (255, 255, 255), 2)
        cv2.line(canvas, (px, py-size), (px, py+size), (255, 255, 255), 2)


def draw_info(canvas, text_lines, pos="top-left"):
    """Draw info text."""
    for i, line in enumerate(text_lines):
        if pos == "top-left":
            cv2.putText(canvas, line, (12, 25 + i * 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        elif pos == "top-right":
            tw = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)[0][0]
            cv2.putText(canvas, line, (CANVAS_W - tw - 12, 25 + i * 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)


def draw_summary(canvas, has_hit, hit_info=None):
    """Draw end summary."""
    is_foul = has_hit and hit_info and hit_info.get('is_foul')
    if is_foul:
        canvas[:] = (40, 0, 0)      # 暗红背景
        text_color = (255, 200, 200)  # 淡红文字
    else:
        canvas[:] = (20, 20, 25)    # 正常暗背景
        text_color = (0, 255, 0)    # 绿色文字
    if has_hit and hit_info:
        title = "NET-CROSSING HIT!" if is_foul else "HIT"
        xyz = hit_info.get("xyz", (0, 0, 0))
        lines = [
            title,
            "Frame: %d" % hit_info["frame"],
            "Position: (%.2f, %.2f) m" % (xyz[0], xyz[2]),
            "Score: %.3f" % hit_info["score"],
        ]
        y = CANVAS_H // 2 - 60
        for line in lines:
            size = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 1.2, 2)[0]
            x = (CANVAS_W - size[0]) // 2
            cv2.putText(canvas, line, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                        1.2, text_color, 2)
            y += 50
        # Red X for foul, green circle for normal
        if is_foul:
            cv2.line(canvas, (CANVAS_W//2 - 30, y + 10),
                     (CANVAS_W//2 + 30, y + 50), (0, 0, 255), 4)
            cv2.line(canvas, (CANVAS_W//2 + 30, y + 10),
                     (CANVAS_W//2 - 30, y + 50), (0, 0, 255), 4)
        else:
            cv2.circle(canvas, (CANVAS_W//2, y + 30), 18, (0, 200, 0), 4)
            cv2.putText(canvas, "OK", (CANVAS_W//2 - 12, y + 38),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 0), 2)
    else:
        text = "No net-crossing hit"
        size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1.2, 2)[0]
        x = (CANVAS_W - size[0]) // 2
        y = CANVAS_H // 2
        cv2.putText(canvas, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                    1.2, (100, 100, 100), 2)


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════
def main():
    if len(sys.argv) < 2:
        print("Usage: python render_video_v2_Gausmo.py <proc_traj.json> [--output out.mp4] [--sigma 1.5]")
        sys.exit(1)

    path = sys.argv[1]
    custom_output = None
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            custom_output = sys.argv[idx + 1]
    sigma = 1.5
    if "--sigma" in sys.argv:
        idx = sys.argv.index("--sigma")
        if idx + 1 < len(sys.argv):
            try:
                sigma = float(sys.argv[idx + 1])
            except ValueError:
                pass

    xs, zs, fnums, nb, raw_frames = load_traj(path)
    n = len(xs)
    if n < 3:
        print("Too few points")
        return

    # ── 使用 analysis.py 处理后的干净 JSON ──
    processed = raw_frames  # raw_frames = data['frames']
    # 按 seg_id 分组构建 segments（世界坐标 (x, z, frame)）
    seg_map = {}
    for f in processed:
        sid = f.get('seg_id', 0)
        if sid not in seg_map:
            seg_map[sid] = []
        seg_map[sid].append((float(f['xyz'][0]), float(f['xyz'][2]), int(f['frame'])))
    segments = [seg_map[sid] for sid in sorted(seg_map)]

    # 击球点
    all_hits = []
    best_hit = None
    for h in processed:
        if h.get('is_hit'):
            hit_info = {
                'frame': h['frame'],
                'xyz': h['xyz'],
                'score': 1.0,
                'is_foul': h.get('is_foul', False),
            }
            all_hits.append(hit_info)
            if not best_hit or (hit_info['is_foul'] and not best_hit.get('is_foul')):
                best_hit = hit_info
    if all_hits and not best_hit:
        best_hit = all_hits[0]

    # ── 高斯平滑：击球点作硬锚点不动，非击球区域平滑 ──
    hit_frame_set = {h['frame'] for h in all_hits}
    if sigma > 0:
        segments = [smooth_segment(seg, hit_frame_set, sigma) for seg in segments]
        print(f"[SMOOTH] sigma={sigma}, 锚点(击球点)={len(hit_frame_set)} 个")
    n = len(processed)
    print(f"Loaded: {n} filtered points, {len(segments)} segments, {len(all_hits)} hits")
    poly = get_net_poly(nb)
    if not poly:
        print("[ERR] No net_band data — aborting")
        sys.exit(1)

    # Determine which segment contains best hit
    best_seg_idx = -1
    if best_hit:
        bf = best_hit["frame"]
        for si, seg in enumerate(segments):
            for pt in seg:
                if pt[2] == bf:
                    best_seg_idx = si
                    break
            if best_seg_idx >= 0:
                break

    # Map all_hits to segments for white cross rendering
    seg_hits = {}
    for h in all_hits:
        hf = h["frame"]
        for si, seg in enumerate(segments):
            for pt in seg:
                if pt[2] == hf:
                    seg_hits.setdefault(si, []).append(h)
                    break
            else:
                continue
            break

    print("Rendering %d detections, %d segments%s" %
          (n, len(segments),
           ", best hit in seg#%d" % best_seg_idx if best_seg_idx >= 0 else ", no hit"))
    if all_hits:
        n_foul = sum(1 for h in all_hits if h.get('is_foul'))
        if n_foul:
            print("  %d foul hit(s), %d other hit(s)" % (n_foul, len(all_hits) - n_foul))
        else:
            print("  %d additional hits for white cross" % len(all_hits))

    # Video writer
    base = os.path.splitext(os.path.basename(path))[0]
    out_path = custom_output if custom_output else os.path.join(os.path.dirname(path), "anim_%s.mp4" % base)
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    writer = cv2.VideoWriter(out_path, fourcc, FPS, (CANVAS_W, CANVAS_H))

    canvas = np.zeros((CANVAS_H, CANVAS_W, 3), dtype=np.uint8)

    total_frames = 0

    # ══════════════════════════════════════════════════════════
    # Render each segment
    # ══════════════════════════════════════════════════════════
    for si, seg in enumerate(segments):
        seg_label = "S%d" % si
        n_pts = len(seg)
        frames_per_seg = int(n_pts * DURATION_PER_POINT * FPS)
        hit_list = seg_hits.get(si, [])

        # ── Animate points appearing ──
        for fi in range(frames_per_seg):
            draw_bg(canvas)
            draw_court(canvas)
            draw_net_polyline(canvas, poly)

            up_to = int(fi / frames_per_seg * n_pts)
            color = COLORS[si % len(COLORS)]

            draw_segment_fade(canvas, seg, up_to, color, seg_label)

            # Hit markers when segment is fully drawn
            if up_to >= n_pts - 1:
                for h in hit_list:
                    if h.get('is_foul'):
                        draw_hit_mark(canvas, h["xyz"], is_best=True)     # red X
                    else:
                        draw_hit_mark(canvas, h["xyz"], is_best=False)   # white cross

            draw_info(canvas, [
                "%s  %d pts" % (seg_label, n_pts),
                "Seg %d/%d" % (si + 1, len(segments)),
            ], pos="top-right")

            writer.write(canvas)
            total_frames += 1

        # ── Hold (segment fully drawn, show hits) ──
        for _ in range(int(HOLD_SECONDS * FPS)):
            draw_bg(canvas)
            draw_court(canvas)
            draw_net_polyline(canvas, poly)
            draw_segment_fade(canvas, seg, n_pts - 1, COLORS[si % len(COLORS)], seg_label)

            has_foul_hold = any(h.get('is_foul') for h in hit_list)
            for h in hit_list:
                if h.get('is_foul'):
                    draw_hit_mark(canvas, h["xyz"], is_best=True)     # red X
                else:
                    draw_hit_mark(canvas, h["xyz"], is_best=False)   # white cross
            if has_foul_hold:
                draw_info(canvas, ["FOUL!"], pos="top-left")
            elif best_hit:
                draw_info(canvas, ["HIT! Frame %d" % best_hit["frame"]], pos="top-left")

            draw_info(canvas, [
                "%s  %d pts" % (seg_label, n_pts),
                "Seg %d/%d" % (si + 1, len(segments)),
            ], pos="top-right")

            writer.write(canvas)
            total_frames += 1

        # ── Clear transition ──
        for _ in range(int(TRANSITION_SECONDS * FPS)):
            draw_bg(canvas)
            draw_court(canvas)
            draw_net_polyline(canvas, poly)
            writer.write(canvas)
            total_frames += 1

    # ══════════════════════════════════════════════════════════
    # Summary
    # ══════════════════════════════════════════════════════════
    for _ in range(int(SUMMARY_SECONDS * FPS)):
        draw_summary(canvas, best_hit is not None, best_hit)
        writer.write(canvas)
        total_frames += 1

    print("  [SAVE] %s" % out_path)
    print("  %d frames written" % total_frames)
    writer.release()


if __name__ == "__main__":
    main()
