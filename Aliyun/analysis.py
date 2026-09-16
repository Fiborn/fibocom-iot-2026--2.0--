#!/usr/bin/env python3
"""
analysis.py — 过网击球检测（3D 轨迹版，运行于阿里云）

处理 SC171 三角化后的 3D 轨迹。坐标约定：
  - xOz 平面 = 球场地面（x 横向、z 纵向/深度）
  - y 方向 = 网柱方向（竖直高度）
  - 球网位于 z=0 平面（球场中央），网高 1.55m

过网击球判定条件:
  1. Z 轴方向速度分量反转（物理击球点，球穿越网平面 z=0 后折返）
  2. 一段轨迹的 100% 在网带的同一侧（z<0 或 z>0）
  3. 球前、击球点、球后都在网带的同一侧
  条件 2+3 同时满足 → 过网击球（foul）

轨迹坐标必须为 3D 世界坐标（米），每点 {frame, x, y, z, conf}。

导出:
  find_hit_by_side(xs, ys, zs, fnums, net_pos=0.0) → [hit, ...]
  find_best_hit(xs, ys, zs, fnums, net_pos=0.0) → hit | None
  compute_suspicion_score(points_3d, net_z=0.0, ...) → (score, info)
  print_suspicion_report(score, info)
  interpolate_trajectory(xs, ys, zs, fnums) → (xs, ys, zs, fnums)
  process_trajectory(raw_data) → dict
"""

import os, sys, json, math
from datetime import datetime

SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "2D")

# ── Detection thresholds（米制）──
MIN_WINDOW = 2                # 前/后各 2 点在同一侧 → 击球点
MAX_WINDOW = 2                # 评分窗口和检测窗口相同
MIN_HIT_SPEED = 0.05          # 前 2 帧总位移最小值（米，排除自然弧度）
MIN_SPEED_DIFF = 0.03         # 前/后位移差异最小值（米，排除对称轨迹）

# ── 轨迹质量过滤（米制，坐标已为世界坐标）──
CLUSTER_VARIANCE_THRESHOLD = 0.05    # 3D 方差 < 此值 → 集群噪点（约 0.22m 半径）
LANDED_VAR_THRESHOLD = 0.001         # 最近 2 点位移平方 < 此值 → 球已落地静止（约 3cm）
SPIKE_WINDOW = 3                     # 参考前 N 个点的位置
SPIKE_THRESHOLD_MULT = 7.0           # 距离 > 参考平均距离 × 此值 = 弹跳噪点
SPIKE_REMOVE_RADIUS = 1              # 标记噪点后连带删掉前后各几个点

# 孤立噪点：某点距离前后各2点都很远的倍数阈值
ISOLATED_RATIO = 5.0                 # 超过局部位移中位数的多少倍视为孤立


def _dist3d(ax, ay, az, bx, by, bz):
    """三维欧氏距离。"""
    dx = ax - bx
    dy = ay - by
    dz = az - bz
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def remove_isolated_noise(xs, ys, zs, fnums, ratio=None):
    """
    孤立噪点过滤（3D）：检查每个点与其前后各2个点的三维距离。
    如果某点到前2点、后2点的全部4个距离都超过局部邻域位移中位数的
    ratio 倍，则视为孤立噪点删除。

    使用局部中位数而非全局中位数，避免高速段落被误判。

    ratio: 默认 ISOLATED_RATIO (5.0)
    返回: (xs, ys, zs, fnums)（已去除孤立噪点）
    """
    if ratio is None:
        ratio = ISOLATED_RATIO
    n = len(xs)
    if n < 6:
        return xs, ys, zs, fnums

    isolated_flags = [False] * n

    for i in range(2, n - 2):
        # ── 局部邻域位移中位数（±3帧范围内，3D 距离）──
        lo = max(1, i - 3)
        hi = min(n - 1, i + 4)
        window_dists = []
        for j in range(lo, hi):
            d = _dist3d(xs[j], ys[j], zs[j], xs[j-1], ys[j-1], zs[j-1])
            window_dists.append(d)
        window_dists.sort()
        local_median = window_dists[len(window_dists) // 2]
        if local_median < 1e-6:
            local_median = 1e-6

        # 到前2个点的距离
        d_prev2 = _dist3d(xs[i], ys[i], zs[i], xs[i-2], ys[i-2], zs[i-2])
        d_prev1 = _dist3d(xs[i], ys[i], zs[i], xs[i-1], ys[i-1], zs[i-1])
        # 到后2个点的距离
        d_next1 = _dist3d(xs[i], ys[i], zs[i], xs[i+1], ys[i+1], zs[i+1])
        d_next2 = _dist3d(xs[i], ys[i], zs[i], xs[i+2], ys[i+2], zs[i+2])

        # 如果都比局部中位数大 ratio 倍以上 -> 孤立噪点
        threshold = local_median * ratio
        if (d_prev2 >= threshold and d_prev1 >= threshold and
                d_next1 >= threshold and d_next2 >= threshold):
            isolated_flags[i] = True

    if not any(isolated_flags):
        return xs, ys, zs, fnums

    n_remove = sum(isolated_flags)
    new_xs = [xs[i] for i in range(n) if not isolated_flags[i]]
    new_ys = [ys[i] for i in range(n) if not isolated_flags[i]]
    new_zs = [zs[i] for i in range(n) if not isolated_flags[i]]
    new_fnums = [fnums[i] for i in range(n) if not isolated_flags[i]]

    print("  [FILTER] 去除 %d 个孤立噪点 (剩余 %d 点)" % (n_remove, len(new_xs)))
    return new_xs, new_ys, new_zs, new_fnums


# ═══════════════════════════════════════════════════════════
# 轨迹质量过滤
# ═══════════════════════════════════════════════════════════

def remove_spike_noise(xs, ys, zs, fnums, mult=None):
    """
    检测轨迹中的弹跳噪点（spike，3D）：某点突然远离前面连续点的位置。

    对每个点 i，用前 SPIKE_WINDOW 个有效点拟合中心，
    如果 i 到中心的距离 > 平均距离 × mult，
    则标记 i 及 SPIKE_REMOVE_RADIUS 范围内为噪点并删除。

    mult: 默认 SPIKE_THRESHOLD_MULT (7.0)
    返回: (xs, ys, zs, fnums)（已去除 spike）
    """
    if mult is None:
        mult = SPIKE_THRESHOLD_MULT
    n = len(xs)
    if n < SPIKE_WINDOW + 2:
        return xs, ys, zs, fnums

    spike_flags = [False] * n

    for i in range(SPIKE_WINDOW, n):
        # ── 取前 SPIKE_WINDOW 个未被标记为 spike 的点做参考 ──
        ref_idx = []
        for j in range(i - SPIKE_WINDOW, i):
            if not spike_flags[j]:
                ref_idx.append(j)
        if len(ref_idx) < 2:
            continue

        # 参考点中心
        cx = sum(xs[j] for j in ref_idx) / len(ref_idx)
        cy = sum(ys[j] for j in ref_idx) / len(ref_idx)
        cz = sum(zs[j] for j in ref_idx) / len(ref_idx)

        # 参考点到中心的平均距离
        avg_dist = sum(_dist3d(xs[j], ys[j], zs[j], cx, cy, cz)
                       for j in ref_idx) / len(ref_idx)
        if avg_dist < 1e-6:
            avg_dist = 1e-6  # 避免除零

        # 当前点到中心的距离
        dist = _dist3d(xs[i], ys[i], zs[i], cx, cy, cz)

        if dist > avg_dist * mult:
            spike_flags[i] = True
            # 连带删除附近点
            for d in range(1, SPIKE_REMOVE_RADIUS + 1):
                if i - d >= 0:
                    spike_flags[i - d] = True
                if i + d < n:
                    spike_flags[i + d] = True

    if not any(spike_flags):
        return xs, ys, zs, fnums

    n_spike = sum(spike_flags)
    new_xs = [xs[i] for i in range(n) if not spike_flags[i]]
    new_ys = [ys[i] for i in range(n) if not spike_flags[i]]
    new_zs = [zs[i] for i in range(n) if not spike_flags[i]]
    new_fnums = ([fnums[i] for i in range(n) if not spike_flags[i]]
                 if fnums else list(range(len(new_xs))))

    print("  [FILTER] 去除 %d 个弹跳噪点 (剩余 %d 点)" % (n_spike, len(new_xs)))
    return new_xs, new_ys, new_zs, new_fnums


def check_cluster_noise(xs, ys, zs, fnums):
    """
    集群噪点检查（3D）：所有点挤在同一小区域（方差 < CLUSTER_VARIANCE_THRESHOLD）
    → 判定为噪点或噪点累积。

    返回: (valid: bool, reason: str, xs, ys, zs, fnums)
    """
    n = len(xs)
    if n < 3:
        return False, 'too_few', xs, ys, zs, fnums

    ctr_x = sum(xs) / n
    ctr_y = sum(ys) / n
    ctr_z = sum(zs) / n
    var = sum((x - ctr_x) ** 2 + (y - ctr_y) ** 2 + (z - ctr_z) ** 2
              for x, y, z in zip(xs, ys, zs)) / n

    if var < CLUSTER_VARIANCE_THRESHOLD:
        return False, 'clustered_noise(var=%.4f)' % var, xs, ys, zs, fnums

    return True, 'ok', xs, ys, zs, fnums


def clip_landed_tail(xs, ys, zs, fnums):
    """
    球已落地：轨迹尾部连续静止点切除。
    只从真实尾部（最后面）倒着数连续静止点，
    连续 ≥ 4 帧且紧贴尾部才切。不会删中间静止段。

    返回: (xs, ys, zs, fnums)（已切除落地静止段）
    """
    n = len(xs)
    if n < 4:
        return xs, ys, zs, fnums

    # 从尾部往前数连续静止点
    tail_count = 0
    for i in range(n - 1, 0, -1):
        d2 = _dist3d(xs[i], ys[i], zs[i], xs[i-1], ys[i-1], zs[i-1]) ** 2
        if d2 <= LANDED_VAR_THRESHOLD:
            tail_count += 1
        else:
            break  # 遇到运动点，结束计数

    if tail_count >= 4:
        clip_idx = n - tail_count
        clipped_xs = xs[:clip_idx]
        clipped_ys = ys[:clip_idx]
        clipped_zs = zs[:clip_idx]
        clipped_fnums = fnums[:clip_idx] if fnums else list(range(clip_idx))
        print("  [FILTER] 切除尾部 %d 个静止点 (剩余 %d 点)" % (tail_count, clip_idx))
        return clipped_xs, clipped_ys, clipped_zs, clipped_fnums

    return xs, ys, zs, fnums


def score_bar(score):
    if score <= -5: return '[X] 已滤去'
    if score <= 0:  return '[  ] 无击球'
    if score < 0.5: return '[─] 疑似'
    if score < 0.8: return '[>] 击球（正常）'
    return '[!] 过网击球'


# ═══════════════════════════════════════════════════════════
# 轨迹插值
# ═══════════════════════════════════════════════════════════

def interpolate_trajectory(xs, ys, zs, fnums):
    """
    在每两个连续检测点之间插入一个拟合点（位置平均，3D）。
    返回插值后的 (xs, ys, zs, fnums)，已按 frame 排序。
    """
    new_xs, new_ys, new_zs, new_fs = [], [], [], []
    for i in range(len(xs)):
        if i > 0:
            gap = fnums[i] - fnums[i-1]
            if gap >= 2:
                mid_f = (fnums[i-1] + fnums[i]) // 2
                mid_x = (xs[i-1] + xs[i]) / 2.0
                mid_y = (ys[i-1] + ys[i]) / 2.0
                mid_z = (zs[i-1] + zs[i]) / 2.0
                new_xs.append(mid_x)
                new_ys.append(mid_y)
                new_zs.append(mid_z)
                new_fs.append(mid_f)
        new_xs.append(xs[i])
        new_ys.append(ys[i])
        new_zs.append(zs[i])
        new_fs.append(fnums[i])
    return new_xs, new_ys, new_zs, new_fs


# ═══════════════════════════════════════════════════════════
# 击球点检测（3D）：前/后点都在该点的同一侧（Z 轴）→ 击球点
# ═══════════════════════════════════════════════════════════

def find_hit_by_side(xs, ys, zs, fnums, net_pos=0.0):
    """
    击球点检测（3D）：前/后点都在该点的同一侧（Z 轴）→ 击球点。
    球网位于 z=net_pos（默认 0.0 = 球场中央平面），检测 Z 方向折返。
    轨迹坐标必须为 3D 世界坐标（米）。

    net_pos: 网带 Z 位置（默认 0.0）

    返回: [ {frame, xyz, score, side, d_before, d_after, consistency, is_foul}, ... ]
    """
    n = len(zs)
    if n < MIN_WINDOW * 2 + 1:
        return []

    candidates = []

    for i in range(MIN_WINDOW, n - 1):
        cz = zs[i]
        half = MIN_WINDOW

        # ── 前 2 和后 2 是否都在同一侧（Z 方向）──
        before = zs[i - half:i]
        after = zs[i + 1:i + 1 + half]
        all_left = all(z < cz for z in before) and all(z < cz for z in after)
        all_right = all(z > cz for z in before) and all(z > cz for z in after)
        if not (all_left or all_right):
            continue
        side = 'left' if all_left else 'right'
        surrounding = list(before) + list(after)
        n_side = (sum(1 for z in surrounding if z < cz) if all_left
                  else sum(1 for z in surrounding if z > cz))
        d_before = abs(zs[i] - zs[i - 2])
        d_after = abs(zs[min(n - 1, i + 2)] - zs[i])
        total = len(surrounding)

        # ── 位移硬过滤（Z 轴折返）──
        if d_before < MIN_HIT_SPEED:
            continue
        if abs(d_before - d_after) < MIN_SPEED_DIFF:
            continue

        score = n_side / max(total, 1)

        # ── 过网击球判定 ──
        is_foul = False
        hit_val = zs[i]
        hit_is_far = hit_val >= net_pos

        # 统一规则: 该段全部轨迹点必须 100% 与网同侧才判过网
        same_side_count = sum(1 for v in zs if (v >= net_pos) == hit_is_far)
        if same_side_count == len(zs):
            is_foul = True

        candidates.append({
            "frame": fnums[i],
            "xyz": [round(xs[i], 4), round(ys[i], 4), round(zs[i], 4)],
            "score": round(score, 3),
            "side": side,
            "is_foul": is_foul,
            "d_before": round(d_before, 4),
            "d_after": round(d_after, 4),
            "consistency": "%d/%d" % (n_side, total),
        })

    return sorted(candidates, key=lambda c: -c['score'])


# ═══════════════════════════════════════════════════════════
# 最佳击球点
# ═══════════════════════════════════════════════════════════

def find_best_hit(xs, ys, zs, fnums, net_pos=0.0):
    """评分最高的过网击球点。"""
    all_hits = find_hit_by_side(xs, ys, zs, fnums, net_pos)
    if not all_hits:
        return None
    return all_hits[0]


# ═══════════════════════════════════════════════════════════
# 两轮迭代去噪
# ═══════════════════════════════════════════════════════════

def iterative_filter(xs, ys, zs, fnums):
    """
    两轮迭代去噪（3D）：
    第1轮（宽松 6×/8×）：只清理最明显噪点，避免误删真实轨迹点
    第2轮（标准 5×/7×）：在已清理的干净数据上重新过滤，参考度量不受噪点污染

    注：击球后球速瞬间加快(1~2x)，宽松阈值(6x/8x)完全覆盖此场景，
    第2轮5x/7x仍远大于1~2x，不会误删真实击球后加速帧。
    """
    # ── 第1轮：宽松去噪（只删最离谱的）──
    xs, ys, zs, fnums = remove_isolated_noise(xs, ys, zs, fnums, ratio=6.0)
    xs, ys, zs, fnums = remove_spike_noise(xs, ys, zs, fnums, mult=8.0)
    xs, ys, zs, fnums = clip_landed_tail(xs, ys, zs, fnums)

    # ── 第2轮：标准去噪（在已清理数据上重新过滤，使用标准阈值）──
    xs, ys, zs, fnums = remove_isolated_noise(xs, ys, zs, fnums, ratio=5.0)
    xs, ys, zs, fnums = remove_spike_noise(xs, ys, zs, fnums, mult=7.0)
    xs, ys, zs, fnums = clip_landed_tail(xs, ys, zs, fnums)

    return xs, ys, zs, fnums


# ═══════════════════════════════════════════════════════════
# 可疑度评分
# ═══════════════════════════════════════════════════════════

SEGMENT_GAP = 6                # 段拆分阈值：帧间隙超过此值视为新段

def compute_suspicion_score(points_3d, net_z=0.0, fnums=None):
    """
    过网击球可疑度计算（逐段检测，仅 Z 轴折返）。
    轨迹坐标必须为 3D 世界坐标（米）。

    points_3d: [(x,y,z), ...] 或 [{'xyz':(x,y,z), 'frame':n}, ...]
    net_z: 网带 Z 位置（默认 0.0 = 球场中央）

    返回: (score, info_dict)
      score > 0 = 过网击球置信度
      score = 0 = 无过网击球
    """
    n = len(points_3d)
    if n < 5:
        return -10.0, {
            'type': 'too_short', 'score': -10.0,
            'note': '只有 %d 个点' % n,
            'hit_points': [],
        }

    # Extract xs, ys, zs, fnums
    if fnums is None:
        if isinstance(points_3d[0], dict):
            xs = [p['xyz'][0] for p in points_3d]
            ys = [p['xyz'][1] for p in points_3d]
            zs = [p['xyz'][2] for p in points_3d]
            fnums = [p.get('frame', i) for i, p in enumerate(points_3d)]
        else:
            xs = [p[0] for p in points_3d]
            ys = [p[1] for p in points_3d]
            zs = [p[2] for p in points_3d]
            fnums = list(range(n))

    # ── 轨迹质量过滤（顺序：集群→两轮迭代去噪） ──
    valid, reason, xs, ys, zs, fnums = check_cluster_noise(xs, ys, zs, fnums)
    if not valid:
        return -10.0, {
            'type': 'noise', 'score': -10.0,
            'note': '滤去: ' + reason,
            'hit_points': [],
        }
    xs, ys, zs, fnums = iterative_filter(xs, ys, zs, fnums)

    # ── 按帧间隙拆段 ──
    segments = []
    cur_x, cur_y, cur_z, cur_f = [], [], [], []
    for i in range(len(xs)):
        if cur_f and fnums[i] - cur_f[-1] > SEGMENT_GAP:
            if len(cur_x) >= 2:
                segments.append((cur_x, cur_y, cur_z, cur_f))
            cur_x, cur_y, cur_z, cur_f = [], [], [], []
        cur_x.append(xs[i])
        cur_y.append(ys[i])
        cur_z.append(zs[i])
        cur_f.append(fnums[i])
    if len(cur_x) >= 2:
        segments.append((cur_x, cur_y, cur_z, cur_f))

    # ── 去除过短段（≤4 点，不能形成有效轨迹）──
    before = len(segments)
    segments = [(x, y, z, f) for x, y, z, f in segments if len(x) > 4]
    if len(segments) < before:
        print("  [FILTER] 丢弃 %d 个过短段 (≤4点)" % (before - len(segments)))

    if not segments:
        return 0.0, {
            'type': 'no_segments', 'score': 0.0,
            'note': '所有段均过短',
            'hit_points': [],
        }

    # ── 逐段检测（仅 Z 轴折返）──
    all_hits = []
    for seg_x, seg_y, seg_z, seg_f in segments:
        seg_x, seg_y, seg_z, seg_f = interpolate_trajectory(seg_x, seg_y, seg_z, seg_f)
        hits = find_hit_by_side(seg_x, seg_y, seg_z, seg_f, net_pos=net_z)
        for h in hits:
            all_hits.append(h)
    all_hits.sort(key=lambda h: (0 if h.get('is_foul') else 1, -h['score']))
    best_hit = all_hits[0] if all_hits else None

    # ── 全局轨迹长度（用于评分，3D） ──
    xs_all, ys_all, zs_all, _ = interpolate_trajectory(xs, ys, zs, fnums)
    total_length = sum(_dist3d(xs_all[i], ys_all[i], zs_all[i],
                               xs_all[i-1], ys_all[i-1], zs_all[i-1])
                       for i in range(1, len(xs_all)))

    if best_hit:
        score = best_hit['score']
        info_type = 'apex_hit'
        foul_tag = ' [过网击球!]' if best_hit.get('is_foul') else ' [普通击球]'
        base = ('击球点! F%d (%.3f,%.3f,%.3f) score=%.3f side=%s 前%.3f→后%.3f 同侧=%s'
                % (best_hit['frame'], best_hit['xyz'][0], best_hit['xyz'][1],
                   best_hit['xyz'][2], best_hit['score'], best_hit['side'],
                   best_hit.get('d_before', 0), best_hit.get('d_after', 0),
                   best_hit['consistency']))
        note = base + foul_tag
    else:
        score = -min(total_length / 3.0, 5.0)
        info_type = 'smooth'
        note = '无击球点（%d 个点, %.2fm）' % (len(xs_all), total_length)

    info = {
        'type': info_type,
        'score': round(score, 3),
        'hit_points': all_hits,
        'hit_count': len(all_hits),
        'note': note,
        'length_m': round(total_length, 3),
    }
    return round(score, 3), info


# ═══════════════════════════════════════════════════════════
# 报告打印
# ═══════════════════════════════════════════════════════════

def print_suspicion_report(score, info):
    print()
    print("  " + "─" * 46)
    print("   过网击球分析（3D Z 轴折返检测）")
    print("  " + "─" * 46)
    print("  类型:     %s" % info['type'])
    print("  说明:     %s" % info['note'])
    print("  轨迹长度: %.3fm" % info['length_m'])

    hit_pts = info.get('hit_points', [])
    if hit_pts:
        print("  ── 击球点候选（%d 个）──" % len(hit_pts))
        for h in hit_pts[:5]:
            foul_flag = ' [过网]' if h.get('is_foul') else ' [普通]'
            print("    F%d (%.3f,%.3f,%.3f) score=%.3f side=%s 前%.3f→后%.3f 同侧=%s%s" %
                  (h['frame'], h['xyz'][0], h['xyz'][1], h['xyz'][2],
                   h['score'], h['side'],
                   h.get('d_before', 0), h.get('d_after', 0),
                   h['consistency'], foul_flag))

    print("  可疑度:   %+.3f  %s" % (score, score_bar(score)))


# ═══════════════════════════════════════════════════════════
# 完整轨迹处理：过滤 + 击球检测 → 干净 JSON
# ═══════════════════════════════════════════════════════════

def process_trajectory(raw_data: dict, net_pos=None) -> dict:
    """
    一站式处理：接收 SC171 三角化后的原始 3D 轨迹数据，
    返回过滤后 + 击球标注的干净 JSON，可直接用于 2D/3D 渲染。

    输入 raw_data: {'frames': [{frame, x, y, z, conf}, ...], ...}
    输出: {
        'frames': [{frame, xyz, conf, seg_id, is_hit, is_foul}, ...],
        'total_frames': int,
        'net_band': {z, height, half_width, type},
        'hit_points': [{frame, xyz, score, is_foul, seg_id, side, ...}, ...],
        'suspicion_score': float,
        'raw_count': int,
        'filtered_count': int,
    }
    """
    frames_raw = raw_data.get('frames', [])
    if not frames_raw:
        return {'error': 'empty', 'frames': [], 'hit_points': [], 'suspicion_score': 0.0}

    raw_count = len(frames_raw)

    # ── 提取坐标（3D：x/y/z）──
    xs = [f['x'] for f in frames_raw]
    ys = [f['y'] for f in frames_raw]
    zs = [f['z'] for f in frames_raw]
    fnums = [f.get('frame', i) for i, f in enumerate(frames_raw)]
    # frame → conf 映射（过滤后按帧号查回）
    _conf_map = {f.get('frame', i): f.get('conf', f.get('confidence', 0))
                 for i, f in enumerate(frames_raw)}

    # ── 网带（3D：固定 z=0 平面，高 1.55m，半宽 3.05m）──
    if net_pos is None:
        net_pos = 0.0
    nb_out = {
        'z': float(net_pos),
        'height': 1.55,
        'half_width': 3.05,
        'type': 'vertical_net',
    }

    # ── 过滤（3D）──
    valid, reason, xs, ys, zs, fnums = check_cluster_noise(xs, ys, zs, fnums)
    if not valid:
        return {'error': reason, 'frames': [], 'hit_points': [], 'suspicion_score': 0.0}
    xs, ys, zs, fnums = iterative_filter(xs, ys, zs, fnums)

    if len(xs) < 3:
        return {'error': 'too_few_after_filter', 'frames': [],
                'hit_points': [], 'suspicion_score': 0.0}

    filtered_count = len(xs)

    # ── 拆段 ──
    segments_raw = []
    cur_x, cur_y, cur_z, cur_f, cur_conf = [], [], [], [], []
    for i in range(len(xs)):
        if cur_f and fnums[i] - cur_f[-1] > SEGMENT_GAP:
            if len(cur_x) >= 2:
                segments_raw.append((cur_x, cur_y, cur_z, cur_f, cur_conf))
            cur_x, cur_y, cur_z, cur_f, cur_conf = [], [], [], [], []
        cur_x.append(xs[i])
        cur_y.append(ys[i])
        cur_z.append(zs[i])
        cur_f.append(fnums[i])
        cur_conf.append(_conf_map.get(fnums[i], 0))
    if len(cur_x) >= 2:
        segments_raw.append((cur_x, cur_y, cur_z, cur_f, cur_conf))

    # ── 击球点检测（逐段，插值后）──
    all_hits = []
    point_hit_map = {}  # frame -> hit info
    for sid, (seg_x, seg_y, seg_z, seg_f, seg_conf) in enumerate(segments_raw):
        ix, iy, iz, inf = interpolate_trajectory(seg_x, seg_y, seg_z, seg_f)
        hits = find_hit_by_side(ix, iy, iz, inf, net_pos=net_pos)
        for h in hits:
            h['seg_id'] = sid
            all_hits.append(h)
            point_hit_map[h['frame']] = h

    all_hits.sort(key=lambda h: (0 if h.get('is_foul') else 1, -h['score']))

    # ── 构建输出 frames（带标注）──
    frames_out = []
    for sid, (seg_x, seg_y, seg_z, seg_f, seg_conf) in enumerate(segments_raw):
        for i in range(len(seg_x)):
            fn = seg_f[i]
            hit = point_hit_map.get(fn)
            frames_out.append({
                'frame': int(fn),
                'xyz': [round(float(seg_x[i]), 4),
                        round(float(seg_y[i]), 4),
                        round(float(seg_z[i]), 4)],
                'conf': round(float(seg_conf[i]), 4) if i < len(seg_conf) else 0,
                'seg_id': sid,
                'is_hit': hit is not None,
                'is_foul': hit.get('is_foul', False) if hit else False,
            })

    # ── 汇总 hit_points ──
    hit_points = [{
        'frame': int(h['frame']),
        'xyz': [round(float(h['xyz'][0]), 4),
                round(float(h['xyz'][1]), 4),
                round(float(h['xyz'][2]), 4)],
        'score': round(float(h['score']), 3),
        'is_foul': bool(h.get('is_foul', False)),
        'seg_id': int(h.get('seg_id', 0)),
        'side': h.get('side', ''),
        'consistency': h.get('consistency', ''),
    } for h in all_hits]

    suspicion_score = hit_points[0]['score'] if hit_points else 0.0
    if hit_points and any(h['is_foul'] for h in hit_points):
        suspicion_score = 1.0

    return {
        'frames': frames_out,
        'total_frames': int(max(fnums) + 1) if fnums else 0,
        'net_band': nb_out,
        'hit_points': hit_points,
        'suspicion_score': round(suspicion_score, 3),
        'raw_count': raw_count,
        'filtered_count': filtered_count,
        'coordinate_system': 'xOz=ground, y=height (meters)',
    }


# ═══════════════════════════════════════════════════════════
# CLI：单文件处理（阿里云上手动测试用）
# ═══════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print("Usage: python analysis.py <trajectory.json> [output.json]")
        sys.exit(1)

    input_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else None

    with open(input_path, 'r', encoding='utf-8') as f:
        raw = json.load(f)

    result = process_trajectory(raw)
    if result.get('error'):
        print("[ERR] %s" % result['error'])
        sys.exit(1)

    print("=" * 48)
    print("  Analysis — 3D Foul Detection (Z-axis + side)")
    print("=" * 48)
    print("  原始点: %d → 过滤后: %d" % (result['raw_count'], result['filtered_count']))
    print("  击球点: %d (过网 %d)" % (
        len(result['hit_points']),
        sum(1 for h in result['hit_points'] if h['is_foul'])))

    score, info = compute_suspicion_score(result['frames'], net_z=0.0)
    print_suspicion_report(score, info)

    if out_path:
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print("  已保存: %s" % out_path)


if __name__ == "__main__":
    main()
