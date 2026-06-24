# Muse S 智能头带测试项目

基于开源工具链的 Muse S EEG 头带实时数据采集与可视化。

## 功能

- **蓝牙连接** Muse S 头带，启动 LSL 数据流（EEG + PPG + ACC + GYRO）
- **实时波形** 4 通道 EEG 滚动显示（5 秒窗口可调）
- **Band Power** Delta / Theta / Alpha / Beta / Gamma 频段功率实时柱状图
- **模拟模式** 无需硬件即可测试可视化界面

## 环境要求

| 依赖 | 版本 |
|------|------|
| Python | ≥ 3.10 |
| Windows | 10+ (64-bit) |
| 蓝牙 | BLE 4.0+ |

Python 包（已安装）：
```
muselsl >= 2.4.0
pylsl >= 1.16.0
numpy >= 1.24.0
scipy >= 1.10.0
matplotlib >= 3.6.0
```

## 项目结构

```
dev/muse/
├── README.md           # 本文件
├── requirements.txt    # Python 依赖
├── band_power.py       # Band Power 计算模块
├── stream_muse.py      # Muse 蓝牙连接 + LSL 流启动
└── lsl_viewer.py       # 实时可视化（波形 + Band Power）
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2a. 模拟模式（无 Muse 设备测试）

```bash
python lsl_viewer.py --simulate
```

无需任何硬件，立即看到实时 EEG 波形和 Band Power 变化。

### 2b. 真机模式

**终端 1 — 启动 LSL 流：**

```bash
# 自动扫描并连接第一个 Muse
python stream_muse.py

# 或指定设备
python stream_muse.py --name MuseS-XXXX

# 查看所有可用命令
python stream_muse.py --help
```

**终端 2 — 启动可视化：**

```bash
python lsl_viewer.py
```

### 3. 停止

- 关闭可视化窗口，或按 `Ctrl+C` 终止两个终端。

## 使用说明

### stream_muse.py

```
python stream_muse.py [OPTIONS]

Options:
  -a, --address MAC     按 MAC 地址连接
  -n, --name NAME       按设备名连接
  -b, --backend BACKEND BLE 后端 (auto/bleak/bluemuse/bgapi)
  --preset PRESET       流预设 (Muse S 推荐 p21)
  --ppg/--acc/--gyro    启用心率/加速度/陀螺仪
  --disable-eeg         禁用 EEG
  --disable-light       关闭头带 LED
  -r, --retries N       连接重试次数 (默认 3)
```

### lsl_viewer.py

```
python lsl_viewer.py [OPTIONS]

Options:
  --simulate            模拟模式（合成 EEG 数据）
  -w, --window SECONDS  波形窗口时长 (默认 5s)
  -r, --refresh MS      动画刷新间隔 (默认 50ms)
  -s, --stream NAME     LSL 流名称 (默认 "Muse")
  -t, --timeout SECONDS LSL 发现超时 (默认 5s)
```

### band_power.py（作为库使用）

```python
from band_power import compute_band_power
import numpy as np

# data: (n_samples, n_channels), sfreq: 256 Hz
bp = compute_band_power(eeg_data, sfreq=256.0)

# 获取 Alpha 频段相对功率
alpha_rel = bp["alpha"]["rel"]  # shape: (n_channels,)
print(f"TP9 alpha: {alpha_rel[0]:.3f}")
```

## 频段说明

| 频段 | 频率范围 | 相关状态 |
|------|---------|---------|
| Delta | 0.5–4 Hz | 深度睡眠、无意识 |
| Theta | 4–8 Hz | 冥想、浅睡、创造力 |
| Alpha | 8–12 Hz | 放松、闭眼静息 |
| Beta | 12–30 Hz | 活跃思考、专注、警觉 |
| Gamma | 30–45 Hz | 高级认知、信息整合 |

## 常见问题

### 蓝牙扫描不到 Muse 设备

1. 确保 Muse S 已开机（短按一次，LED 闪烁）
2. 检查 Windows 蓝牙是否开启
3. 先在 Windows 设置 > 蓝牙中添加 Muse S 设备
4. 尝试不同 BLE 后端：`python stream_muse.py --backend bluemuse`

### LSL 流找不到

- 确认 `stream_muse.py` 在另一个终端正在运行
- 等待 5-10 秒让流稳定
- 检查是否出现 "Streaming..." 提示

### 可视化窗口闪烁/卡顿

- 增大刷新间隔：`python lsl_viewer.py --refresh 100`
- 缩小波形窗口：`python lsl_viewer.py --window 3`

## 参考资源

- [muselsl PyPI](https://pypi.org/project/muselsl/)
- [muse-lsl GitHub](https://github.com/alexandrebarachant/muse-lsl)
- [Lab Streaming Layer](https://github.com/sccn/labstreaminglayer)
- [Petal Metrics](https://petal.tech/) (商业替代，需订阅)
