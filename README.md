# jev-jarvis（macOS）

微信弹出一条消息 → 悬浮窗立刻告诉你**这句话的真实意图**、**风险几级**、**该怎么回**。

Mac 版 V0：**纯只读、零封号风险**——不注入、不 hook、不解密数据库，只是「看屏幕 + 本地模型判断」。

![演示：微信群消息进来 → 面板给出意图、风险分级与候选回复 → 点「填入」直接进微信输入框](docs/demo.gif)

## 交流反馈

用着有问题、想提需求、或者想一起改，扫码进群（二维码 7 天失效，过期了在 issue 说一声）：

<img src="docs/wechat-group.png" width="200" alt="扫码加入微信交流群">

## 现在能做什么

- **意图 + 风险**：8 类意图零样本 **86.4%**（22 条实测），风险 0–9 分级 + 行动建议，本地模型一次前向出全分布
- **候选回复**：按选中的「话术」生成（内置 10 种、最多 3 种并发、keep-alive 复用连接）→ 先上屏（流式端点逐行、其余整包，标「排序中」）→ 本地模型排序后原位重排，压掉踩雷选项
- **YOLO 检测框**（可选）：把每次 OCR 命中的消息实时框在微信窗口上——对方/我/发送者分色 + 真实置信度，被判断那条带「意图·风险」标签；点击穿透、不进抓图。env 里 `JEV_BOXES=1` 启动即开，菜单栏 J 图标随时切换
- **快**：消息一出现预判+生成同时起跑（latest-wins，刷屏中间态自动作废），停稳窗口把生成耗时整个藏进去
- 防刷屏：消息停稳才分析（上限 1.2 s，连续 3 跳安静最早 ~0.7 s 开闸）、两次分析最小间隔 2 s；分析期间读屏不中断
- 悬浮窗跟随微信窗口（非激活不抢焦点、多显示器不跳屏）；「复制」/「填入」走辅助功能接口，输入框已有字时**追加**不覆盖，写完读回确认

## 用法

**只想用**：[Releases](https://github.com/jev-jarvis/jev-jarvis/releases) 下载 `.app`，解压拖进「应用程序」。**第一次打开要右键 → 打开**（没做公证，双击会被 Gatekeeper 拦）；首次启动联网装依赖约 3 s，授予「屏幕录制」权限后**退出重开**。

**从源码跑**（微信在运行、终端已授予「屏幕录制」）：`./start.command`。「填入」另需「辅助功能」权限，第一次点会弹系统授权框。分层自测：

```bash
uv run python src/perception.py                  # 感知层：识别到的消息 + 耗时
uv run python src/judge.py "这个需求你今天跟一下"  # 单条消息出判断
uv run python src/judge_zh_test.py               # 22 条中文意图回归
uv run python src/generate.py --check            # 生成层凭据解析
```

## 面板读法

微信浅色配色（深色模式下也保持浅色），从上到下：

```
等待微信消息…                                  ← 状态行
这个方案今天能给我吗？                            ← 被判断的消息
来自 王总 · 上文：昨天的评审意见…                   ← 上文（发送者 + 最近一条）
催进度                                        ← 意图（8 类之一）
意图识别率 92%                                 ← 这次判断的把握
● 留神  4/9                                   ← 风险（绿 ≤3 / 橙 4–6 / 红 7–9）
先给当前状态 · 给明确的完成时间                    ← 行动建议
候选回复（按合适度排序）
[高情商话术                  ▾]                 ← 下拉框可以换话术
    #1 · 94%  今天下班前给您初稿   [复制] [填入]
    #2 · 41%  我明早一上班就发您    [复制] [填入]
[贴吧老哥 v1.0               ▾]                 ← 每种话术一组，各出 2 条
[不用                        ▾]                 ← 第三个槽位自己选
```

- 意图/风险停稳即出（判断在停稳窗口里已提前算完）；候选随后上屏，排序完成后原位重排、补上百分比
- **每种话术出 2 条、故意一稳一放**：一条稳妥能直接发，一条把人设做足；改选话术立刻按当前消息重新生成
- **加自己的话术不用改代码**：`~/.config/jev-jarvis/env` 里加一行，`|` 分隔、每条「名字=说明」，同名覆盖内置，重启生效：

  ```bash
  export JEV_TONES="摸鱼大师=像资深摸鱼选手，把活推得漂亮又不失礼|孙子兵法=用兵法比喻说话，比如「先稳住阵脚」"
  ```

  说明是直接喂给模型的指令，写清「什么语气 + 别变成什么」比只写一个形容词管用。

## 实测数字（M1 Pro）

| 指标 | 数值 |
|---|---|
| 轮询 | 静止 0.25 s/跳；变化后 0.45 s×3 跳再回 1 s。指纹相同整跳跳过 OCR，安静时新消息 ≤0.25 s 可见 |
| 读屏 | 进程内抓图 ~5–25 ms（休眠回退子进程 ~180 ms）；OCR 1x 采集，合成图实测 2x ~140 → 1x ~100 ms、逐字一致 |
| 判断 | ~0.8 s 稳态（回归口径 ~0.55 s/条）；线上带 2 轮上下文；冷启动 9–15 s 已挪到启动预热 |
| 生成 | ~0.5–1.1 s（DeepSeek），流式首条 ~0.6 s；消息一到就起跑，通常藏在停稳窗口内 |
| 意图识别率 | **86.4%**（22 条，多数类基线 13.6%）※ |
| 端到端 | 出意图 ~1.5 s；出候选先上屏 ~1.5–2 s、排序完成再 +~0.5 s（优化前 ~3 s）※ |

※ 86.4% 是**无上下文**回归口径（`src/judge_zh_test.py`，直接 import 线上 prompt），22 条样本量不大，且错例集中在「催进度 vs 问进度」边界；改判断层 prompt 后必须重跑再引用。端到端优化后数值为机制推算，以日志实测为准。

## 配置

两层、两个 key、都可以不填——判断层不填就走本地 decider-2b（首次下载约 7 GB），功能照常；生成层不填候选区空着 + 启动弹窗提醒。

全部配置在一个 env 文件（shell 格式），**不提供第二种配置文件格式**。查找顺序：真实环境变量 > `$XDG_CONFIG_HOME/jev-jarvis/env` > `~/.config/jev-jarvis/env` > `~/Library/Application Support/jev-jarvis/env`：

```bash
mkdir -p ~/.config/jev-jarvis
cat > ~/.config/jev-jarvis/env <<'ENV'
# 判断层（可选）：TypeSafe Jev，不填用本地 decider-2b
export TYPESAFE_API_KEY=""

# 生成层（要出候选就得填）：任意 OpenAI 兼容端点
export OPENAI_API_KEY="sk-你的key"
export OPENAI_BASE_URL="https://api.deepseek.com"
export OPENAI_MODEL="deepseek-chat"

# 或 Anthropic 兼容端点（二选一，都填时上面优先）
# export ANTHROPIC_API_KEY=""
# export ANTHROPIC_BASE_URL="https://open.bigmodel.cn/api/anthropic"
# export ANTHROPIC_MODEL="glm-4-flash"
ENV
chmod 600 ~/.config/jev-jarvis/env
```

- **凭据解析以 key 为准**：提供 key 的来源同时决定端点和模型，否则拿着 A 家 key 请求 B 家，401 得莫名其妙
- 实测可用端点：DeepSeek `https://api.deepseek.com` + `deepseek-chat`（最快 ~0.7 s）；智谱 `https://open.bigmodel.cn/api/anthropic` + `glm-4-flash`；本地 Ollama `http://localhost:11434/v1` + `qwen2.5:7b`（完全不出网）。Jev 网关：OpenRouter / Vercel AI Gateway / Opper（换 `TYPESAFE_BASE_URL`/`TYPESAFE_MODEL`），调用失败自动回退本地并在面板标注
- **别用带思考（thinking）的模型**：思考吃光 `max_tokens`，候选 0 条，面板只报「候选生成失败」——DeepSeek 认准 `deepseek-chat`
- 自查（不打印完整 key）：`uv run python src/generate.py --check`、`uv run python src/judge_jev.py`；不想落盘可用钥匙串 `security find-generic-password -s jev-jarvis -w`
- 其它开关：`JEV_BOXES=1` 启动即打开 YOLO 检测框（默认关，菜单栏可切）；`JEV_TONES` 加自定义话术（见面板读法）

## 架构

```
微信窗口 ──进程内抓图 CGImage（~25ms，休眠时回退 screencapture）──▶ Vision OCR（只扫聊天区 ROI，1x）
                                                                     │
                                                     过滤 UI 噪声 + 左右分句 + 发送者
                                                                     │
                                                               最新一条消息
                                                                     │
                                                     ┌───────────────┘
                                                     ▼
                                  decider-2b 本地推理（8 意图 + 风险分布，一次前向）
                                                     │
                                    生成层 API 并发出候选 ──▶ 本地排序 ──▶ 悬浮窗 NSPanel
```

- 每 0.25 s 抓帧算指纹，没变跳过 OCR；消息一出现，本地判断与生成**并行起跑**，停稳门只消费最新结果
- 抓窗口而不是抓屏：只取微信自己的内容，悬浮窗浮在上面也不污染 OCR，微信被挡住也能抓
- 本地推理用 float16（MPS 对 bf16 算子覆盖不全，实测慢 2 倍，准确率不变）；生成层上下文 4 轮、判断层 2 轮

## 已知限制 & 排查

- 左右分句靠 x 坐标，非对称布局可能误判；图片/表情包读不出内容；引用回复当普通文本；公众号文章卡片会被当消息解读
- 微信改版会让布局常量失效（`src/perception.py` 顶部常量需重新校准）
- 多窗口时优先识别主窗口「微信 / WeChat」，避免锁定较大的独立窗口；自己发出的短消息不会按昵称过滤，左边缘对齐的紧邻续行保留原消息的发送方。回归检查：`uv run --frozen python probe/perception_regression.py`。
- 判断模型冷启动 10–20 s 已挪到启动后台预热；启动后第一条慢是正常现象。嫌冷启动慢就把判断层换 TypeSafe Jev（走网络不加载本地模型）
- 觉得慢/不对先看日志（分阶段耗时、不含消息正文，可放心贴 issue）：`tail -40 ~/Library/Logs/jev-jarvis.log`

## 下一步（按优先级）

1. **攒标注数据**：把误判的（尤其「催进度 vs 问进度」）记下来，微调冲 95%+。
2. **区分聊天消息和分享的文章卡片**：用「听全文」「阅读」等特征做保守过滤，风险是误杀正常消息。

## 打包与发版（开发者）

```bash
./packaging/build_app.sh           # 生成 ./jev-jarvis.app（约 400 KB 启动器包，不冻结 torch）
./packaging/release.sh --publish   # 干净 worktree 构建 + 解压回验 + gh release（需 gh 已登录）
```

版本号只有 `pyproject.toml` 一处；包跟 `.python-version`（3.12）走，有独立 TCC 身份，`LSUIElement` 不占 Dock；无 Apple 公证，首次打开要教右键；有开发者证书可加 `--sign "Developer ID Application: ..."`。

## 许可与免责

MIT（见 `LICENSE`）。本项目只读取**你自己屏幕上、你自己账号的**聊天内容，不注入、不 hook、不解密数据库、不自动发送任何消息。请在自己设备上自用；装到别人机器上读别人的聊天记录是另一回事，本项目不为那种用法背书。微信客户端改版可能导致布局识别失效，请遵守微信软件许可协议。
