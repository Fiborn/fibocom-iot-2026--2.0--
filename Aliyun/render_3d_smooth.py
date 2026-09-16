import json, os, sys, math
import numpy as np

# ─── 配置 ──────────────────────────────────────────────────────────────
SIGMA            = 1.5           # 高斯平滑 sigma
SUBSAMPLE_HTML   = 4             # 交互式 HTML 的降采样（每隔 N 取一点）

# 3D 网带端点（世界坐标，米）：网带沿 x 方向、位于 z=0、顶部高 1.55m
NET_L = (-3.05, 1.55, 0.0)
NET_R = (3.05, 1.55, 0.0)


# ─── 一维高斯平滑 ──────────────────────────────────────────────────────
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


def smooth_segment_3d(seg, hit_frame_set, sigma):
    """对一段 3D 轨迹做高斯平滑（x/y/z 各自平滑），击球点作为硬锚点不动。

    seg: [{frame, xyz:[x,y,z], conf, ...}, ...]
    返回平滑后的 seg（击球点坐标保持原样，其余点平滑）。
    """
    n = len(seg)
    if n < 5:
        return seg
    anchor_idx = sorted([i for i, p in enumerate(seg) if p['frame'] in hit_frame_set])
    anchor_xyz = {i: seg[i]['xyz'] for i in anchor_idx}

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
        xs = gaussian_smooth_1d([p['xyz'][0] for p in pts], sigma)
        ys = gaussian_smooth_1d([p['xyz'][1] for p in pts], sigma)
        zs = gaussian_smooth_1d([p['xyz'][2] for p in pts], sigma)
        for k in range(len(pts)):
            i = lo + k
            p = dict(pts[k])
            p['xyz'] = [float(xs[k]), float(ys[k]), float(zs[k])]
            out[i] = p

    # 锚点强制回原值（保持尖锐转折不偏移）
    for i, xyz in anchor_xyz.items():
        out[i] = dict(seg[i])
        out[i]['xyz'] = list(xyz)
    # 兜底（理论上不会发生）
    for i in range(n):
        if out[i] is None:
            out[i] = seg[i]
    return out


# ─── 交互式 HTML 模板 ──────────────────────────────────────────────────
HTML_INTERACTIVE_TPL = r"""<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8">
<title>3D 轨迹回放</title>
<style>
body{margin:0;overflow:hidden;font-family:sans-serif;background:#111418}
#info{position:absolute;top:10px;left:10px;color:#fff;background:rgba(0,0,0,0.6);padding:8px 14px;border-radius:6px;font-size:14px;pointer-events:none;z-index:10}
#controls{position:absolute;bottom:20px;left:50%;transform:translateX(-50%);display:flex;gap:10px;align-items:center;z-index:10;background:rgba(0,0,0,0.5);padding:8px 16px;border-radius:8px}
#controls button{background:#444;color:#fff;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:14px;min-width:36px}
#controls input[type=range]{width:280px}
#controls span{color:#fff;font-size:13px;min-width:80px;text-align:center}
#legend{position:absolute;bottom:80px;right:20px;color:#ccc;font-size:12px;background:rgba(0,0,0,0.5);padding:8px 12px;border-radius:6px;z-index:10}
</style></head><body>
<div id="info">3D 轨迹回放 · 逐段播放 · 拖拽旋转 · 滚轮缩放</div>
<div id="controls">
  <button id="btnPlay">⏸</button>
  <input type="range" id="timeline" min="0" max="100" value="0" step="1">
  <span id="frameLabel">段 0 / 0: 0 / 0</span>
</div>
<div id="legend">● 检测点  ● 插值点  ◆ 击球点  ━ 网带</div>
<div id="errbox" style="position:absolute;top:0;left:0;right:0;z-index:999;background:#c22;color:#fff;padding:10px 16px;font-size:14px;font-family:monospace;display:none"></div>
<script type="importmap">
{"imports":{"three":"https://unpkg.com/three@0.170.0/build/three.module.js","three/addons/":"https://unpkg.com/three@0.170.0/examples/jsm/"}}
</script>
<script>
window.addEventListener('error', function(e) {
    var el = document.getElementById('errbox');
    if (el) { el.style.display='block'; el.textContent += '[ERR] ' + (e.message||e.error||'unknown') + '\n'; }
});
window.addEventListener('unhandledrejection', function(e) {
    var el = document.getElementById('errbox');
    if (el) { el.style.display='block'; el.textContent += '[PROMISE] ' + (e.reason||'unknown') + '\n'; }
});
</script>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
// ===== 轨迹数据 =====
const SEGMENTS = %SEGMENTS_JSON%;
const NET = %NET_JSON%;
const HITS = %HITS_JSON%;
const TOTAL_FRAMES_PTS = %TOTAL_FRAMES_PTS%;
const GLB_B64 = "";
const SUBSAMPLE = %SUBSAMPLE%;

// ===== Scene =====
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x111418);

const camera = new THREE.PerspectiveCamera(45, window.innerWidth/window.innerHeight, 0.1, 100);
camera.position.set(0, 8, 2);
camera.lookAt(0, 0.8, -2);

const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
document.body.appendChild(renderer.domElement);

// ===== Lights =====
const ambient = new THREE.AmbientLight(0x404050, 0.6);
scene.add(ambient);
const dirLight = new THREE.DirectionalLight(0xffffff, 1.2);
dirLight.position.set(5, 12, 8);
scene.add(dirLight);
const fillLight = new THREE.DirectionalLight(0x8888ff, 0.4);
fillLight.position.set(-5, 6, -8);
scene.add(fillLight);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0.8, -2);
controls.update();

// ===== Procedural Court =====
function buildFallbackCourt() {
    const groundGeo = new THREE.PlaneGeometry(14.0, 14.0);
    const groundMat = new THREE.MeshBasicMaterial({color:0x2d6b2d, side:THREE.DoubleSide});
    const ground = new THREE.Mesh(groundGeo, groundMat);
    ground.rotation.x = -Math.PI/2;
    scene.add(ground);

    function addLine(x1,z1,x2,z2, w) {
        if (!w) w = 0.03;
        const dir = new THREE.Vector3(x2-x1, 0, z2-z1);
        const len = dir.length(); dir.normalize();
        const mid = new THREE.Vector3((x1+x2)/2, 0.005, (z1+z2)/2);
        const cyl = new THREE.Mesh(new THREE.CylinderGeometry(w, w, len, 4),
            new THREE.MeshBasicMaterial({color:0xffffff}));
        cyl.position.copy(mid);
        cyl.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0), dir);
        scene.add(cyl);
    }
    addLine(-3.05,-6.7,-3.05,6.7); addLine(3.05,-6.7,3.05,6.7);
    addLine(-3.05,-6.7,3.05,-6.7); addLine(-3.05,6.7,3.05,6.7);
    addLine(-2.6,-6.7,-2.6,6.7); addLine(2.6,-6.7,2.6,6.7);
    addLine(-3.05,-1.98,3.05,-1.98, 0.025);
    addLine(-3.05,1.98,3.05,1.98, 0.025);
    addLine(-3.05,-5.94,3.05,-5.94, 0.025);
    addLine(-3.05,5.94,3.05,5.94, 0.025);
    addLine(0,-6.7,0,-1.98, 0.025);
    addLine(0,1.98,0,6.7, 0.025);
    addLine(-3.05,0,3.05,0, 0.025);

    const nm = new THREE.Mesh(new THREE.PlaneGeometry(6.1,1.55),
        new THREE.MeshBasicMaterial({color:0x4488aa,transparent:true,opacity:0.15,side:THREE.DoubleSide}));
    nm.position.set(0,0.775,0); scene.add(nm);

    const postMat = new THREE.MeshBasicMaterial({color:0xffdd44});
    for (let x of [-3.05,3.05]) {
        const pm = new THREE.Mesh(new THREE.CylinderGeometry(0.04,0.05,1.55,8),postMat);
        pm.position.set(x,0.775,0); scene.add(pm);
    }
}
buildFallbackCourt();

scene.add(new THREE.ArrowHelper(new THREE.Vector3(1,0,0), new THREE.Vector3(0,0,0), 2, 0xff0000, 0.15, 0.2));
scene.add(new THREE.ArrowHelper(new THREE.Vector3(0,1,0), new THREE.Vector3(0,0,0), 2, 0x00ff00, 0.15, 0.2));
scene.add(new THREE.ArrowHelper(new THREE.Vector3(0,0,1), new THREE.Vector3(0,0,0), 2, 0x0000ff, 0.15, 0.2));
const gridHelper = new THREE.GridHelper(14, 14, 0x444444, 0x222222);
gridHelper.position.y = 0.001;
scene.add(gridHelper);

// ===== Detected net band (red thick cylinder, Y=1.55m) =====
const netLineMat = new THREE.MeshBasicMaterial({color:0xff3333});
if (NET && NET.length >= 2) {
    const b1 = new THREE.Vector3(NET[0][0], NET[0][1], NET[0][2]);
    const b2 = new THREE.Vector3(NET[1][0], NET[1][1], NET[1][2]);
    const dir = new THREE.Vector3().copy(b2).sub(b1);
    const len = dir.length(); dir.normalize();
    const mid = new THREE.Vector3().copy(b1).add(b2).multiplyScalar(0.5);
    const cyl = new THREE.Mesh(new THREE.CylinderGeometry(0.015,0.015,len,6), netLineMat);
    cyl.position.copy(mid);
    cyl.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0), dir);
    scene.add(cyl);
}

// ===== Build per-segment data with hit markers =====
const segData = [];
SEGMENTS.forEach((seg, si) => {
    const pts = seg.map(p => ({pos: new THREE.Vector3(...p.xyz), conf: p.conf || 0.3, fn: p.fn || 0}));
    const segHits = [];
    HITS.forEach(h => {
        if (h.seg_idx === si) {
            segHits.push({frame: h.frame, pos: new THREE.Vector3(...h.xyz), isFoul: h.is_foul || false});
        }
    });
    segData.push({points: pts, hits: segHits});
});
if (!segData.some(s => s.hits.length > 0)) {
    HITS.forEach(h => {
        const hPos = new THREE.Vector3(...h.xyz);
        let bestSeg = 0, bestDist = Infinity;
        segData.forEach((s, si) => {
            if (s.points.length === 0) return;
            const dist = s.points[Math.floor(s.points.length/2)].pos.distanceTo(hPos);
            if (dist < bestDist) { bestDist = dist; bestSeg = si; }
        });
        segData[bestSeg].hits.push({frame: h.frame || 0, pos: hPos, isFoul: h.is_foul || false});
    });
}

// ===== Animation state =====
let currentSeg = 0;
let currentPt = 0;
let playing = true;
let segComplete = false;
let interSegTimer = 0;

const tlInput = document.getElementById('timeline');
const frameLabel = document.getElementById('frameLabel');
const btnPlay = document.getElementById('btnPlay');

const segCumul = [0];
segData.forEach(s => segCumul.push(segCumul[segCumul.length-1] + s.points.length));
const maxFrame = segCumul[segCumul.length-1] - 1;
tlInput.max = Math.max(maxFrame, 1);

let trailGroup = new THREE.Group();
trailGroup.userData.isTrail = true;
scene.add(trailGroup);

let hitMarkers = [];

function clearHitMarkers() {
    hitMarkers.forEach(m => scene.remove(m));
    hitMarkers = [];
}

function addHitMarker(pos, isFoul) {
    const sz = 0.06;
    const radius = 0.008;
    const color = isFoul ? 0xff2222 : 0xffffff;
    const group = new THREE.Group();
    const mat = new THREE.MeshBasicMaterial({color: color});
    for (let arm of [
        [new THREE.Vector3(-sz, -sz, sz), new THREE.Vector3(sz, sz, -sz)],
        [new THREE.Vector3(sz, -sz, sz), new THREE.Vector3(-sz, sz, -sz)]
    ]) {
        const a = arm[0], b = arm[1];
        const dir = new THREE.Vector3().copy(b).sub(a);
        const len = dir.length();
        dir.normalize();
        const mid = new THREE.Vector3().copy(a).add(b).multiplyScalar(0.5).add(pos);
        const cyl = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, len, 6), mat);
        cyl.position.copy(mid);
        cyl.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0), dir);
        group.add(cyl);
    }
    scene.add(group);
    hitMarkers.push(group);
    return group;
}

function updateScene() {
    scene.remove(trailGroup);
    const visiblePts = [];
    if (currentSeg < segData.length) {
        const seg = segData[currentSeg];
        for (let pi = 0; pi <= currentPt; pi++) {
            visiblePts.push({pos: seg.points[pi].pos, conf: seg.points[pi].conf, segIdx: currentSeg});
        }
    }
    if (interSegTimer > 0) {
        clearHitMarkers();
        const totalSegs = segData.length;
        frameLabel.textContent = `段 ${currentSeg+1}/${totalSegs} 完成 · 等待中... (${(interSegTimer/30).toFixed(1)}s)`;
        tlInput.value = (currentSeg + 1 < segData.length) ? segCumul[currentSeg + 1] : segCumul[segCumul.length - 1];
        return;
    }
    if (visiblePts.length > 0) {
        const TRAIL_LEN = 80;
        const trail = visiblePts.slice(-TRAIL_LEN);
        const positions = new Float32Array(trail.length * 3);
        trail.forEach((pt, i) => {
            positions[i*3] = pt.pos.x;
            positions[i*3+1] = pt.pos.y;
            positions[i*3+2] = pt.pos.z;
        });
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
        const segColors = [0xff8844, 0x44ff88, 0x4488ff, 0xff44ff, 0xffff44, 0x44ffff];
        const c = segColors[currentSeg % segColors.length];
        const mat = new THREE.PointsMaterial({
            color: c, size: 0.07, transparent: true, opacity: 0.85,
            blending: THREE.AdditiveBlending, depthWrite: false
        });
        trailGroup = new THREE.Points(geo, mat);
        trailGroup.userData.isTrail = true;
        scene.add(trailGroup);
        if (trail.length >= 2) {
            const linePts = trail.map(pt => pt.pos);
            const lineGeo = new THREE.BufferGeometry().setFromPoints(linePts);
            const lineMat = new THREE.LineBasicMaterial({color: c, transparent: true, opacity: 0.2});
            const line = new THREE.Line(lineGeo, lineMat);
            line.userData.isTrail = true;
            trailGroup.add(line);
        }
    }
    clearHitMarkers();
    if (currentSeg < segData.length) {
        const seg = segData[currentSeg];
        const curFn = (currentPt >= 0 && currentPt < seg.points.length) ? seg.points[currentPt].fn : 0;
        seg.hits.forEach(h => {
            if (curFn >= h.frame) {
                addHitMarker(h.pos, h.isFoul);
            }
        });
    }
    const totalSegs = segData.length;
    const totalPtsInSeg = currentSeg < segData.length ? segData[currentSeg].points.length : 0;
    frameLabel.textContent = `段 ${currentSeg+1}/${totalSegs}: ${currentPt+1}/${totalPtsInSeg}`;
    let cumulFrame = 0;
    for (let si = 0; si < currentSeg; si++) {
        cumulFrame += segData[si].points.length;
    }
    cumulFrame += currentPt;
    tlInput.value = cumulFrame;
}

tlInput.addEventListener('input', () => {
    let target = parseInt(tlInput.value);
    let cum = 0;
    for (let si = 0; si < segData.length; si++) {
        const segLen = segData[si].points.length;
        if (target < cum + segLen) {
            currentSeg = si;
            currentPt = target - cum;
            break;
        }
        cum += segLen;
        if (si === segData.length - 1) {
            currentSeg = si;
            currentPt = segLen - 1;
        }
    }
    updateScene();
});

btnPlay.addEventListener('click', () => {
    playing = !playing;
    btnPlay.textContent = playing ? '⏸' : '▶';
});

let lastTime = 0;
const FRAME_INTERVAL = 30;

function animate(time) {
    requestAnimationFrame(animate);
    if (playing) {
        if (time - lastTime > FRAME_INTERVAL) {
            lastTime = time;
            if (currentSeg >= segData.length) {
                playing = false;
                btnPlay.textContent = '▶';
            } else {
                const seg = segData[currentSeg];
                if (currentPt < seg.points.length - 1) {
                    currentPt++;
                    updateScene();
                } else {
                    if (interSegTimer === 0) {
                        scene.remove(trailGroup);
                        clearHitMarkers();
                    }
                    interSegTimer++;
                    if (interSegTimer >= 67) {
                        interSegTimer = 0;
                        currentSeg++;
                        currentPt = 0;
                        if (currentSeg < segData.length) {
                            updateScene();
                        } else {
                            playing = false;
                            btnPlay.textContent = '▶';
                            updateScene();
                        }
                    }
                }
            }
        }
    }
    controls.update();
    renderer.render(scene, camera);
}

if (segData.length > 0) {
    updateScene();
    animate(0);
}

window.addEventListener('resize', () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
});

</script>
</body></html>"""


def generate_interactive_html(segments_data, net_data, hits_data, total_frames, out_path, subsample=4):
    """
    生成独立的交互式 3D HTML 查看器
    segments_data: [ [{"xyz":[x,y,z], "conf":f, "frame":n}, ...], ... ]
    net_data: [[x,y,z], [x,y,z]]
    hits_data: [{"frame":n, "xyz":[x,y,z], "is_foul":bool}, ...]
    """
    # 降采样
    segs_sampled = [[p for i, p in enumerate(seg) if i % subsample == 0 or p.get("conf", 0.3) >= 0.8]
                    for seg in segments_data]
    segs_sampled = [s for s in segs_sampled if len(s) >= 2]

    segs_json = json.dumps([[{
        "xyz": [round(float(p["xyz"][0]), 4), round(float(p["xyz"][1]), 4), round(float(p["xyz"][2]), 4)],
        "conf": float(p.get("conf", 0.3)),
        "fn": int(p["frame"]),
    } for p in seg] for seg in segs_sampled])
    net_json = json.dumps([[float(v) for v in ep] for ep in net_data])

    # Build frame→seg_idx lookup against FULL data
    frame_to_seg = {}
    for si, seg in enumerate(segments_data):
        for p in seg:
            frame_to_seg[int(p['frame'])] = si

    hits_json = json.dumps([{
        "seg_idx": frame_to_seg.get(int(h["frame"]), -1),
        "frame": int(h["frame"]),
        "xyz": [round(float(h["xyz"][0]), 4), round(float(h["xyz"][1]), 4), round(float(h["xyz"][2]), 4)],
        "is_foul": bool(h.get("is_foul", False)),
    } for h in hits_data])

    total_pts = sum(len(s) for s in segs_sampled)

    html = HTML_INTERACTIVE_TPL \
        .replace("%SEGMENTS_JSON%", segs_json) \
        .replace("%NET_JSON%", net_json) \
        .replace("%HITS_JSON%", hits_json) \
        .replace("%TOTAL_FRAMES_PTS%", str(total_pts)) \
        .replace("%SUBSAMPLE%", str(subsample))

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  Interactive: {out_path}")


# ─── main ──────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print("Usage: python render_3d_smooth.py <proc_traj.json>")
        sys.exit(1)

    input_path = sys.argv[1]
    if not os.path.exists(input_path):
        print(f"ERROR: input not found: {input_path}")
        sys.exit(1)

    with open(input_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    frames = data.get('frames', [])
    hit_points = data.get('hit_points', [])
    nb = data.get('net_band', {})

    if not frames:
        print("ERROR: no frames")
        sys.exit(1)

    print(f"[Phase 1] 加载 {len(frames)} 个 3D 轨迹点，{len(hit_points)} 个击球候选")

    # ── 按 seg_id 分组 ──
    seg_map = {}
    for f in frames:
        sid = f.get('seg_id', 0)
        seg_map.setdefault(sid, []).append(f)
    seg_ids = sorted(seg_map)

    # 击球点帧集合（高斯平滑锚点）
    hit_frame_set = {h['frame'] for h in hit_points}

    # ── 高斯平滑（每段 x/y/z 各自平滑，击球点锚点不动）──
    segments_3d = []
    for sid in seg_ids:
        seg = seg_map[sid]
        if len(seg) < 2:
            continue
        seg_smooth = smooth_segment_3d(seg, hit_frame_set, SIGMA)
        segments_3d.append(seg_smooth)

    print(f"[Phase 2] 高斯平滑完成 (sigma={SIGMA})：{len(segments_3d)} 段")

    # 网带 3D 端点
    half_width = float(nb.get('half_width', 3.05)) if nb else 3.05
    net_z = float(nb.get('z', 0.0)) if nb else 0.0
    net_h = float(nb.get('height', 1.55)) if nb else 1.55
    net_3d = [[-half_width, net_h, net_z], [half_width, net_h, net_z]]

    # ── 生成交互式 HTML ──
    out_dir = os.path.dirname(os.path.abspath(input_path))
    prefix = os.path.splitext(os.path.basename(input_path))[0]
    html_path = os.path.join(out_dir, "smooth3d__" + prefix + ".html")

    generate_interactive_html(
        segments_3d, net_3d, hit_points, len(frames), html_path,
        subsample=SUBSAMPLE_HTML)

    print(f"\n[Done] HTML: {html_path}")


if __name__ == "__main__":
    main()
