#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sc171_pipeline_gamadj.py — SC171 GPU 双摄三角化版
GPU(SNPE/DLC) 双摄推理 → 红色灯时间对齐 → 立体三角化 → 上传阿里云。
轨迹通过 HTTP 直接上传到阿里云 (8.140.192.151)。

用法:
  python3 sc171_pipeline_gamadj.py
  # 需要先在同目录创建 badminton.env 配置阿里云上传地址与 API Key
"""

from __future__ import annotations

import ctypes
import fcntl
import glob
import json
import math
import mmap
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import socket
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, "/home/fibo/.local/lib/python3.8/site-packages")
import gpio_util

# ── GPU 推理（SNPE/DLC）──
try:
    from api_infer import SnpeContext, Runtime, PerfProfile, LogLevel
    HAS_GPU = True
except Exception:
    HAS_GPU = False
    SnpeContext = Runtime = PerfProfile = LogLevel = None

from utils import preprocess_letterbox, detect_postprocess

# ============================================================
# Signal
# ============================================================
RUNNING = True
def _signal_handler(sig, frame):
    global RUNNING
    print("\n[SHUTDOWN] Ctrl+C pressed, exiting...", flush=True)
    RUNNING = False
signal.signal(signal.SIGINT, _signal_handler)

# ============================================================
# WiFi 自动连接
# ============================================================
def _run_shell(cmd: str, timeout: float = 10) -> tuple[int, str, str]:
    """Run shell command, return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return -1, "", str(e)

def _find_wlan_iface() -> Optional[str]:
    """Find active WiFi interface name."""
    for name in ("wlan0", "wlan1"):
        rc, _, _ = _run_shell(f"ip link show {name} 2>/dev/null")
        if rc == 0:
            return name
    try:
        for d in Path("/sys/class/net").iterdir():
            if d.name.startswith("wlan") or d.name.startswith("wlp"):
                return d.name
    except Exception:
        pass
    return None

def _wlan_has_ip(iface: str) -> bool:
    """Check whether interface has an IPv4 address and is UP."""
    rc, out, _ = _run_shell(f"ip -4 addr show {iface} 2>/dev/null")
    if rc != 0:
        return False
    return "inet " in out and "state UP" in out

def _try_android_wifi(ssid: str, pwd: str) -> bool:
    """Android cmd wifi connect-network."""
    rc, _, err = _run_shell(f'cmd wifi connect-network "{ssid}" wpa2 "{pwd}" 2>&1')
    if rc == 0:
        return True
    # Also try svc
    _run_shell("svc wifi enable 2>/dev/null")
    time.sleep(1)
    rc2, _, _ = _run_shell(f'cmd wifi connect-network "{ssid}" wpa2 "{pwd}" 2>&1')
    return rc2 == 0

def _try_wpa_cli(ssid: str, pwd: str) -> bool:
    """Connect via wpa_cli (wpa_supplicant)."""
    rc, _, _ = _run_shell("which wpa_cli 2>/dev/null")
    if rc != 0:
        return False

    # Find interface
    for iface in ("wlan0", "wlan1"):
        rc, out, _ = _run_shell(f"wpa_cli -i {iface} status 2>/dev/null")
        if rc == 0 and "wpa_state" in out:
            break
    else:
        return False

    # Add network
    rc, out, _ = _run_shell(f"wpa_cli -i {iface} add_network 2>/dev/null")
    if rc != 0:
        return False
    net_id = out.strip()

    _run_shell(f'wpa_cli -i {iface} set_network {net_id} ssid \'"{ssid}"\' 2>/dev/null')
    _run_shell(f'wpa_cli -i {iface} set_network {net_id} psk \'"{pwd}"\' 2>/dev/null')
    _run_shell(f"wpa_cli -i {iface} enable_network {net_id} 2>/dev/null")
    _run_shell(f"wpa_cli -i {iface} select_network {net_id} 2>/dev/null")
    _run_shell(f"wpa_cli -i {iface} save_config 2>/dev/null")
    return True

def _try_nmcli(ssid: str, pwd: str) -> bool:
    """Connect via NetworkManager nmcli."""
    rc, _, _ = _run_shell("which nmcli 2>/dev/null")
    if rc != 0:
        return False
    # Turn WiFi on
    _run_shell("nmcli radio wifi on 2>/dev/null")
    time.sleep(0.5)
    rc, _, _ = _run_shell(f'nmcli dev wifi connect "{ssid}" password "{pwd}" 2>&1')
    return rc == 0

def ensure_wifi_connected(ssid: str = "#######", password: str = "###########",
                          timeout: float = 30) -> bool:
    """Ensure WiFi is connected. Auto-connect to specified network if not.

    Returns True if connected (or already was), False on failure.
    """
    print("[WIFI] Checking connection...", flush=True)

    iface = _find_wlan_iface()
    if iface and _wlan_has_ip(iface):
        current_ssid = "unknown"
        rc, out, _ = _run_shell(f"iwgetid {iface} --raw 2>/dev/null")
        if rc == 0 and out.strip():
            current_ssid = out.strip()
        print(f"[WIFI] Already connected to '{current_ssid}', skipping", flush=True)
        return True

    print(f"[WIFI] Not connected, searching for '{ssid}'...", flush=True)

    if not iface:
        _run_shell("ip link set wlan0 up 2>/dev/null")
        time.sleep(1)
        iface = _find_wlan_iface() or "wlan0"

    methods = [
        ("Android cmd", lambda: _try_android_wifi(ssid, password)),
        ("nmcli", lambda: _try_nmcli(ssid, password)),
        ("wpa_cli", lambda: _try_wpa_cli(ssid, password)),
    ]

    for name, method in methods:
        print(f"[WIFI] Trying {name}...", flush=True)
        try:
            if method():
                print(f"[WIFI] {name} connection initiated", flush=True)
                break
        except Exception as e:
            print(f"[WIFI] {name} error: {e}", flush=True)
    else:
        print("[WIFI] All connection methods failed", flush=True)
        return False

    print("[WIFI] Waiting for IP...", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _wlan_has_ip(iface):
            print(f"[WIFI] Connected! ({iface})", flush=True)
            return True
        time.sleep(1)

    print(f"[WIFI] Timeout waiting for IP on {iface}", flush=True)
    return False

# ============================================================
# 环境变量加载
# ============================================================
ENV_FILE = Path(__file__).resolve().parent / "badminton.env"

def load_env(path: Path) -> None:
    if not path.is_file():
        print(f"[ENV] 未找到配置文件：{path}，使用默认值", flush=True)
        return
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not key:
            continue
        os.environ[key] = value

def cfg(key: str, default: str) -> str:
    return os.environ.get(key, default)

try:
    load_env(ENV_FILE)
except (OSError, ValueError) as e:
    print(f"[ENV] 加载失败: {e}", flush=True)

# ============================================================
# 摄像头自动探测
# ============================================================
_V4L2_QUERYCAP = 0x80685600            # VIDIOC_QUERYCAP (_IOR('V', 0, 104))
_V4L2_CAP_VIDEO_CAPTURE = 0x00000001   # V4L2_CAP_VIDEO_CAPTURE

def _probe_video_capture(dev: str) -> bool:
    """判断 /dev/videoX 是否为 Video Capture 设备（排除 metadata/解码器节点）。"""
    try:
        fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        cap = ctypes.create_string_buffer(104)
        fcntl.ioctl(fd, _V4L2_QUERYCAP, cap)
        device_caps = struct.unpack_from('<I', cap, 88)[0]
        if device_caps == 0:
            device_caps = struct.unpack_from('<I', cap, 84)[0]
        return bool(device_caps & _V4L2_CAP_VIDEO_CAPTURE)
    except OSError:
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

def find_video_capture_device() -> Optional[str]:
    """自动探测摄像头：遍历 /dev/video*（按数字排序），返回第一个 Video Capture 设备。"""
    def _num(p: str) -> int:
        try:
            return int(p.rsplit("video", 1)[1])
        except (ValueError, IndexError):
            return 10 ** 9
    for dev in sorted(glob.glob("/dev/video*"), key=_num):
        if _probe_video_capture(dev):
            print(f"  [AUTODETECT] 探测到摄像头: {dev}", flush=True)
            return dev
    return None

# ============================================================
# 配置
# ============================================================
CAM0_DEV = cfg("CAM0_DEV", "").strip()
if not CAM0_DEV:
    CAM0_DEV = find_video_capture_device()
    if CAM0_DEV is None:
        print("[WARN] 自动探测摄像头失败，回退 /dev/video0（请检查 USB 摄像头是否连接）", flush=True)
        CAM0_DEV = "/dev/video0"
CAM1_DEV = cfg("CAM1_DEV", "/dev/video4").strip()
CAM_W     = int(cfg("CAM_W", "1280"))
CAM_H     = int(cfg("CAM_H", "720"))
CAM_FPS   = int(cfg("CAM_FPS", "160"))

BUFFER_SECONDS = float(cfg("BUFFER_SECONDS", "2.5"))
BUF_FRAMES = int(CAM_FPS * BUFFER_SECONDS)  # 160*2.5=400 帧

_SCRIPT_DIR = Path(__file__).resolve().parent

MODEL_PATH = cfg(
    "MODEL_PATH",
    str(_SCRIPT_DIR / "yolo_v11" / "best_final.dlc"),
)
YOLO_CONF = float(cfg("YOLO_CONF", "0.35"))
YOLO_IOU  = float(cfg("YOLO_IOU", "0.45"))
YOLO_THREADS = int(cfg("YOLO_THREADS", "8"))
IMG_SIZE = int(cfg("IMG_SIZE", "640"))
N_WORKERS = int(cfg("N_WORKERS", "2"))
SEGMENT_GAP = int(cfg("SEGMENT_GAP", "15"))
NOISE_CALIB_FRAMES = int(cfg("NOISE_CALIB_FRAMES", "5"))
NOISE_R = int(cfg("NOISE_R", "30"))

# ── GPU 推理（SNPE/DLC）──
GPU_RUNTIME = cfg("GPU_RUNTIME", "GPU")   # GPU / CPU / DSP
GPU_PROFILE = int(cfg("GPU_PROFILE", "5"))  # 5=BURST

# ── 立体标定（三角化）──
STEREO_PATH = cfg("STEREO_PATH", str(_SCRIPT_DIR / "stereo_params.npz"))

# ── 红色闪烁灯时间对齐 ──
RED_LIGHT_THRESHOLD = float(cfg("RED_LIGHT_THRESHOLD", "0.001"))  # 红色像素占比阈值

# ── 阿里云上传 ──
UPLOAD_URL = cfg("SERVER_UPLOAD_URL", "http://8.140.192.151/api/upload")
API_KEY = cfg("SC171_API_KEY", "")
UPLOAD_TIMEOUT = int(cfg("UPLOAD_TIMEOUT_SECONDS", "30"))
UPLOAD_RETRIES = int(cfg("UPLOAD_RETRIES", "3"))
VERIFY_HTTPS = cfg("VERIFY_HTTPS", "true").strip().lower() in ("1", "true", "yes")

# ── ESP UDP 控制端口 ──
UDP_BIND = cfg("P4_UDP_BIND", "0.0.0.0")
UDP_PORT = int(cfg("P4_UDP_PORT", "9000"))

# ============================================================
# ESP32-P4 UDP 通信（与 detect_yolo.py 相同）
# ============================================================
def send_esp_udp(data: bytes, addr: tuple[str, int]) -> None:
    """发送 UDP 到 ESP32-P4。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.sendto(data, addr)
        sock.close()
    except OSError as e:
        print(f"  [UDP] 发送失败: {e}", flush=True)

# ============================================================
# V4L2 iotcl 号（arm64 kernel 5.4，从 C 编译器取）
_V4L2_SON   = 0x40045612  # STREAMON
_V4L2_SOFF  = 0x40045613  # STREAMOFF
_V4L2_SFMT  = 0xC0D05605  # S_FMT
_V4L2_RBUF  = 0xC0145608  # REQBUFS
_V4L2_QBF   = 0xC0585609  # QBUF
_V4L2_QBF2  = 0xC058560F  # QBUF (2nd)
_V4L2_DQBF  = 0xC0585611  # DQBUF


# ============================================================
# V4L2Grabber — 与 detect_yolo.py 一致的 V4L2 MJPEG 帧抓取
# ============================================================
class V4L2Grabber:
    def __init__(self, device_path: str, w: int = 1280, h: int = 720,
                 fps: int = 160, nbufs: int = 4):
        self.fd = -1
        self.buffers: list[mmap.mmap] = []
        self._start(device_path, w, h, fps, nbufs)

    def _start(self, dev: str, w: int, h: int, fps: int, nbufs: int):
        self.fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
        flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

        # S_FMT: MJPG
        buf = ctypes.create_string_buffer(208)
        struct.pack_into('<I', buf, 0, 1)  # type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        struct.pack_into('<4I', buf, 4, w, h, 0x47504A4D, 0)  # MJPG
        fcntl.ioctl(self.fd, _V4L2_SFMT, buf)

        # REQBUFS (MMAP)
        req = ctypes.create_string_buffer(20)
        struct.pack_into('<III', req, 0, nbufs, 1, 1)  # count, memory=MMAP, reserved
        fcntl.ioctl(self.fd, _V4L2_RBUF, req)
        actual = struct.unpack_from('<I', req, 0)[0]

        # mmap buffers + QBUF 两次
        for i in range(actual):
            qbuf = ctypes.create_string_buffer(88)
            struct.pack_into('<III', qbuf, 0, i, 1, 1)  # index, type, memory, ...
            fcntl.ioctl(self.fd, _V4L2_QBF, qbuf)
            length = struct.unpack_from('<I', qbuf, 72)[0]
            offset = struct.unpack_from('<i', qbuf, 64)[0]
            addr = mmap.mmap(self.fd, length, mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE,
                             offset=offset)
            self.buffers.append(addr)
            # QBUF 第二次=入队
            struct.pack_into('<I', qbuf, 0, i)
            fcntl.ioctl(self.fd, _V4L2_QBF2, qbuf)

        # STREAMON
        fcntl.ioctl(self.fd, _V4L2_SON, ctypes.c_int(1))
        print(f"  [V4L2] {dev}: {w}x{h} MJPG @ {fps}fps, {actual} buffers", flush=True)

    def grab(self) -> bytes:
        buf = ctypes.create_string_buffer(88)
        struct.pack_into('<I', buf, 4, 1)   # type
        struct.pack_into('<I', buf, 60, 1)  # memory
        fcntl.ioctl(self.fd, _V4L2_DQBF, buf)
        idx = struct.unpack_from('<I', buf, 0)[0]
        bytesused = struct.unpack_from('<I', buf, 8)[0]
        data = bytes(self.buffers[idx][:bytesused])
        struct.pack_into('<I', buf, 0, idx)
        fcntl.ioctl(self.fd, _V4L2_QBF2, buf)
        return data

    def close(self):
        if self.fd < 0:
            return
        try:
            fcntl.ioctl(self.fd, _V4L2_SOFF, ctypes.c_int(1))
        except Exception:
            pass
        for p in self.buffers:
            try:
                p.close()
            except Exception:
                pass
        os.close(self.fd)
        self.fd = -1
        self.buffers = []

# ============================================================
# Ring buffer
# ============================================================
class RingBuffer:
    """固定容量环形缓冲（覆盖最旧帧）。"""
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.buf = [None] * capacity
        self.pos = 0
        self.count = 0
        self._lock = threading.Lock()

    def add(self, jpg_bytes: bytes) -> None:
        with self._lock:
            self.buf[self.pos] = jpg_bytes
            self.pos = (self.pos + 1) % self.capacity
            if self.count < self.capacity:
                self.count += 1

    def clear(self) -> None:
        with self._lock:
            self.buf = [None] * self.capacity
            self.pos = 0
            self.count = 0

    def frame_count(self) -> int:
        with self._lock:
            return self.count

    def get_all_bytes(self) -> list[bytes]:
        with self._lock:
            n = self.count
            if n == 0:
                return []
            start = (self.pos - n) % self.capacity
            result = []
            for i in range(n):
                result.append(self.buf[(start + i) % self.capacity])
            return result

    def peek_first(self) -> Optional[bytes]:
        with self._lock:
            if self.count == 0:
                return None
            start = (self.pos - self.count) % self.capacity
            return self.buf[start]

# ============================================================
# YOLO
# ============================================================
def _compute_gamma(mean_b: float, target: float = 120.0) -> float:
    """根据平均亮度计算 gamma：低于 target 调亮(gamma<1)，高于调暗(gamma>1)。"""
    mean_b = max(float(mean_b), 1.0)
    if abs(mean_b - target) < 1.0:
        return 1.0
    try:
        gamma = math.log(target / 255.0) / math.log(mean_b / 255.0)
    except (ValueError, ZeroDivisionError):
        return 1.0
    return float(np.clip(gamma, 0.45, 1.8))


def _build_lut(gamma: float) -> np.ndarray:
    """构建 256 项 gamma LUT（调亮/调暗）。"""
    inv = np.arange(256, dtype=np.float64) / 255.0
    return np.clip(255.0 * np.power(inv, gamma), 0, 255).astype(np.uint8)


def _compute_ref_brightness(frames_bytes: list, n_ref: int = 3) -> float:
    """取 ring 前 n_ref 帧的平均灰度作为亮度参考。"""
    vals = []
    for jpg in frames_bytes[:n_ref]:
        if not jpg:
            continue
        frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        vals.append(float(gray.mean()))
    if not vals:
        return 120.0
    return sum(vals) / len(vals)


class YOLOInfer:
    _gpu_lock = threading.Lock()  # 类级锁：SNPE 上下文非线程安全，GPU 调用串行化

    def __init__(self, onnx_path: str, conf: float = 0.2,
                 iou: float = 0.45, img_size: int = 640, gamma: float = 1.0):
        self.conf = conf
        self.iou = iou
        self.img_size = img_size
        self._gamma = gamma
        self._lut = None
        if abs(gamma - 1.0) > 1e-6:
            self._lut = _build_lut(gamma)

        if not HAS_GPU:
            raise RuntimeError("api_infer (SNPE) 不可用，无法进行 GPU 推理")
        print(f"  [YOLO] 加载模型: {onnx_path}", flush=True)
        rt = getattr(Runtime, GPU_RUNTIME, Runtime.GPU)
        self.snpe = SnpeContext(
            dlc_path=onnx_path,
            output_tensors=["output0"],
            runtime=rt,
            profile_level=GPU_PROFILE,
            log_level=LogLevel.ERROR,
            input_names=["images"],
            input_shapes=[[1, 3, img_size, img_size]])
        if self.snpe.Initialize() != 0:
            raise RuntimeError("SNPE Init 失败")
        print(f"  [YOLO] 加载完成 (conf={conf}, iou={iou}, runtime={GPU_RUNTIME})", flush=True)

    def brighten(self, bgr: np.ndarray) -> np.ndarray:
        """按当前 gamma LUT 调亮/调暗帧。gamma≈1 时原样返回。"""
        if self._lut is None:
            return bgr
        return cv2.LUT(bgr, self._lut)

    def set_gamma(self, gamma: float) -> None:
        """运行中更新 gamma 并重建 LUT。"""
        self._gamma = gamma
        self._lut = _build_lut(gamma) if abs(gamma - 1.0) > 1e-6 else None

    def infer(self, bgr: np.ndarray) -> list[list]:
        bgr = self.brighten(bgr)
        h, w = bgr.shape[:2]
        tensor, ratio, pad = preprocess_letterbox(
            bgr, (self.img_size, self.img_size))
        with YOLOInfer._gpu_lock:
            outputs = self.snpe.Execute(["output0"], {"images": tensor})
        if outputs is None:
            return []
        dets = detect_postprocess(
            outputs.get("output0", []),
            (h, w),
            (self.img_size, self.img_size),
            conf_thres=self.conf,
            iou_thres=self.iou,
            ratio_pad=(ratio, pad))
        # detect_postprocess → [(cx, cy, w, h, conf), ...]
        # 返回格式: [cx, cy, w, h, conf, cls_id]（单类羽毛球，cls_id 固定 0）
        return [[cx, cy, w, h, conf, 0] for (cx, cy, w, h, conf) in dets]

    def release(self):
        try:
            self.snpe.Release()
        except Exception:
            pass

# ============================================================
# 网带检测
# ============================================================
def _fit_line_vertical(pts, w, h):
    """用 cv2.fitLine 拟合纵向直线并延伸到画面上下边界。

    对竖直方向的线使用 direction-vector 外推而非 y=kx+b，
    避免竖直斜率无穷大导致数值不稳定。
    """
    pts_arr = np.array(pts, dtype=np.float32).reshape(-1, 2)
    if len(pts_arr) < 2:
        return None, None
    [vx, vy, cx, cy] = cv2.fitLine(pts_arr, cv2.DIST_L2, 0, 0.01, 0.01)
    vx, vy = float(vx), float(vy)
    if abs(vy) < 1e-6:
        return None, None
    # 外推到画面上下边界 y=0 和 y=h
    t_top = (0 - cy) / vy
    t_bot = (h - cy) / vy
    x_top = int(np.clip(cx + vx * t_top, 0, w))
    x_bot = int(np.clip(cx + vx * t_bot, 0, w))
    return (x_top, 0), (x_bot, h)

def net_band_detect(frame: np.ndarray) -> Optional[dict]:
    """检测纵向网带（亮黄色或白色），优先视野中央的竖直网带。

    1. 颜色：亮黄色 HSV(28-65, 80-255, 100-255) 或 白色 HSV(0-180, 0-30, 200-255)
    2. 方向：只保留竖直方向（与竖直线夹角 < 30°）的轮廓
    3. 优先：多根候选时选离画面水平中央最近的
    """
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h, w = frame.shape[:2]

    # ── 亮黄色 ──
    mask_yellow = cv2.inRange(hsv,
                              np.array([28, 80, 100]),
                              np.array([65, 255, 255]))
    # ── 白色（低饱和度 + 高亮度）──
    mask_white = cv2.inRange(hsv,
                             np.array([0, 0, 200]),
                             np.array([180, 30, 255]))
    # ── 合并 ──
    mask = cv2.bitwise_or(mask_yellow, mask_white)

    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    # ── 过滤：只保留纵向轮廓（cv2.fitLine 方向与竖直线夹角 < 30°）──
    vertical = []
    for c in contours:
        if cv2.contourArea(c) < 500:
            continue
        pts = c.reshape(-1, 2).astype(np.float32)
        if len(pts) < 2:
            continue
        [vx, vy, _x, _y] = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01)
        # |vy| > cos(30°) ≈ 0.866 → 方向向量与竖直线夹角 < 30°
        if abs(vy) > 0.866:
            vertical.append(c)

    if not vertical:
        return None

    # ── 优先画面水平中央 ──
    center_x = w / 2
    def _center_dist(c):
        M = cv2.moments(c)
        if M["m00"] == 0:
            return float("inf")
        return abs(M["m10"] / M["m00"] - center_x)

    vertical.sort(key=_center_dist)
    best = vertical[0]

    pts = [p[0].tolist() for p in best]
    p1, p2 = _fit_line_vertical(pts, w, h)
    if p1 is None:
        return None

    # 网带轮廓质心（用于双摄三角化网带 3D 位置）
    M = cv2.moments(best)
    cx = M["m10"] / M["m00"] if M["m00"] != 0 else w / 2
    cy = M["m01"] / M["m00"] if M["m00"] != 0 else h / 2

    return {
        "points": [list(p1), list(p2)],
        "center": [float(cx), float(cy)],
        "confidence": 0.95,
    }

# ============================================================
# 轨迹构建
# ============================================================
def build_trajectory(detections: list[dict], net_band: dict, frame_w: int = 1280, frame_h: int = 720) -> dict:
    frames_out = []
    for det in detections:
        frames_out.append({
            "frame": det["frame"],
            "xy": [det["cx"], det["cy"]],
            "conf": det["conf"],
        })
    n_total = max(d["frame"] for d in detections) + 1 if detections else 0

    # 分段
    frames_sorted = sorted(detections, key=lambda x: x["frame"])
    segments = []
    cur = [frames_sorted[0]["frame"]] if frames_sorted else []
    for i in range(1, len(frames_sorted)):
        if frames_sorted[i]["frame"] - frames_sorted[i - 1]["frame"] > SEGMENT_GAP:
            segments.append(cur)
            cur = [frames_sorted[i]["frame"]]
        else:
            cur.append(frames_sorted[i]["frame"])
    if cur:
        segments.append(cur)

    return {
        "frames": frames_out,
        "total_frames": n_total,
        "segments": segments,
        "net_band": net_band,
        "frame_w": frame_w,
        "frame_h": frame_h,
    }

# ============================================================
# 红色闪烁灯检测 + 软件时间对齐
# ============================================================
def detect_red_light(bgr: np.ndarray, threshold: float = None) -> bool:
    """检测画面中红色闪烁灯是否亮（红色像素占比超过阈值）。"""
    if bgr is None:
        return False
    thr = RED_LIGHT_THRESHOLD if threshold is None else threshold
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask1 = cv2.inRange(hsv, np.array([0, 100, 100]), np.array([10, 255, 255]))
    mask2 = cv2.inRange(hsv, np.array([170, 100, 100]), np.array([180, 255, 255]))
    mask = cv2.bitwise_or(mask1, mask2)
    ratio = float(cv2.countNonZero(mask)) / float(bgr.shape[0] * bgr.shape[1])
    return ratio > thr


def red_light_sequence(frames_bytes: list) -> list:
    """对一组 JPEG 帧逐帧检测红色灯状态，返回 [0/1, ...] 序列。"""
    seq = []
    for jpg in frames_bytes:
        if not jpg:
            seq.append(0)
            continue
        bgr = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
        seq.append(1 if detect_red_light(bgr) else 0)
    return seq


def time_align_offset(seq0: list, seq1: list) -> int:
    """用两路红色灯闪烁序列做互相关，返回 cam1 相对 cam0 的帧偏移 offset。

    含义：cam1 的第 i 帧 对应 cam0 的第 (i + offset) 帧。
    """
    if not seq0 or not seq1:
        return 0
    a = np.array(seq0, dtype=np.float64)
    b = np.array(seq1, dtype=np.float64)
    if a.std() < 1e-9 or b.std() < 1e-9:
        # 无闪烁信号（灯未检测到），退化为 0 偏移
        return 0
    a = a - a.mean()
    b = b - b.mean()
    corr = np.correlate(a, b, mode="full")
    lag = int(np.argmax(corr)) - (len(b) - 1)
    return lag


# ============================================================
# 立体三角化（竖直基线）
# ============================================================
def _rectify_point(x: float, y: float, map_x, map_y):
    """将原始像素坐标校正到立体校正坐标系。返回 (rx, ry) 或 None。"""
    xi = int(round(x))
    yi = int(round(y))
    if 0 <= xi < map_x.shape[1] and 0 <= yi < map_x.shape[0]:
        return float(map_x[yi, xi]), float(map_y[yi, xi])
    return None


def _triangulate_xy(x0: float, y0: float, x1: float, y1: float, stereo: dict):
    """三角化一对对应像素点，返回相机坐标系 3D 坐标 (X, Y, Z) 或 None。

    左相机校正坐标系：X 向右、Y 向下、Z 深度（米）。
    """
    P_l = stereo["P_l"].astype(np.float64)
    P_r = stereo["P_r"].astype(np.float64)
    map_lx, map_ly = stereo["map_lx"], stereo["map_ly"]
    map_rx, map_ry = stereo["map_rx"], stereo["map_ry"]

    pl = _rectify_point(x0, y0, map_lx, map_ly)
    pr = _rectify_point(x1, y1, map_rx, map_ry)
    if pl is None or pr is None:
        return None

    pt_l = np.array([[pl[0]], [pl[1]]], dtype=np.float64)
    pt_r = np.array([[pr[0]], [pr[1]]], dtype=np.float64)
    pts_4d = cv2.triangulatePoints(P_l, P_r, pt_l, pt_r)
    if abs(pts_4d[3][0]) < 1e-9:
        return None
    pt_3d = pts_4d[:3, 0] / pts_4d[3, 0]
    return float(pt_3d[0]), float(pt_3d[1]), float(pt_3d[2])


def calibrate_net_band(g0, g1, stereo: dict, frame_w: int, frame_h: int, n_frames: int = 5):
    """启动时网带标定：双摄检测网带 → 三角化网带 3D 位置 → 相机到网带距离。

    参数:
      g0, g1: 双摄 V4L2Grabber（任一可为 None）
      stereo: stereo_params.npz 加载的 dict
      n_frames: 每摄检测帧数（取中位数，抗单帧误检）

    返回:
      dict 或 None:
        net_band_2d: {"cam0": {"points","center"}, "cam1": {...}}  各相机像素检测
        net_band_3d: {"x","y","z"}  球场世界坐标（xOz=地面, y=向上）
        cam_to_net_m: 相机到网带距离（= net_band_3d.z 深度，米）
    """
    centers0, centers1 = [], []
    nb0 = nb1 = None

    def _grab_decode(g):
        try:
            jpg = g.grab()
            return cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            return None

    for _ in range(n_frames):
        if g0 is not None:
            frame = _grab_decode(g0)
            if frame is not None:
                nb = net_band_detect(frame)
                if nb is not None and nb.get("center"):
                    centers0.append(nb["center"])
                    if nb0 is None:
                        nb0 = nb
        if g1 is not None:
            frame = _grab_decode(g1)
            if frame is not None:
                nb = net_band_detect(frame)
                if nb is not None and nb.get("center"):
                    centers1.append(nb["center"])
                    if nb1 is None:
                        nb1 = nb

    if not centers0 or not centers1:
        return None

    c0 = np.median(np.array(centers0, dtype=np.float64), axis=0)
    c1 = np.median(np.array(centers1, dtype=np.float64), axis=0)

    xyz = _triangulate_xy(float(c0[0]), float(c0[1]), float(c1[0]), float(c1[1]), stereo)
    if xyz is None:
        return None
    X_cam, Y_cam, Z_cam = xyz

    # 翻转 Y：相机坐标系(Y 向下) → 球场世界坐标(y 向上 = 网柱方向)
    net_x, net_y, net_z = X_cam, -Y_cam, Z_cam

    return {
        "net_band_2d": {
            "cam0": {"points": nb0["points"] if nb0 else None,
                     "center": [float(c0[0]), float(c0[1])]},
            "cam1": {"points": nb1["points"] if nb1 else None,
                     "center": [float(c1[0]), float(c1[1])]},
        },
        "net_band_3d": {"x": round(net_x, 4), "y": round(net_y, 4), "z": round(net_z, 4)},
        "cam_to_net_m": round(net_z, 4),
    }


def triangulate_trajectory(dets0: list, dets1: list, offset: int,
                           stereo: dict, frame_w: int = 1280, frame_h: int = 720,
                           net_band_3d: dict = None) -> dict:
    """对时间对齐后的左右检测点做三角化，生成 3D 轨迹。

    参数:
      dets0: cam0 检测点 [{frame, cx, cy, conf}, ...]
      dets1: cam1 检测点 [{frame, cx, cy, conf}, ...]
      offset: cam1 帧 i 对应 cam0 帧 i+offset（来自 time_align_offset）
      stereo: stereo_params.npz 加载的 dict
      net_band_3d: 网带 3D 位置 {x, y, z}；提供则把轨迹平移到网带（xOz 原点对齐网带）

    返回:
      3D 轨迹 dict，frames 为 [{frame, x, y, z, conf}, ...]
      坐标：球场世界坐标系（米），xOz 平面 = 球场地面（x 横向、z 深度），
            y 方向 = 网柱方向（竖直向上）
    """
    P_l = stereo["P_l"].astype(np.float64)
    P_r = stereo["P_r"].astype(np.float64)
    map_lx, map_ly = stereo["map_lx"], stereo["map_ly"]
    map_rx, map_ry = stereo["map_rx"], stereo["map_ry"]

    det0_by_frame = {d["frame"]: d for d in dets0}
    det1_by_frame = {d["frame"]: d for d in dets1}

    frames_3d = []
    for d0 in dets0:
        f0 = d0["frame"]
        # cam1 帧 i 对应 cam0 帧 i+offset → cam0 帧 f0 对应 cam1 帧 f0-offset
        f1 = f0 - offset
        d1 = det1_by_frame.get(f1)
        if d1 is None:
            for delta in (1, -1, 2, -2):
                d1 = det1_by_frame.get(f1 + delta)
                if d1 is not None:
                    break
        if d1 is None:
            continue

        pl = _rectify_point(d0["cx"], d0["cy"], map_lx, map_ly)
        pr = _rectify_point(d1["cx"], d1["cy"], map_rx, map_ry)
        if pl is None or pr is None:
            continue

        pt_l = np.array([[pl[0]], [pl[1]]], dtype=np.float64)
        pt_r = np.array([[pr[0]], [pr[1]]], dtype=np.float64)
        pts_4d = cv2.triangulatePoints(P_l, P_r, pt_l, pt_r)
        if abs(pts_4d[3][0]) < 1e-9:
            continue
        pt_3d = pts_4d[:3, 0] / pts_4d[3, 0]
        x_cam, y_cam, z_cam = float(pt_3d[0]), float(pt_3d[1]), float(pt_3d[2])
        if not (0.1 < z_cam < 15.0):
            continue
        # 坐标变换：左相机校正坐标系(X右, Y下, Z深度) → 球场世界坐标系
        #   竖直基线双目中 Y 轴即图像竖直方向（向下），翻转后 y 向上 = 网柱方向；
        #   xOz 平面即球场地面（x 横向、z 深度）。
        x = x_cam
        y = -y_cam
        z = z_cam
        # 坐标对齐：平移到网带（xOz 原点在网带：x 横向中央、z 深度参考）
        if net_band_3d is not None:
            x = x - net_band_3d["x"]
            z = z - net_band_3d["z"]
            # y 保持绝对高度（网带中心约 0.775m），不平移
        frames_3d.append({
            "frame": f0,
            "x": round(x, 4),
            "y": round(y, 4),
            "z": round(z, 4),
            "conf": round(min(d0["conf"], d1["conf"]), 4),
        })

    total_frames = 0
    if dets0:
        total_frames = max(d["frame"] for d in dets0) + 1
    elif dets1:
        total_frames = max(d["frame"] for d in dets1) + 1

    return {
        "frames": frames_3d,
        "total_frames": total_frames,
        "stereo": True,
        "baseline_m": round(float(np.linalg.norm(stereo["T"].ravel())), 4),
        "time_offset": offset,
        "frame_w": frame_w,
        "frame_h": frame_h,
        "coordinate_system": "court-world (xOz=ground, y=up/net-post direction)",
        "net_band_3d": net_band_3d,
        "cam_to_net_m": round(net_band_3d["z"], 4) if net_band_3d else None,
    }


# ============================================================
# 阿里云上传（三角化后的 trajectory.json）
# ============================================================
def upload_trajectory(traj_data: dict) -> bool:
    """将轨迹数据上传到阿里云。traj_data 为任意 JSON 可序列化 dict。"""
    if not API_KEY:
        print("  [UPLOAD] SC171_API_KEY 未配置，跳过上传", flush=True)
        return False

    body = json.dumps(traj_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        UPLOAD_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-API-Key": API_KEY,
            "User-Agent": "Fibocom-SC171/2.0",
        },
    )
    ssl_ctx = None
    if UPLOAD_URL.startswith("https://") and not VERIFY_HTTPS:
        ssl_ctx = ssl._create_unverified_context()

    for attempt in range(1, UPLOAD_RETRIES + 1):
        try:
            with urllib.request.urlopen(
                request, timeout=UPLOAD_TIMEOUT, context=ssl_ctx,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
                task_id = result.get("task_id", "unknown")
                print(f"  [UPLOAD] 上传成功 HTTP {response.status}, task_id={task_id}", flush=True)
                return True
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            print(f"  [UPLOAD] 服务器拒绝 HTTP {e.code}: {detail}", file=sys.stderr, flush=True)
            return False
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  [UPLOAD] JSON 无效: {e}", file=sys.stderr, flush=True)
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  [UPLOAD] 第{attempt}/{UPLOAD_RETRIES}次失败: {e}", file=sys.stderr, flush=True)
            if attempt < UPLOAD_RETRIES:
                time.sleep(2 * attempt)
    print("  [UPLOAD] 所有重试失败", file=sys.stderr, flush=True)
    return False


# ============================================================
# 工具函数
# ============================================================
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================
# Controller
# ============================================================
def _send_ack(sock: socket.socket, addr: tuple, signal: str = "") -> None:
    """回复 ESP32-P4 ACK ('ACK:x')，匹配 sc171_udp.c acknowledgement_matches"""
    try:
        sock.sendto(b"ACK:" + signal.encode(), addr)
    except OSError:
        pass


class Controller:
    def __init__(self):
        self.recording = False
        self.buffer_frozen = True  # buffer 默认冻结，收到 'a' 后开始填充，'b' 后冻结
        self.net_band = None
        self.noise_hits = set()
        self.buf0 = RingBuffer(BUF_FRAMES)
        self.buf1 = RingBuffer(BUF_FRAMES)
        self.grab0 = None
        self.grab1 = None
        self.cam0_ok = True
        self.cam1_ok = True
        # 立体标定参数（三角化）
        self.stereo = None
        # 网带标定结果（启动时三角化，用于坐标对齐）
        self.net_band_3d = None       # 网带 3D 位置 {x, y, z}（球场世界坐标）
        self.net_band_calib = None    # 完整标定结果（含 cam_to_net_m）
        # UDP 去重
        self._last_cmd = ""
        self._last_cmd_time = 0.0
        self._p4_addr = None  # 最近一次收到 P4 UDP 指令的地址

    def _grab_loop(self, grabber: V4L2Grabber, buffer: RingBuffer, label: str):
        while RUNNING:
            try:
                if self.buffer_frozen:
                    time.sleep(0.001)
                    continue
                jpg = grabber.grab()
                buffer.add(jpg)
            except Exception:
                break

    def _start_capture(self):
        try:
            self.grab0 = V4L2Grabber(CAM0_DEV, CAM_W, CAM_H, CAM_FPS)
            t0 = threading.Thread(target=self._grab_loop, args=(self.grab0, self.buf0, "cam0"), daemon=True)
            t0.start()
        except Exception:
            self.cam0_ok = False
            self.grab0 = None
        try:
            self.grab1 = V4L2Grabber(CAM1_DEV, CAM_W, CAM_H, CAM_FPS)
            t1 = threading.Thread(target=self._grab_loop, args=(self.grab1, self.buf1, "cam1"), daemon=True)
            t1.start()
        except Exception:
            self.cam1_ok = False
            self.grab1 = None

    def _stop_capture(self):
        if self.grab0:
            self.grab0.close()
            self.grab0 = None
        if self.grab1:
            self.grab1.close()
            self.grab1 = None

    def _init_detect(self):
        """初始化：加载立体标定 + 网带检测 + 噪点校准（双摄像头）"""
        print("[INIT] 启动采集进行初始化...", flush=True)

        # ── 加载立体标定参数（三角化）──
        try:
            if os.path.isfile(STEREO_PATH):
                self.stereo = np.load(STEREO_PATH)
                base = float(np.linalg.norm(self.stereo["T"].ravel()))
                print(f"  [INIT] 立体标定已加载: {STEREO_PATH} (基线={base:.4f}m)", flush=True)
            else:
                self.stereo = None
                print(f"  [INIT] 未找到立体标定文件: {STEREO_PATH}，三角化禁用", flush=True)
        except Exception as e:
            self.stereo = None
            print(f"  [INIT] 立体标定加载失败: {e}", flush=True)

        # ── 打开双摄 ──
        g0 = None
        g1 = None
        try:
            g0 = V4L2Grabber(CAM0_DEV, CAM_W, CAM_H, CAM_FPS)
        except Exception:
            self.cam0_ok = False
        try:
            g1 = V4L2Grabber(CAM1_DEV, CAM_W, CAM_H, CAM_FPS)
        except Exception:
            self.cam1_ok = False

        if g0 is None and g1 is None:
            self.net_band = {"points": [[CAM_W // 2, 0], [CAM_W // 2, CAM_H]], "confidence": 0}
            self._frame_w, self._frame_h = CAM_W, CAM_H
            return

        # ── 获取实际帧尺寸（以 cam0 为准，fallback cam1）──
        self._frame_w, self._frame_h = CAM_W, CAM_H
        sample_grabber = g0 if g0 is not None else g1
        try:
            jpg_sample = sample_grabber.grab()
            frame_sample = cv2.imdecode(np.frombuffer(jpg_sample, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame_sample is not None:
                self._frame_h, self._frame_w = frame_sample.shape[:2]
        except Exception:
            pass

        # ── 网带标定：双摄检测 + 三角化网带 3D 位置（坐标对齐参考）──
        self.net_band = {"points": [[self._frame_w // 2, 0], [self._frame_w // 2, self._frame_h]], "confidence": 0}
        self.net_band_3d = None
        self.net_band_calib = None
        if self.stereo is not None and g0 is not None and g1 is not None:
            try:
                calib = calibrate_net_band(g0, g1, self.stereo, self._frame_w, self._frame_h)
            except Exception as e:
                calib = None
                print(f"  [INIT] 网带三角化异常: {e}", flush=True)
            if calib is not None:
                self.net_band_calib = calib
                self.net_band_3d = calib["net_band_3d"]
                p0 = calib["net_band_2d"]["cam0"]["points"]
                if p0:
                    self.net_band = {"points": [list(p) for p in p0], "confidence": 0.95}
                n3 = self.net_band_3d
                print(f"  [INIT] 网带三角化: xyz=({n3['x']:.3f},{n3['y']:.3f},{n3['z']:.3f})m "
                      f"相机→网带={calib['cam_to_net_m']:.3f}m", flush=True)
            else:
                print("  [INIT] 网带检测/三角化失败，回退中央竖线", flush=True)
        else:
            print(f"  [INIT] 帧尺寸={self._frame_w}x{self._frame_h} 网带: x={self._frame_w // 2}（单摄/无标定，未三角化）", flush=True)

        # ── 噪点校准（GPU 双摄）──
        model = None
        if HAS_GPU:
            try:
                model = YOLOInfer(MODEL_PATH, conf=YOLO_CONF, iou=YOLO_IOU, img_size=IMG_SIZE)
            except Exception as e:
                print(f"  [INIT] 模型加载失败: {e}，跳过噪点校准", flush=True)
        else:
            print("  [INIT] GPU 推理不可用，跳过噪点校准", flush=True)

        nh = set()
        if model is not None:
            for label, g in (("cam0", g0), ("cam1", g1)):
                if g is None:
                    continue
                for _ in range(NOISE_CALIB_FRAMES):
                    jpg = g.grab()
                    frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if frame is None:
                        continue
                    try:
                        dets = model.infer(frame)
                    except Exception:
                        continue
                    for d in dets:
                        cx, cy = int(d[0]), int(d[1])
                        nh.add((cx, cy))
            model.release()
        self.noise_hits = nh
        print(f"  [INIT] 噪点校准完成，收集 {len(nh)} 个静态位置", flush=True)

        if g0 is not None:
            g0.close()
        if g1 is not None:
            g1.close()
        print("[INIT] 初始化完成", flush=True)

    # ── 信号处理 ──
    def on_signal_start(self):
        """a → 开始录制"""
        if self.recording:
            return
        self.recording = True
        self.buffer_frozen = False
        self.buf0.clear()
        self.buf1.clear()
        print("[CMD] 开始录制 (buffer 解除冻结)", flush=True)

    def on_signal_stop(self):
        """b → 停止录制（冻结 buffer）"""
        if not self.recording:
            return
        self.recording = False
        self.buffer_frozen = True
        print("[CMD] 停止录制 (buffer 冻结)", flush=True)

    def on_signal_challenge(self):
        """c → 双摄 GPU 推理 → 红色灯时间对齐 → 三角化 → 上传阿里云"""
        if self.recording:
            print("[CMD] 正在录制中，不允许挑战推理", flush=True)
            return

        print("[CMD] 挑战推理开始", flush=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        ring_dir = Path("/home/fibo/ALL/_ring") / timestamp
        t0 = time.time()

        frames0 = self.buf0.get_all_bytes() if self.cam0_ok else []
        frames1 = self.buf1.get_all_bytes() if self.cam1_ok else []
        n0, n1 = len(frames0), len(frames1)
        print(f"  [YOLO] 缓冲帧: cam0={n0}, cam1={n1}", flush=True)

        if n0 < 10 and n1 < 10:
            print("  [YOLO] 帧数不足，跳过推理", flush=True)
            return

        # ── 红色闪烁灯时间对齐 ──
        offset = 0
        if n0 >= 10 and n1 >= 10:
            seq0 = red_light_sequence(frames0)
            seq1 = red_light_sequence(frames1)
            offset = time_align_offset(seq0, seq1)
            print(f"  [TIMEALIGN] 红色灯时间对齐 offset={offset} 帧", flush=True)

        # ── 自动调亮：取 ring 前 3 帧平均亮度作参考 ──
        ref_frames = frames0 if n0 >= 3 else frames1
        ref_brightness = _compute_ref_brightness(ref_frames, n_ref=3)
        gamma = _compute_gamma(ref_brightness, target=120.0)
        tag = "调亮" if gamma < 1.0 else ("调暗" if gamma > 1.0 else "不调")
        print(f"  [GAMADJ] 前3帧平均亮度={ref_brightness:.1f} → gamma={gamma:.3f} ({tag})", flush=True)

        if not HAS_GPU:
            print("  [YOLO] GPU 推理不可用，无法推理", flush=True)
            return

        model = YOLOInfer(MODEL_PATH, conf=YOLO_CONF, iou=YOLO_IOU, img_size=IMG_SIZE, gamma=gamma)

        # ── 并行推理 ──
        def _infer_frames(frames: list, nh: set) -> list:
            chunk_size = max(1, len(frames) // N_WORKERS)
            chunks = [(i, frames[i:i + chunk_size]) for i in range(0, len(frames), chunk_size)]
            total_frames = len(frames)
            prog_lock = threading.Lock()
            prog_done = [0]
            prog_last_pct = [0]

            def _infer_chunk(chunk_start: int, chunk_bytes: list, nh: set) -> list:
                results = []
                prev2 = None
                prev = None
                for frame_i, jpg_bytes in enumerate(chunk_bytes):
                    frame_num = chunk_start + frame_i
                    bgr = cv2.imdecode(np.frombuffer(jpg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if bgr is None:
                        prev2 = prev
                        prev = None
                        with prog_lock:
                            prog_done[0] += 1
                        continue
                    dets = model.infer(bgr)

                    # 过滤噪点
                    candidates = []
                    for d in dets:
                        cx, cy = int(d[0]), int(d[1])
                        if (cx, cy) in nh:
                            continue
                        candidates.append({
                            "frame": frame_num,
                            "cx": d[0],
                            "cy": d[1],
                            "w": d[2],
                            "h": d[3],
                            "conf": d[4],
                        })

                    if not candidates:
                        prev2 = prev
                        prev = None
                        with prog_lock:
                            prog_done[0] += 1
                        continue

                    # 选点：速度外推
                    if prev2 is not None and prev is not None:
                        vx = prev["cx"] - prev2["cx"]
                        vy = prev["cy"] - prev2["cy"]
                        candidates.sort(key=lambda c:
                            abs(c["cx"] - (prev["cx"] + vx)) +
                            abs(c["cy"] - (prev["cy"] + vy)))
                    elif prev is not None:
                        candidates.sort(key=lambda c:
                            abs(c["cx"] - prev["cx"]) +
                            abs(c["cy"] - prev["cy"]))
                    else:
                        candidates.sort(key=lambda c: -c["conf"])

                    best = candidates[0]
                    results.append(best)

                    prev2 = prev
                    prev = best

                    with prog_lock:
                        prog_done[0] += 1
                        pct = int(prog_done[0] * 100 / total_frames)
                        if pct >= prog_last_pct[0] + 10:
                            prog_last_pct[0] = (pct // 10) * 10
                            print(f"  [YOLO] 推理进度: {prog_done[0]}/{total_frames} ({pct}%)", flush=True)
                            # 上报进度到 P4
                            if self._p4_addr is not None:
                                try:
                                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                                    sock.settimeout(0.3)
                                    sock.sendto(f"PROGRESS:SC171:{pct}\n".encode(),
                                                (self._p4_addr[0], 9001))
                                    sock.close()
                                except Exception:
                                    pass
                return results

            # 多线程
            all_results = []
            if N_WORKERS > 1:
                with ThreadPoolExecutor(max_workers=N_WORKERS) as pool:
                    futures = {
                        pool.submit(_infer_chunk, cs, cb, nh): (cs, cb)
                        for cs, cb in chunks
                    }
                    for fut in as_completed(futures):
                        all_results.extend(fut.result())
                all_results.sort(key=lambda r: r["frame"])
            else:
                for cs, cb in chunks:
                    all_results.extend(_infer_chunk(cs, cb, nh))
            return all_results

        print(f"  [YOLO] 推理计算中... ({N_WORKERS}线程)", flush=True)

        dets0 = _infer_frames(frames0, self.noise_hits) if len(frames0) >= 10 else []
        dets1 = _infer_frames(frames1, self.noise_hits) if len(frames1) >= 10 else []

        model.release()

        # ── 100% 进度上报（防止条卡在 90% 不动）──
        if self._p4_addr is not None:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(0.3)
                sock.sendto(b"PROGRESS:SC171:100\n", (self._p4_addr[0], 9001))
                sock.close()
            except Exception:
                pass

        # ── 保存 buffer 帧到磁盘（双摄）──
        print(f"  [SAVE] 保存帧到 {ring_dir} ...", flush=True)
        self._save_ring_frames(ring_dir, frames0, frames1)

        # ── 帧内去重 ──
        def _remove_duplicates(dets: list) -> list:
            seen_frames = set()
            result = []
            for d in dets:
                if d["frame"] not in seen_frames:
                    result.append(d)
                    seen_frames.add(d["frame"])
            return result

        dets0 = _remove_duplicates(dets0)
        dets1 = _remove_duplicates(dets1)
        elapsed = time.time() - t0
        print(f"  [YOLO] 推理完成: cam0={len(dets0)}点, cam1={len(dets1)}点 ({elapsed:.1f}s)", flush=True)

        # ── 三角化 → 3D 立体轨迹 ──
        traj3d = {}
        if self.stereo is not None and len(dets0) >= 3 and len(dets1) >= 3:
            traj3d = triangulate_trajectory(
                dets0, dets1, offset, self.stereo, self._frame_w, self._frame_h,
                net_band_3d=self.net_band_3d)
            n3d = len(traj3d.get("frames", []))
            print(f"  [TRIANG] 三角化完成: {n3d} 个 3D 点 (offset={offset})", flush=True)
        else:
            print("  [TRIANG] 立体标定缺失或检测点不足，跳过三角化", flush=True)

        # ── 构建 2D 轨迹（cam0，用于静止检测）──
        traj0 = build_trajectory(dets0, self.net_band, self._frame_w, self._frame_h) if len(dets0) >= 3 else {}

        # ── 保存轨迹到本地 ──
        try:
            traj_payload = {
                "cam0": dict(traj0) if traj0 else {},
                "cam1": build_trajectory(dets1, self.net_band, self._frame_w, self._frame_h) if len(dets1) >= 3 else {},
                "stereo_3d": dict(traj3d) if traj3d else {},
            }
            for label in ("cam0", "cam1", "stereo_3d"):
                d = traj_payload[label]
                if d:
                    d.setdefault("source_device", "Fibocom-SC171")
                    d.setdefault("camera", label)
                    d.setdefault("sc171_uploaded_at", _utc_now())
            traj_path = ring_dir / "trajectory.json"
            ring_dir.mkdir(parents=True, exist_ok=True)
            traj_path.write_text(json.dumps(traj_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            n_traj0 = len(traj0.get("frames", []))
            print(f"  [SAVE] 轨迹已保存: cam0={n_traj0}点, 3D={len(traj3d.get('frames', []))}点 -> {traj_path}", flush=True)
        except Exception as e:
            import traceback
            print(f"  [SAVE] 轨迹保存失败: {e}", flush=True)
            traceback.print_exc()

        # ── 第一重静止检测（方差 < 500）→ 直接通知 P4 返回主页 ──
        if self._is_no_clear_shuttle(traj0):
            print(f"  [NO_SHUTTLE] 轨迹静止/无有效运动，通知 P4", flush=True)
            if self._p4_addr is not None:
                p4_ip = self._p4_addr[0]
                send_esp_udp(b"NO_SHUTTLE\n", (p4_ip, 9001))
            return

        # ── 上传完整轨迹（cam0/cam1 2D + stereo_3d 3D）到阿里云 ──
        upload_payload = {}
        for label in ("cam0", "cam1", "stereo_3d"):
            d = traj_payload.get(label)
            if d:
                d.setdefault("source_device", "Fibocom-SC171")
                d.setdefault("camera", label)
                d.setdefault("sc171_uploaded_at", _utc_now())
                upload_payload[label] = d

        if upload_payload:
            upload_trajectory(upload_payload)
        else:
            print("  [UPLOAD] 无有效轨迹，跳过上传", flush=True)

        print(f"  [YOLO] 总耗时: {time.time() - t0:.1f}s", flush=True)

    @staticmethod
    def _is_no_clear_shuttle(traj0: dict) -> bool:
        """第一重静止检测：轨迹方差 < 500 → 无明确轨迹。"""
        frames = traj0.get("frames", [])
        if len(frames) < 3:
            return True
        xs = [f["xy"][0] for f in frames]
        ys = [f["xy"][1] for f in frames]
        cx = sum(xs) / len(xs)
        cy = sum(ys) / len(ys)
        var = sum((x - cx) ** 2 + (y - cy) ** 2 for x, y in zip(xs, ys)) / len(xs)
        return var < 500

    def _save_ring_frames(self, dst: Path, frames0: list, frames1: list = None):
        """将 buffer 帧写入 _ring/<timestamp>/camX/*.jpg（双摄像头）"""
        if frames0:
            dst_cam0 = dst / "cam0"
            dst_cam0.mkdir(parents=True, exist_ok=True)
            for i, jpg in enumerate(frames0):
                (dst_cam0 / f"{i:08d}.jpg").write_bytes(jpg)
        if frames1:
            dst_cam1 = dst / "cam1"
            dst_cam1.mkdir(parents=True, exist_ok=True)
            for i, jpg in enumerate(frames1):
                (dst_cam1 / f"{i:08d}.jpg").write_bytes(jpg)

        # 写入 metadata
        info = {
            "timestamp": dst.name,
            "cam0_frames": len(frames0),
            "cam1_frames": len(frames1 or []),
            "width": CAM_W,
            "height": CAM_H,
            "fps": CAM_FPS,
            "device_cam0": CAM0_DEV,
            "device_cam1": CAM1_DEV,
        }
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "info.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  [SAVE] 完成: cam0={len(frames0)}帧, cam1={len(frames1 or [])}帧", flush=True)

    def run(self):
        print("=" * 50, flush=True)
        print(" SC171 GPU 双摄三角化版", flush=True)
        print("=" * 50, flush=True)
        print(f"  摄像头: cam0={CAM0_DEV}, cam1={CAM1_DEV}", flush=True)
        print(f"  分辨率: {CAM_W}x{CAM_H}@{CAM_FPS}fps", flush=True)
        print(f"  缓冲: {BUFFER_SECONDS}s", flush=True)
        print(f"  UDP监听: {UDP_BIND}:{UDP_PORT}", flush=True)
        print(f"  模型: {MODEL_PATH}", flush=True)
        print(f"  上传: {UPLOAD_URL}", flush=True)
        print(flush=True)

        # ── 初始化 ──
        self._init_detect()

        # ── UDP 接收线程 ──
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((UDP_BIND, UDP_PORT))
        sock.settimeout(1.0)
        print(f"[UDP] 监听 {UDP_BIND}:{UDP_PORT}", flush=True)

        # ── 开始环缓冲采集 ──
        self._start_capture()
        print("[CAPTURE] 环缓冲采集已启动", flush=True)
        print("[READY] 等待ESP32-P4 UDP指令 (a=开始录制 b=停止录制 c=推理)", flush=True)

        while RUNNING:
            try:
                data, addr = sock.recvfrom(64)
            except socket.timeout:
                continue
            except OSError:
                break

            if not data:
                continue
            cmd = data.strip().decode("utf-8", errors="replace").lower()

            # UDP 去重
            now = time.time()
            dedup_secs = int(cfg("UDP_DEDUP_SECONDS", "3"))
            if cmd == self._last_cmd and (now - self._last_cmd_time) < dedup_secs:
                print(f"[UDP] 去重忽略 '{cmd}' ({self._last_cmd_time:.0f}+{dedup_secs}s)", flush=True)
                _send_ack(sock, addr, cmd)
                continue
            self._last_cmd = cmd
            self._last_cmd_time = now
            self._p4_addr = addr

            print(f"[UDP] 收到 '{cmd}' 来自 {addr}", flush=True)

            if cmd == "a":
                gpio_util.gpio_pulse(8)
                self.on_signal_start()
                _send_ack(sock, addr, cmd)
            elif cmd == "b":
                gpio_util.gpio_pulse(9)
                self.on_signal_stop()
                _send_ack(sock, addr, cmd)
            elif cmd == "c":
                gpio_util.gpio_pulse(5)
                _send_ack(sock, addr, cmd)  # 先回 ACK，再推理（避免 ESP32 重发）
                self.on_signal_challenge()
            else:
                print(f"[UDP] 未知指令: {cmd}", flush=True)

        self._stop_capture()
        sock.close()
        print("[SHUTDOWN] 退出", flush=True)

# ============================================================
# Main
# ============================================================
def main() -> int:
    # ── 自动联网 ──
    ensure_wifi_connected(ssid="#######", password="###########")

    ctrl = Controller()
    try:
        ctrl.run()
    except KeyboardInterrupt:
        pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
