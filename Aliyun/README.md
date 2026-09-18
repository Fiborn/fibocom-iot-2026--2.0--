# Aliyun — 云端判罚与渲染服务

云端服务器，接收 SC171 上传的三角化 3D 轨迹，完成轨迹过滤、过网击球判定、二维/三维渲染，并向 ESP32-P4 裁判端与网页观众端分发结果。

## 目录结构

| 文件 | 作用 |
|------|------|
| `aliyun_pipeline.py` | 主服务器（Flask）：接收上传 → 过滤 → 2D 动画 → 抽帧给 P4 → 3D HTML |
| `analysis.py` | 过网击球检测（3D 轨迹版）：去噪 + Z 轴折返判罚 + 网侧一致性 |
| `render_video_v2_Gausmo.py` | 2D 轨迹动画渲染（X/Z 俯视图，高斯平滑），输出 MP4 |
| `render_3d_smooth.py` | 3D 轨迹高斯平滑渲染，输出交互式 HTML |
| `.env.example` | 环境变量示例（密钥、端口、路径等） |
| `court/` | 观众端三维球场 C++ 工程（详见下） |

### court/ 子工程

| 路径 | 作用 |
|------|------|
| `src/` | 球场/球网几何建模与渲染源码（`court_model.cpp`、`net_model.cpp`、`main.cpp` 等） |
| `output/` | 导出的球场模型（`*.obj` / `*.mtl`） |
| `court_v1.glb` | 三维球场模型（供观众端场景加载） |
| `glm/`、`GLFW/`、`glad/` | 第三方数学与窗口库 |
| `CMakeLists.txt`、`build_and_run.bat` | 构建脚本 |

## 坐标约定

- xOz 平面 = 球场地面（x 横向、z 纵向/深度）
- y 方向 = 网柱方向（竖直高度）
- 球网位于 z=0 平面，网高 1.55m

## 运行说明

启动主服务器：`python aliyun_pipeline.py [--port 5000]`（依赖 Flask、OpenCV、NumPy）。
