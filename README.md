# wxagentbot

Python 微信任务助手，使用 Linux AT-SPI2 读取微信控件，使用 **OpenAI 官方 `openai-codex` Python SDK** 执行任务，并把最终文本回复到原聊天。提供带 Token 鉴权的主动推送 HTTP API。本版只接入 Codex。

微信收发层可独立导入，不依赖模型、任务数据库或 HTTP 服务。其他项目接入见 [复用说明](docs/reuse.md) 和 [独立收发示例](examples/wechat_only.py)。

## 当前交付状态

- 已实现：配置、私聊/群聊触发规则、消息窗口增量对比、SQLite 任务及发送队列、不同会话并发及同会话顺序执行、独立定时调度、Codex SDK 适配、上下文续接、超时中断、分段回复、HTTP API、诊断及离线演示。
- 本机 Codex CLI 已登录 ChatGPT，SDK 默认复用现有认证；模型留空时使用账号默认配置。
- 已通过桌面工具确认当前微信暴露 `消息` 列表、消息条目、输入框与发送按钮。
- 运行依赖已安装，私聊已由桌面终端运行的服务完成真实消息接收 → Codex → 微信回复，天气查询也已实际发送。编码命令环境仍拒绝桌面 D-Bus 连接和 HTTP 端口绑定，需要在正常桌面终端启动；没有发布对外地址。详细记录见 [VALIDATION.md](VALIDATION.md)。
- `config.example.yml` 中的示例列表均未启用；本地 `config.yml` 保存实际监听列表及各群的 `self_names`。当前客户端不提供群发送人，已使用每条消息独立任务的兼容处理。`demo` 使用显式模拟对象，不会发微信，也不调用模型。
- 群聊独立任务模式已实际运行并完成回复。当前使用 `listen_mode="independent"`，从独立聊天窗口后台读取，用户已确认监听与发送恢复正常。本次新增的独立定时调度已通过离线回归，需要重启加载后验证真实微信收发。

## 首次运行

在这台机器的 Dashboard **Terminal** 中打开项目目录；程序应与微信属于同一个桌面用户。Python 3.11+，系统需要 PyGObject、AT-SPI2 和 `xdotool`（本机已有）。

```bash
cd ~/Desktop/wxagentbot
./install.sh
./run.sh check
./run.sh doctor
./run.sh codex-check
```

从 Git 仓库首次获取项目时，先执行 `cp config.example.yml config.yml` 并填写配置。本地 `config.yml`、密钥、数据库和会话工作目录均已加入 `.gitignore`；仓库保留 `config.example.yml` 作为模板。Python 包和安装后的命令均为 `wxagentbot`，也可执行 `python -m wxagentbot --help`。

`install.sh` 创建带 `system-site-packages` 的虚拟环境并安装依赖，生成 `.secrets/api-token`（600 权限，不会显示密钥）。若系统缺少 GI，需安装系统包 `python3-gi`、`gir1.2-atspi-2.0`，再重建带系统包访问权限的虚拟环境。

本机桌面环境通常是：

```bash
export DISPLAY=:1
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
```

设置变量不能解除运行环境的权限限制。如果 `doctor` 仍报告 `Operation not permitted`，需使用可以访问该桌面会话的运行环境；程序会在进入 libatspi 前返回诊断，避免原生库直接中止进程。

`codex-check` 连接官方 SDK 并读取账号状态，不调用模型；`codex-test` 会进行一次真实、简短的模型调用，不发送微信。默认使用官方 SDK 配套的 Codex runtime；只有明确需要指定兼容版本时才设置 `codex.binary`。

## 配置监听列表

配置使用 YAML，通过 PyYAML 的安全加载器解析。编辑 `config.yml` 的 `conversations` 列表，注意缩进：

```yaml
conversations:
  - id: "friend"
    name: "好友的准确备注名"
    kind: "private"
    enabled: true
    allow_push: true
    workspace: "workspaces/friend"

  - id: "team"
    name: "群的准确名称"
    kind: "group"
    enabled: true
    allow_push: true
    workspace: "workspaces/team"
    self_names: ["我在这个群的昵称"]
    mention_mode: "metadata"
    missing_sender_policy: "ignore"
    allowed_senders: []
```

默认加载 `config.yml`；也可用 `./run.sh --config /path/to/custom.yaml check` 指定其他 YAML 文件。重复配置项、格式错误或不符合结构的列表会报错。路径相对于配置文件所在目录解析。

- `enabled` 控制是否监听并自动执行；`allow_push` 独立控制 HTTP 主动发送。允许只推送、不自动执行。
- 私聊只处理对方的新增文本；群聊只处理提及本人的新增文本。群发送人缺失时默认忽略；显式设置 `missing_sender_policy="isolated"` 后，每条提及消息可用独立上下文执行。配置了 `allowed_senders` 时仍须识别发送人并命中名单。
- `id` 是本地唯一别名。名称或别名冲突会拒绝加载；运行时无法唯一定位同名会话会停止该次操作。建议先在微信设置唯一备注名/群名。
- 私聊上下文按会话保存；群聊已知发送人时按“群＋发送人”保存，并使用成员各自的子目录。缺少发送人时，`isolated` 模式按唯一消息事件创建 `tasks/` 下的独立目录，不保存或续接 SDK 会话；下一条消息需提供完整任务信息。不同会话的写入工作目录不能重叠。
- SDK 的 `workspace_write` 限定写入范围，读取范围由 Codex 沙箱及账户策略决定；工作目录本身不是完整的多用户安全隔离。
- `codex.approval_mode="deny_all"` 使需要额外权限的动作被拒绝；支持显式配置 `auto_review`。模型、超时、说明文字和 `read_only/workspace_write` 均可配置。
- 在会话中发送 `/new`（群聊仍需提及本人）重置该上下文。

修改配置后先 `./run.sh check`。运行时可调用 `/v1/config/reload` 重载会话列表，其他参数需要重启；存在执行中任务或被修改会话的排队任务时会返回冲突。

## 微信回复格式与提示词

当前 `codex` 配置：

```yaml
codex:
  instructions_file: "prompts/wechat.txt"
  reply_format: "plain"
```

[prompts/wechat.txt](prompts/wechat.txt) 用于补充微信回复提示词，要求简洁中文、自然段、换行和编号，避免加粗、标题、表格和代码围栏。它与 `codex.instructions` 一起作为 SDK 的 `developer_instructions` 传入。文件内容会在每条新任务开始时读取；修改该文件的文字不需要重启。修改文件路径、`reply_format` 或其他 YAML 参数仍需重启。提示词变化会使用新的模型会话，避免续接旧的输出规则。

`plain` 还会在入发送队列前处理常见 Markdown：去掉正文加粗及标题标记、把表格转换为逐项文字、保留完整链接；代码围栏内的语法与缩进、行内代码内容及运算符会保留。它处理常见格式，不是完整的 Markdown 渲染器。数据库任务结果仍保存模型原文，发送记录保存实际显示的文字。

该处理只作用于 Codex 最终回复。微信底层 `send()` 和 HTTP 主动推送按调用方原文发送，便于其他项目复用。需要关闭回复清理时设置 `reply_format="raw"`，并自行调整提示词文件中的格式要求。

更新到本版代码后先在原终端按 `Ctrl+C`，再执行 `./start.sh` 加载；本次尚未通过真实新消息验证更新后的输出格式。

## 先校准微信，再启用监听

```bash
./run.sh dump-tree
./run.sh inspect friend
./run.sh inspect team
```

`dump-tree` 保存节点类型、名称、状态、坐标及属性到 `data/wechat-tree.json`（600 权限）。文件可能含聊天内容，已被 Git 忽略。`inspect` 会切换到指定聊天，打印解析结果，但不会发送消息或调用 Codex。

群聊每次读取后会更新 `data/diagnostics/会话ID.json`，保留最近最多 20 条消息的解析结果、原始节点属性及描述，以及实际加载的 `missing_sender_policy`。该文件可能包含聊天内容，权限为 600，目录已被 Git 忽略。`isolated` 模式保留发送人为空的真实状态，并为每条消息分配独立上下文。

应确认：

1. 会话标题、私人/群聊类型正确；群标题需暴露人数标记，如 `群名(5)`。
2. 消息的 `direction` 能区分 `incoming/outgoing`。如果控件没有方向属性，会通过可选的截图头像位置判断；看不清或无法截图时保持 `unknown` 并忽略，避免把自己的消息当任务。
3. 检查群消息是否能读到 `sender`。若发送人和正文在同一文本节点中，可设置 `wechat.sender_regex` 的 `sender`、`text` 命名组；没有可靠发送人时，默认 `missing_sender_policy="ignore"`。需要兼容这种客户端时可设为 `isolated`；每条消息独立执行，且无法使用成员白名单。
4. `mention_mode="metadata"` 需要客户端确实暴露 `mentions` 属性，当前微信版本是否提供它尚未验证。默认不将普通 `@昵称` 文字认作真实提及。如果实测只能读文字，可显式设为 `text`：匹配 `@群昵称` 加空白边界，引用或手打同样文本仍可能误触发。

无法从界面推断的消息字段不会由模型猜测。客户端版本不同可能需要调整适配器；无障碍树不提供完整历史消息、稳定服务端消息 ID 或可靠送达回执。

## 启动、停止和后台运行

桌面有“启动 wxagentbot”入口，或在正常桌面 Terminal 中运行 `./start.sh`。
该脚本会补装缺失依赖、检查桌面连接及监听列表，再启动服务；不会在检查失败时报告已开始监听。

```bash
./run.sh serve
```

默认地址 `http://127.0.0.1:8765`，`Ctrl+C` 停止。`./run.sh listen` 可仅启动监听和队列，不启动 HTTP 接口。

可参考 `scripts/wxagentbot.service.example` 创建桌面用户的 systemd 服务。需要登录态微信和同一桌面总线；不要把服务放到无法访问桌面的容器或受限进程里。

微信读取、切换和发送在一个操作线程串行运行；通过进程和窗口标题校验目标。启动准备和发送尊重 Dashboard 桌面接管状态，后台只读操作可以继续。

## 不同会话并发处理

```yaml
codex:
  max_parallel_conversations: 3
```

默认最多同时执行 3 个不同会话的 AI 任务，支持 1～16；设为 1 恢复全局串行。修改后需要重启服务。

同一个私聊或群聊始终按入队顺序执行。即使群内发送人不同，或使用 `isolated` 为每条消息创建独立上下文，同群也只执行一个任务。领取任务时会跳过已有任务运行的会话，让其他会话继续执行。并发上限满时，新增任务保持排队。

各任务独立管理 SDK 客户端、模型轮次、超时和取消；取消一个任务不会中断其他窗口。微信控件读取与实际发送仍通过原来的单线程串行完成，同一会话的回复按任务完成顺序进入发送队列。程序停止时会等待所有执行任务清理，运行中的任务记为 `interrupted`，未开始的任务保留在队列中。

`GET /v1/status` 的 `active_tasks` 返回全部执行中的任务，每项包含 `id` 和 `conversation`；`max_parallel_conversations` 显示并发上限。保留的 `active_task` 兼容字段只显示最早仍活动的一个任务。配置重载在任意任务运行期间均会拒绝，需等任务结束或取消后再操作。

预约的登记、查询、取消由宿主即时处理，等待时间不占用会话或 AI 并发名额。到期后的实际执行加入对应会话队列，仍遵循同会话顺序执行和模型超时限制。

## 定时任务

私聊直接发送请求，群聊仍需 @自己配置的群昵称。例如：

```text
13点50分播报广州天气
明天下午两点提醒我开会
10分钟后提醒我喝水
下周一上午9点提醒我交材料
每天早上8点播报广州天气
每周五下午5点提醒我写周报
每隔30分钟提醒我休息
```

明确的时间表达由 Python 直接识别，不调用模型来等待。保存预约与确认消息在同一个数据库事务中完成，随后立即进入发送队列。确认消息包含完整执行日期、时区和预约编号。等待期间，同群的其他问题可以继续处理；到期后才调用 Codex 查询天气或执行任务，结果回复原会话。

“查一下明天广州天气”“13:50的日志帮我分析一下”属于即时查询，不会因内容含有时间而创建预约。支持常见中文钟点、`HH:MM[:SS]`、明确日期、相对延迟、每日、工作日、每周及固定间隔。周期任务的固定间隔至少 60 秒。日期已过、只有“明天提醒我”而没有钟点等表达会要求补充完整时间和任务；复杂、不明确的预约不会由模型自行启动等待进程。

默认按北京时间解析，不受机器本地时区影响：

```yaml
scheduler:
  timezone: "Asia/Shanghai"
  poll_seconds: 1.0
  misfire_grace_seconds: 300
```

`timezone` 可设置为其他 IANA 时区；修改调度配置后需要重启。单次预约的日期与周期规则在登记时确定。预约使用独立的执行上下文，任务内容应写清楚，不依赖登记后继续变化的聊天内容。模型提示词禁止通过 `sleep`、循环检查时间或自行创建后台进程来等待未来时刻。

在原会话中管理预约，群聊仍需 @昵称：

```text
/schedules
/cancel-schedule 预约编号
```

也支持“查看定时任务”和“取消定时任务 预约编号”。编号可使用确认消息中的前 8 位；如果不唯一，需提供更长编号。只能通过微信命令管理当前会话的预约。当前客户端不能识别群成员时，管理范围是整个群，不能提供按成员的预约权限隔离。

预约保存在 SQLite，重启后继续等待；领取到期任务和记录触发状态在一个事务中完成，重复轮询不会重复创建同一次任务。周期任务的上次执行尚未结束时合并触发，不积压大量重复执行。已经运行但因停止服务而中断的任务不会自动重跑。

程序停止、微信退出或机器暂停期间无法按时发送。恢复后，超出 `misfire_grace_seconds` 的未执行任务记为 `missed` 并通知原会话，不补跑过期天气查询；周期任务跳到下一次未来时间。到点触发后的实际发送仍受同会话排队、模型查询和微信发送耗时影响，不保证秒级准时。会话停用、目标名称变化或成员权限不再允许时，预约会停用，避免回复到错误目标。

`/v1/status` 的 `schedules` 显示各预约状态数量，`scheduler_error` 显示调度故障。一次性预约会经过 `scheduled`（等待）、`triggered`（已入队）再到 `completed` 等结束状态。周期预约保持 `scheduled`，通过 `last_task_id` 查看最近一次执行。

## 静默监听

当前 `config.yml` 的 `wechat` 配置已设置：

```yaml
wechat:
  listen_mode: "independent"
```

启动时会一次性双击监听列表中的会话，打开缺少的独立聊天窗口；已经打开的会话直接复用。准备完成后，监听只读取各独立窗口的控件树和后台像素，不激活窗口、不切换主窗口会话，也不移动鼠标。发送最终回复时才激活对应聊天，校验目标与草稿并发送。输入框按聊天区域的布局定位，兼容名称为“输入”或会话名的客户端。

独立窗口需要保持打开且未最小化，可被其他窗口遮住。后台像素用于区分收发方向，正文来自完整消息列表；不会用侧栏截断的最后一条摘要替代正文。主窗口可以留在其他聊天或最小化，程序不会在定时读取时把它置前。

窗口被关闭、最小化、身份不唯一或后台捕获不可用时，该会话会记录读取错误，并继续监听其他可用会话。不会为恢复读取而反复点击或置前窗口。启动准备失败时也停止重复尝试；手动打开独立窗口后可重启，或调用配置重载重新准备。`/v1/status` 的 `scan_errors`、`listen_mode`、`windows_prepared` 可用于诊断。

该模式使用本机已有的 X11 Composite 库，`doctor` 会检查库是否安装。切换模式需要重启。`config.example.yml` 保留 `listen_mode="active"`，该模式会切换主窗口读取完整聊天，并在需要时恢复最小化窗口。需要使用旧模式时可显式改回 `active`。

机器暂停、停止或微信退出期间不能收发消息。停止后进程结束，需重新启动服务；用户 systemd 服务可负责恢复。

## 主动推送 API

所有 `/v1/*` 接口需要：

```text
Authorization: Bearer <.secrets/api-token 中的值>
```

`POST /v1/messages/send`：

```json
{"conversation": "friend", "message": "任务已完成，请查看结果。"}
```

`conversation` 可使用配置 `id` 或唯一名称。消息最长 16000 字；请求体最长 64 KiB。可带 `Idempotency-Key`（1～128 字符）；同一个键同一内容返回原记录，内容或目标不同返回 409。

返回 `202`：

```json
{"id": "发送记录ID", "status": "queued", "status_url": "/v1/messages/发送记录ID"}
```

`202` 表示已入队。查询 `status_url` 获取 `queued/sending/sent/failed/unknown`。`sent` 表示在本机界面确认新增的发出气泡，不能证明对方已经收到。

示例客户端自动读取 Token，避免在命令行参数里放密钥：

```bash
.venv/bin/python examples/push.py --conversation friend --message '提醒：任务完成' --wait
# 外部机器使用发布后的 HTTPS 地址，并通过 BRIDGE_API_TOKEN 环境变量提供 Token：
python3 examples/push.py --base-url https://你的地址 --conversation friend --message '外部通知'
```

其他接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/healthz` | 进程存活检查，返回最小信息 |
| GET | `/v1/status` | 微信连接、队列、诊断与当前任务 |
| GET | `/v1/conversations` | 查看配置列表 |
| GET | `/v1/messages/{id}` | 查询发送状态 |
| GET | `/v1/tasks` | 最近 30 条任务 |
| GET | `/v1/tasks/{id}` | 查询任务及最终结果、发送记录 ID |
| POST | `/v1/tasks/{id}/cancel` | 取消排队或执行中的任务 |
| GET | `/v1/schedules` | 查看预约列表，最多 200 条 |
| GET | `/v1/schedules/{id}` | 查询预约及最近执行任务 ID |
| POST | `/v1/schedules/{id}/cancel` | 取消预约及尚未完成的执行 |
| POST | `/v1/config/reload` | 校验并重载会话列表 |

取消会中断 Codex，但已发生的文件修改不会自动回滚。

本机运行服务后，可使用 `./publish.sh` 通过 instabox 发布端口获得外部访问地址。服务始终需要应用层 Token；发布地址只在机器运行时可用。本次交付没有执行发布。

## 恢复、去重和日志

- 首次看到一个聊天时建立基线，不执行已有历史消息。之后对齐上次窗口尾部，保留连续相同文本的新增条目。
- 窗口没有重叠时重新建立基线并在 `/v1/status.scan_warnings` 记录缺口，避免把历史批量当成新任务。因此不能保证高频聊天无损，也无法区分所有“同文替换且窗口不增长”的情况。
- 消息游标和新任务在同一 SQLite 事务中提交；最终回复和发送记录也一起提交。已完成的 Codex 任务不会因发送失败而重新执行。
- 中断时的 `running` 任务恢复为 `interrupted`，发送中的记录恢复为 `unknown`，均不自动重跑或重发。
- 对确认尚未发送的 `failed` 记录，可执行 `./run.sh retry-message 发送记录ID` 重新入队，复用已有回复；`unknown` 和 `sent` 不允许通过此命令重试。
- 需要先更新运行代码时，可执行 `./run.sh retry-message 发送记录ID --after-restart`，只登记请求，在服务下次启动时补发。若启动前该记录已经发送或变为不确定状态，则跳过。
- 分段回复逐段记录进度；`unknown` 需先核对微信，避免重复发送。`failed` 表示本次未能执行发送。修正问题后，可用新的幂等键主动推送已保存的任务结果。
- 日志默认写到标准错误，仅包含会话 ID、任务 ID 和异常类型，不记录 Token 或聊天正文。SQLite 包含任务正文和结果，请按私人数据管理。

## 测试与离线演示

```bash
./run.sh demo
.venv/bin/python -m unittest discover -s tests -v
```

核心测试使用标准库，覆盖触发规则、重复文本、上下文、重启恢复、会话间并发、同群顺序执行、并发限额、独立取消和超时、定时识别、登记不阻塞同群、到期去重、错过时间及旧数据库迁移、发送异常和桌面串行操作。SDK 测试使用与官方 API 签名匹配的替身；不表示真实模型调用成功。HTTP 测试需额外安装 `httpx`，缺少 FastAPI/httpx 时会明确跳过：

```bash
uv pip install --python .venv/bin/python httpx
```

官方参考：[Codex Python SDK](https://learn.chatgpt.com/docs/codex-sdk)、[AT-SPI2 Accessible](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/class.Accessible.html)、[AT-SPI2 事件](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/method.EventListener.register.html)。

## 空输入框被报告为草稿，或桌面接管后一直暂停

已修正 PyGObject 的 `Accessible.get_text()` 与 `Text.get_text()` 方法重名问题：输入框读取使用显式的 `Atspi.Text` 接口，空输入框按字符数判定，不使用会话名称代替内容。真实草稿仍会保留。

桌面接管遵循 instabox 的 30 秒心跳过期规则。有效接管期间暂停，交还控制或心跳过期后恢复；程序不会修改接管标记。更新代码后需重启已运行的服务才能加载修复。
