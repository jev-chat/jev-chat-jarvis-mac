# 贡献指南

欢迎贡献！本仓库 CI 会自动跑离线回归（`tests/`），但读屏、判断 prompt、生成层这些 CI 覆盖不到的部分，**自测仍靠贡献者自己**，所以认领方式和自测要求请读完这一页。核心原则只有一条：**纯只读**——不注入、不 hook、不解密微信数据。「填入」是唯一写动作：辅助功能（AX）写入优先；微信不提供输入控件时，显式点击「填入」可走视觉兼容路径（点击输入区 + 键盘事件，**不发送、不用剪贴板/Cmd+V、不覆盖草稿、OCR 读回确认**，见 README「输入区检测框与填入」）。任何破坏这条的改动不会被接受。

## 开工前：先认领，再动手

很多人（和他们的 AI）同时扫 issue，不认领就会撞车。**动代码前必须完成认领三步自检**：

```bash
gh issue view <n> --json assignees      # ① 已有 assignee 且不是自己 → 停手，换 issue
gh issue view <n> --comments            # ② 已有别人的「认领」评论 → 停手，换 issue
gh pr list --state open --search "<n>"  # ③ 已有关联 open PR → 停手，换 issue
```

三步都干净，再认领占坑（评论 + assignee，两个动作都要）：

```bash
gh issue comment <n> --body "认领：<一句话说打算怎么修>"
gh issue edit <n> --add-assignee @me
```

- **弃坑规则**：认领后 7 天没有 open PR 视为自动释放，其他人可接手（接手前在原认领评论下回复一声）。
- **没有现成 issue 的新想法**：先开 issue 说清要做什么，等维护者确认后再认领，避免方向跑偏白干。
- 多个 issue 想一起做：可以，但**会改到相同文件的 issue 必须串行**，别并行开两个分支互相 rebase 地狱。

## 怎么交

- 有仓库写权限：从 `master` 拉分支 `<type>/<issue号>-<摘要>`，如 `fix/11-own-message-misjudged`；没有写权限就 fork 后同样命名，从 fork 提 PR。
- **一个 PR 只做一件事**。commit 用 Conventional Commits + 中文描述：`fix(perception): …` / `feat(generate): …` / `docs: …`。
- PR 描述写清三件事：改了什么、为什么改、怎么测的（有实测数字写实测数字）；关联 issue 用 `Closes #n` 写在描述里，**不写进 commit 标题**（squash 合并会自动追加 PR 号，双编号分不清）。
- 合并统一 squash，一个 issue 对应 master 上一个干净提交。
- PR 出现冲突：`git fetch origin master && git rebase origin/master` 就地解决、自测跑过再 push，**不要**在 GitHub 网页上手改文件绕过（跳过了本地自测）。
- **代解前先声明**：冲突原则上由 PR 作者自己 rebase 解决；维护者或其他 AI 会话想代解，必须先在 PR 里评论说一声「我来解冲突」，避免两条线同时在解、互相强推顶掉（#36 的实际教训）。
- **fork PR 勾选允许维护者修改**：从 fork 提 PR 时勾选「Allow edits by maintainers」，维护者才能代为解决冲突或顺手小修，否则只能等你回来 rebase。

## 自测要求（CI 管离线回归 + 覆盖率门禁，其余绿灯就是你自己）

离线回归由 CI 在 PR 和 master push 上自动执行（`.github/workflows/ci.yml`），红灯不许合。其余层按层自测，**改哪层跑哪层**：

| 你改了什么 | 必跑 |
|---|---|
| 感知层 `perception.py` | CLI 进程里验不了读屏（会静默降级）——起真应用看 `~/Library/Logs/jev-jarvis.log`，日志刻意不含消息正文，可放心贴进 PR |
| 判断层 `judge.py`（尤其 prompt） | `uv run python src/judge_zh_test.py`——意图回归，数字退了不许合 |
| 发出消息识别 / 回复目标切换（`perception.py` + `hud.py`） | `uv run python -B -m unittest discover -s tests`——离线回归（合成 OCR，不读屏、不调 API、不读凭据） |
| 生成层 `generate.py` | `uv run python src/generate.py --check`（凭据解析）+ 真跑一条候选生成确认非空 |
| 悬浮窗/轮询 `hud.py` | 起真应用走一轮完整流程：消息出现 → 判断 → 候选上屏 → 一键填入 |

CI 还带两道**覆盖率门禁**（2026-09 起）：

- **全局基线棘轮**：总覆盖率跌破 `ci/coverage-min.txt` 里的基线即红。基线只许随 PR 上调（覆盖率涨了顺手把数字改大）；下调会被 CI 机械拦截（对比 origin/master 的基线），确需下调（如移除大模块）在 PR 里说明理由交维护者特批。
- **增量门禁（仅 PR）**：相对 `origin/master` 的改动/新增行 ≥80% 要被测试触达，存量零追缴；纯 CI/文档改动不受影响。

本地自查（与 CI 同款命令）：

```bash
uv run --locked --with coverage coverage run --source=src -m unittest discover -s tests
uv run --locked --with coverage coverage report --omit=src/judge_zh_test.py --fail-under="$(cat ci/coverage-min.txt)"
uv run --locked --with coverage coverage xml --omit=src/judge_zh_test.py -o coverage.xml
uv run --locked --with diff-cover diff-cover coverage.xml --compare-branch origin/master --fail-under=80
```

几条必守（都是实测过的教训，动手前先读对应源码顶部注释）：

- 判断层 prompt **别顺手优化措辞**：压缩/改写实测掉点，语义等价的瘦身也不行（`judge.py` INTENTS 上的注释有记录）；改了 prompt 必须重跑上面那张表里的回归。
- 生成层不能用 thinking 模型：思考吃光 `max_tokens`，候选 0 条，面板只报「生成失败」。
- 轮询与线程结构别「顺手优化」：停稳窗口与最小分析间隔是防刷屏上限、不能删，分析跑在独立线程（`hud.py` 内注释有原因），塞回轮询线程会让分析期间读屏停摆。
- 改了用户可见行为 → 同步 `README.md`；改动不能破坏纯只读原则。「填入」AX 优先，视觉路径只做显式点击触发的后备（别改回剪贴板 + Cmd+V，原因见 `src/fill.py` 顶部注释；路径边界见 README「输入区检测框与填入」）。
- PR 描述里标注改动类型：【新增】/【修改】/【删除】各点了哪些类、方法、配置，方便 review。

## Issue 标签约定

提 issue、处理 issue 都要打标签，方便筛选与认领：

- **类型标签必打，且只有一个**：`bug`（缺陷）/ `enhancement`（功能建议）/ `question`（提问）/ `documentation`（文档）。没有类型标签的 issue 无法按缺陷/改进筛选。
- **模块标签 `area/*` 按需可多打**（不能替代类型标签）：`area/perception`（读屏 OCR）、`area/judge`（判断层）、`area/generate`（生成层）、`area/hud`（悬浮窗）、`area/fill`（填入）、`area/config`（配置）、`area/packaging`（打包与启动）、`area/docs`（文档）。
- 宁可晚打不要错打；`wontfix` / `invalid` 关闭时要在回复里说明理由。

命令：`gh issue edit <n> --add-label "bug" --add-label "area/perception"`。

## 有问题？

- Bug / 功能建议 → [Issues](../../issues)，认领前先读上面的三步自检
- 不确定怎么改 → 先开 issue 讨论，别直接动代码
