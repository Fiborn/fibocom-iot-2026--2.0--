# ESP32-P4 — 裁判终端

ESP32-P4 触摸显示终端，作为裁判人机交互设备，负责比赛计分、挑战申请、判罚动画播放，并通过 WiFi 与 SC171 进行 UDP 控制通信、从云端 HTTP 拉取判罚动画帧。

## 目录结构

| 路径 | 作用 |
|------|------|
| `main/app_main.c` | 程序入口，任务初始化 |
| `main/ui.c` / `ui.h` | 触摸屏计分界面（得分、局次、挑战次数显示） |
| `main/animation_player.c` / `.h` | 判罚动画播放（双缓冲乒乓切换） |
| `main/cloud_player.c` / `.h` | 云端判罚动画拉取与播放 |
| `main/http_frames.c` / `.h` | HTTP 帧下载（manifest + JPEG） |
| `main/sc171_udp.c` / `.h` | 与 SC171 的 UDP 控制通信（开始/结束/挑战） |
| `main/time_sync.c` / `.h` | 时间同步 |
| `main/wifi_manager.c` / `.h` | WiFi 连接管理 |
| `main/Kconfig.projbuild` | 工程配置项（服务器地址、WiFi 等） |
| `main/challenge.jpg`、`challenge_anim/`、`scoreboard.rgb565` | UI 图片与计分板资源 |
| `main/CMakeLists.txt`、`idf_component.yml` | 构建与依赖配置 |
| `tools/` | 资源生成工具（`build_*_asset.py`、`pack_animation.py`，`frames/` 原始帧） |
| `sdkconfig`、`sdkconfig.defaults*` | ESP-IDF 配置（芯片版本、Flash/PSRAM、WiFi 等） |

## 构建与烧录

1. 配置 `main/Kconfig.projbuild` 中的服务器地址与 WiFi 凭据。
2. `idf.py build` 编译，`idf.py flash` 烧录。
3. 开发板 v1.4 经 CP2102N（UART0）烧录，v1.5.2 经内置 USB Serial/JTAG 烧录。
