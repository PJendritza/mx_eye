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

在追踪器中点击 **Start**。在客户端中点击 **Connect**；客户端的
**Start tracker** 和 **Stop tracker** 按钮控制同一个会话。
当会话已在运行时，START 是幂等的。也可以先连接客户端，再从中启动会话。

## SDK

SDK 即 `py-mx-eye` 包；`uv sync --all-extras` 会安装它。

```python
from py_mx_eye import Client

with Client("127.0.0.1") as eye:
    eye.start()
    # Inside your behavioral-task loop:
    sample = eye.latest(max_age_ms=50)
    if sample is not None:
        x, y = sample.frame.payload.x, sample.frame.payload.y
    # At the end of the session:
    eye.stop()
```

在追踪与时钟同步就绪之前，`latest()` 返回 None；坐标是未标定的源图像像素。
一个可运行的示例：`uv run python -m mx_eye_client.receive_minimal`。
更多细节见 AGENTS.md。
