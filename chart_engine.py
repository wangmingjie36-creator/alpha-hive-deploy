"""
chart_engine.py  —  Alpha Hive 图表引擎
========================================
生成两种嵌入式图表（base64 PNG），可直接插入 HTML 报告的 <img> 标签。

用法（在 generate_deep_v2.py 里）：
    from chart_engine import render_confidence_chart, render_options_chart

    confidence_img = render_confidence_chart(data, ticker, date_str)
    options_img    = render_options_chart(data, ticker, date_str, current_price)

    # 在 HTML 模板里：
    <img src="data:image/png;base64,{confidence_img}" style="width:100%;border-radius:8px;">

依赖：matplotlib（pip3 install matplotlib）
"""

from __future__ import annotations
import base64, io, math
from typing import Optional

# ─── 懒加载 matplotlib（避免 import 时崩溃）────────────────────────────────────
def _get_mpl():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import matplotlib.font_manager as fm
    # macOS 上自动找中文字体（PingFang > Heiti > STHeiti > fallback）
    _CJK_CANDIDATES = [
        "PingFang SC", "PingFang TC", "Heiti SC", "STHeiti",
        "Microsoft YaHei", "WenQuanYi Micro Hei",
        "Droid Sans Fallback", "Noto Sans CJK SC",
        "DejaVu Sans",
    ]
    available = {f.name for f in fm.fontManager.ttflist}
    chosen = next((f for f in _CJK_CANDIDATES if f in available), "DejaVu Sans")
    plt.rcParams["font.family"] = [chosen, "DejaVu Sans"]
    return plt, mpatches, fm

# ─── 调色板 ───────────────────────────────────────────────────────────────────
_BG    = "#0d1117"
_CARD  = "#161b22"
_ACCENT= "#58a6ff"
_RED   = "#f85149"
_GREEN = "#3fb950"
_GOLD  = "#d29922"
_T1    = "#e6edf3"
_T3    = "#8b949e"


def _fig_to_b64(fig) -> str:
    """Save matplotlib figure to base64 PNG string."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=_BG)
    buf.seek(0)
    import matplotlib.pyplot as plt
    plt.close(fig)
    return base64.b64encode(buf.read()).decode()


def _score_color(s: float) -> str:
    return _RED if s < 4 else (_GOLD if s < 6 else _GREEN)


# ═══════════════════════════════════════════════════════════════════════════════
# Chart 1  —  置信区间 + 维度评分
# ═══════════════════════════════════════════════════════════════════════════════

def render_confidence_chart(
    data: dict,
    ticker: str,
    date_str: str,
) -> Optional[str]:
    """
    生成蜂群评分置信区间图表，返回 base64 PNG 字符串。
    数据来源：data['swarm_results']

    返回 None 表示数据不足，无法生成图表。
    """
    try:
        plt, mpatches, fm = _get_mpl()

        sr = data.get("swarm_results", {})
        cc = sr.get("confidence_calibration", {})
        if not cc or not cc.get("confidence_band"):
            return None

        dims        = sr.get("dimension_scores", {})
        wts         = sr.get("dimension_weights", {})
        final_score = float(sr.get("final_score") or 0)
        cb          = cc["confidence_band"]
        cb_lo, cb_hi= float(cb[0]), float(cb[1])
        band_w      = float(cc.get("band_width", cb_hi - cb_lo))
        dim_std     = float(cc.get("dimension_std", 0))
        disc        = cc.get("discrimination", "–")

        DIM_ORDER  = ["catalyst", "odds", "signal", "sentiment", "risk_adj"]
        DIM_LABELS = ["催化剂", "胜率", "技术信号", "市场情绪", "风险调整"]

        fig = plt.figure(figsize=(12, 7), facecolor=_BG)

        # Title
        fig.text(0.5, 0.97,
                 f"{ticker}  ·  蜂群评分置信区间分析  ·  {date_str}",
                 ha="center", va="top", fontsize=14, fontweight="bold", color=_T1)
        direction = sr.get("direction", "–")
        fig.text(0.5, 0.918,
                 f"最终评分 {final_score:.2f}  ·  方向 {direction}  ·  "
                 f"置信区间 [{cb_lo:.2f}–{cb_hi:.2f}]  ·  "
                 f"维度分散度 σ={dim_std:.1f}  ·  鉴别力 {disc}",
                 ha="center", va="top", fontsize=9, color=_T3)

        score_col = _score_color(final_score)

        # ── Panel A: Score Ruler ──────────────────────────────────────────────
        ax1 = fig.add_axes([0.06, 0.60, 0.88, 0.26])
        ax1.set_facecolor(_CARD)
        ax1.set_xlim(0, 10); ax1.set_ylim(0, 1)
        for sp in ax1.spines.values(): sp.set_visible(False)
        ax1.set_xticks([]); ax1.set_yticks([])

        for x0, x1, col in [(0, 4, "#2a1414"), (4, 6, "#232314"), (6, 10, "#142314")]:
            ax1.add_patch(mpatches.Rectangle((x0, 0), x1-x0, 1, color=col, zorder=1))

        for i in range(11):
            ax1.axvline(i, ymin=0, ymax=0.12, color=_T3, lw=0.7, zorder=2)
            ax1.text(i, -0.10, str(i), ha="center", va="top", fontsize=9, color=_T3)

        for x, lbl, col in [(2, "BEARISH", _RED), (5, "NEUTRAL", _GOLD), (8, "BULLISH", _GREEN)]:
            ax1.text(x, 0.88, lbl, ha="center", fontsize=7.5, color=col,
                     alpha=0.45, fontweight="bold")

        # Confidence band
        ax1.add_patch(mpatches.Rectangle(
            (cb_lo, 0.22), cb_hi - cb_lo, 0.56, color=_ACCENT, alpha=0.18, zorder=3))
        ax1.axvline(cb_lo, ymin=0.18, ymax=0.82, color=_ACCENT, lw=1.5, ls="--", alpha=0.7, zorder=4)
        ax1.axvline(cb_hi, ymin=0.18, ymax=0.82, color=_ACCENT, lw=1.5, ls="--", alpha=0.7, zorder=4)
        ax1.annotate("", xy=(cb_hi, 0.5), xytext=(cb_lo, 0.5),
                     arrowprops=dict(arrowstyle="<|-|>", color=_ACCENT, lw=1.5), zorder=5)
        ax1.text(cb_lo, 0.85, f"{cb_lo:.2f}", ha="center", fontsize=9,
                 color=_ACCENT, fontweight="bold")
        ax1.text(cb_hi, 0.85, f"{cb_hi:.2f}", ha="center", fontsize=9,
                 color=_ACCENT, fontweight="bold")
        ax1.text((cb_lo+cb_hi)/2, 0.08, f"置信区间  宽={band_w:.2f}",
                 ha="center", fontsize=7.5, color=_ACCENT, alpha=0.85)

        # Final score diamond
        ax1.plot(final_score, 0.5, "D", ms=18, color=score_col,
                 zorder=7, mec="white", mew=1.8)
        ax1.text(final_score, 0.5, f"{final_score:.2f}",
                 ha="center", va="center", fontsize=8, color="white",
                 fontweight="bold", zorder=8)
        ax1.text(final_score, -0.24, "▲ 最终评分", ha="center",
                 fontsize=8, color=score_col, fontweight="bold")
        ax1.set_title("评分尺  (0=极度看跌  ·  5=中性  ·  10=极度看涨)",
                      fontsize=8.5, color=_T3, pad=5, loc="left")

        # ── Panel B: Dimension Bars ───────────────────────────────────────────
        ax2 = fig.add_axes([0.06, 0.09, 0.54, 0.42])
        ax2.set_facecolor(_CARD)
        for sp in ax2.spines.values(): sp.set_visible(False)

        scores  = [float(dims.get(k, 0) or 0) for k in DIM_ORDER]
        weights = [float(wts.get(k, 0) or 0)  for k in DIM_ORDER]
        colors  = [_score_color(s) for s in scores]
        y_pos   = list(range(len(DIM_ORDER)))

        ax2.barh(y_pos, scores, color=colors, height=0.58, alpha=0.88)
        for i, (s, w) in enumerate(zip(scores, weights)):
            ax2.text(s + 0.12, i, f"{s:.1f}", va="center",
                     fontsize=9.5, color=_T1, fontweight="bold")
            ax2.text(10.35, i, f"{w*100:.0f}%", va="center",
                     fontsize=7.5, color=_T3)

        ax2.set_xlim(0, 11.5)
        ax2.set_yticks(y_pos)
        ax2.set_yticklabels(DIM_LABELS, fontsize=9.5, color=_T1)
        ax2.axvline(final_score, color=score_col, lw=1.5, ls=":", alpha=0.75)
        ax2.axvline(5, color=_T3, lw=0.7, ls="--", alpha=0.35)
        ax2.set_xticks([0, 2, 4, 6, 8, 10])
        ax2.tick_params(axis="x", colors=_T3, labelsize=8)
        ax2.tick_params(axis="y", length=0)
        ax2.grid(axis="x", color="#30363d", lw=0.5)
        ax2.text(10.35, 4.85, "权重", fontsize=7, color=_T3, ha="center")
        ax2.set_title("五维度评分分解 (加权)", fontsize=9.5, color=_T3, pad=5, loc="left")

        # ── Panel C: Stats Card ───────────────────────────────────────────────
        ax3 = fig.add_axes([0.64, 0.09, 0.30, 0.42])
        ax3.set_facecolor(_CARD)
        for sp in ax3.spines.values(): sp.set_visible(False)
        ax3.set_xticks([]); ax3.set_yticks([])
        ax3.set_xlim(0, 1); ax3.set_ylim(0, 1)

        std_col = _RED if dim_std >= 2.5 else (_GOLD if dim_std >= 1.5 else _GREEN)
        std_lbl = "极高分散⚠" if dim_std >= 2.5 else ("中等分散" if dim_std >= 1.5 else "低分散✓")
        disc_col = _GREEN if disc == "high" else (_GOLD if disc == "medium" else _RED)
        conflict_col = _RED if sr.get("conflict_level") == "heavy" else _GOLD

        stats = [
            ("最终评分",      f"{final_score:.2f}",                   score_col),
            ("置信区间",      f"[{cb_lo:.2f} – {cb_hi:.2f}]",        _ACCENT),
            ("区间宽度",      f"{band_w:.2f}",                         _T1),
            ("维度分散度 σ",  f"{dim_std:.1f}  {std_lbl}",            std_col),
            ("鉴别力",        disc,                                    disc_col),
            ("冲突等级",      sr.get("conflict_level", "–"),          conflict_col),
            ("惩罚合计",      f"–{float(sr.get('total_penalty',0)):.2f}", _RED),
            ("GuardBee罚分",  f"–{float(sr.get('guard_penalty',0)):.3f}", _RED),
        ]
        for i, (lbl, val, col) in enumerate(stats):
            y = 0.94 - i * 0.115
            ax3.text(0.05, y, lbl, fontsize=8, color=_T3, va="center")
            ax3.text(0.95, y, val, fontsize=8.5, color=col, va="center",
                     ha="right", fontweight="bold")
            if i < len(stats) - 1:
                ax3.axhline(y - 0.053, color="#30363d", lw=0.5)
        ax3.set_title("统计摘要", fontsize=9.5, color=_T3, pad=5, loc="left")

        return _fig_to_b64(fig)

    except Exception as e:
        print(f"[chart_engine] render_confidence_chart failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Chart 2  —  期权关键水位图
# ═══════════════════════════════════════════════════════════════════════════════

def render_options_chart(
    data: dict,
    ticker: str,
    date_str: str,
    current_price: float = 0.0,
) -> Optional[str]:
    """
    生成期权关键水位图，返回 base64 PNG 字符串。
    数据来源：data['swarm_results']['agent_details']['OracleBeeEcho']

    返回 None 表示数据不足，无法生成图表。
    """
    try:
        plt, mpatches, fm = _get_mpl()

        oracle_raw = (data.get("swarm_results", {})
                          .get("agent_details", {})
                          .get("OracleBeeEcho", {}))
        # agent_details values may be dicts or strings
        if isinstance(oracle_raw, str):
            return None
        oracle = oracle_raw.get("details", {})
        if isinstance(oracle, str):
            return None

        support    = oracle.get("key_levels", {}).get("support",    [])
        resistance = oracle.get("key_levels", {}).get("resistance", [])
        unusual    = oracle.get("unusual_activity", [])
        pc_ratio   = float(oracle.get("put_call_ratio", 0) or 0)
        iv_rank    = float(oracle.get("iv_rank",    0) or 0)
        iv_curr    = float(oracle.get("iv_current", 0) or 0)
        total_oi   = int(oracle.get("total_oi",     0) or 0)

        if not support and not resistance:
            return None

        fig = plt.figure(figsize=(12, 7), facecolor=_BG)
        fig.text(0.5, 0.97,
                 f"{ticker}  ·  期权关键水位图  ·  {date_str}",
                 ha="center", va="top", fontsize=14, fontweight="bold", color=_T1)
        price_str = f"${current_price:.1f}" if current_price else "N/A"
        fig.text(0.5, 0.918,
                 f"P/C Ratio {pc_ratio:.2f}  ·  IV Rank {iv_rank:.1f}%  ·  "
                 f"IV Current {iv_curr:.1f}%  ·  Total OI {total_oi/1e6:.2f}M  ·  当前价 {price_str}",
                 ha="center", va="top", fontsize=9, color=_T3)

        # Aggregate OI
        res_dict: dict[float, int] = {}
        for r in resistance:
            s = float(r["strike"])
            res_dict[s] = res_dict.get(s, 0) + int(r.get("oi", 0))

        sup_dict: dict[float, int] = {}
        for s in support:
            k = float(s["strike"])
            sup_dict[k] = sup_dict.get(k, 0) + int(s.get("oi", 0))

        all_strikes = sorted(set(
            list(res_dict.keys()) + list(sup_dict.keys()) +
            [float(u["strike"]) for u in unusual]
        ))
        y_min = min(all_strikes) - 15
        y_max = max(all_strikes) + 15

        max_oi = max(
            max(res_dict.values()) if res_dict else 1,
            max(sup_dict.values()) if sup_dict else 1,
        )
        scale = 4.5 / max_oi

        ax = fig.add_axes([0.10, 0.10, 0.58, 0.76])
        ax.set_facecolor(_CARD)
        for sp in ax.spines.values(): sp.set_visible(False)
        ax.set_ylim(y_min, y_max)

        # Resistance (calls) → right
        for strike, oi in res_dict.items():
            w = oi * scale
            ax.barh(strike, w, height=5, left=0.3, color=_GREEN, alpha=0.80, zorder=3)
            ax.text(w + 0.45, strike, f"${strike:.0f}  {oi/1e3:.0f}K",
                    va="center", fontsize=9, color=_GREEN, fontweight="bold")

        # Support (puts) → left
        for strike, oi in sup_dict.items():
            w = oi * scale
            ax.barh(strike, -w, height=5, left=-0.3, color=_RED, alpha=0.80, zorder=3)
            ax.text(-w - 0.45, strike, f"{oi/1e3:.0f}K  ${strike:.0f}",
                    va="center", ha="right", fontsize=9, color=_RED, fontweight="bold")

        # Unusual activity dots (deduplicated by strike)
        ua_done: set = set()
        for ua in unusual:
            s = float(ua["strike"])
            if s not in ua_done:
                col = _GREEN if ua.get("bullish") else _RED
                ax.plot(0, s, "o", ms=7, color=col, alpha=0.55,
                        zorder=5, mec="white", mew=0.8)
                ua_done.add(s)

        # Current price line
        if current_price:
            ax.axhline(current_price, color=_GOLD, lw=2.2, zorder=6, alpha=0.95)
            ax.text(-5.8, current_price + 1.5,
                    f"当前价 ${current_price:.1f}",
                    fontsize=9, color=_GOLD, fontweight="bold", ha="left")

        ax.set_xlim(-6.5, 6.5)
        ax.axvline(0, color="#30363d", lw=1, zorder=2)
        ax.set_yticks([]);  ax.set_xticks([])
        ax.text(-3.5, y_max + 4, "← PUT 支撑", ha="center",
                fontsize=10, color=_RED, fontweight="bold")
        ax.text(3.5,  y_max + 4, "CALL 阻力 →", ha="center",
                fontsize=10, color=_GREEN, fontweight="bold")
        ax.set_title(
            "期权多空水位  (支撑=Put高OI  ·  阻力=Call高OI  ·  金线=当前价)",
            fontsize=9, color=_T3, pad=6, loc="left")

        # Side metrics
        ax2 = fig.add_axes([0.71, 0.10, 0.26, 0.76])
        ax2.set_facecolor(_CARD)
        for sp in ax2.spines.values(): sp.set_visible(False)
        ax2.set_xticks([]); ax2.set_yticks([])
        ax2.set_xlim(0, 1); ax2.set_ylim(0, 1)

        pc_col  = _RED   if pc_ratio > 1   else (_GOLD if pc_ratio > 0.7 else _GREEN)
        ivr_col = _RED   if iv_rank  > 70  else (_GOLD if iv_rank  > 40  else _GREEN)
        pc_sub  = ("Put偏多 →看跌" if pc_ratio > 1 else
                   ("偏中性" if pc_ratio > 0.7 else "Call偏多 →看涨"))

        # max by OI value, not by strike key
        top_sup = max(sup_dict, key=lambda k: sup_dict[k]) if sup_dict else 0.0
        top_res = max(res_dict, key=lambda k: res_dict[k]) if res_dict else 0.0
        metrics = [
            ("P/C Ratio",    f"{pc_ratio:.2f}",      pc_col,  pc_sub),
            ("IV Rank",      f"{iv_rank:.1f}%",       ivr_col, "高波动" if iv_rank>70 else ("中等" if iv_rank>40 else "低波动区间")),
            ("IV Current",   f"{iv_curr:.1f}%",       _ACCENT, "当前隐含波动率"),
            ("Total OI",     f"{total_oi/1e6:.2f}M",  _T1,     "全链条未平仓"),
            ("Put 最强支撑", f"${top_sup:.0f}",       _RED,    f"{sup_dict.get(top_sup,0)/1e3:.0f}K OI"),
            ("Call 最强阻力",f"${top_res:.0f}",       _GREEN,  f"{res_dict.get(top_res,0)/1e3:.0f}K OI"),
        ]
        for i, (lbl, val, col, sub) in enumerate(metrics):
            y = 0.94 - i * 0.155
            ax2.text(0.5, y,       val, ha="center", fontsize=13, color=col, fontweight="bold")
            ax2.text(0.5, y-0.05,  lbl, ha="center", fontsize=7.5, color=_T3)
            ax2.text(0.5, y-0.09,  sub, ha="center", fontsize=7,   color=col, alpha=0.75)
            if i < len(metrics) - 1:
                ax2.axhline(y - 0.115, color="#30363d", lw=0.5)
        ax2.set_title("期权指标", fontsize=10, color=_T3, pad=5, loc="center")

        return _fig_to_b64(fig)

    except Exception as e:
        print(f"[chart_engine] render_options_chart failed: {e}")
        return None
