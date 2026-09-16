#!/usr/bin/env python3
"""
pc_test.py — 阿里云 3D 轨迹处理服务器

接收 SC171 上传的三角化 3D 轨迹（stereo_3d），完成：
  1. 过滤轨迹（analysis.process_trajectory，3D 去噪 + Z 轴过网击球检测）
  2. 渲染 2D 俯视图动画（render_video_v2_Gausmo.py，取 X/Z 坐标）
  3. 提取 JPEG 帧 → 供 ESP32-P4 HTTP 拉取
  4. 生成 3D 交互式 HTML（render_3d_smooth.py，高斯平滑）→ 网页端访问

轨迹坐标约定（SC171 三角化输出）：
  - xOz 平面 = 球场地面（x 横向、z 纵向/深度）
  - y 方向 = 网柱方向（竖直高度）
  - 球网位于 z=0 平面

启动: python pc_test.py [--port 5000]
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request, send_from_directory

# ── 路径 ──
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
AUDIENCE_DIR = BASE_DIR / "audience"
REFEREE_DIR = BASE_DIR / "referee"
P4_FRAME_ROOT = BASE_DIR / "p4_frames"
TEMPLATE_DIR = BASE_DIR / "templates"
HTML3D_DIR = BASE_DIR / "html3d"

# ── 渲染脚本（同目录）──
RENDER_2D_SCRIPT = BASE_DIR / "render_video_v2_Gausmo.py"
RENDER_3D_SCRIPT = BASE_DIR / "render_3d_smooth.py"

sys.path.insert(0, str(BASE_DIR))
import analysis

for d in (UPLOAD_DIR, AUDIENCE_DIR, REFEREE_DIR, P4_FRAME_ROOT, TEMPLATE_DIR, HTML3D_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── 配置 ──
PORT = 5000
P4_FRAME_FPS = 20
P4_MAX_FRAMES = 600
P4_MAX_TOTAL_JPEG_BYTES = 48 * 1024 * 1024

if "--port" in sys.argv:
    idx = sys.argv.index("--port")
    PORT = int(sys.argv[idx + 1])

RUNNING = True

# 状态
_state_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "current_task_id": None,
    "last_upload": None,
}


def _console(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def _new_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{uuid.uuid4().hex[:8]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ================================================================
# Step 1: 过滤轨迹（analysis.process_trajectory）
# ================================================================
def _process_and_save(raw_data: dict, out_path: Path) -> Path | None:
    """调用 analysis.process_trajectory，保存处理后 JSON。"""
    try:
        processed = analysis.process_trajectory(raw_data)
        if processed.get('error'):
            _console(f"[PROC] 处理失败: {processed['error']}")
            return None
        _write_json_atomic(out_path, processed)
        n_foul = sum(1 for h in processed.get('hit_points', []) if h.get('is_foul'))
        _console(f"[PROC] {out_path.name}: {processed['filtered_count']}pts "
                 f"{len(processed['hit_points'])}hits ({n_foul}过网)")
        return out_path
    except Exception as e:
        _console(f"[PROC] 异常: {e}")
        return None


# ================================================================
# Step 2: 渲染 2D 俯视图动画
# ================================================================
def _render_2d(proc_path: Path, out_mp4: Path) -> bool:
    """调用 render_video_v2_Gausmo.py 渲染 2D 俯视图 MP4。"""
    if not RENDER_2D_SCRIPT.is_file():
        _console(f"[2D] 脚本不存在: {RENDER_2D_SCRIPT}")
        return False
    try:
        result = subprocess.run(
            [sys.executable, str(RENDER_2D_SCRIPT),
             str(proc_path), "--output", str(out_mp4)],
            check=False, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            tail = "\n".join(result.stderr.splitlines()[-5:])
            _console(f"[2D] 失败 (rc={result.returncode}): {tail}")
            return False
        if out_mp4.is_file() and out_mp4.stat().st_size > 1024:
            _console(f"[2D] 完成: {out_mp4.name} ({out_mp4.stat().st_size / 1024:.0f} KB)")
            return True
        _console("[2D] 输出文件缺失或过小")
        return False
    except Exception as e:
        _console(f"[2D] 异常: {e}")
        return False


# ================================================================
# Step 3: 提取 JPEG 帧给 P4
# ================================================================
def _extract_frames(video_path: Path) -> tuple[str, int]:
    """从 MP4 提取 JPEG 帧存入 P4_FRAME_ROOT/generation/。"""
    generation = _frame_generation()
    frame_dir = P4_FRAME_ROOT / generation
    frame_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")

    total_fps = cap.get(cv2.CAP_PROP_FPS)
    step = max(1, round(total_fps / P4_FRAME_FPS))
    count = 0
    total_jpeg_bytes = 0

    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if idx % step == 0:
            small = cv2.resize(frame, (640, 480), interpolation=cv2.INTER_AREA)
            _, jpeg_buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
            jpeg_bytes = jpeg_buf.tobytes()
            if total_jpeg_bytes + len(jpeg_bytes) > P4_MAX_TOTAL_JPEG_BYTES:
                break
            fp = frame_dir / f"frame_{count + 1:05d}.jpg"
            fp.write_bytes(jpeg_bytes)
            total_jpeg_bytes += len(jpeg_bytes)
            count += 1
            if count >= P4_MAX_FRAMES:
                break
        idx += 1
    cap.release()

    manifest = {
        "protocol": 1,
        "generation": generation,
        "format": "jpeg",
        "count": count,
        "width": 640,
        "height": 480,
        "delay_ms": 1000 // P4_FRAME_FPS,
    }
    (frame_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    _console(f"[FRAMES] {count} 帧 generation={generation[:12]}...")
    return generation, count


def _frame_generation() -> str:
    import hashlib
    h = hashlib.sha256(f"pc_test_{time.time()}_{uuid.uuid4().hex[:8]}".encode())
    return h.hexdigest()


# ================================================================
# Step 4: 生成 3D 交互式 HTML
# ================================================================
def _render_3d_html(proc_path: Path) -> Path | None:
    """调用 render_3d_smooth.py 生成交互式 3D HTML。"""
    if not RENDER_3D_SCRIPT.is_file():
        _console(f"[3D] 脚本不存在: {RENDER_3D_SCRIPT}")
        return None

    try:
        _console("[3D] 调用 render_3d_smooth.py ...")
        result = subprocess.run(
            [sys.executable, str(RENDER_3D_SCRIPT), str(proc_path)],
            capture_output=True, text=True, timeout=300,
        )
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line and any(kw in line for kw in ("Phase", "Done", "HTML", "ERROR", "Interactive")):
                _console(f"[3D] {line}")
        if result.returncode != 0:
            _console(f"[3D] 退出码={result.returncode}")
            for line in result.stderr.split("\n")[-5:]:
                if line.strip():
                    _console(f"[3D] stderr: {line.strip()}")

        out_dir = proc_path.parent
        candidates = sorted(
            out_dir.glob("smooth3d__*.html"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            html_path = candidates[0]
            _console(f"[3D] HTML 生成成功: {html_path.name} "
                     f"({html_path.stat().st_size / 1024:.0f} KB)")
            return html_path
        _console("[3D] 未找到生成的 HTML 文件")
        return None
    except subprocess.TimeoutExpired:
        _console("[3D] 渲染超时 (300s)")
        return None
    except Exception as e:
        _console(f"[3D] 渲染异常: {e}")
        return None


# ================================================================
# 处理流水线（单条 3D 轨迹）
# ================================================================
def process_3d_trajectory(stereo_3d: dict) -> None:
    """完整流水线：保存 → 过滤 → 2D 渲染 → 抽帧 → 3D HTML。"""
    task_id = _new_id()
    with _state_lock:
        _state["running"] = True
        _state["current_task_id"] = task_id

    try:
        # ── 1. 保存原始 3D 轨迹 ──
        raw_path = UPLOAD_DIR / f"traj3d_{task_id}.json"
        _write_json_atomic(raw_path, stereo_3d)
        n_pts = len(stereo_3d.get("frames", []))
        _console(f"[TASK {task_id}] 收到 3D 轨迹: {n_pts} 点")

        # ── 2. 过滤 ──
        proc_path = UPLOAD_DIR / f"proc3d_{task_id}.json"
        proc_path = _process_and_save(stereo_3d, proc_path)
        if proc_path is None:
            _console(f"[TASK {task_id}] 过滤失败，终止")
            return

        # ── 3. 2D 俯视图渲染 ──
        mp4_path = REFEREE_DIR / f"anim_{task_id}.mp4"
        if _render_2d(proc_path, mp4_path):
            # ── 4. 抽帧给 P4 ──
            try:
                _extract_frames(mp4_path)
            except Exception as e:
                _console(f"[TASK {task_id}] 抽帧失败: {e}")
        else:
            _console(f"[TASK {task_id}] 2D 渲染失败")

        # ── 5. 3D HTML ──
        html_path = _render_3d_html(proc_path)
        if html_path:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            local_html = HTML3D_DIR / f"traj3d_{stamp}.html"
            local_html.write_bytes(html_path.read_bytes())
            latest_html = HTML3D_DIR / "latest.html"
            latest_html.write_bytes(html_path.read_bytes())
            _console(f"[TASK {task_id}] 3D HTML: /media/html3d/{local_html.name}")
            _console(f"[TASK {task_id}] 稳定别名: /media/html3d/latest.html")

        _console(f"[TASK {task_id}] 流水线完成")
    except Exception as e:
        _console(f"[TASK {task_id}] 异常: {e}")
    finally:
        with _state_lock:
            _state["running"] = False
            _state["current_task_id"] = None


# ================================================================
# Flask App（上传 + P4 帧 + 3D HTML + 状态查询）
# ================================================================
app = Flask(__name__, template_folder=str(TEMPLATE_DIR))


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "pc_test_3d"})


@app.post("/api/upload")
def upload_json():
    """接收 SC171 上传的 payload {cam0, cam1, stereo_3d}，提取 stereo_3d 处理。"""
    if not request.is_json:
        return jsonify({"ok": False, "error": "Content-Type必须是application/json"}), 415
    try:
        data = request.get_json()
    except Exception:
        return jsonify({"ok": False, "error": "JSON 解析失败"}), 400

    stereo_3d = data.get("stereo_3d", {})
    if not isinstance(stereo_3d, dict) or not stereo_3d.get("frames"):
        return jsonify({"ok": False, "error": "缺少 stereo_3d 轨迹数据"}), 400

    with _state_lock:
        _state["last_upload"] = _utc_now()

    # 后台线程处理（不阻塞上传响应）
    threading.Thread(
        target=process_3d_trajectory,
        args=(stereo_3d,),
        daemon=True,
    ).start()

    return jsonify({
        "ok": True,
        "message": "已接收 3D 轨迹，开始处理",
        "points": len(stereo_3d.get("frames", [])),
    })


@app.get("/api/status")
def status():
    with _state_lock:
        snapshot = json.loads(json.dumps(_state))
    return jsonify({"ok": True, **snapshot})


# ── P4 帧端点 ──
@app.get("/v1/manifest")
def p4_manifest():
    manifest = _current_frames()
    if manifest is None:
        return jsonify({"ok": False, "error": "尚无帧"}), 409
    after = request.args.get("after_generation", "")
    if after and manifest.get("generation") == after:
        return jsonify({"ok": False, "error": "未更新"}), 409
    resp = jsonify(manifest)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/v1/frames/<int:index>.jpg")
def p4_frame(index: int):
    generation = request.args.get("generation", "")
    if len(generation) != 64 or not all(c in "0123456789abcdefABCDEF" for c in generation):
        return jsonify({"ok": False, "error": "generation无效"}), 400
    mp = P4_FRAME_ROOT / generation / "manifest.json"
    if not mp.is_file():
        return jsonify({"ok": False, "error": "generation不存在"}), 404
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
        count = int(manifest["count"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return jsonify({"ok": False, "error": "manifest损坏"}), 500
    if not 0 <= index < count:
        return jsonify({"ok": False, "error": "索引越界"}), 404
    fp = P4_FRAME_ROOT / generation / f"frame_{index + 1:05d}.jpg"
    if not fp.is_file():
        return jsonify({"ok": False, "error": "帧文件丢失"}), 500
    return Response(fp.read_bytes(), mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ── 媒体端点 ──
@app.get("/media/audience/<path:filename>")
def audience_video(filename: str):
    return send_from_directory(AUDIENCE_DIR, filename, conditional=True)


@app.get("/media/referee/<path:filename>")
def referee_video(filename: str):
    return send_from_directory(REFEREE_DIR, filename, conditional=True)


@app.get("/media/html3d/<path:filename>")
def html3d_viewer(filename: str):
    """网页端访问 3D 可交互式轨迹回放。"""
    return send_from_directory(HTML3D_DIR, filename, conditional=True,
                               mimetype="text/html")


@app.get("/api/public-status")
def public_status():
    return jsonify({"ok": True, **_current_status()})


# ── 帧缓存查询 ──
def _current_frames() -> dict[str, Any] | None:
    """按目录修改时间倒序返回最新的 generation manifest。"""
    if not P4_FRAME_ROOT.is_dir():
        return None
    dirs = [d for d in P4_FRAME_ROOT.iterdir() if d.is_dir()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs:
        if (d / "manifest.json").is_file():
            try:
                return json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    return None


def _current_status() -> dict[str, Any]:
    manifest = _current_frames()
    with _state_lock:
        current_id = _state["current_task_id"]
    return {"frames": manifest, "running": current_id is not None}


# ================================================================
# 主入口
# ================================================================
def _signal_handler(sig, frame):
    global RUNNING
    _console("收到退出信号")
    RUNNING = False


def main():
    signal.signal(signal.SIGINT, _signal_handler)

    _console("阿里云 3D 轨迹处理服务器")
    _console(f"  监听端口: {PORT}")
    _console(f"  上传端点: /api/upload (POST, stereo_3d)")
    _console(f"  P4 帧拉取: /v1/manifest + /v1/frames/<n>.jpg")
    _console(f"  3D HTML: /media/html3d/latest.html")
    _console(f"  工作目录: {UPLOAD_DIR}")

    app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
