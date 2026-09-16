"""SC171 GPIO 控制工具（sc171_gpio_ctrl 接口）"""
import threading, time, os

GPIO_CTRL = "/sys/class/sc171_gpio_class/sc171_gpio_dev/sc171_gpio_ctrl"

def gpio_pulse(pin: int, duration_ms: int = 2000):
    """通过 sc171_gpio_ctrl 将 pin 置低 duration_ms 毫秒后恢复"""
    def _pulse():
        try:
            if not os.path.exists(GPIO_CTRL):
                print(f"  [GPIO{pin}] ✗ {GPIO_CTRL} not found", flush=True)
                return
            with open(GPIO_CTRL, "w") as f:
                f.write(f"{pin},0\n")
            time.sleep(duration_ms / 1000.0)
            with open(GPIO_CTRL, "w") as f:
                f.write(f"{pin},1\n")
            print(f"  [GPIO{pin}] ✓ pulse {duration_ms}ms", flush=True)
        except Exception as e:
            print(f"  [GPIO{pin}] ✗ {e}", flush=True)

    threading.Thread(target=_pulse, daemon=True).start()
