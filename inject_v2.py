#!/usr/bin/env python3
"""
inject_v2.py — Claude 推理注入器
将 NARRATIVES 中的七章深度叙事注入到对应的 HTML 报告文件中。

用法：
  python3 inject_v2.py
"""

import re
import sys
import glob
from pathlib import Path
from datetime import date

# ── 路径配置 ──────────────────────────────────────────────────────────────────
import glob as _g
_SESSIONS = sorted(_g.glob("/sessions/*/mnt/Alpha Hive"), reverse=True)
ALPHAHIVE_DIR = Path(_SESSIONS[0]) if _SESSIONS else Path("/sessions/keen-magical-wright/mnt/Alpha Hive")
_DEEP_DIRS = sorted(_g.glob("/sessions/*/mnt/深度分析报告/深度"), reverse=True)
OUTPUT_DIR = Path(_DEEP_DIRS[0]) if _DEEP_DIRS else ALPHAHIVE_DIR

TODAY = date.today().strftime("%Y-%m-%d")
TICKER = "NVDA"
HTML_FILE = OUTPUT_DIR / f"deep-{TICKER}-{TODAY}.html"

# ── 七章叙事内容（由 Claude 推理填写）────────────────────────────────────────
NARRATIVES = {

"swarm_analysis": "",

"resonance": "",

"catalyst": "",

"options": "",

"macro": "",

"scenario": "",

"risk": "",

}

# ── 注入引擎 ──────────────────────────────────────────────────────────────────

def inject_narratives(html_path: Path, narratives: dict) -> str:
    """将 narratives 内容注入 HTML，替换对应章节的 prose div"""
    content = html_path.read_text(encoding="utf-8")

    CH_MAP = {
        "swarm_analysis": ("ch1", "ch2"),
        "resonance":      ("ch2", "ch3"),
        "catalyst":       ("ch3", "ch4"),
        "options":        ("ch4", "ch5"),
        "macro":          ("ch5", "ch6"),
        "scenario":       ("ch6", "ch7"),
        "risk":           ("ch7", None),
    }

    replaced = 0
    report = {}

    for key, new_html in narratives.items():
        if not new_html.strip():
            print(f"   ⏭  {key}: 空内容，跳过")
            continue

        ch_start, ch_end = CH_MAP[key]

        # 提取当前章节的 HTML 范围
        start_marker = f'<!-- {ch_start.upper()} -->'
        end_marker   = f'<!-- {ch_end.upper()} -->' if ch_end else '</body>'

        start_idx = content.find(start_marker)
        end_idx   = content.find(end_marker, start_idx + 1) if ch_end else len(content) - 20

        if start_idx == -1:
            print(f"   ⚠️  找不到标记 {start_marker}")
            continue

        chapter_html = content[start_idx:end_idx]

        # 找到 prose div 并替换
        prose_pattern = r'<div class="prose"[^>]*>.*?</div>'
        prose_match = re.search(prose_pattern, chapter_html, re.DOTALL)
        if not prose_match:
            print(f"   ⚠️  {key}: 找不到 prose div")
            continue

        new_prose = f'<div class="prose">{new_html}</div>'
        new_chapter = chapter_html[:prose_match.start()] + new_prose + chapter_html[prose_match.end():]
        content = content[:start_idx] + new_chapter + content[end_idx:]
        replaced += 1
        report[key] = "✅"
        print(f"   ✅ {key}: 已替换")

    # 特殊结构检查
    sc_card_ok = "sc-card" in content and "sc-note" in content
    trade_grid_ok = "trade-grid" in content
    risk_ol_ok = "<ol>" in content and "<li>" in content
    stop_loss_ok = bool(re.search(r'\$\d+\.?\d*\s*(或|做止损参考)', content))

    print(f"\n   📊 结构检查:")
    print(f"      CH6 sc-card:    {'✅' if sc_card_ok else '❌'}")
    print(f"      CH6 trade-grid: {'✅' if trade_grid_ok else '❌'}")
    print(f"      CH7 risk-list:  {'✅' if risk_ol_ok else '❌'}")
    print(f"      止损价格格式:   {'✅' if stop_loss_ok else '⚠️  未检测到'}")

    return content, replaced


def main():
    if not HTML_FILE.exists():
        # 尝试找最新文件
        candidates = sorted((OUTPUT_DIR).glob(f"deep-{TICKER}-*.html"), reverse=True)
        if candidates:
            target = candidates[0]
            print(f"⚠️  {HTML_FILE.name} 不存在，使用最新: {target.name}")
        else:
            print(f"❌ 找不到 deep-{TICKER}-*.html，请先运行 generate_deep_v2.py")
            sys.exit(1)
    else:
        target = HTML_FILE

    print(f"🐝 inject_v2.py — 注入 Claude 推理叙事")
    print(f"   目标文件: {target}")
    print(f"   章节数量: {len([v for v in NARRATIVES.values() if v.strip()])}/7")
    print()

    new_content, replaced = inject_narratives(target, NARRATIVES)

    # 写回文件
    target.write_text(new_content, encoding="utf-8")

    print(f"\n✅ 注入完成！{replaced}/7 章节已替换")
    print(f"   输出: {target}")

    return replaced


if __name__ == "__main__":
    main()
