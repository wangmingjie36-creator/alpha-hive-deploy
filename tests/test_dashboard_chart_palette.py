"""仪表板图表配色：紫色清零 + 分类色令牌过校验（v0.45.358）

背景
----
`templates/dashboard.js` 的 Chart.js 图表一直硬编码着「AI 味」紫色 `#667eea` / `#764ba2` / `#8b5cf6`
（雷达图 ×2、胜率趋势、资金曲线 Gross 线、多标的趋势调色板）。单系列的几处换成 `--acc` 即可；
**多标的趋势图不行**：站点令牌里没有「分类色」（`--acc2`/`--acc3` 与 `--bull`/`--neut` 同值），旧的
10 色表还混着看多绿 `#22c55e` / 看空红 `#ef4444`——本站红 = 看空，标的线被涂红会被读成信号——且按 `i%10` 循环。

于是本版在 `:root` / `html.dark` 新增 `--series-1..4`，用 dataviz skill 的校验器（`validate_palette.js`，
Machado 2009 CVD 模拟、OKLab ΔE×100）在两套主题、两种底色上 all-pairs 通过后才落地。
all-pairs 而不是 adjacent：趋势图的颜色跟标的走（激活领最小空闲槽），任意两个槽都可能同屏。

本文件把那次校验**钉成会红的测试**：谁改了分类色的值却没重跑校验器，这里红。
校验算法照抄校验器（常数逐字一致），并用「复现校验器当时报出的数字」自证抄对了——
不直接调用 skill 目录里的脚本：那个路径只在一台机器上存在（CLAUDE.md「skip 守卫要问 X 在哪些环境里存在」）。
"""

import math
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "templates" / "dashboard.css").read_text(encoding="utf-8")
JS = (ROOT / "templates" / "dashboard.js").read_text(encoding="utf-8")

# ────────── 校验器算法（与 dataviz validate_palette.js 同常数）──────────

_MACHADO = {
    "protan": [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
    "deutan": [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.011820, 0.042940, 0.968881]],
}
BAND = {"light": (0.43, 0.77), "dark": (0.48, 0.67)}
CHROMA_FLOOR, CVD_TARGET, NORMAL_FLOOR, CONTRAST_MIN = 0.10, 8.0, 15.0, 3.0


def _lin(h):
    h = h.lstrip("#")
    return [(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
            for c in (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))]


def _oklab_from_lin(rgb):
    r, g, b = rgb
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _simulate(h, kind):
    M = _MACHADO[kind]
    r, g, b = _lin(h)
    return [max(0.0, min(1.0, M[i][0] * r + M[i][1] * g + M[i][2] * b)) for i in range(3)]


def delta_e(h1, h2, kind=None):
    a = _oklab_from_lin(_simulate(h1, kind) if kind else _lin(h1))
    b = _oklab_from_lin(_simulate(h2, kind) if kind else _lin(h2))
    return 100 * math.dist(a, b)


def oklch(h):
    L, a, b = _oklab_from_lin(_lin(h))
    return L, math.hypot(a, b)


def contrast(a, b):
    def lum(h):
        r, g, bb = _lin(h)
        return 0.2126 * r + 0.7152 * g + 0.0722 * bb
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def worst_all_pairs(palette):
    pairs = [(palette[i], palette[j]) for i in range(len(palette)) for j in range(i + 1, len(palette))]
    cvd = min(delta_e(a, b, k) for a, b in pairs for k in ("protan", "deutan"))
    normal = min(delta_e(a, b) for a, b in pairs)
    return cvd, normal


# ────────── 从 dashboard.css 读令牌（`--series-1:var(--acc)` 要解引用）──────────

def _tokens(selector):
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    out = {}
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in (p.strip() for p in m.group(1).split(",")):
            for d in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+)", m.group(2)):
                out[d.group(1)] = d.group(2).strip()
    return out


def _theme(mode):
    toks = _tokens(":root")
    if mode == "dark":
        toks.update(_tokens("html.dark"))

    def resolve(v, depth=0):
        m = re.fullmatch(r"var\(\s*(--[\w-]+)\s*\)", v)
        return resolve(toks[m.group(1)], depth + 1) if m and depth < 5 else v
    return {k: resolve(v) for k, v in toks.items()}


SERIES = ["--series-1", "--series-2", "--series-3", "--series-4"]


def _palette(mode):
    t = _theme(mode)
    return [t[s] for s in SERIES], t


# ────────── A. 尺子先自证：复现校验器 09-28 报出的数字 ──────────

@pytest.mark.parametrize("pal,cvd,normal", [
    (["#B7410E", "#0250ab", "#1a9a95", "#8c1d62"], 13.3, 17.0),   # 浅色，validate_palette.js --pairs all
    (["#E05A1F", "#1c6ec9", "#09aba2", "#ad4389"], 10.9, 18.6),   # 暗色
])
def test_port_reproduces_the_validator(pal, cvd, normal):
    c, n = worst_all_pairs(pal)
    assert round(c, 1) == cvd and round(n, 1) == normal


def test_port_rejects_the_old_palette():
    """旧 10 色表的前 4 个（默认同屏的那几条）：同一把尺子要能判它不合格。"""
    c, n = worst_all_pairs(["#667eea", "#F4A532", "#22c55e", "#ef4444"])
    assert c < CVD_TARGET or n < NORMAL_FLOOR


# ────────── B. 分类色令牌的六项门槛（两套主题 × 两种图表底色）──────────

@pytest.mark.parametrize("mode", ["light", "dark"])
def test_series_tokens_pass_the_gates(mode):
    pal, t = _palette(mode)
    assert all(re.fullmatch(r"#[0-9a-fA-F]{6}", c) for c in pal), f"{mode} 解析出的分类色不是 hex：{pal}"
    lo, hi = BAND[mode]
    for c in pal:
        L, C = oklch(c)
        assert lo <= L <= hi, f"{c} 亮度 {L:.3f} 出了 {mode} 带 {lo}–{hi}"
        assert C >= CHROMA_FLOOR, f"{c} 彩度 {C:.3f} 低于 {CHROMA_FLOOR}，读起来是灰的"
        for surf in ("--surface", "--surface2"):
            assert contrast(c, t[surf]) >= CONTRAST_MIN, f"{c} 在 {surf}={t[surf]} 上对比度不足 3:1"
    cvd, normal = worst_all_pairs(pal)
    assert cvd >= CVD_TARGET, f"{mode} all-pairs CVD ΔE {cvd:.1f} < {CVD_TARGET}"
    assert normal >= NORMAL_FLOOR, f"{mode} all-pairs 常视 ΔE {normal:.1f} < {NORMAL_FLOOR}"


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_ticker_colors_do_not_look_like_bull_or_bear(mode):
    """本站红 = 看空、绿 = 看多。标的线（slot 2–4）与涨跌色的常视 ΔE 至少 10。
    slot 1 是站点强调色 --acc，全站本就与涨跌色并用，不在此列。"""
    pal, t = _palette(mode)
    for c in pal[1:]:
        for sem in ("--bull", "--bear"):
            assert delta_e(c, t[sem]) >= 10, f"{mode} {c} 太像 {sem}={t[sem]}"


# ────────── C. 紫色清零 + JS 接线 ──────────

_PURPLE = re.compile(r"#(667eea|764ba2|8b5cf6)\b|rgba?\(\s*(102\s*,\s*126\s*,\s*234|118\s*,\s*75\s*,\s*162)", re.I)


def _strip_comments(src, kind):
    if kind in ("css", "js"):
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    if kind == "js":
        src = re.sub(r"(?m)(^|[^:'\"])//.*$", r"\1", src)
    if kind == "py":
        src = re.sub(r"(?m)#(?![0-9a-fA-F]{3,8}\b).*$", "", src)
    return src


@pytest.mark.parametrize("rel,kind", [
    ("templates/dashboard.js", "js"), ("templates/dashboard.css", "css"),
    ("templates/dashboard.html", "html"), ("dashboard_renderer.py", "py"),
])
def test_no_ai_purple_on_the_dashboard(rel, kind):
    src = _strip_comments((ROOT / rel).read_text(encoding="utf-8"), kind)
    hits = sorted({m.group(0) for m in _PURPLE.finditer(src)})
    assert not hits, f"{rel} 里还有紫色：{hits}（单系列用 --acc，多系列用 --series-N）"


def test_purple_pattern_has_teeth():
    js = "backgroundColor:'rgba(102,126,234,.13)',borderColor:'#667eea',"
    assert len(_PURPLE.findall(_strip_comments(js, "js"))) == 2
    assert not _PURPLE.search(_strip_comments("// 旧的 #667eea 已清掉", "js"))


def test_trend_chart_uses_series_tokens_without_cycling():
    start = JS.index("window.AH.initTrendChart=function")
    body = JS[start:JS.index("\nwindow.AH.initTrendChart();", start)]   # 同名调用在 toggleDark 里更早出现过
    assert len(body) > 1000, "没截到趋势图函数体"
    assert "const SERIES=['--series-1','--series-2','--series-3','--series-4'];" in body
    assert "SERIES.map(_tok)" in body
    assert "%colors.length" not in body and "% colors.length" not in body, "调色板又在循环了"
    assert not re.search(r"'#[0-9a-fA-F]{6}'", _strip_comments(body, "js")), "趋势图里又出现了硬编码色"
    # 选中状态要活过明暗切换（切换会重建图、不重建 chip）
    assert "window.AH._trendState" in body and "window.AH.toggleTrendTicker(tk)" in body


def test_series_count_matches_css():
    assert set(SERIES) <= set(_tokens(":root")), "JS 用到的 --series-N 在 :root 里没定义全"
