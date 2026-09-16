#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calib_netpos.py — Web-based net band alignment tool

浏览器实时查看 SC171 摄像头画面，画面中央标注竖线（x=640），
方便物理调整网带位置使其对准画面中心。

用法:
  SC171 端:  python3 calib_netpos.py
  PC 浏览器: http://<sc171-ip>:5004
"""

import os, sys
import subprocess
import time
import threading
from pathlib import Path
from typing import List

import cv2
import numpy as np
from flask import Flask, Response, render_template_string


# ============================================================
# 自动探测 USB 摄像头
# ============================================================
def probe_cameras(max_idx: int = 9) -> List[int]:
    """扫描 /dev/video0~max_idx，返回可用摄像头编号列表"""
    cams = []
    for idx in range(max_idx + 1):
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            ret, _ = cap.read()
            cap.release()
            if ret:
                cams.append(idx)
                print(f"  [PROBE] /dev/video{idx} ✓")
            else:
                print(f"  [PROBE] /dev/video{idx} 可打开但无帧")
        # else: skip
    return cams


# ============================================================
# 配置
# ============================================================
FRAME_W = 1280
FRAME_H = 720
CENTER_X = FRAME_W // 2   # 640
PORT = 5004

# ============================================================
# 帧缓存
# ============================================================
latest_jpg = None
_data_lock = threading.Lock()


def cam_loop(idx: int):
    """持续从摄像头读取帧，叠加中央竖线后存入缓存"""
    global latest_jpg

    cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
    if not cap.isOpened():
        print(f"  [CAM] /dev/video{idx} 打开失败")
        return

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
    actual_w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    actual_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    print(f"  [CAM] /dev/video{idx} {actual_w:.0f}x{actual_h:.0f}")

    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue

        # ── 叠加中央竖线 ──
        # 主线: 绿色虚线 (x=640)
        cx = actual_w / 2 if actual_w != FRAME_W else CENTER_X
        h = frame.shape[0]
        for y in range(0, h, 8):
            y1 = min(y + 3, h - 1)
            cv2.line(frame, (int(cx), y), (int(cx), y1), (0, 255, 0), 2)

        # 辅助线: 左右 ±10px 范围，红色细线（精确对齐参考）
        for dx in (-10, 10):
            xx = int(cx + dx)
            cv2.line(frame, (xx, 0), (xx, h - 1), (0, 0, 255), 1)

        # 顶部标注
        cv2.putText(frame, f"CENTER x={int(cx)}", (int(cx) - 80, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.putText(frame, "align net here", (int(cx) - 90, 52),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

        # 十字准星
        cv2.line(frame, (int(cx) - 30, h // 2), (int(cx) + 30, h // 2),
                 (0, 255, 0), 1)
        cv2.line(frame, (int(cx), h // 2 - 30), (int(cx), h // 2 + 30),
                 (0, 255, 0), 1)

        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        with _data_lock:
            latest_jpg = buf.tobytes()


# ============================================================
# Flask Web
# ============================================================
app = Flask(__name__)

HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Net Band Alignment</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font:14px/1.5 system-ui,sans-serif;background:#1a1a2e;color:#eee;text-align:center}
h1{padding:10px;font-size:16px;background:#16213e}
.img-wrap{display:inline-block;position:relative;margin:10px}
.img-wrap img{max-width:96vw;max-height:80vh;display:block;border:2px solid #333;border-radius:4px}
.info{padding:8px;font-size:13px;color:#888}
.info span{color:#00b894;font-weight:bold}
</style>
</head>
<body>
<h1>SC171 Net Band Alignment</h1>
<div class="img-wrap"><img id="feed" src="/frame"></div>
<div class="info">
  中央 <span>绿色竖线</span> = 画面中心 x={{center_x}}<br>
  红色辅助线 = 中心 ±10px 容差参考<br>
  物理移动网带使其与绿色竖线重合 → 即完成对齐
</div>
<script>
function refresh(){
 document.getElementById('feed').src='/frame?'+Date.now();
}
setInterval(refresh, 250);  // ~4fps MJPEG
</script>
</body>
</html>"""


@app.route('/')
def index():
    return render_template_string(HTML, center_x=CENTER_X)


@app.route('/frame')
def frame():
    with _data_lock:
        data = latest_jpg
    return Response(data or b'', mimetype='image/jpeg')


# ============================================================
if __name__ == '__main__':
    print("=" * 55)
    print("  SC171 Net Band Alignment Tool")
    print("=" * 55)

    # ── 自动探测摄像头 ──
    print("\n[PROBE] 扫描 USB 摄像头...")
    cams = probe_cameras()

    # ── 选择摄像头 ──
    cam_idx = int(sys.argv[1]) if len(sys.argv) > 1 else None
    if cam_idx is not None:
        if cam_idx not in cams:
            print(f"  [ERR] /dev/video{cam_idx} 不可用，可用: {cams}")
            sys.exit(1)
    elif cams:
        cam_idx = cams[0]  # 默认取第一个
    else:
        print("  [ERR] 未检测到可用摄像头")
        sys.exit(1)

    print(f"\n  Camera: /dev/video{cam_idx}")
    print(f"  Resolution: {FRAME_W}x{FRAME_H}")
    print(f"  Center line: x={CENTER_X}")
    print(f"  Web: http://<sc171-ip>:{PORT}")
    print()

    threading.Thread(target=cam_loop, args=(cam_idx,), daemon=True).start()
    time.sleep(2)  # 等摄像头就绪

    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
