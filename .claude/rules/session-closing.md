---
paths:
  - "CHANGELOG.md"
  - "changelog.d/**"
---

# 收尾：版本条目与 Memory 2.0 规则原文 · 按路径加载

> 读 / 改 `CHANGELOG.md` 或 `changelog.d/` 下的版本碎片（每个改了东西的会话收尾都要碰）时 Claude Code 自动加载。v0.45.728 起新条目写在 `changelog.d/<版本>.md`、`CHANGELOG.md` 冻结为档案——下文逐字原文里的「更新 CHANGELOG.md」即写好自己的碎片。v0.45.727 从根目录 `CLAUDE.md` **逐字**搬来「Memory 2.0 自动更新规则」全文；根目录保留同名标题与全部可执行规则的精简版（含提交推送命令）。v0.45.732 又从根目录**逐字**搬来「版本历史规则」「并发开工必须先占号」「CHANGELOG 完整性 hook」三节（0.45.728 改写后的全文）；根目录只留占号命令、两条 hook 警告与指针。

## 版本历史规则

- **每次 session 结束前必须写好自己那一版的条目**：v0.45.728 起写在 `changelog.d/<版本>.md`（一个版本一个文件，格式、示例、流程见 `changelog.d/README.md`）。`CHANGELOG.md` 已冻结为档案，**不许再加版本标题**（提交 / 推送 / 全量测试都会红）；冻结前在里面占的号就地完成
- 格式：`Added` / `Changed` / `Fixed` / `Removed`，注明文件名和改动摘要
- 版本号：patch（0.x.y+1）= bug fix；minor（0.x+1.0）= 新功能批次
- 最大号 / 下一个号 / 谁还挂着占位：`/usr/local/bin/python3 changelog_fragments.py status`；合并视图 `... render`（生成结果不提交）

### 并发开工必须先占号（2026-08-27 起；v0.45.728 起占号 = 往 main 推一个碎片）

多个 session 同时开工时，**开工第一件事就是占号**，不要等完工再编号：

`/usr/local/bin/python3 changelog_fragments.py claim "一句话说明在做什么" --push`

它在 `origin/main` 上另建一个只含 `changelog.d/<下一个号>.md` 占位碎片的提交、推上 main 并用 `ls-remote` 核实（被抢了自动取下一个号），再把这个提交接进当前分支——不用先切分支、不用清工作区（暂存区有东西时拒绝接入，照提示做，**别用裸 `git stash`**）。完工时只改碎片的标题和正文，**认领行 `<!-- 认领：… -->` 不许改**。

- ⚠️ **别手写碎片、别省 `--push`。** 认领行里的随机串让每次占号内容都不同——内容逐字相同的两个碎片，git 会无声合并成一个，撞号就看不见了；不推送的占号别的 session 看不见，等于没占。
- **为什么值得**：2026-08-26~27 两条线并行，`0.45.36–0.45.40` 被双方各用了一遍、`0.45.47` 与 `0.45.48` 又各撞一次，前后返工三轮。改成碎片后，不同的号是不同的文件，合并不再互相冲突；只有同一个号才冲突——冲突本身就是撞号。
- **撞上了怎么办**：**先进 main 的号算数**。后者 rebase 时在 `changelog.d/<号>.md` 上撞 add/add 冲突 ⇒ 保留 main 那份（`git checkout origin/main -- changelog.d/<号>.md`）、`rebase --continue`，再 `claim` 一个新号、把条目搬过去。占了不用的号：标题改成 `占位关闭：<原因>` 并写一句正文，**不删文件**（推往 main 时删已有碎片会被拦）。

### CHANGELOG 完整性 hook（2026-09-13 起；v0.45.728 起含碎片检查）

`changelog_guard.py` 挂在 pre-commit + pre-push 上，只挡**动了 `CHANGELOG.md` 或 `changelog.d/`** 的提交与推往 main 的推送。设计与实测全在该文件 docstring。

- 动了档案：跑 `tests/test_changelog_entry_integrity.py`（冲突标记 / 重号 / 空标题 / 冻结：加号、删条目、改顶号都红）。
- 动了碎片：跑 `changelog_fragments.py` 的检查（文件名 = 标题里的号、认领行恰好一行、日期、冲突标记、占位以外要有正文、号要大于档案顶号）。pre-commit 读**暂存区**、不读工作区；pre-push 读**被推的那个提交**并对照远端 main——已在 main 上的碎片不许删、认领行不许改，算不出远端 main 的基准就拦。
- 推往 main 时另有两道与 CHANGELOG 无关的闸，各判各的：
  - **契约闸**（v0.45.355）：被推区间动了代码 ⇒ 在导出树里跑步骤契约测试。
  - **根目录 CLAUDE.md 预算闸**（v0.45.738）：被推区间动了根目录 `CLAUDE.md` ⇒ 拿被推提交里那份，比被推提交里 `claude_md_budget.py` 写的预算；超了就拦，并打印搬家步骤（与 `tests/test_claude_md_partitions.py` 的失败信息是同一段）。只放在 pre-push：rebase 不跑 pre-commit，两个会话各自没超、合起来才超的只有 pre-push 看得到。
- ⚠️ **pre-commit 管不到 `git rebase --continue`**（实测只触发 post-rewrite）——解冲突后兜底的是 pre-push，别以为提交成功就代表检查过。
- `.git/hooks` 不被跟踪：重新 clone 或清过之后要重装 `/usr/local/bin/python3 changelog_guard.py --install-hook`，**否则没人会知道它没了**。钩子壳没变，碎片检查随代码合入即生效，已装的钩子不用重装。

## Memory 2.0 自动更新规则

- **Auto Memory 路径**：`~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory/MEMORY.md`
- 每次 session 修改了代码/新增模块/修复 bug 后，Claude 必须自动更新 MEMORY.md 对应章节
- 控制在 200 行以内；超出时压缩旧版本历史或移除已被代码覆盖的实现细节
- 旧记忆路径 `~/.claude/projects/-Users-igg/memory/` 已弃用，勿再写入
- **索引行只写「这条是什么 + 什么时候该打开它」，单条 ≤150 字符**；细节一律进 topic 文件
  （2026-09-11 实测教训：索引曾涨到 40KB、单条 2054 字符，且**有些教训只活在索引行里、
  topic 文件一个字都没有** ⇒ 照字数裁会无声丢知识。裁之前必须先把索引行里的标志物
  逐个 grep topic 文件，缺的先搬回去再裁。）
- **这条上限自 v0.45.208 起有会红的守卫**：memory 仓的 `check_index_line_length.py`
  （pre-commit）。**动了 `MEMORY.md` 才拦，没动只吵** —— 例行拦无关提交会把人
  养成 `--no-verify` 的习惯。**只检测，绝不自动裁**：裁哪里是语义判断，
  自动裁剪器正是上一条要防的东西。接手 / 重装（幂等，钩子不进 git、clone 之后就没了
  且无人知晓；**必须写全路径**，从本仓 worktree 执行相对路径是 exit 2）：
  `/usr/local/bin/python3 ~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory/check_index_line_length.py --install-hook`

### memory 目录已纳入 git，**每次 session 收尾必须提交并推送**（2026-09-11 起提交；2026-09-18 起加推送）

`~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory/` 是一个独立的 git 仓库
（分支 `main`）。**2026-09-18 起已有远端** `origin` → 私有仓库
`wangmingjie36-creator/alpha-hive-memory`（用户在对话里明确批准后新建并首推；
建仓前已扫过当前内容 + 全部 git 历史，零命中密钥形状字符串）。没有任何东西会
替你提交，所以这是 session 收尾清单的一项，与「更新 CHANGELOG.md」并列：

```bash
git -C ~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory add -A && git -C ~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory commit -m "<本次记了什么>" && git -C ~/.claude/projects/-Users-igg-Desktop-Alpha-Hive/memory push origin main
```

- **没有改动就不提交**，别造空提交（自然也不用 push）。
- ⚠️ **这个目录会被多个 session 并发写**。`add -A` 可能捎上别人未提交的编辑——
  记忆是追加性的，捎上比让它一直裸着好，**但 commit message 必须如实说明
  哪些文件不是本次改的**，否则历史会张冠李戴（同「并发开工必须先占号」那节的教训）。
- **推送已获用户明确批准为默认行为，不必每次重新问**（2026-09-18 用户在对话里
  明确要求「以后每次收尾都自动推」）——这条本身就是「durable instruction」，
  区别于一次性的「这一次可以推」。若某次改动的内容明显敏感（本仓迄今未出现过），
  推送前仍应停下来问。
- 加远端本身（第一次从无到有）属对外动作，当时已经问过用户；**这条历史决策
  不需要每次复述**，以后新建远端才要重新走「先问用户」。
