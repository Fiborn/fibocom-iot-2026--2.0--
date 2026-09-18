# SC171 — 边缘计算核心

SC171（广和通 QCS6490）是系统的边缘计算核心，部署于比赛现场，负责高帧率双目图像采集、羽毛球目标检测、双摄立体三角化，并将结构化 3D 轨迹上传至阿里云。

## 目录结构

| 文件 | 作用 |
|------|------|
| `sc171_pipeline_gamadj.py` | 主程序：GPU(SNPE/DLC) 双摄推理 → 红色闪烁灯时间对齐 → 立体三角化 → HTTP 上传阿里云 |
| `api_infer.py` | SNPE GPU 推理封装（SnpeContext，支持 GPU/CPU/DSP） |
| `utils.py` | 图像预处理（preprocess_letterbox）与检测后处理（detect_postprocess） |
| `v4l2_grab.c` | C 语言 V4L2 采集程序，底层调用 Linux 设备接口实现高速抓帧 |
| `gpio_util.py` | GPIO 控制，驱动三色 LED 状态显示（绿=开始 / 红=结束 / 蓝=挑战） |
| `calibrate_sc171.py` | 双目棋盘格标定脚本，计算左右内外参、畸变与相对位姿 |
| `calib_netpos.py` | 网带位置校准工具（Flask Web 实时预览辅助物理对齐） |
| `badminton.env` | 运行配置（阿里云上传地址、API Key、模型路径等） |
| `stereo_params.npz` | 双目标定参数（内参、畸变、旋转平移、投影矩阵） |
| `yolo_v11/` | YOLO 模型文件（`best_final.onnx` / `.dlc` / `.pt`） |

## 运行说明

1. 在同目录创建 `badminton.env`，配置阿里云上传地址与 API Key。
2. 运行 `python3 sc171_pipeline_gamadj.py`。
3. 模型与标定文件按 `badminton.env` 指定路径放置，依赖 SC171 的 SNPE 运行库。
