---
paths:
  - "CHANGELOG.md"
  - "changelog.d/**"
---

# 收尾：Memory 2.0 规则原文 · 按路径加载

> 读 / 改 `CHANGELOG.md` 或 `changelog.d/` 下的版本碎片（每个改了东西的会话收尾都要碰）时 Claude Code 自动加载。v0.45.728 起新条目写在 `changelog.d/<版本>.md`、`CHANGELOG.md` 冻结为档案——下文逐字原文里的「更新 CHANGELOG.md」即写好自己的碎片。v0.45.727 从根目录 `CLAUDE.md` **逐字**搬来「Memory 2.0 自动更新规则」全文；根目录保留同名标题与全部可执行规则的精简版（含提交推送命令）。

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
