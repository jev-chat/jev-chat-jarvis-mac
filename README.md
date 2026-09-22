# jev-chat-jarvis（macOS）

微信弹出一条消息 → 悬浮窗立刻告诉你**这句话的真实意图**、**风险几级**、**该怎么回**。

**纯只读、零封号风险**——不注入、不 hook、不解密数据库，只是「看屏幕 + 本地模型判断」。

![演示：微信群消息进来 → 面板给出意图、风险分级与候选回复 → 点「填入」直接进微信输入框](docs/demo.gif)

## 交流反馈

用着有问题、想提需求、想一起改，扫码进群（二维码 7 天失效，过期了在 issue 说一声）：

<img src="docs/wechat-group.png" width="200" alt="扫码加入微信交流群">

## 它能做什么

- **意图 + 风险**：8 类意图零样本 **86.4%**（22 条回归口径），风险 0–9 分级 + 行动建议，本地模型一次前向出全分布
- **候选回复**：内置 10 种话术并发生成（每条一稳一放各出 2 条）→ 先上屏 → 本地模型排序后原位重排；换话术立刻按当前消息重新生成
- **快**：消息一出现判断 + 生成同时起跑，M1 Pro 出意图 ~1.5 s、出候选 ~1.5–2 s（端到端为机制推算口径，以日志实测为准）
- **YOLO 检测框**（可选，`JEV_BOXES=1` 启动即开、菜单栏可切）：OCR 命中的消息实时框在微信窗口上，对方/我分色 + 置信度

## 用法

**只想用**：[Releases](https://github.com/jev-chat/jev-chat-jarvis-mac/releases) 下载 `.app`，解压拖进「应用程序」，**第一次右键 → 打开**（没做公证，双击会被 Gatekeeper 拦）。

首次启动按提示授予「屏幕录制」权限（系统设置 › 隐私与安全性 › 录屏与系统录音，给 **jev-jarvis** 打开），**退出重开**生效；「填入」另需「辅助功能」权限，第一次点会弹系统授权框。v0.3.1 及更早的旧版本还需把 **python3.12** 那条一并打开。

缺少可用的 uv 时，两种启动入口都先完整下载并执行官方安装脚本（下载含超时和重试），失败后尝试已有的 Homebrew。失败提示区分网络、证书、磁盘和安装器错误，详细输出见 `~/Library/Logs/jev-jarvis.log`。官方脚本安装到 `~/.local/bin`，不修改 shell 配置。

**从源码跑**（微信在运行、终端已授予屏幕录制）：`./start.command`。分层自测：

```bash
uv run python src/perception.py                  # 感知层：识别到的消息 + 耗时
uv run python src/judge.py "这个需求你今天跟一下"  # 单条消息出判断
uv run python src/judge_zh_test.py               # 22 条中文意图回归
uv run python src/generate.py --check            # 生成层凭据解析
uv run python -B -m unittest discover -s tests   # 发出消息/异步结果回归（合成 OCR，不读屏）
uv run python probe/bootstrap_regression.py      # 两种启动入口的离线回归；不联网、不实际安装
```

## 配置

两层、两个 key、**都可以不填**：判断层不填走本地 decider-2b（首次下载约 7 GB）；生成层打包版内置共享 key，不配也能出候选。全部配置在一个 env 文件（**不提供第二种格式**）：

```bash
mkdir -p ~/.config/jev-jarvis
cat > ~/.config/jev-jarvis/env <<'ENV'
# 判断层（可选）：TypeSafe Jev，不填用本地 decider-2b
export TYPESAFE_API_KEY=""

# 生成层：任意 OpenAI 兼容端点
export OPENAI_API_KEY="sk-你的key"
export OPENAI_BASE_URL="https://api.deepseek.com"
export OPENAI_MODEL="deepseek-chat"
# 端点的思考模式要靠额外字段关时填（Qwen3 这类不关会慢几十倍）
# export OPENAI_EXTRA_BODY='{"enable_thinking":false}'
ENV
chmod 600 ~/.config/jev-jarvis/env
```

- **凭据解析以 key 为准**：提供 key 的来源同时决定端点和模型。实测可用：DeepSeek `deepseek-chat`（最快）；智谱 `glm-4-flash`（换 `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`/`ANTHROPIC_MODEL`，两组都填 OpenAI 组优先）；本地 Ollama `qwen2.5:7b`（完全不出网）
- **别用 thinking 模型**：思考吃光 `max_tokens`，候选 0 条，面板只报「候选生成失败」——DeepSeek 认准 `deepseek-chat`
- **自定义话术**：env 加一行 `JEV_TONES`（`|` 分隔、每条「名字=说明」，同名覆盖内置，重启生效），如 `摸鱼大师=像资深摸鱼选手，把活推得漂亮又不失礼`；说明写清「什么语气 + 别变成什么」最管用
- 自查凭据（不打印完整 key）：`uv run python src/generate.py --check`、`uv run python src/judge_jev.py`

## 磁盘占用与清理

| 内容 | 位置 | 大小 | 清理 |
|---|---|---|---|
| 判断层本地模型 `decider-2b`（不配判断层 key 才会下载，判断+排序共用） | `~/.cache/huggingface/hub/models--Mapika--decider-2b` | ~7 GB | `rm -rf ~/.cache/huggingface/hub/models--Mapika--decider-2b`；之后走本地判断会重新下载 |
| Python 运行环境（venv） | `~/Library/Application Support/jev-jarvis/venv` | ~0.7 GB | 删除 .app 不会连带删它，需手动删 |

生成层配 Ollama 的话模型在 Ollama 自己的目录（`~/.ollama`），非本项目下载。

## 已知限制

- 收发方向靠文字位置判断：横跨左右或居中、无法确认方向的文本标「方向未确认」，**不作为回复目标**（很宽的对方消息可能被跳过）；只有明确识别为「对方」的消息才触发判断与生成，只有自己消息时面板显示「等待可确认的对方消息…」
- 图片/表情包读不出内容；引用回复当普通文本；公众号卡片可能被当消息解读；微信全屏布局下识别可能失效（布局常量待动态化，见 #17）
- 微信改版会让布局常量失效（`src/perception.py` 顶部常量需重新校准）；多窗口优先识别主窗口「微信 / WeChat」
- 启动后第一条判断慢是正常现象（本地模型预热）；不对劲先看日志（分阶段耗时、**不含消息正文**，可放心贴 issue）：`tail -40 ~/Library/Logs/jev-jarvis.log`

## 输入区检测框与填入

菜单栏「YOLO 检测框」同时显示消息框和输入目标，约每秒刷新：蓝色实线表示辅助功能接口定位到的输入控件，橙色虚线表示从截图边界推测的输入区；无法定位时显示原因。虚线不代表已取得可写控件，也不修复 #17 的聊天区域固定比例问题。

「填入」优先通过辅助功能接口写入并读回确认。部分微信版本不提供输入控件时，显式点击「填入」会尝试视觉兼容路径：复核窗口、输入区和标题，激活微信、点击输入区、输入文字，再用 OCR 核对。该路径需要屏幕录制及辅助功能权限，会移动鼠标；填入期间请勿操作键鼠或切换聊天。不会自动按发送键，也不使用剪贴板或 Cmd+V；换行和制表符转换为空格。

兼容路径读到已有草稿时停止，提示使用「复制」手动插入；辅助功能路径仍追加原有文字。窗口、焦点或会话变化时停止，画面无法确认时提示检查草稿，不自动重试。视觉边界及 OCR 都可能误判，标题检查也不是会话 ID，不能消除用户同时操作时的竞争；深色主题、多显示器与其他微信版本仍需更多验证。

## 下一步（按优先级）

1. **攒标注数据**：把误判的（尤其「催进度 vs 问进度」）记下来，微调冲 95%+
2. **区分聊天消息和分享的文章卡片**：保守过滤，风险是误杀正常消息

## 开发者

- **贡献前必读**：[CONTRIBUTING.md](CONTRIBUTING.md)——动代码前先在 issue 认领（评论 + assignee），分层自测改哪层跑哪层
- 打包 `./packaging/build_app.sh`；发版 `./packaging/release.sh --publish`（干净 worktree 构建 + 解压回验 + gh release）。版本号只有 `pyproject.toml` 一处；有开发者证书可加 `--sign "Developer ID Application: ..."`
- 架构一句话：进程内抓微信窗口 → Vision OCR（只扫聊天区）→ 本地 decider-2b 出意图/风险 → LLM 并发出候选 → 本地排序 → 悬浮窗 NSPanel。抓窗口不抓屏：微信被挡住也能抓，悬浮窗不污染 OCR

## 许可与免责

MIT（见 `LICENSE`）。只读**你自己屏幕上、你自己账号的**聊天内容，不注入、不 hook、不解密数据库、不自动发送任何消息。请在自己设备上自用；装到别人机器上读别人的聊天记录是另一回事，本项目不为那种用法背书。微信改版可能导致布局识别失效，请遵守微信软件许可协议。
