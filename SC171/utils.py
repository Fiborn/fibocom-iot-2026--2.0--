#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
utils.py — YOLOv11 羽毛球检测后处理（单类模型专用）
"""

import cv2
import numpy as np


# ═══════════════════════════════════════════════════════════════
# 通用几何工具
# ═══════════════════════════════════════════════════════════════

def xywh2xyxy(x):
    """(cx, cy, w, h) → (x1, y1, x2, y2)"""
    y = np.copy(x)
    y[:, 0] = x[:, 0] - x[:, 2] / 2
    y[:, 1] = x[:, 1] - x[:, 3] / 2
    y[:, 2] = x[:, 0] + x[:, 2] / 2
    y[:, 3] = x[:, 1] + x[:, 3] / 2
    return y


def xyxy2xywh(box):
    """(x1, y1, x2, y2) → (x1, y1, w, h)"""
    box[:, 2:] = box[:, 2:] - box[:, :2]
    return box


def nms_single(dets, thresh):
    """单类 Non-Maximum Suppression
    dets: (N, 5) → [x1, y1, x2, y2, score]
    """
    if len(dets) == 0:
        return dets
    x1, y1 = dets[:, 0], dets[:, 1]
    x2, y2 = dets[:, 2], dets[:, 3]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = dets[:, 4].argsort()[::-1]
    keep = []
    while len(order) > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1 + 1) * np.maximum(0, yy2 - yy1 + 1)
        ious = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[1:][ious <= thresh]
    return dets[keep]


def letterbox(img, new_shape=(640, 640), color=(114, 114, 114),
              auto=True, scaleup=True, stride=32):
    """等比例缩放 + 黑边填充，保持宽高比不变"""
    h, w = img.shape[:2]
    r = min(new_shape[0] / h, new_shape[1] / w)
    if not scaleup:
        r = min(r, 1.0)
    new_w, new_h = int(round(w * r)), int(round(h * r))
    dw = new_shape[1] - new_w
    dh = new_shape[0] - new_h
    if auto:
        dw = dw % stride
        dh = dh % stride
    dw //= 2
    dh //= 2
    if (new_w, new_h) != (w, h):
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    img = cv2.copyMakeBorder(img, dh, dh, dw, dw,
                             cv2.BORDER_CONSTANT, value=color)
    return img, r, (dw, dh)


def scale_coords(coords, img0_shape, img1_shape, ratio_pad=None):
    """将模型坐标缩放回原始图像坐标
    coords: (N, 4) xyxy
    img0_shape: (H, W) 原始图
    img1_shape: (H, W) 模型输入图
    ratio_pad: (ratio, (dw, dh)) 来自 letterbox
    """
    h1, w1 = img1_shape[:2]
    h0, w0 = img0_shape[:2]
    if ratio_pad is not None:
        gain = ratio_pad
        pad_w, pad_h = ratio_pad[1] if isinstance(ratio_pad[1], (int, float)) \
            else ratio_pad[1]
        if not isinstance(gain, (int, float)):
            gain = gain[0] if isinstance(gain, (list, tuple)) else gain
    else:
        gain = min(h1 / h0, w1 / w0)
        pad_w = (w1 - w0 * gain) / 2
        pad_h = (h1 - h0 * gain) / 2
    coords[:, [0, 2]] -= pad_w
    coords[:, [1, 3]] -= pad_h
    coords[:, :4] /= gain
    # clip
    coords[:, 0] = coords[:, 0].clip(0, w0)
    coords[:, 1] = coords[:, 1].clip(0, h0)
    coords[:, 2] = coords[:, 2].clip(0, w0)
    coords[:, 3] = coords[:, 3].clip(0, h0)
    return coords


# ═══════════════════════════════════════════════════════════════
# YOLOv11 羽毛球检测 — 预处理 + 后处理
# ═══════════════════════════════════════════════════════════════

CLASS_NAME = "shuttlecock"


def preprocess_img(img, target_shape=(640, 640),
                   div_num=255, means=None, stds=None):
    """
    图像预处理: resize(target_shape) → /div_num → (可选)z-score → NCHW
    """
    img = cv2.resize(img.copy(), target_shape)
    img = img.astype(np.float32) / div_num
    if means is not None and stds is not None:
        means = np.array(means, dtype=np.float32).reshape(1, 1, -1)
        stds = np.array(stds, dtype=np.float32).reshape(1, 1, -1)
        img = (img - means) / stds
    return img[np.newaxis, :].transpose(0, 3, 1, 2).astype(np.float32)


def preprocess_letterbox(img, target_shape=(640, 640), div_num=255):
    """
    letterbox 预处理: 等比例缩放+填充 → /div_num → NCHW
    返回: (input_tensor, ratio, (dw, dh))
    """
    img_padded, ratio, pad = letterbox(
        img.copy(), target_shape, auto=False)
    tensor = img_padded.astype(np.float32) / div_num
    tensor = tensor[np.newaxis, :].transpose(0, 3, 1, 2).astype(np.float32)
    return tensor, ratio, pad


def detect_postprocess(prediction, img0shape, img1shape,
                       conf_thres=0.2, iou_thres=0.45, ratio_pad=None):
    """
    YOLOv11 单类检测后处理（shuttlecock）

    参数:
      prediction: np.ndarray, 模型原始输出 (1, N, 6) 或 (1, 6, N)
      img0shape:  (H, W) 原始图像尺寸
      img1shape:  (H, W) 模型输入尺寸
      conf_thres: 置信度阈值
      iou_thres:  NMS IOU 阈值
      ratio_pad:  letterbox 参数 (ratio, (dw, dh))

    返回: [(cx, cy, w, h, conf), ...]  原始图像坐标
    """
    h1, w1 = img1shape[:2]
    h0, w0 = img0shape[:2]

    # 统一 shape → (N, 6)
    p = np.array(prediction)
    if p.ndim == 3 and p.shape[1] > p.shape[2]:
        p = p.transpose(0, 2, 1)  # (1, 6, N) → (1, N, 6)
    p = p.reshape(-1, 6)

    # 置信度过滤
    mask = p[:, 4] > conf_thres
    p = p[mask]
    if len(p) == 0:
        return []

    # denormalize → 模型坐标
    p[:, 0] *= w1
    p[:, 1] *= h1
    p[:, 2] *= w1
    p[:, 3] *= h1
    p[:, :4] = xywh2xyxy(p[:, :4])

    # NMS
    kept = nms_single(p[:, :5], iou_thres)
    if len(kept) == 0:
        return []

    # scale back → 原始图像坐标
    boxes = kept[:, :4].copy()
    if ratio_pad is not None:
        boxes = scale_coords(boxes, img0shape, img1shape, ratio_pad)
    else:
        boxes[:, 0] = boxes[:, 0] * w0 / w1
        boxes[:, 1] = boxes[:, 1] * h0 / h1
        boxes[:, 2] = boxes[:, 2] * w0 / w1
        boxes[:, 3] = boxes[:, 3] * h0 / h1

    # → [(cx, cy, w, h, conf), ...]
    results = []
    for i in range(len(kept)):
        x1, y1, x2, y2 = boxes[i]
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
        w = x2 - x1
        h = y2 - y1
        results.append((float(cx), float(cy), float(w), float(h),
                        float(kept[i, 4])))
    return results


def draw_detect_res(img, detections):
    """在图像上绘制检测结果（调试用）"""
    img = img.copy().astype(np.uint8)
    for det in detections:
        cx, cy, w, h, conf = det
        x1 = int(cx - w / 2)
        y1 = int(cy - h / 2)
        x2 = int(cx + w / 2)
        y2 = int(cy + h / 2)
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 100), 2)
        cv2.circle(img, (int(cx), int(cy)), 3, (0, 255, 100), -1)
        cv2.putText(img, f"{CLASS_NAME} {conf:.2f}",
                    (x1, y1 - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 100), 1)
    return img
