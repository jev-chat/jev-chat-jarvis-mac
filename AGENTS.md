# AGENTS.md

微信悬浮窗助手（macOS）：OCR 读微信窗口 → 本地模型判意图/风险 → LLM 生成候选回复 → 悬浮窗展示/一键填入。纯只读、零封号风险是**核心原则**，任何改动不得破坏。

## 目录与命令

- `src/perception.py` 抓图+OCR+抽消息；`src/judge.py` 本地判断（decider-2b）；`src/judge_jev.py` 云端判断（TypeSafe Jev）；`src/generate.py` 候选生成（OpenAI/Anthropic 兼容 API）；`src/hud.py` 悬浮窗+轮询主循环；`src/fill.py` 辅助功能写入；`src/styles.py` 话术；`src/userconfig.py` 配置加载
- 启动：`./start.command`（用户平时的方式；`Ctrl+C` 退出）。没有正式测试套件，分层自测：

  ```bash
  uv run python src/perception.py                  # 感知层（读屏，见下方 CLI 验证陷阱）
  uv run python src/judge.py "这个需求你今天跟一下"
  uv run python src/judge_zh_test.py               # 22 条意图回归——改判断层 prompt 后必须重跑
  uv run python src/generate.py --check            # 生成层凭据解析
  ```

- 日志：`~/Library/Logs/jev-jarvis.log`，分阶段耗时（读屏/判断/生成/排序/端到端）。**刻意不含消息正文与候选文字**（用户可放心贴 issue），只在事件发生时打、不在每跳打；首次调用标注「首次」。
- 发版：版本号只有 `pyproject.toml` 一处；`./packaging/release.sh --publish` 从**干净 worktree** 构建（zip 解压回验+SHA256+gh release）；无 Apple 公证，首次打开要教右键。
- 认领协议：动任何 issue 的代码前，先按 [CONTRIBUTING.md](CONTRIBUTING.md) 完成认领三步自检 + 评论认领 + 设 assignee——多人多 AI 并行扫 issue，不认领必撞车。

## 架构与硬约束

- **纯只读**：不注入、不 hook、不解密微信数据。「填入」是唯一写动作：AX 写入优先；微信不提供 AX 输入控件时（如 4.1 部分环境），显式点击「填入」走 `visual_fill` 视觉兼容路径——点击输入区 + CGEvent 键盘事件，**不发送、不用剪贴板/Cmd+V、不覆盖草稿、窗口/焦点/会话签名三重复核、OCR 读回确认、失败不自动重试**（README「输入区检测框与填入」）——**别改回剪贴板+模拟 Cmd+V**（切前台不可靠、覆盖剪贴板、失败会贴进别的应用，见 `src/fill.py` 顶部注释）。
- **轮询**：定时器 0.25s 触发，`_next_read_ts` 门控分三档——静止（指纹相同）跳过 OCR、0.25s 一跳；**变化后先 0.45s×3 跳**（burst 下一条尽快被发现），持续再动才回 1s。**未变化帧仍要跑停稳判定**（复用 `_last_full` 缓存），否则分析永远不触发。停稳 `SETTLE_S=1.2` 是防刷屏**上限不能删**；连续 `STABLE_READS` 跳安静最早 `EARLY_SETTLE_S` 可提前开闸。最小分析间隔 `MIN_GAP_S=2.0` 不能删（预判命中路径本就免冷却）。
- **预判+生成都早跑**（`_prejudge_loop` / `_pregen_loop`，同款 latest-wins 槽位）：消息一出现两个半边同时起跑，停稳门只消费「文本仍是最新」的结果；生成结果还要话术匹配（`_take_pregen`），迟到/过期结果由 `applyCandidates_` 的话术守卫挡掉。候选**先上屏再排序**（prob=None 显示「排序中」，`_rank_payload` 完成后原位重排）。
- **分析在独立线程**（`_run_analysis` + `_analyzing` 防重入），别塞回 tick 线程——那会重新造成分析期间轮询停摆。
- **YOLO 检测框**（`_build_overlay`/`applyBoxes_`，`JEV_BOXES=1` 启动即开、菜单栏可切、默认关）：透明点击穿透窗把最近一次 OCR 的消息画成检测框，纯视觉层——窗口 ID 抓图看不见它、不参与任何管线逻辑；坐标映射依赖 1x nominal 采集尺寸=窗口点尺寸（`capture_image(nominal=True)` 成立）。`Message` 的 x/w 是框几何，折行时在 `extract_messages` 里维护。
- **本地推理用 float16**：MPS 对 bfloat16 算子覆盖不全会走慢路径（实测 ~1.4s vs ~0.75s，准确率不变）。
- **OCR 用 Vision**：语言只留 `zh-Hans`（多加 en-US 逐块一致却慢 30%）、Accurate 档（Fast 漏字）、语言校正开着、别缩 ROI（丢上下文）。**采集分辨率降到 1x**（`kCGWindowImageNominalResolution`）是实测过的例外：合成中文 6 行 2x ~140ms → 1x ~100ms、逐字一致；布局常量全是归一化的，不受影响。
- **HTTP 走 keep-alive 池**（`generate.py` 的 `http_post_json`，judge_jev 共用）：每次 urllib.urlopen 新建 DNS+TCP+TLS 白付 ~0.1–0.3 s。网络异常换新连接重试一次；>=300 按 `urllib.error.HTTPError` 形状抛（调用方 `e.read()` 拿正文），不跟随重定向。
- **配置只有 env 一种格式**（无 config.json）：`~/.config/jev-jarvis/env` 等，**凭据解析以 key 为准**——提供 key 的来源同时决定端点和模型。不提供第二种配置文件格式是有意为之。
- 两种启动方式（`start.command` / `.app`）必须同 Python 3.12（包跟 `.python-version` 走）；`.app` 是「启动器包」（不冻结 torch，首次启动 uv 建 venv）。

## 已知的坑

- **CLI 进程里验不了感知层**：独立 shell 进程里 `CGWindowListCreateImage` 会被拒（静默退子进程路径、无指纹）。验证要么用合成 CGImage 测纯函数，要么起真应用看日志。
- 坐标系：本模块布局常量（`CHAT_PANE_X_MIN` 等）是**底部原点**（Vision 口径）；`CGImageCreateWithImageInRect` 是**左上原点**，换算别搞反。
- 生成层**不能用 thinking 模型**（思考吃光 `max_tokens`，候选 0 条，面板只报「生成失败」误导用户）。
- README 实测数字皆有口径：意图 86.4% 是**无上下文**回归口径，改判断层 prompt 后别直接引用，要重跑 `judge_zh_test.py`；判断耗时引用应用内实测（~1s），不是 benchmark 的 0.75s。`judge_zh_test.py` 直接 import `judge.INTENTS`，测的就是线上 prompt。
- **判断层 prompt 描述别瘦身**：两轮压缩措辞（保语义锚点）实测 81.8% / 77.3%，低于原文 86.4%——批评/要解释的边界对措辞极敏感，省的那点 prefill 时间又藏在停稳窗口里，不划算（见 `judge.py` INTENTS 上的注释）。
- `.gitignore` 忽略全部 png 只放行 `docs/**`；新图片必须进 docs/。
- AppKit 控件宽度要渲染成 PNG 实测，`cellSize()` 会谎报。
- 判断模型冷启动 10–20s 是已知问题（见 issue #1 预热方案）；启动后第一条慢是正常现象，别误判成回归。
- 改动用户可见行为要同步 README；待办与已定方案看 GitHub issues 和 README「下一步」。
