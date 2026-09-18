"""守卫：`setup_cron.py` 的 crontab 白名单不放行已退役的 `alpha-hive-daily.sh`（v0.45.293）

为什么
------
`~/.claude/scripts/alpha-hive-daily.sh`（仓库外死脚本）在 v0.45.293 退役、移入 `scripts/retired/`。
它此前留在 `set_crontab` 的 `ALLOWED_SCRIPTS` 里：手写进 crontab 的一行会被**放行并安装成功**，
之后每次触发都找不到脚本。摘掉后同样的一行在**安装时**就被拦下（`Blocked crontab entry`）——
失败点从「每天静默一次」前移到「安装那一刻」。

为什么用 AST 读白名单、不直接调 `set_crontab`
-------------------------------------------
`set_crontab` 放行时会真的执行 `crontab -`（**覆盖用户的 crontab**）。靠 `mock.patch(Popen)` 拦它，
一旦哪天有人把 `Popen` 换成 `subprocess.run`，打桩就落空，这条测试会**改写真 crontab**。
所以这里只读源码里的字面量，不执行任何东西。

`test_live_script_still_allowed` 是对照：证明解析器找到的确实是那张真白名单，
不是一个空集合——否则「退役脚本不在里面」在任何情况下都绿。
"""

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "setup_cron.py"


def _allowed_scripts() -> set:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "ALLOWED_SCRIPTS" for t in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError("找不到 ALLOWED_SCRIPTS 的赋值 —— setup_cron.py 的白名单结构变了，本守卫要跟着改")


def test_live_script_still_allowed():
    # 按文件名后缀认、不写死家目录路径（tests/test_reads_own_checkout.py 禁测试里出现 /Users/…）
    assert any(p.endswith("/run_alpha_hive_daily.sh") for p in _allowed_scripts())


def test_retired_script_is_not_allowed():
    hits = [p for p in _allowed_scripts() if p.endswith("alpha-hive-daily.sh")]
    assert not hits, f"已退役的脚本又回到了 crontab 白名单：{hits}（它在 scripts/retired/，路径已不存在）"
