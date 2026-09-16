#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
api_infer.py — SNPE GPU 推理封装（适配 YOLOv11 羽毛球检测）
"""

import sys, os, json, time, functools
import numpy as np

lib_path = os.getenv("FIBO_LIB", "")
if not lib_path:
    for candidate in [
        "/usr/local/lib/python3.8/dist-packages/fiboaisdk",
        os.path.expanduser("~/fiboaisdk_ubuntu_aarch64/lib"),
    ]:
        if os.path.isdir(candidate):
            lib_path = candidate
            break
if not lib_path:
    print("Please set FIBO_LIB or install fiboaisdk")
    sys.exit(1)
sys.path.append(lib_path)
from api_aisdk_py import api_infer_py


def timer(func):
    @functools.wraps(func)
    def wrapper_timer(*args, **kwargs):
        tic = time.perf_counter()
        value = func(*args, **kwargs)
        toc = time.perf_counter()
        elapsed = toc - tic
        print(f"{func.__name__}: {elapsed*1000:.2f}ms")
        return value
    return wrapper_timer


def _build_config(model_name, model_path, run_backend, output_names,
                  profile_level, log_level,
                  input_names=None, input_shapes=None):
    """构建 SNPE 推理配置（YOLOv11 单类模型专用）"""
    if input_names is None:
        input_names = ["images"]
    if input_shapes is None:
        input_shapes = [[1, 3, 640, 640]]

    return json.dumps({
        "name": "yolo_infer",
        "version": "1.0.0",
        "logger": {
            "log_level": log_level,
            "log_path": "snpe_infer.log",
            "pattern": "[%Y-%m-%d %H:%M:%S.%e] [%n] [%^---%L---%$] [thread %t]:%g %# %v",
            "max_size": 1048576,
            "max_count": 5,
            "enable_console": True,
            "enable_file": False
        },
        "device": {
            "board_ssid": "board_ssid",
            "board_name": "QCS6490",
            "board_manufacturer": "qualcomm",
            "board_type": "board_type",
            "board_version": "board_version",
            "board_arch": "aarch64",
            "board_os": "linux",
            "soc_name": "QCS6490",
            "soc_id": "QCS6490",
            "soc_ip": [
                {"ip_name": "A78", "family": "ARMv8", "type": "CPU",
                 "cores": 8, "frequency": 2400, "memory": 8192},
                {"ip_name": "Adreno643", "family": "Adreno600",
                 "type": "GPU", "cores": 4, "frequency": 800, "memory": 4096}
            ]
        },
        "infer_engine": {
            "name": "default_session",
            "version": "1.0.0",
            "strategy": 0,
            "batch_timeout": 1000,
            "engine_num": 1,
            "priority": 0
        },
        "all_models": [{
            "model_name": model_name,
            "model_size": "",
            "version": "1.0.0",
            "model_path": model_path,
            "model_type": "",
            "model_cache": False,
            "model_cache_path": "",
            "run_backend": run_backend,
            "run_framework": "snpe",
            "model_version": "1.0.0",
            "batch_size": 1,
            "output_names": output_names,
            "external_params": {"profile_level": profile_level}
        }],
        "graphs": [{
            "graph_name": "yolo_graph",
            "version": "1.0.0",
            "graph_params": "",
            "graph_input_names": input_names,
            "graph_input_shapes": input_shapes,
            "graph_input_types": ["float32"] * len(input_names),
            "graph_input_layouts": ["NCHW"] * len(input_names),
            "graph_output_names": output_names,
            "graph_output_shapes": [[-1]] * len(output_names),
            "graph_output_types": ["float32"] * len(output_names),
            "graph_output_layouts": ["NCHW"] * len(output_names),
            "all_nodes_params": {
                "nodes": [{
                    "node_name": "yolo_infer",
                    "node_type": "infer",
                    "version": "1.0.0",
                    "run_backend": run_backend,
                    "run_framework": "snpe",
                    "model_name": model_name,
                    "model_type": "all",
                    "net_type": "all",
                    "node_input_names": input_names,
                    "node_input_types": ["float32"] * len(input_names),
                    "node_input_shapes": input_shapes,
                    "node_input_layouts": ["NCHW"] * len(input_names),
                    "node_output_names": output_names,
                    "node_output_shapes": [[-1]] * len(output_names),
                    "node_output_types": ["float32"] * len(output_names),
                    "node_output_layouts": ["NCHW"] * len(output_names),
                    "extra_node_params": ""
                }]
            }
        }],
        "application": {
            "name": "yolo_shuttlecock",
            "version": "1.0.0",
            "description": "YOLOv11 badminton shuttlecock detection",
            "app_params": "",
            "input_algorithm_name": [],
            "output_algorithm_name": [],
            "all_algorithm_params": {
                "algorithms": [{
                    "algorithm_name": "yolo_detect",
                    "version": "1.0.0",
                    "type": "detection",
                    "size": "",
                    "description": "Shuttlecock detection",
                    "algorithm_params": "",
                    "input_graph_name": ["default_graph"],
                    "output_graph_name": ["default_graph"],
                    "all_graph_params": {}
                }]
            }
        }
    }, indent=2)


class LogLevel:
    TRACE = "trace"
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warn"
    ERROR = "error"
    CRITICAL = "critical"


class PerfProfile:
    BALANCED = 0
    HIGH_PERFORMANCE = 1
    POWER_SAVER = 2
    SYSTEM_SETTINGS = 3
    SUSTAINED_HIGH_PERFORMANCE = 4
    BURST = 5
    LOW_POWER_SAVER = 6
    HIGH_POWER_SAVER = 7
    LOW_BALANCED = 8
    EXTREME_POWERSAVER = 9


class Runtime:
    CPU = "CPU"
    GPU = "GPU"
    DSP = "DSP"


class SnpeContext:
    """SNPE 推理上下文（YOLOv11 羽毛球检测）"""

    def __init__(self,
                 dlc_path: str,
                 output_tensors: list = None,
                 runtime: str = Runtime.GPU,
                 profile_level: int = PerfProfile.BURST,
                 log_level: str = LogLevel.ERROR,
                 input_names: list = None,
                 input_shapes: list = None):
        self.m_dlcpath = dlc_path
        self.m_output_tensors = output_tensors or ["output0"]
        self.m_runtime = runtime
        self.profiling_level = profile_level
        self.log_level = log_level
        self.input_names = input_names or ["images"]
        self.input_shapes = input_shapes or [[1, 3, 640, 640]]
        self.m_context = api_infer_py.InferAPI()

    def Initialize(self):
        config = _build_config(
            model_name=self.m_dlcpath,
            model_path=self.m_dlcpath,
            run_backend=self.m_runtime,
            output_names=self.m_output_tensors,
            profile_level=self.profiling_level,
            log_level=self.log_level,
            input_names=self.input_names,
            input_shapes=self.input_shapes)
        return self.m_context.Init(config)

    def Execute(self, output_names, input_feed):
        """执行推理，返回输出字典。output_names=[] 表示获取所有输出。"""
        input_feed_flat = {
            k: v.astype(np.float32).flatten().tolist()
            for k, v in input_feed.items()}
        if self.m_context.Execute_float(input_feed_flat) == 0:
            return self.m_context.FetchOutputs_float(
                output_names or self.m_output_tensors)
        return None

    def Release(self):
        return self.m_context.Release()
