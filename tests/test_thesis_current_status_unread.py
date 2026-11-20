"""守卫：生产代码不许读 `thesis_breaks_config.json` 里手写的 `current_status`（v0.45.793）。

为什么：`current_status` 是人手写的散文快照（如「✅ 正常（+8% QoQ）」），写下那天就开始过时，
没有任何东西会让它自动更新。它唯一的读者 `ThesisBreakMonitor.generate_html_section` 还把监控表每一行
写死成「✅ 安全」——条件真触发了也照样显示安全。那个渲染器只被 `__main__` 示例调用，日报、网站、
Alpha Bot、LLM 提示词都不读这个字段；v0.45.793 删掉了它。

本守卫防「以后有人顺手把 current_status 接进展示 / 提示词」：那等于把一份过时的手写结论当成实时状态给人看。
要展示条件当前状态，走 Alpha Bot 的自动核对（`alphabot/thesis_auto.py`）或真实取数，不读这个字段。

判据：生产 Python 里出现字符串字面量 "current_status"（取下标 / .get / 字段名表）即红；
注释、docstring 里提到它不算（tokenize 后只看「整个字面量就是这个词」的 STRING）。
前端 JS / HTML 模板按文本找。
"""
import io
import re
import subprocess
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIELD = "current_status"
# 不算生产代码的目录：测试、实验重放、退役归档
EXCLUDED_PREFIXES = ("tests/", "experiments/", "_retired")
_QUOTED_IN_FSTRING = re.compile(r"""\{[^{}]*['"]current_status['"][^{}]*\}""")


def _tracked(pattern_suffixes):
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True).stdout
    files = [f for f in out.splitlines() if f.endswith(pattern_suffixes) and not f.startswith(EXCLUDED_PREFIXES)]
    assert files, "git ls-files 一个文件都没列出来——守卫没在扫任何东西"
    return files


def python_reads(source: str) -> list:
    """返回源码里「整个字符串字面量就是 current_status」的行号。"""
    hits = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.STRING:
            continue
        prefix = re.match(r"^[rRbBuUfF]*", tok.string).group(0)
        # Python 3.11 里整个 f-string 是一个 STRING token：f"{c['current_status']}" 里的读法不会单独成 token。
        # 被删的渲染器恰恰是这么读的——不查这一支，守卫对它一个都抓不到（v0.45.793 变异实测）。
        if "f" in prefix.lower() and _QUOTED_IN_FSTRING.search(tok.string):
            hits.append(tok.start[0])
            continue
        body = tok.string[len(prefix):]
        for q in ('"""', "'''", '"', "'"):
            if body.startswith(q) and body.endswith(q) and len(body) >= 2 * len(q):
                body = body[len(q):-len(q)]
                break
        if body == FIELD:
            hits.append(tok.start[0])
    return hits


def test_scanner_catches_reads():
    """正对照：扫描器真能抓到读法，且不把注释 / docstring 当读。"""
    src = (
        'def f(c):\n'
        '    """提到 current_status 的 docstring 不算"""\n'
        '    # current_status 注释也不算\n'
        '    a = c["current_status"]\n'
        "    b = c.get('current_status')\n"
        '    h = f"<td>{c[\'current_status\']}</td>"\n'
        '    t = f"字段名 current_status 只是文字"\n'
        '    return a, b, h, t\n'
    )
    assert python_reads(src) == [4, 5, 6]


# 被删渲染器的原文（v0.45.793 前 thesis_breaks.py 第 211–273 行的读法，逐字摘录）。
# 不从 git 历史取：CI 是浅克隆（actions/checkout 缺省 fetch-depth 1），历史提交不存在。
_DELETED_RENDERER = '''
def generate_html_section(self):
    html = ""
    for condition in self.config["level_1_warning"]["conditions"]:
        html += f"""
                            <p><span class="label">当前状态：</span>{condition['current_status']}</p>
        """
    for condition in self.config["level_2_stop_loss"]["conditions"]:
        html += f"""
                            <p><span class="label">当前状态：</span>{condition['current_status']}</p>
        """
    for condition in conditions:
        html += f"""
                            <td>{condition['current_status']}</td>
                            <td>✅ 安全</td>
        """
'''


def test_scanner_catches_the_deleted_renderer():
    """反向自证：被删渲染器的真实读法（f-string 里取下标）必须 3 处全抓到。
    v0.45.793 首版扫描器只看独立字符串 token，对删除前的真文件扫出 0 处——守卫当时是没牙的。"""
    assert len(python_reads(_DELETED_RENDERER)) == 3


def test_production_python_does_not_read_current_status():
    bad = []
    for rel in _tracked((".py",)):
        try:
            src = (ROOT / rel).read_text(encoding="utf-8")
            lines = python_reads(src)
        except (OSError, UnicodeDecodeError, tokenize.TokenError, SyntaxError) as exc:
            pytest.fail(f"{rel} 读不了 / 分词失败（守卫不能跳过它）：{exc}")
        bad += [f"{rel}:{n}" for n in lines]
    assert not bad, (
        "生产代码读了手写的 current_status（过时的人工快照）：\n  " + "\n  ".join(bad)
        + "\n要展示条件状态请走 alphabot/thesis_auto.py 的自动核对，见本文件 docstring。"
    )


def test_frontend_does_not_read_current_status():
    bad = [rel for rel in _tracked((".js", ".html", ".j2", ".jinja"))
           if FIELD in (ROOT / rel).read_text(encoding="utf-8", errors="replace")]
    assert not bad, f"前端读了手写的 current_status：{bad}"


def test_dead_renderer_stays_deleted():
    import thesis_breaks
    assert not hasattr(thesis_breaks.ThesisBreakMonitor, "generate_html_section"), (
        "generate_html_section 回来了：它把每条条件写死成「✅ 安全」，v0.45.793 删除，勿恢复")
