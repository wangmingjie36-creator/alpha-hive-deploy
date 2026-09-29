"""dashboard.css 组件层不许写死颜色（v0.45.368）

v0.45.367 把 dashboard.js 清到零颜色字面量；本文件把同一条线画到 dashboard.css：颜色只在
`:root` / `html.dark` 里定义一次，组件规则一律 `var(--x)`，半透明淡染写 `rgba(var(--tint-x),α)`。

写死颜色的后果从不报错，只在某一种主题下看得见。改前实测（WCAG 对比度，字色 vs 淡染合成后的底）：
  - `.fresh-*`（数据新鲜度徽章）用的是暗色主题的高亮色 `#4ade80`/`#fbbf24`，浅色主题下 1.57 / 1.52:1；
  - `.tb-l2` / `.tb-l1`（失效条件分级）反过来用浅色深红深橙，暗色下 2.34 / 2.69:1；
  - 实色块上的 `color:#fff` 在暗色下压高亮 `--acc2`/`--acc3` 只剩 2.28 / 2.15:1。
没有任何测试会因此变红——本文件就是那个会红的。

放行的只有「本就不该随主题变」的几类，逐条写明理由（`ALLOWED`）；打印样式整段不查（纸永远是白的）。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "templates" / "dashboard.css"

# CSS 命名色：常见的这批写进来（全表 148 个，罕见名不值得换可读性）。复查时发现最初只认 white/black，
# `color:red` 能原样溜过去。
_NAMED = ("white|black|red|green|blue|gray|grey|orange|yellow|purple|pink|brown|navy|teal|silver|gold|"
          "maroon|lime|olive|aqua|cyan|magenta|crimson|tomato|coral|salmon|khaki|ivory|beige|indigo|violet|"
          "darkred|darkgreen|darkblue|darkorange|lightgray|lightgrey|darkgray|darkgrey|dimgray|dimgrey|"
          "whitesmoke|gainsboro|slategray|slategrey|steelblue|firebrick|forestgreen|goldenrod|orangered")
_LITERAL = re.compile(
    r"#[0-9A-Fa-f]{3,8}\b"
    r"|rgba?\((?!\s*var\()[^)]*\)"          # rgba(var(--tint-x),α) 是令牌写法，放行
    r"|hsla?\([^)]*\)|(?:ok)?(?:lab|lch)\([^)]*\)|hwb\([^)]*\)|color\([^)]*\)"
    rf"|(?<![-\w])(?:{_NAMED})(?![-\w])"    # 不误伤 white-space / --bull
)
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")  # font-family / content 里的字符串不算颜色

# (选择器, 声明里出现的字面量) → 为什么它不该是令牌
ALLOWED = {
    (".slogo", "#fff"): "公司 logo 托底板：logo 图按白底设计，暗色主题下也要白底",
    (".share-btn-x:hover", "#000"): "X（Twitter）品牌色",
    (".share-btn-x:hover", "#fff"): "X（Twitter）品牌色",
    (".scard-share", "rgba(0,0,0,.35)"): "浮在卡片角上的半透明遮罩按钮，两种主题都是暗遮罩",
    (".scard-share", "#fff"): "同上，遮罩按钮上的图标",
    (".kb-help", "rgba(0,0,0,.5)"): "模态遮罩（scrim），两种主题都压暗背后内容",
    (".nav-overlay", "rgba(0,0,0,.5)"): "移动端菜单遮罩（scrim）",
    (".ah-macro-viewport", "#000"): "mask-image 只取 alpha 通道，不是显示色",
}


def _strip_print(css: str) -> str:
    """去掉 `@media print{...}` 整段（花括号配对，内部有嵌套规则）。"""
    out, i = [], 0
    for m in re.finditer(r"@media\s+print\s*\{", css):
        if m.start() < i:
            continue
        out.append(css[i:m.start()])
        depth, j = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        i = j
    out.append(css[i:])
    return "".join(out)


def _offenders(css: str, allowed=ALLOWED):
    css = _QUOTED.sub('""', _strip_print(re.sub(r"/\*.*?\*/", "", css, flags=re.S)))
    hits = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        sel = m.group(1).strip()
        if sel in (":root", "html.dark"):
            continue
        for lit in _LITERAL.finditer(m.group(2)):
            if (sel, lit.group(0)) not in allowed:
                hits.append(f"{sel} → {lit.group(0)}")
    return hits


def _tokens(selector: str) -> dict:
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    out = {}
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if m.group(1).strip() == selector:
            out.update({d.group(1): d.group(2).strip()
                        for d in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+)", m.group(2))})
    return out


# ────────── 主断言 ──────────

def test_no_color_literals_outside_token_blocks():
    hits = _offenders(CSS.read_text(encoding="utf-8"))
    assert not hits, (
        "dashboard.css 组件规则里写死了颜色（换主题不会跟着变）：\n  " + "\n  ".join(hits)
        + "\n文字/线用 var(--tp/--ts/--bull/--bear/--neut/--acc)，实色块上的字用 var(--on-solid)，"
        "淡染底用 rgba(var(--tint-bull),.12)。真不该随主题变的，加进 ALLOWED 并写理由。")


def test_allowlist_is_not_stale():
    """白名单只许缩：对应规则改掉了，条目要一起删，否则它会默默放行将来同名的新字面量。"""
    live = set()
    css = _strip_print(re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S))
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        live |= {(m.group(1).strip(), lit.group(0)) for lit in _LITERAL.finditer(m.group(2))}
    assert not (set(ALLOWED) - live), f"白名单里已不存在的条目：{sorted(set(ALLOWED) - live)}"


def test_tint_channels_are_rgb_triples():
    root, dark = _tokens(":root"), _tokens("html.dark")
    tints = {k: v for k, v in root.items() if k.startswith("--tint-")}
    assert {"--tint-bull", "--tint-bear", "--tint-neut", "--tint-acc", "--tint-slate", "--tint-ink"} <= set(tints)
    for k, v in {**tints, **{k: v for k, v in dark.items() if k.startswith("--tint-")}}.items():
        parts = [p.strip() for p in v.split(",")]
        assert len(parts) == 3 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts), (
            f"{k}: {v!r} 不是 r,g,b 通道——rgba(var({k}),α) 会整条失效，底色直接消失")
    assert "--tint-ink" in dark, "--tint-ink 是墨色淡染，暗色主题必须翻成浅色"


# ────────── 实色块上的字 ──────────

def _lum(hexc):
    c = [int(hexc.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _cr(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_helper_self_check():
    assert round(_cr("#FFFFFF", "#000000"), 1) == 21.0
    assert round(_cr("#FFFFFF", "#22c55e"), 2) == 2.28  # 改前暗色 .trend-chip.active 的实际值


@pytest.mark.parametrize("theme", [":root", "html.dark"])
@pytest.mark.parametrize("fill", ["--acc", "--acc2", "--acc3"])
def test_on_solid_reads_on_every_fill(theme, fill):
    toks = {**_tokens(":root"), **_tokens(theme)}
    ratio = _cr(toks["--on-solid"], toks[fill])
    assert ratio >= 4.5, f"{theme} 下 --on-solid 压 {fill} 只有 {ratio:.2f}:1"


def test_solid_fills_use_on_solid_ink():
    """以 --acc/--acc2/--acc3 为实底、同一条规则里又设了字色的，字色必须是 --on-solid。"""
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    bad, seen = [], 0
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        body = m.group(2)
        if re.search(r"background:var\(--acc[23]?\)", body):
            c = re.search(r"(?<![-\w])color:([^;}]+)", body)
            if c:
                seen += 1
                if c.group(1).strip() != "var(--on-solid)":
                    bad.append(f"{m.group(1).strip()} color:{c.group(1).strip()}")
    assert seen >= 6, f"只找到 {seen} 条实色块规则，扫描器多半坏了"
    assert not bad, f"实色块上的字没走 --on-solid：{bad}"


# ────────── 尺子要有牙 ──────────

@pytest.mark.parametrize("snippet,expected", [
    (".x{color:#fff}", [".x → #fff"]),
    (".x{background:rgba(34,197,94,.12)}", [".x → rgba(34,197,94,.12)"]),
    (".x{border:1px solid white}", [".x → white"]),
    (".x{background:hsl(0 0% 0%)}", [".x → hsl(0 0% 0%)"]),
    (".x{color:red}", [".x → red"]),
    (".x{border-color:slategray}", [".x → slategray"]),
    (".x{color:oklch(60% .1 30)}", [".x → oklch(60% .1 30)"]),
    (".x{font-family:'Gold Sans',serif;content:\"red\"}", []),
    (".x{color:var(--bull);border-left:3px solid var(--bear)}", []),
    (".x{background:rgba(var(--tint-bull),.12);color:var(--bull);white-space:nowrap}", []),
    ("@media print{body{background:#fff;color:#000}.hero{background:#f8f8f8}}", []),
    ("@media print{body{color:#000}}.y{color:#333}", [".y → #333"]),
    (":root{--tp:#1A1208}html.dark{--tp:#e2e8f0}", []),
    ("/* color:#fff */.z{color:var(--tp)}", []),
])
def test_scanner_has_teeth(snippet, expected):
    assert _offenders(snippet, allowed={}) == expected


def test_scanner_flags_the_pre_fix_rules():
    """改前的真实写法（v0.45.367 时的 dashboard.css 原文）喂回去必须点名。"""
    before = (
        ".fresh-ok{background:rgba(34,197,94,.12);color:#4ade80;border:1px solid rgba(34,197,94,.3)}\n"
        ".trend-chip.active{background:var(--acc2);color:#fff;border-color:var(--acc2)}\n"
        "html:not(.dark) .hm-tk{background:rgba(0,0,0,.04);border-color:rgba(0,0,0,.06)}\n"
        ".srank{background:var(--acc);color:#0A0F1C}\n"
    )
    assert len(_offenders(before)) == 7
