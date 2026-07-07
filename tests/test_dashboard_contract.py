"""P1-2 (v0.38.0): 模板 ↔ JS 契约测试

防 equityChart 式死骨架复发：v35.2 曾发现 dashboard.html 里的
`<canvas id="equityChart">` 有 HTML 骨架但 dashboard.js 从未接线，
面板静默空白数周。本测试用纯静态文本断言锁住两层契约：

1. 模板/渲染器产出的每个 canvas id，在 dashboard.js 中必须有对应引用
   （getElementById / renderChart 分支 / 动态拼接前缀）
2. dashboard.js 消费的每个 `__AH__.<key>`，必须存在于
   dashboard_renderer.py 的 _data_obj 键集合中

零浏览器依赖，毫秒级运行。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
TPL_HTML = ROOT / "templates" / "dashboard.html"
TPL_JS = ROOT / "templates" / "dashboard.js"
RENDERER = ROOT / "dashboard_renderer.py"


@pytest.fixture(scope="module")
def html_src() -> str:
    return TPL_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js_src() -> str:
    return TPL_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def renderer_src() -> str:
    return RENDERER.read_text(encoding="utf-8")


# ── 契约 1：canvas id ↔ JS handler ──────────────────────────────────────────

# 动态生成的 canvas（渲染器 f-string 拼接 id 前缀），JS 端按前缀处理
_DYNAMIC_CANVAS_PREFIXES = ("radar-",)


def _canvas_ids(src: str) -> set:
    """提取 <canvas id="..."> 的静态 id（跳过含 f-string 插值的动态 id）"""
    ids = set()
    for m in re.finditer(r'<canvas\s+[^>]*id="([^"{]+)"', src):
        ids.add(m.group(1))
    return ids


def test_template_canvas_ids_have_js_handlers(html_src, js_src):
    """dashboard.html 模板里每个静态 canvas id 都必须被 dashboard.js 引用"""
    missing = []
    for cid in sorted(_canvas_ids(html_src)):
        if any(cid.startswith(p) for p in _DYNAMIC_CANVAS_PREFIXES):
            continue
        if f"'{cid}'" not in js_src and f'"{cid}"' not in js_src:
            missing.append(cid)
    assert not missing, (
        f"模板中的 canvas id 在 dashboard.js 中无对应引用（死骨架）: {missing}\n"
        "→ 参考 v35.2 equityChart 事故：给 renderChart() 加分支 + "
        "加入图表 id 列表（IntersectionObserver/load 兜底/暗黑重绘/bfcache 四处）"
    )


def test_renderer_canvas_ids_have_js_handlers(renderer_src, js_src):
    """dashboard_renderer.py 生成的 HTML blob 里的静态 canvas id 同样必须接线"""
    missing = []
    for cid in sorted(_canvas_ids(renderer_src)):
        if any(cid.startswith(p) for p in _DYNAMIC_CANVAS_PREFIXES):
            continue
        if f"'{cid}'" not in js_src and f'"{cid}"' not in js_src:
            missing.append(cid)
    assert not missing, (
        f"渲染器生成的 canvas id 在 dashboard.js 中无对应引用（死骨架）: {missing}"
    )


def test_dynamic_radar_prefix_still_wired(js_src):
    """动态 radar-{ticker} canvas 依赖 JS 端的前缀处理逻辑——防止被重构删掉"""
    assert "radar-" in js_src, "dashboard.js 已无 radar- 前缀处理，动态雷达图将全部空白"


# ── 契约 2：__AH__ 消费键 ↔ _data_obj 生产键 ────────────────────────────────

def _consumed_ah_keys(js_src: str) -> set:
    """提取 dashboard.js 中 __AH__.xxx 的消费键"""
    return set(re.findall(r"__AH__\.([A-Za-z_][A-Za-z0-9_]*)", js_src))


def _produced_data_keys(renderer_src: str) -> set:
    """提取 dashboard_renderer.py 中 _data_obj 的生产键。

    覆盖两种写法：字典字面量 `"key": ...`（_data_obj = {...} 块内）
    与后续赋值 `_data_obj["key"] = ...`
    """
    keys = set(re.findall(r'_data_obj\["([^"]+)"\]', renderer_src))
    # 字典字面量块：从 `_data_obj = {` 到闭合 `}`（按缩进近似截取）
    m = re.search(r"_data_obj\s*=\s*\{(.*?)\n    \}", renderer_src, re.S)
    if m:
        keys |= set(re.findall(r'^\s*"([A-Za-z_][A-Za-z0-9_]*)":', m.group(1), re.M))
    return keys


def test_ah_consumed_keys_are_produced(js_src, renderer_src):
    """dashboard.js 消费的 __AH__ 键必须由 dashboard_renderer._data_obj 生产"""
    consumed = _consumed_ah_keys(js_src)
    produced = _produced_data_keys(renderer_src)
    assert produced, "解析 _data_obj 生产键失败——renderer 结构变了，更新本测试的提取逻辑"
    missing = sorted(consumed - produced)
    assert not missing, (
        f"dashboard.js 消费了 _data_obj 未生产的键（运行时 undefined）: {missing}"
    )


def test_data_json_placeholder_in_template(html_src):
    """模板必须注入 window.__AH__ = {{ data_json }}，否则全部图表无数据"""
    assert "__AH__" in html_src and "data_json" in html_src, (
        "dashboard.html 缺少 window.__AH__ = {{ data_json }} 注入"
    )


# ── 契约 3：模板占位符 ↔ 渲染器 render kwargs ───────────────────────────────

def test_template_placeholders_have_render_kwargs(html_src, renderer_src):
    """dashboard.html 的 {{ placeholder }} 必须在 render(...) kwargs 中提供。

    Jinja2 未定义变量默认渲染为空字符串（静默丢内容），这里显式锁住。
    """
    placeholders = set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", html_src))
    # render kwargs: `name=` 出现在 _tpl.render( 之后的块里；宽松匹配全文 `name=`
    missing = [p for p in sorted(placeholders)
               if not re.search(rf"\b{re.escape(p)}\s*=", renderer_src)]
    assert not missing, (
        f"模板占位符在 dashboard_renderer.py 中无对应 render kwarg（会渲染为空）: {missing}"
    )
