# mx_eye

[English](README.md) | 中文

首个集成的 MXBI 瞳孔 / 角膜反射追踪器，配有独立的接收端 SDK 与客户端。
需要 Python 3.11+；桌面 UI 面向 Windows/Linux。

本仓库是一个 uv workspace，包含四个包：`mx-eye`（追踪器）、
`mx-eye-client`（远程客户端）、`py-mx-eye`（Python SDK）和
`mx-eye-protocol`（它们共享的线格式）。

## 安装与运行

在仓库根目录执行一次安装：

```bash
uv sync --all-extras
```

启动追踪器，并在第二个终端中启动远程客户端：

```bash
uv run mx-eye
uv run mx-eye-receiver
```

若想在没有摄像头的情况下先做一次检查：

```bash
uv run mx-eye --demo
```

先在追踪器中点击 **Start**，再在客户端点击 **Connect**：SDK 的数据端口只在
会话出流期间存在，先连接会直接报连接被拒。客户端的 **Start tracker** 与
**Stop tracker** 按钮通过控制端口操作同一个会话；当会话已在运行时，START 是幂等的。

## 时钟同步

追踪器与消费端设备通过系统级 NTP 共用一个时基；时延与 age 读数直接对报文
时间戳做减法。在消费端设备作为 NTP server 的网线直连场景下，在 mx_eye 侧
设备上执行一次（Raspberry Pi OS/Debian，chrony）：

```bash
sudo scripts/install-chrony-client.sh 192.168.50.1   # 消费端设备地址
scripts/check-chrony-client.sh                       # 等待并校验同步
```

更多细节见 AGENTS.md。

## SDK

SDK 即 `py-mx-eye` 包；`uv sync --all-extras` 会安装它。SDK 不拥有任何线程：
每次调用都跑在调用方线程上，因此自己持有线程的消费端始终掌控它。

```python
from py_mx_eye import MxEye, MxEyeConfig

# with 块负责打开样本流，并在退出时确保释放
with MxEye(MxEyeConfig(host="127.0.0.1")) as eye:
    for sample in eye.read():  # 在本线程上等待每一个样本
        x, y = sample.frame.payload.x, sample.frame.payload.y
```

`MxEyeConfig` 是承载连接参数的 frozen dataclass，因此构造函数只需要一个入参。
`with` 只包裹样本流：由 `connect()` 打开（数据端口只在会话运行期间存在）、由
`close()` 释放；采集始终通过 `start()`/`stop()` 显式控制，退出 `with` 不会停止追踪器。

`read()` 只产出仍然是"当前测量"的样本：丢失、过期、无效的样本在等待过程中被跳过。
`timeout` 限制的是**每次等待**而不是整个循环，等待超时即结束循环：因此 `read(0)`
是不阻塞的排空（GUI 定时器用这个），有限 timeout 还会在该时长的静默后结束循环，
默认则无限等待。传入 `max_age_ms=None, require_valid=False` 则产出下一个到达的样本，
适合绘图与诊断。

错误以异常形式暴露，而不是静默状态：追踪器未出流时 `connect()` 抛错；流被关闭或
出错时抛 `ConnectionError`（重新 `connect()` 即可恢复）；帧格式非法时抛 `ValueError`。
坐标是未标定的源图像像素。一个可运行的示例：
`uv run python -m mx_eye_client.receive_minimal`。更多细节见 AGENTS.md。
