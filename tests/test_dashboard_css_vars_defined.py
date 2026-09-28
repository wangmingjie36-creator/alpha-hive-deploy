"""仪表板引用的 CSS 自定义属性必须在 `:root` 定义（v0.45.352）

背景
----
`var(--mt)` / `var(--t)` / `var(--card)` 从未定义过（站点令牌里只有 `--tp`/`--ts`/`--tm`/
`--surface`/`--surface2`），却在 `dashboard_renderer.py` 与 `templates/dashboard.js` 里活了一个月：
08-31 的 `e8bac95` 指出过、只修了 renderer 那 9 处、从未合并；v0.45.226 对账再指出一次；
其间 JS 那 8 处和 `--card` 一直没人看见。

后果**不是**报错，也**不是** `e8bac95` 提交信息说的「文字恒黑」。按 CSS 规范，引用未定义变量的
声明在计算值阶段失效（invalid at computed-value time），退化为该属性的「未设置」值：
  - `color` 是继承属性 → 等于父元素颜色。v0.45.226 实测 266 个元素 0 个黑、全部等于父色 ⇒
    「弱化色 / 主色」的区分悄悄丢了（「今日 Actionable」标题本该弱化，渲染成了主色）；
  - `background` 不继承 → 初始值 transparent（交易统计卡片的底色从未出现过）。
浏览器不报错、测试不报错、截图上只是「颜色有点不对」—— **没有任何东西会红**。本文件就是那个会红的。

扫描范围与口径
--------------
- 仪表板（`index.html`）的全部样式来源：renderer 内联样式 + `templates/` 下的 css / html / js。
- 引用有两种写法都要管：`var(--x)`；以及 canvas 取令牌的 `getPropertyValue('--x')`
  （取不到返回空串，Chart.js 静默退回默认灰，同样不报错）。
- 「已定义」只认 `templates/dashboard.css` 里选择器为 `:root` 的规则。**不**从全文 grep `--x:` ——
  renderer 里一段 Markdown 表格分隔行 `|---|:---:|` 就会被这种粗扫当成定义了 `---`。
- 不覆盖：`generate_ml_report.py` / `generate_deep_v2.py` / `paper_portfolio.py` 等独立页面，
  它们的令牌来自别处拼装的 CSS（如 `DEEP_REPORT_CSS.txt`），要单独确认各自的定义来源再立守卫。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "templates" / "dashboard.css"
SOURCES = [
    ROOT / "dashboard_renderer.py",
    ROOT / "templates" / "dashboard.css",
    ROOT / "templates" / "dashboard.html",
    ROOT / "templates" / "dashboard.js",
]

_NAME = r"--[A-Za-z0-9_-]+"
_REF_PATTERNS = (
    re.compile(rf"var\(\s*({_NAME})"),
    re.compile(rf"getPropertyValue\(\s*['\"]({_NAME})['\"]"),
)
# JS 里把令牌名当字符串传的写法（v0.45.358 起 `_tok('--acc')` / `SERIES=['--series-1',...]`）。
# 只对 .js 生效：.py 里的 '--xxx' 多半是命令行参数。
_JS_QUOTED = re.compile(rf"['\"]({_NAME})['\"]")
_COLOR_VALUE = re.compile(r"^\s*(#[0-9A-Fa-f]{3,8}|rgba?\(|hsla?\()")


def _tokens(css: str, selector: str) -> dict:
    """选择器（逗号分隔中任一项）恰为 `selector` 的规则里声明的自定义属性 → 值。

    只匹配不含嵌套花括号的最内层规则，所以 `@media{...}` 里的同名规则也算（那也是定义）。
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = {}
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if selector not in (p.strip() for p in m.group(1).split(",")):
            continue
        for d in re.finditer(rf"({_NAME})\s*:\s*([^;]+)", m.group(2)):
            out[d.group(1)] = d.group(2).strip()
    return out


def _references(text: str, js: bool = False):
    """→ [(name, lineno)]，按出现顺序。`js=True` 时另收字符串字面量里的令牌名。"""
    refs = []
    for pat in _REF_PATTERNS + ((_JS_QUOTED,) if js else ()):
        for m in pat.finditer(text):
            refs.append((m.group(1), text.count("\n", 0, m.start()) + 1))
    return sorted(set(refs), key=lambda r: r[1])


def _undefined(text: str, defined, js: bool = False) -> list:
    return [(n, ln) for n, ln in _references(text, js) if n not in defined]


@pytest.fixture(scope="module")
def root_tokens():
    return _tokens(CSS.read_text(encoding="utf-8"), ":root")


# ────────── A. 主断言 ──────────

def test_every_referenced_custom_property_is_defined_in_root(root_tokens):
    offenders = []
    for src in SOURCES:
        for name, ln in _undefined(src.read_text(encoding="utf-8"), root_tokens, js=src.suffix == ".js"):
            offenders.append(f"{src.relative_to(ROOT)}:{ln} {name}")
    assert not offenders, (
        "引用了 :root 里没有的 CSS 变量（不会报错，只会悄悄退化成继承色 / 透明）：\n  "
        + "\n  ".join(offenders)
        + f"\n已定义的令牌：{sorted(root_tokens)}。"
        "文字弱化色用 --ts（--tm 在浅色底上只有 2.2~2.5:1，达不到正文对比度），主色用 --tp。"
    )


def test_dark_theme_overrides_every_color_token(root_tokens):
    """`:root` 里的每个颜色令牌都必须在 `html.dark` 里重定义一次。

    漏了的那个在暗色主题下沿用浅色值（如浅色的深棕文字压在海军蓝底上）。
    这和「未定义」是同一类：不报错，只在切到暗色时才看得见。
    """
    dark = _tokens(CSS.read_text(encoding="utf-8"), "html.dark")
    colors = {n for n, v in root_tokens.items() if _COLOR_VALUE.match(v)}
    assert colors, "解析器一个颜色令牌都没读到——本条会空转"
    missing = sorted(colors - set(dark))
    assert not missing, f"这些颜色令牌只有浅色值、暗色主题下不变：{missing}"


# ────────── B. 尺子自己要准（否则上面的全绿证明不了任何事）──────────

def test_root_parser_reads_the_site_tokens(root_tokens):
    expected = {"--bg", "--surface", "--surface2", "--border", "--tp", "--ts", "--tm",
                "--acc", "--bull", "--bear", "--neut"}
    assert expected <= set(root_tokens), f"缺 {sorted(expected - set(root_tokens))}"
    assert "---" not in root_tokens


def test_scanner_sees_the_real_references():
    """扫描器若因正则回归一个引用都找不到，主断言会恒绿。"""
    names = set()
    total = 0
    for src in SOURCES:
        refs = _references(src.read_text(encoding="utf-8"))
        total += len(refs)
        names |= {n for n, _ in refs}
    assert total > 200, f"四个文件只扫到 {total} 处引用，扫描器多半坏了"
    assert {"--bull", "--bear", "--neut", "--tp", "--ts", "--border"} <= names
    js = (ROOT / "templates" / "dashboard.js").read_text(encoding="utf-8")
    js_names = {n for n, _ in _references(js, js=True)}
    assert {"--acc", "--bull", "--series-1", "--series-4", "--surface"} <= js_names, (
        f"dashboard.js 里 `_tok('--x')` / SERIES 数组的令牌名没被扫到：{sorted(js_names)}")


@pytest.mark.parametrize("snippet,expected", [
    ('<div style="color:var(--mt);">', ["--mt"]),
    ("var c=color||'var(--t)';", ["--t"]),
    ("background:var(--card);border:1px", ["--card"]),
    ("rootCS.getPropertyValue('--nope').trim()", ["--nope"]),
    ("borderColor:_tok('--nope2'),", ["--nope2"]),
    ("const SERIES=['--series-1','--series-9'];", ["--series-9"]),
    ('color:var( --spaced )', ["--spaced"]),
    ('color:var(--bull);background:var(--surface2)', []),
])
def test_scanner_catches_undefined_names(root_tokens, snippet, expected):
    """反向自证：把本版修掉的三个名字喂回去，扫描器必须点名。"""
    assert [n for n, _ in _undefined(snippet, root_tokens, js=True)] == expected


def test_scanner_flags_the_pre_fix_sources(root_tokens):
    """用修复前的真实代码形状再证一次（不依赖 git 历史）：v0.45.352 之前这些行都在。"""
    before = (
        "'<div style=\"font-weight:700;color:var(--mt);margin-bottom:6px\">今日 Actionable</div>'\n"
        "<div style=\"color:var(--t);font-size:.92em;margin-bottom:3px\"><strong>{action}</strong></div>\n"
        "return '<div style=\"background:var(--card);border:1px solid var(--border);border-radius:8px\">'+\n"
    )
    assert [n for n, _ in _undefined(before, root_tokens)] == ["--mt", "--t", "--card"]
