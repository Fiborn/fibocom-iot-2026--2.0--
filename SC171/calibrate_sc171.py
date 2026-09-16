#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calibrate_sc171.py  -  Web-based stereo calibration tool

Usage: python3 calibrate_sc171.py
       Browser: http://<sc171-ip>:5001
"""

import numpy as np
import cv2
import time
import threading
from pathlib import Path
from flask import Flask, Response, jsonify, render_template_string

# ============================================================
# Camera: 2x Sunplus 1bcf:28c4
# Left  (cam0) : /dev/video2, Bus 008 xhci-hcd
# Right (cam1) : /dev/video4, Bus 006 renesas xhci (separate USB controller)
CAM_LEFT = 2
CAM_RIGHT = 4
FRAME_W, FRAME_H = 1280, 720
CHESS_SIZE = (9, 6)
SQUARE_MM = 24
MIN_PAIRS = 30
MIN_DIVERSITY = 0.15

OUTPUT_DIR = Path("/home/fibo/Project_v1/data/calibration")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
(OUTPUT_DIR / "left").mkdir(exist_ok=True)
(OUTPUT_DIR / "right").mkdir(exist_ok=True)
(OUTPUT_DIR / "preview").mkdir(exist_ok=True)

criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
captured_poses = []

# ============================================================
# Global cache: raw frames (for capture detection) + JPEG (for web display)
# ============================================================
latest_raw_l = None   # BGR numpy array
latest_raw_r = None
latest_jpg_l = None   # JPEG bytes
latest_jpg_r = None
_data_lock = threading.Lock()

def cam_loop(idx, side):
    """continuously read frames from one camera, update cache."""
    global latest_raw_l, latest_raw_r, latest_jpg_l, latest_jpg_r
    cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
    if not cap.isOpened():
        print("  [CAM%d] FAILED to open" % idx)
        return
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
    print("  [CAM%d] opened" % idx)
    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue
        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        with _data_lock:
            if side == 'left':
                latest_raw_l = frame
                latest_jpg_l = buf.tobytes()
            else:
                latest_raw_r = frame
                latest_jpg_r = buf.tobytes()

# ============================================================
def detect_chessboard(gray):
    ret, corners = cv2.findChessboardCorners(gray, CHESS_SIZE, None)
    if ret:
        corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
    return ret, corners

def check_diversity(corners_l, corners_r):
    if not captured_poses:
        return True
    pts_l = corners_l.reshape(-1, 2)
    tl, br = pts_l.min(axis=0), pts_l.max(axis=0)
    cx = (tl[0]+br[0])/2/FRAME_W
    cy = (tl[1]+br[1])/2/FRAME_H
    area = (br[0]-tl[0])*(br[1]-tl[1])/(FRAME_W*FRAME_H)
    for pcx, pcy, parea in captured_poses:
        if abs(cx-pcx)<MIN_DIVERSITY and abs(cy-pcy)<MIN_DIVERSITY and abs(area-parea)/max(parea,0.001)<MIN_DIVERSITY:
            return False
    return True

def do_capture():
    """detect + save from cached frames (no camera conflict)."""
    with _data_lock:
        fl = latest_raw_l.copy() if latest_raw_l is not None else None
        fr = latest_raw_r.copy() if latest_raw_r is not None else None

    if fl is None or fr is None:
        return {"ok": False, "err": "no frame yet, wait..."}

    gl = cv2.cvtColor(fl, cv2.COLOR_BGR2GRAY)
    gr = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
    dl, cl = detect_chessboard(gl)
    dr, cr = detect_chessboard(gr)

    if not dl or not dr:
        return {"ok": False, "err": "detect fail L=%s R=%s" % (dl, dr)}

    if not check_diversity(cl, cr):
        return {"ok": False, "err": "pose repeated, move board"}

    n = len(captured_poses) + 1
    cv2.imwrite(str(OUTPUT_DIR / "left" / "left_%04d.png" % n), fl)
    cv2.imwrite(str(OUTPUT_DIR / "right" / "right_%04d.png" % n), fr)

    preview = cv2.drawChessboardCorners(fl.copy(), CHESS_SIZE, cl, True)
    cv2.putText(preview, "#%d" % n, (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
    cv2.imwrite(str(OUTPUT_DIR / "preview" / "preview_%04d.png" % n), preview)

    pts_l = cl.reshape(-1, 2)
    tl, br = pts_l.min(axis=0), pts_l.max(axis=0)
    captured_poses.append((
        (tl[0]+br[0])/2/FRAME_W,
        (tl[1]+br[1])/2/FRAME_H,
        (br[0]-tl[0])*(br[1]-tl[1])/(FRAME_W*FRAME_H)
    ))
    return {"ok": True, "n": n, "total": MIN_PAIRS}

# ============================================================
def run_calibration():
    left_paths = sorted((OUTPUT_DIR / "left").glob("*.png"))
    right_paths = sorted((OUTPUT_DIR / "right").glob("*.png"))
    n = len(left_paths)

    objp = np.zeros((CHESS_SIZE[0]*CHESS_SIZE[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0:CHESS_SIZE[0], 0:CHESS_SIZE[1]].T.reshape(-1, 2)
    objp *= (SQUARE_MM / 1000.0)

    objpoints, ipl, ipr = [], [], []
    img_size = None
    for pl, pr in zip(left_paths, right_paths):
        gl = cv2.imread(str(pl), cv2.IMREAD_GRAYSCALE)
        gr = cv2.imread(str(pr), cv2.IMREAD_GRAYSCALE)
        if gl is None or gr is None:
            continue
        img_size = gl.shape[::-1]
        rl, cl = cv2.findChessboardCorners(gl, CHESS_SIZE, None)
        rr, cr = cv2.findChessboardCorners(gr, CHESS_SIZE, None)
        if not rl or not rr:
            continue
        cl = cv2.cornerSubPix(gl, cl, (11, 11), (-1, -1), criteria)
        cr = cv2.cornerSubPix(gr, cr, (11, 11), (-1, -1), criteria)
        objpoints.append(objp)
        ipl.append(cl)
        ipr.append(cr)

    if len(objpoints) < 5:
        return {"ok": False, "err": "only %d valid pairs" % len(objpoints)}

    re_l, K_l, D_l, _, _ = cv2.calibrateCamera(objpoints, ipl, img_size, None, None)
    re_r, K_r, D_r, _, _ = cv2.calibrateCamera(objpoints, ipr, img_size, None, None)

    ret, K_l, D_l, K_r, D_r, R, T, E, F = cv2.stereoCalibrate(
        objpoints, ipl, ipr, K_l, D_l, K_r, D_r, img_size,
        None, None, None, None, flags=cv2.CALIB_FIX_INTRINSIC)

    R_l, R_r, P_l, P_r, Q, _, _ = cv2.stereoRectify(
        K_l, D_l, K_r, D_r, img_size, R, T, alpha=0.9)

    mlx, mly = cv2.initUndistortRectifyMap(K_l, D_l, R_l, P_l, img_size, cv2.CV_32FC1)
    mrx, mry = cv2.initUndistortRectifyMap(K_r, D_r, R_r, P_r, img_size, cv2.CV_32FC1)

    op = OUTPUT_DIR / "stereo_params.npz"
    np.savez(str(op),
             K_l=K_l, K_r=K_r,
             dist_l=D_l, dist_r=D_r,
             R=R, T=T, F=F,
             P_l=P_l, P_r=P_r,
             map_lx=mlx, map_ly=mly,
             map_rx=mrx, map_ry=mry)

    return {
        "ok": True, "n": n,
        "err_left": round(re_l, 4),
        "err_right": round(re_r, 4),
        "err_stereo": round(ret, 4),
        "baseline": round(np.linalg.norm(T), 4),
        "output": str(op)
    }

# ============================================================
app = Flask(__name__)

HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SC171 Stereo Calibration</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font:14px/1.5 system-ui,sans-serif;background:#1a1a2e;color:#eee;text-align:center}
h1{padding:12px;font-size:18px;background:#16213e}
.row{display:flex;justify-content:center;gap:10px;padding:10px;flex-wrap:wrap}
.box{background:#0f0f23;border:2px solid #333;border-radius:6px;width:330px}
.box h2{font-size:14px;padding:6px;background:#16213e}
.box img{width:320px;height:240px;display:block;margin:0 auto;background:#000}
.ctrl{padding:10px}
.btn{padding:10px 28px;margin:4px;font-size:16px;border:none;border-radius:6px;cursor:pointer;color:#fff}
.btn-cap{background:#00b894}
.btn-calib{background:#0984e3}
.btn:disabled{opacity:0.4;cursor:default}
.status{margin:8px;padding:8px;background:#0f0f23;border-radius:6px;min-height:36px;white-space:pre-line;font-size:13px}
.bar{height:6px;background:#333;border-radius:3px;margin:8px;overflow:hidden}
.bar-fill{height:100%;background:#00b894;transition:width .3s}
.kbd{font-size:12px;color:#888;margin-top:4px}
</style>
</head>
<body>
<h1>SC171 Stereo Calibration</h1>
<div class="row">
 <div class="box"><h2>Left (CAM 2)</h2><img id="imgL" src="/frame/left"></div>
 <div class="box"><h2>Right (CAM 4)</h2><img id="imgR" src="/frame/right"></div>
</div>
<div class="ctrl">
 <button class="btn btn-cap" id="btnCap" onclick="capture()">Capture</button>
 <button class="btn btn-calib" id="btnCalib" onclick="calibrate()">Calibrate ({{count}}/{{total}})</button>
</div>
<div class="bar"><div class="bar-fill" id="fill" style="width:{{pct}}%"></div></div>
<div class="status" id="status">Ready. Align chessboard in both cameras, then click Capture.
[Space]=Capture  [Q]=Calibrate</div>
<div class="kbd">Cameras open/close alternately (~3fps). Capture uses cached frames, no conflict.</div>
<script>
var count={{count}}, total={{total}};

function refresh(){
 var t=Date.now();
 document.getElementById('imgL').src='/frame/left?'+t;
 document.getElementById('imgR').src='/frame/right?'+t;
}
setInterval(refresh,400);

function updateStatus(msg, n){
 document.getElementById('status').textContent=msg;
 if(n!==undefined){
  count=n;
  var pct=Math.min(100,Math.round(count/total*100));
  document.getElementById('fill').style.width=pct+'%';
  document.getElementById('btnCalib').textContent='Calibrate ('+count+'/'+total+')';
 }
}

async function capture(){
 var btn=document.getElementById('btnCap');
 btn.disabled=true; btn.textContent='...';
 try{
  var r=await fetch('/capture');
  var j=await r.json();
  if(j.ok) updateStatus('[OK] Saved '+j.n+'/'+total, j.n);
  else updateStatus('[FAIL] '+j.err);
 }catch(e){updateStatus('[FAIL] '+e);}
 btn.disabled=false; btn.textContent='Capture';
}

async function calibrate(){
 var btn=document.getElementById('btnCalib');
 var status=document.getElementById('status');
 btn.disabled=true; status.textContent='Calibrating...';
 try{
  var r=await fetch('/calibrate');
  var j=await r.json();
  if(j.ok){
   status.innerHTML='[OK] Calibration done!<br>'
    +'Left: '+j.err_left+'px | Right: '+j.err_right+'px | Stereo: '+j.err_stereo+'px<br>'
    +'Baseline: '+j.baseline+'m | File: '+j.output;
  }else{
   status.textContent='[FAIL] '+j.err;
  }
 }catch(e){status.textContent='[FAIL] '+e;}
 btn.disabled=false;
}

document.onkeydown=function(e){
 if(e.key==' '){e.preventDefault();capture();}
 if(e.key=='q'||e.key=='Q'){e.preventDefault();calibrate();}
};
</script>
</body>
</html>"""

@app.route('/')
def index():
    return render_template_string(HTML,
        count=len(captured_poses),
        total=MIN_PAIRS,
        pct=min(100, len(captured_poses)*100//MIN_PAIRS))

@app.route('/frame/left')
def frame_left():
    with _data_lock:
        data = latest_jpg_l or b''
    return Response(data, mimetype='image/jpeg')

@app.route('/frame/right')
def frame_right():
    with _data_lock:
        data = latest_jpg_r or b''
    return Response(data, mimetype='image/jpeg')

@app.route('/capture')
def api_capture():
    return jsonify(do_capture())

@app.route('/calibrate')
def api_calibrate():
    return jsonify(run_calibration())

# ============================================================
if __name__ == '__main__':
    print("=" * 60)
    print("  SC171 Web Calibration Tool")
    print("=" * 60)
    print("  URL: http://<sc171-ip>:5001")
    print("  Camera: Sunplus 1bcf:28c4 x2")
    print("  Resolution: %dx%d" % (FRAME_W, FRAME_H))
    print("  Board: %dx%d, %dmm squares" % (CHESS_SIZE[0], CHESS_SIZE[1], SQUARE_MM))
    print("  Target: %d pairs" % MIN_PAIRS)
    print()

    threading.Thread(target=cam_loop, args=(CAM_LEFT, 'left'), daemon=True).start()
    threading.Thread(target=cam_loop, args=(CAM_RIGHT, 'right'), daemon=True).start()
    time.sleep(2)

    app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)
