# 在其他项目中复用微信收发

微信适配器可单独使用，不需要加载 Codex SDK、HTTP 服务或任务数据库。它面向当前 Linux 微信的 AT-SPI2/X11 桌面环境；其他系统、微信版本可能需要重新适配。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `wechat.py` | 准备聊天窗口、读取消息快照、填写并发送、确认本机发出气泡 |
| `wechat_types.py` | 通用桌面参数、会话标识和消息类型，不含模型、任务或数据库配置 |
| `window.py`、`capture.py` | 按进程与标题定位窗口、后台读取窗口像素 |
| `domain.py` | 私聊/群 @ 触发规则、业务上下文、快照增量对比 |
| `store.py`、`service.py` | 任务与发送队列、数据库、重启恢复、会话间并发与桌面串行调度 |
| `codex_agent.py` | 官方 Codex SDK 调用、提示词与会话续接 |
| `formatting.py` | AI 回复的微信纯文本展示；底层发送保持原文 |
| `scheduling.py` | 定时语句识别、时区转换和下一次时间计算，不操作微信或启动模型 |

公开入口是 `WechatClient`（`AtspiWechat` 的别名）、`WechatOptions`、`ChatTarget` 和 `Message`。业务侧的 `Settings`、`Conversation` 通过转换方法生成通用参数；适配器不会反过来读取业务配置。

## 安装与最小示例

另一个项目可安装此目录：

```bash
python -m pip install /path/to/wxagentbot
```

基础 Python 依赖只有 Pillow，系统仍需 PyGObject、AT-SPI2、`xdotool` 和 X11 Composite 库。虚拟环境需能访问系统的 `gi` 包。完整助手的 `install.sh` 安装 `.[bridge]`，额外包含 PyYAML、Codex SDK、FastAPI 和 Uvicorn。

```python
from wxagentbot import ChatTarget, WechatClient, WechatOptions

client = WechatClient(WechatOptions(listen_mode="independent"))
chat = ChatTarget(id="friend", name="夜伴", kind="private")
try:
    client.prepare_windows([chat])
    for message in client.read(chat):
        print(message.direction, message.text)
    # 真正需要发送时再调用：
    # client.send(chat, "你好")
finally:
    client.close()
```

完整可运行示例见 [examples/wechat_only.py](../examples/wechat_only.py)。默认只读取所列聊天的消息快照，不调用 AI，也不自动回复：

```bash
.venv/bin/python examples/wechat_only.py
# 显式发送一条消息：
.venv/bin/python examples/wechat_only.py --send yeiban '你好'
```

可按自己的配置生成 `ChatTarget` 列表，无需沿用助手的 YAML 或数据库。

## 调用约定

- 同一个客户端的桌面操作应在同一线程串行执行；耗时业务可放到其他线程。异步项目可参考 `service.py` 使用单线程执行器。
- `read()` 返回当前可读取的完整消息快照，不是“仅新消息”事件流。首次基线、增量对比、去重、断点恢复由调用方负责；`Message.fingerprint()` 可用于比对。示例只在快照变化时打印，不能直接当生产消息队列使用。
- 适配器只提供识别到的消息字段，不决定是否执行任务、是否命中群 @ 或成员名单；这些规则在业务层。
- `send()` 按原文发送。Markdown 清理由 AI 业务调用 `wechat_plain_text()` 完成，外部主动推送也保持调用方原文。
- `NotSent` 表示本次未执行发送；`SendUncertain` 表示可能已经发送，应先核对微信，避免盲目重试。成功表示本机出现发出气泡，不能证明对方收到。
- 独立聊天窗口需要打开且未最小化，可以被遮挡。`prepare_windows()` 首次可能操作界面；正常轮询读取不会切换窗口，实际发送才激活目标窗口。界面操作尊重有效的人为桌面接管。

如果不通过包安装而复制源码，需要一起保留 `__init__.py`、`wechat.py`、`wechat_types.py`、`window.py`、`capture.py`，维持包结构与相对导入。传输层的隔离导入测试位于 `tests/test_reuse.py`。
