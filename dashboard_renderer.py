"""
Alpha Hive Dashboard Renderer

从 alpha_hive_daily_report.py 提取的 HTML 仪表板生成逻辑。
原方法 _generate_index_html (2190 行) + _DASHBOARD_CSS (471 行)
"""

import json
import logging
from typing import Dict, List

_log = logging.getLogger("alpha_hive.dashboard_renderer")

from pathlib import Path as _Path_mod
from jinja2 import Environment


# ── 模板文件路径 ──
_TPL_DIR = _Path_mod(__file__).parent / "templates"


def _load_tpl(name: str) -> str:
    """加载模板文件内容"""
    return (_TPL_DIR / name).read_text(encoding="utf-8")


# 预加载 CSS（模块级缓存）
_DASHBOARD_CSS = _load_tpl("dashboard.css")

def render_dashboard_html(report: Dict, date_str: str,
                         report_dir, opportunities: List,
                         dashboard_css: str = None) -> str:
    """
    从 swarm report + .swarm_results_*.json 生成完整 GitHub Pages 仪表板。

    Args:
        report: 蜂群扫描报告 dict
        date_str: 日期字符串 (YYYY-MM-DD)
        report_dir: 报告目录 (Path)
        opportunities: OpportunityItem 列表
        dashboard_css: CSS 字符串（默认使用 DASHBOARD_CSS）
    """
    dashboard_css = dashboard_css or _DASHBOARD_CSS

    from datetime import datetime as _dt
    import html as _html
    from pathlib import Path as _Path

    # --- 准确率数据加载 ---
    _acc_stats = {}
    try:
        from backtester import PredictionStore
        _ps = PredictionStore()
        _acc_stats = _ps.get_accuracy_stats(period="t7", days=90) or {}
    except Exception as _ace:
        _log.debug("准确率统计加载失败: %s", _ace)
    _acc_total_checked = _acc_stats.get("total_checked", 0)
    _acc_overall       = _acc_stats.get("overall_accuracy", 0.0)
    _acc_avg_return    = _acc_stats.get("avg_return", 0.0)
    _acc_correct       = _acc_stats.get("correct_count", 0)
    _acc_by_dir        = _acc_stats.get("by_direction", {})
    _acc_by_ticker     = _acc_stats.get("by_ticker", {})

    # F11: 增强准确率数据（胜率走势、最佳/最差预测、Sharpe）
    _acc_weekly_trend = []  # [{week, accuracy, total}]
    _acc_best3 = []   # [{ticker, date, direction, score, return_t7}]
    _acc_worst3 = []
    _acc_sharpe = 0.0
    _acc_max_dd = 0.0
    _acc_win_streak = 0
    try:
        from backtester import PredictionStore as _PS11
        import sqlite3 as _sq11
        _ps11 = _PS11()
        with _sq11.connect(_ps11.db_path) as _cn11:
            _cn11.row_factory = _sq11.Row
            # 周胜率走势（最近 12 周）
            _wrows = _cn11.execute("""
                SELECT strftime('%Y-W%W', date) as week,
                       COUNT(*) as total,
                       SUM(CASE WHEN correct_t7=1 THEN 1 ELSE 0 END) as correct,
                       AVG(return_t7) as avg_ret
                FROM predictions WHERE checked_t7=1
                GROUP BY week ORDER BY week DESC LIMIT 12
            """).fetchall()
            _acc_weekly_trend = [
                {"week": r["week"], "accuracy": round(r["correct"]/r["total"]*100, 1) if r["total"] else 0,
                 "total": r["total"], "avg_ret": round(r["avg_ret"] or 0, 2)}
                for r in reversed(_wrows)
            ]
            # 最佳预测 Top 3（收益最高）
            _brows = _cn11.execute("""
                SELECT ticker, date, direction, final_score, return_t7, correct_t7,
                       price_at_predict, price_t7
                FROM predictions WHERE checked_t7=1 AND return_t7 IS NOT NULL
                ORDER BY return_t7 DESC LIMIT 3
            """).fetchall()
            _acc_best3 = [dict(r) for r in _brows]
            # 最差预测 Top 3（亏损最大）
            _wrows2 = _cn11.execute("""
                SELECT ticker, date, direction, final_score, return_t7, correct_t7,
                       price_at_predict, price_t7
                FROM predictions WHERE checked_t7=1 AND return_t7 IS NOT NULL
                ORDER BY return_t7 ASC LIMIT 3
            """).fetchall()
            _acc_worst3 = [dict(r) for r in _wrows2]
            # Sharpe Ratio（基于 T+7 收益）
            _ret_rows = _cn11.execute("""
                SELECT return_t7 FROM predictions
                WHERE checked_t7=1 AND return_t7 IS NOT NULL
            """).fetchall()
            if len(_ret_rows) >= 2:
                _rets = [r["return_t7"] for r in _ret_rows]
                _mean_r = sum(_rets) / len(_rets)
                _std_r = (sum((x - _mean_r)**2 for x in _rets) / (len(_rets) - 1)) ** 0.5
                _acc_sharpe = round(_mean_r / _std_r, 2) if _std_r > 0 else 0.0
                # 最大回撤（连续亏损预测累计）
                _cum = 0.0
                _peak = 0.0
                _max_dd_val = 0.0
                for _rv in _rets:
                    _cum += _rv
                    if _cum > _peak:
                        _peak = _cum
                    _dd = _peak - _cum
                    if _dd > _max_dd_val:
                        _max_dd_val = _dd
                _acc_max_dd = round(_max_dd_val, 2)
            # 当前连胜
            _streak_rows = _cn11.execute("""
                SELECT correct_t7 FROM predictions
                WHERE checked_t7=1 ORDER BY date DESC, id DESC
            """).fetchall()
            _acc_win_streak = 0
            for _sr in _streak_rows:
                if _sr["correct_t7"] == 1:
                    _acc_win_streak += 1
                else:
                    break
    except Exception as _e11:
        _log.debug("F11 准确率增强数据加载失败: %s", _e11)

    try:
        from zoneinfo import ZoneInfo as _ZI
        now_str = _dt.now(_ZI("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        from datetime import timezone as _tz
        now_str = _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M UTC")
    date_str = date_str
    opps = report.get("opportunities", [])
    meta = report.get("swarm_metadata", {})
    n_tickers = meta.get("tickers_analyzed", len(opps))
    n_agents = meta.get("total_agents", 7)
    n_resonance = meta.get("resonances_detected", 0)

    # 读取详细 swarm_results（含 IV Rank、P/C Ratio、内幕信号等）
    swarm_detail: Dict = {}
    try:
        sr_path = report_dir / f".swarm_results_{date_str}.json"
        if sr_path.exists():
            with open(sr_path) as _f:
                swarm_detail = json.load(_f)
    except (OSError, json.JSONDecodeError):
        pass

    # 将 opportunities 按 ticker 建立索引，并补充 swarm 详细数据
    opp_by_ticker = {o.get("ticker"): o for o in opps}
    # 若 swarm_detail 有更多 ticker（超过 opportunities 的 5 个），全部纳入
    all_tickers_sorted = [o.get("ticker") for o in opps]
    for t in swarm_detail:
        if t not in all_tickers_sorted:
            all_tickers_sorted.append(t)
    # 用实际扫描数量覆盖（swarm_detail 包含全部标的，opportunities 只存前 5）
    n_tickers = len(all_tickers_sorted) or n_tickers

    dir_map = {"bullish": ("看多", "bullish", "#28a745"),
               "bearish": ("看空", "bearish", "#dc3545"),
               "neutral": ("中性", "neutral", "#ffc107")}

    def sc_cls(score):
        return "sc-h" if score >= 7.0 else ("sc-m" if score >= 5.5 else "sc-l")

    def _detail(ticker):
        """提取单个 ticker 的详细指标（含 GEX / 期权流向 / 维度数据质量）"""
        sd = swarm_detail.get(ticker, {})
        ad = sd.get("agent_details", {})
        oracle = ad.get("OracleBeeEcho", {}).get("details", {})
        scout_disc = ad.get("ScoutBeeNova", {}).get("discovery", "")
        bear_score = ad.get("BearBeeContrarian", {}).get("score", 0.0)
        ab = sd.get("agent_breakdown", {})
        iv_rank = oracle.get("iv_rank", None)
        pc = oracle.get("put_call_ratio", None)
        real_pct = sd.get("data_real_pct", None)
        # ── 新增期权信号字段（#1）──
        gex = oracle.get("gamma_exposure", None)
        flow_dir = oracle.get("flow_direction", None)
        gsr = oracle.get("gamma_squeeze_risk", None)
        iv_current = oracle.get("iv_current", None)
        signal_sum = oracle.get("signal_summary", "")
        # ── 价格数据（#10）── fallback: ScoutBee → OracleBee discovery → yfinance
        scout_det = ad.get("ScoutBeeNova", {}).get("details", {})
        _price_raw = scout_det.get("price")
        _momentum_raw = scout_det.get("momentum_5d")
        # Fallback 1: 从 OracleBee discovery 解析价格（格式 "... | $XX.XX"）
        if _price_raw is None:
            _oracle_disc = ad.get("OracleBeeEcho", {}).get("discovery", "")
            import re as _re_mod
            _pm = _re_mod.search(r'\$(\d+(?:\.\d+)?)', _oracle_disc)
            if _pm:
                _price_raw = float(_pm.group(1))
        # Fallback 2: 直接从 yfinance 获取
        if _price_raw is None:
            try:
                import yfinance as _yf
                _h = _yf.Ticker(ticker).history(period="5d")
                if not _h.empty:
                    _price_raw = float(_h["Close"].iloc[-1])
                    if len(_h) >= 2 and _momentum_raw is None:
                        _momentum_raw = (_h["Close"].iloc[-1] / _h["Close"].iloc[0] - 1) * 100
            except (ValueError, IndexError, KeyError, AttributeError):
                pass
        # ── 维度数据质量（#3）──
        dim_dq = sd.get("dim_data_quality", {})
        # 内幕信号：取 ScoutBeeNova discovery 第一个 | 段
        insider_hint = scout_disc.split("|")[0].strip() if scout_disc else ""
        insider_color = "#28a745" if "买入" in insider_hint else ("#dc3545" if "卖出" in insider_hint else "#666")
        # 期权流向颜色
        _flow_colors = {"bullish": "#28a745", "bearish": "#dc3545", "neutral": "#666"}
        flow_color = _flow_colors.get(flow_dir, "#666")
        # GEX 格式化（已除以1e6，≥1 显示 M，否则显示 k）
        if gex is None:
            gex_str = "-"
        elif abs(gex) >= 1.0:
            gex_str = f"{gex:+.1f}M"
        else:
            gex_str = f"{gex*1000:+.1f}k"
        return {
            "iv_rank": f"{iv_rank:.1f}" if iv_rank is not None else "-",
            "pc": f"{pc:.2f}" if pc is not None else "-",
            "bear_score": float(bear_score),
            "bullish": ab.get("bullish", 0),
            "bearish_v": ab.get("bearish", 0),
            "neutral_v": ab.get("neutral", 0),
            "insider_hint": _html.escape(insider_hint[:35]) if insider_hint else "",
            "insider_color": insider_color,
            "real_pct": f"{real_pct:.0f}%" if real_pct is not None else "-",
            # 新期权字段
            "gex": gex_str,
            "flow_dir": flow_dir or "-",
            "flow_color": flow_color,
            "gsr": gsr or "-",
            "iv_current": f"{iv_current:.1f}%" if iv_current is not None else "-",
            "signal_sum": _html.escape(signal_sum[:45]) if signal_sum else "",
            # 维度数据质量
            "dim_dq": dim_dq,
            # 价格数据
            "price": round(float(_price_raw), 2) if _price_raw is not None else None,
            "momentum_5d": round(float(_momentum_raw), 2) if _momentum_raw is not None else None,
        }

    # ── 维度数据质量 HTML 构建器（#3）──
    _DIM_DQ_LABELS = {
        "signal": "信号", "catalyst": "催化", "sentiment": "情绪",
        "odds": "赔率", "risk_adj": "风险",
    }

    def _build_dim_dq_html(dim_dq: dict) -> str:
        """生成维度数据质量迷你条形图"""
        if not dim_dq:
            return ""
        items = []
        for dim, label in _DIM_DQ_LABELS.items():
            pct = dim_dq.get(dim)
            if pct is None:
                continue
            color = "#28a745" if pct >= 80 else ("#ffc107" if pct >= 50 else "#dc3545")
            items.append(
                f'<span class="dq-item" title="{label} 数据质量 {pct:.0f}%">'
                f'<span class="dq-lbl">{label}</span>'
                f'<span class="dq-bar"><span class="dq-fill" style="width:{pct:.0f}%;background:{color};"></span></span>'
                f'<span class="dq-val">{pct:.0f}%</span>'
                f'</span>'
            )
        if not items:
            return ""
        return '<div class="dim-dq-row">' + "".join(items) + '</div>'

    # ── 升级 A: 通俗一句话解读 ──
    def _build_plain_insight(ticker: str, sd: dict) -> str:
        """基于 Agent 数据生成通俗中文一句话（规则模板，无需 LLM）"""
        ad = sd.get("agent_details", {})
        parts = []
        # 1. 内幕信号
        insider = ad.get("ScoutBeeNova", {}).get("details", {}).get("insider", {})
        insider_sent = insider.get("sentiment", "neutral")
        if insider_sent == "bullish":
            parts.append("公司高管在买入股票")
        elif insider_sent == "bearish":
            amt = insider.get("dollar_sold", 0)
            if amt and amt > 1_000_000:
                parts.append(f"高管近期卖出${amt/1e6:.1f}M")
            elif amt and amt > 0:
                parts.append(f"高管近期卖出${amt:,.0f}")
            else:
                parts.append("高管在卖出股票")
        # 2. 期权信号
        oracle_dir = ad.get("OracleBeeEcho", {}).get("direction", "neutral")
        if oracle_dir == "bullish":
            parts.append("期权市场看涨")
        elif oracle_dir == "bearish":
            parts.append("期权市场偏空")
        # 3. 催化剂（最近的未来事件）
        cats = ad.get("ChronosBeeHorizon", {}).get("details", {}).get("catalysts", [])
        future_cats = [c for c in cats
                       if isinstance(c.get("days_until"), (int, float)) and c["days_until"] > 0]
        if future_cats:
            nearest = min(future_cats, key=lambda c: c["days_until"])
            days = int(nearest["days_until"])
            _emap = {"earnings": "财报", "dividend": "分红",
                     "fda": "FDA审批", "conference": "行业大会"}
            ename = _emap.get(nearest.get("type", ""), nearest.get("event", "")[:8])
            if days <= 7:
                parts.append(f"{ename}{days}天后")
            elif days <= 30:
                parts.append(f"{ename}约{days}天后")
        # 4. 动量上下文
        momentum = ad.get("BuzzBeeWhisper", {}).get("details", {}).get("momentum_5d")
        if momentum is not None:
            if momentum < -3:
                parts.append("近5日大幅下跌")
            elif momentum < -1:
                parts.append("近期小幅回调")
            elif momentum > 3:
                parts.append("近5日大幅上涨")
            elif momentum > 1:
                parts.append("近期稳步上涨")
        # 5. 冲突提示（内幕卖出 + 综合看多）
        overall_dir = sd.get("direction", "neutral")
        if insider_sent == "bearish" and overall_dir == "bullish":
            parts.append("但高管在减持")
        elif insider_sent == "bullish" and overall_dir == "bearish":
            parts.append("但高管在增持")
        if not parts:
            # Fallback：取第一个 agent 的 discovery 首段
            for agt in ["ScoutBeeNova", "OracleBeeEcho", "BuzzBeeWhisper"]:
                d = ad.get(agt, {}).get("discovery", "")
                if d:
                    return d.split("|")[0].strip()[:80]
            return ""
        return "，".join(parts)

    # ── 升级 B: 风险等级标签 ──
    def _risk_badge(ticker: str, sd: dict) -> str:
        """多因子风险评分 → 🟢低风险 / 🟡中风险 / 🔴高风险"""
        risk_pts = 0
        ad = sd.get("agent_details", {})
        # F1: GuardBee 分数（低=高风险）
        guard = float(ad.get("GuardBeeSentinel", {}).get("score", 5.0))
        if guard <= 3.0:
            risk_pts += 3
        elif guard <= 5.0:
            risk_pts += 1
        # F2: 内幕卖出
        if ad.get("ScoutBeeNova", {}).get("details", {}).get(
                "insider", {}).get("sentiment") == "bearish":
            risk_pts += 2
        # F3: 拥挤度
        crowding = ad.get("ScoutBeeNova", {}).get("details", {}).get("crowding_score")
        if crowding is not None:
            if crowding > 60:
                risk_pts += 2
            elif crowding > 40:
                risk_pts += 1
        # F4: IV Rank（高波动）
        iv = ad.get("OracleBeeEcho", {}).get("details", {}).get("iv_rank")
        if iv is not None:
            if iv > 70:
                risk_pts += 2
            elif iv > 50:
                risk_pts += 1
        # F5: risk_adj 维度分
        radj = float(sd.get("dimension_scores", {}).get("risk_adj", 5.0))
        if radj <= 3.0:
            risk_pts += 2
        elif radj <= 5.0:
            risk_pts += 1
        if risk_pts >= 7:
            return '<span class="risk-badge risk-high">高风险</span>'
        elif risk_pts >= 4:
            return '<span class="risk-badge risk-med">中风险</span>'
        return '<span class="risk-badge risk-low">低风险</span>'

    # ── 升级 D: 催化剂倒计时 ──
    def _catalyst_countdown(ticker: str, sd: dict) -> str:
        """从 ChronosBeeHorizon 提取最近催化剂，生成倒计时 HTML"""
        cats = sd.get("agent_details", {}).get(
            "ChronosBeeHorizon", {}).get("details", {}).get("catalysts", [])
        future = [c for c in cats
                  if isinstance(c.get("days_until"), (int, float)) and c["days_until"] > 0]
        if not future:
            return ""
        nearest = min(future, key=lambda c: c["days_until"])
        days = int(nearest["days_until"])
        _emap = {"earnings": "财报", "dividend": "分红",
                 "fda": "FDA审批", "conference": "行业大会"}
        ename = _emap.get(nearest.get("type", ""), nearest.get("event", "")[:10])
        if days <= 3:
            ucls = "cat-urgent"
        elif days <= 7:
            ucls = "cat-soon"
        else:
            ucls = "cat-normal"
        tstr = f"{days}天后" if days <= 14 else f"约{days // 7}周后"
        return (f'<div class="catalyst-cd {ucls}">'
                f'<span class="cat-icon">\U0001f4c5</span>'
                f'<span class="cat-text">{_html.escape(ename)} {tstr}</span>'
                f'</div>')

    # ── 升级 F: 信号冲突预警 ──
    def _signal_conflicts(ticker: str, sd: dict) -> str:
        """检测 Agent 间信号矛盾，返回黄色预警 HTML"""
        ad = sd.get("agent_details", {})
        dirs = sd.get("agent_directions", {})
        overall = sd.get("direction", "neutral")
        conflicts = []
        # C1: 内幕卖出 vs 整体看多
        insider_sent = ad.get("ScoutBeeNova", {}).get("details", {}).get(
            "insider", {}).get("sentiment", "neutral")
        if insider_sent == "bearish" and overall == "bullish":
            amt = ad.get("ScoutBeeNova", {}).get("details", {}).get(
                "insider", {}).get("dollar_sold", 0)
            amt_str = f"(${amt / 1e6:.1f}M)" if amt and amt > 1_000_000 else ""
            conflicts.append(f"高管在卖出{amt_str} vs 整体看多")
        # C2: 期权方向 vs 整体
        oracle_dir = dirs.get("OracleBeeEcho", "neutral")
        if oracle_dir == "bearish" and overall == "bullish":
            conflicts.append("期权市场偏空 vs 整体看多")
        elif oracle_dir == "bullish" and overall == "bearish":
            conflicts.append("期权市场看涨 vs 整体看空")
        # C3: Bear strength 高 + 综合看多
        bear_str = float(sd.get("bear_strength", 0))
        if bear_str >= 3.0 and overall == "bullish":
            conflicts.append(f"看空力度较强({bear_str:.1f}/10)")
        if not conflicts:
            return ""
        items = "；".join(conflicts[:2])
        return (f'<div class="conflict-warn">'
                f'<span class="cw-icon">\u26a0\ufe0f</span>'
                f'<span class="cw-text">信号冲突：{_html.escape(items)}</span>'
                f'</div>')

    # ── 升级 G: 维度 Tooltip ──
    _DIM_TOOLTIPS = {
        "信号": "聪明钱交易信号（SEC 内幕交易、机构持仓变化）",
        "催化": "未来催化剂事件清晰度（财报、FDA、产品发布）",
        "情绪": "市场舆情方向与质量（新闻、Reddit）",
        "赔率": "市场赔率错配（期权 IV、Put/Call）",
        "风险": "风险调整评估（回撤、流动性、拥挤度）",
    }

    # 计算 avg real_pct
    real_pcts = [swarm_detail[t].get("data_real_pct", 0) for t in swarm_detail if swarm_detail[t].get("data_real_pct")]
    avg_real = f"{sum(real_pcts)/len(real_pcts):.0f}%" if real_pcts else "-"

    # ── 机会卡片（Top 6）──
    cards_html = ""
    for i, ticker in enumerate(all_tickers_sorted[:6], 1):
        opp = opp_by_ticker.get(ticker, {})
        score = float(opp.get("opp_score") or swarm_detail.get(ticker, {}).get("final_score", 0))
        direction = str(opp.get("direction") or swarm_detail.get(ticker, {}).get("direction", "neutral")).lower()
        if direction not in dir_map:
            direction = "bullish" if "多" in direction else ("bearish" if "空" in direction else "neutral")
        resonance = opp.get("resonance", swarm_detail.get(ticker, {}).get("resonance", {}).get("resonance_detected", False))
        supporting = int(opp.get("supporting_agents") or swarm_detail.get(ticker, {}).get("supporting_agents", 0))
        dir_label, dir_cls, dir_color = dir_map[direction]
        border = " style=\"border-color:#28a745;border-width:2px;\"" if i == 1 else ""
        rank_style = " style=\"background:#28a745;color:white;\"" if i == 1 else ""
        sc = sc_cls(score)
        res_badge = (f'<span class="res-badge res-y">{supporting} Agent 共振</span>'
                     if resonance else '<span class="res-badge res-n">无共振</span>')
        d = _detail(ticker)
        pc_color = ' style="color:#28a745;font-weight:bold;"' if d["pc"] != "-" and float(d["pc"]) < 0.7 else (
                   ' style="color:#dc3545;font-weight:bold;"' if d["pc"] != "-" and float(d["pc"]) > 1.5 else "")
        bear_pct = min(100, int(d["bear_score"] * 10))
        insider_row = (f'<div class="mr"><span class="lbl">内幕信号</span>'
                       f'<span class="val" style="color:{d["insider_color"]};">{d["insider_hint"]}</span></div>'
                       if d["insider_hint"] else "")
        ml_link = _Path(report_dir / f"alpha-hive-{ticker}-ml-enhanced-{date_str}.html")
        ml_row = (f'<div class="mr"><span class="lbl">ML 报告</span>'
                  f'<span class="val"><a href="alpha-hive-{ticker}-ml-enhanced-{date_str}.html" style="color:#667eea;">查看详情</a></span></div>'
                  if ml_link.exists() else "")
        cards_html += f"""
            <div class="opp-card"{border}>
                <div class="card-rank"{rank_style}>#{i}</div>
                <div class="card-hd">
                    <h3>{_html.escape(ticker)}</h3>
                    <div class="dir-badge dir-{dir_cls}">{dir_label}</div>
                </div>
                <div class="card-body">
                    <div class="mr"><span class="lbl">综合分</span><span class="val {sc}">{score:.1f}/10</span></div>
                    <div class="mr"><span class="lbl">共振信号</span>{res_badge}</div>
                    <div class="mr"><span class="lbl">投票</span><span class="val">{d['bullish']}多 / {d['bearish_v']}空 / {d['neutral_v']}中</span></div>
                    <div class="mr"><span class="lbl">IV Rank</span><span class="val">{d['iv_rank']}</span>{f'<span class="lbl" style="margin-left:8px;">当前IV</span><span class="val">{d["iv_current"]}</span>' if d["iv_current"] != "-" else ""}</div>
                    <div class="mr"><span class="lbl">P/C Ratio</span><span class="val"{pc_color}>{d['pc']}</span></div>
                    {f'<div class="mr"><span class="lbl">期权流向</span><span class="val" style="color:{d["flow_color"]};font-weight:bold;">{d["flow_dir"]}</span></div>' if d["flow_dir"] != "-" else ""}
                    {f'<div class="mr"><span class="lbl">GEX</span><span class="val">{d["gex"]}</span></div>' if d["gex"] != "-" else ""}
                    {insider_row}
                    <div class="mr"><span class="lbl">看空强度</span><span class="val">{d['bear_score']:.1f}/10</span></div>
                    <div class="bear-bar"><div class="bear-fill" style="width:{bear_pct}%"></div></div>
                    {ml_row}
                </div>
            </div>"""

    # ── 完整表格（全部 ticker）──
    rows_html = ""
    for i, ticker in enumerate(all_tickers_sorted, 1):
        opp = opp_by_ticker.get(ticker, {})
        score = float(opp.get("opp_score") or swarm_detail.get(ticker, {}).get("final_score", 0))
        direction = str(opp.get("direction") or swarm_detail.get(ticker, {}).get("direction", "neutral")).lower()
        if direction not in dir_map:
            direction = "bullish" if "多" in direction else ("bearish" if "空" in direction else "neutral")
        resonance = opp.get("resonance", swarm_detail.get(ticker, {}).get("resonance", {}).get("resonance_detected", False))
        supporting = int(opp.get("supporting_agents") or swarm_detail.get(ticker, {}).get("supporting_agents", 0))
        dir_label, _, dir_color = dir_map[direction]
        sc = sc_cls(score)
        d = _detail(ticker)
        res_html = (f'<span class="res-badge res-y">{supporting} Agent</span>'
                    if resonance else '<span class="res-badge res-n">无</span>')
        row_style = " style=\"background:#f0fff0;\"" if i == 1 else ""
        ml_link = _Path(report_dir / f"alpha-hive-{ticker}-ml-enhanced-{date_str}.html")
        ml_td = (f'<a href="alpha-hive-{ticker}-ml-enhanced-{date_str}.html" style="color:#667eea;">查看</a>'
                 if ml_link.exists() else "-")
        pc_style = (' style="color:#28a745;font-weight:bold;"' if d["pc"] != "-" and float(d["pc"]) < 0.7
                    else (' style="color:#dc3545;font-weight:bold;"' if d["pc"] != "-" and float(d["pc"]) > 1.5 else ""))
        rows_html += f"""
            <tr{row_style}>
                <td>{i}</td>
                <td><strong>{_html.escape(ticker)}</strong></td>
                <td style="color:{dir_color};font-weight:bold;">{dir_label}</td>
                <td class="{sc}"><strong>{score:.1f}</strong>/10</td>
                <td>{res_html}</td>
                <td>{d['bullish']} / {d['bearish_v']} / {d['neutral_v']}</td>
                <td>{d['iv_rank']}</td>
                <td{pc_style}>{d['pc']}</td>
                <td style="color:#fd7e14;">{d['bear_score']:.1f}/10</td>
                <td>{ml_td}</td>
            </tr>"""

    # ── Phase 3 增强：宏观面板 + 深度卡片 + Markdown 渲染 ──
    import re as _re


    # F&G 指数 + 平均情绪
    _fg_val = None
    _avg_sent, _sent_cnt = 0.0, 0
    for _t3 in all_tickers_sorted:
        _b3 = swarm_detail.get(_t3, {}).get("agent_details", {}).get("BuzzBeeWhisper", {}).get("discovery", "")
        if _fg_val is None:
            _m3 = _re.search(r'F&G\s*(\d+)', _b3)
            if _m3:
                _fg_val = int(_m3.group(1))
        _s3 = _re.search(r'情绪\s*([\d.]+)%', _b3)
        if _s3:
            _avg_sent += float(_s3.group(1))
            _sent_cnt += 1
    _fv3 = _fg_val if _fg_val is not None else 50
    _fg_color = "#dc3545" if _fv3 <= 45 else ("#ffc107" if _fv3 <= 55 else "#28a745")
    _fg_label = (("极度恐惧" if _fv3 <= 25 else "恐惧") if _fv3 <= 45
                 else (("中性" if _fv3 <= 55 else "贪婪") if _fv3 <= 75 else "极度贪婪"))
    _fg_str = str(_fg_val) if _fg_val is not None else "?"
    _avg_sent_str = f"{_avg_sent/_sent_cnt:.0f}%" if _sent_cnt else "-"

    # ML 快捷链接
    _ml_ql = ""
    for _t3 in all_tickers_sorted:
        if _Path(report_dir / f"alpha-hive-{_t3}-ml-enhanced-{date_str}.html").exists():
            _ml_ql += (f'<a href="alpha-hive-{_t3}-ml-enhanced-{date_str}.html"'
                       f' class="rl ml-rl">{_html.escape(_t3)}</a> ')

    # 个股深度分析卡片
    _dir_hdr = {"bullish": "#28a745", "bearish": "#dc3545", "neutral": "#e67e22"}
    company_cards_html = ""
    for _tkr3 in all_tickers_sorted:
        _sd3 = swarm_detail.get(_tkr3, {})
        _ad3 = _sd3.get("agent_details", {})
        _sc3 = float(opp_by_ticker.get(_tkr3, {}).get("opp_score") or _sd3.get("final_score", 0))
        _dr3 = str(opp_by_ticker.get(_tkr3, {}).get("direction") or _sd3.get("direction", "neutral")).lower()
        if _dr3 not in dir_map:
            _dr3 = "bullish" if "多" in _dr3 else ("bearish" if "空" in _dr3 else "neutral")
        _dlbl3, _, _ = dir_map[_dr3]
        _hc3 = _dir_hdr.get(_dr3, "#667eea")
        _scls3 = sc_cls(_sc3)
        _det3 = _detail(_tkr3)
        _blist = []
        for _disc3, _ico3, _lb3 in [
            (_ad3.get("ScoutBeeNova", {}).get("discovery", ""), "📋", "内幕"),
            (_ad3.get("OracleBeeEcho", {}).get("discovery", ""), "📊", "期权"),
            (_ad3.get("BuzzBeeWhisper", {}).get("discovery", ""), "💬", "情绪"),
            (_ad3.get("ChronosBeeHorizon", {}).get("discovery", ""), "📅", "催化剂"),
            (_ad3.get("BearBeeContrarian", {}).get("discovery", ""), "🐻", "风险"),
        ]:
            _f3 = _disc3.split("|")[0].strip()[:90] if _disc3 else ""
            if _f3:
                _blist.append(f'<li>{_ico3} <strong>{_lb3}：</strong>{_html.escape(_f3)}</li>')
        _bhtml3 = "\n                        ".join(_blist) if _blist else "<li>数据采集中...</li>"
        _ml3ex = _Path(report_dir / f"alpha-hive-{_tkr3}-ml-enhanced-{date_str}.html").exists()
        _mlbtn3 = (f'<a href="alpha-hive-{_tkr3}-ml-enhanced-{date_str}.html" class="ml-btn">ML 增强分析 →</a>'
                   if _ml3ex else '<span class="ml-btn-na">ML 报告生成中</span>')
        company_cards_html += f"""
        <div class="company-card">
            <div class="cc-header" style="background:{_hc3};">
                <span class="cc-ticker">{_html.escape(_tkr3)}</span>
                <span class="cc-dir">{_dlbl3}</span>
                <span class="cc-score {_scls3}">{_sc3:.1f}/10</span>
            </div>
            <div class="cc-body">
                <div class="cc-metrics">
                    <div class="cc-metric"><span class="cm-l">IV Rank</span><span class="cm-v">{_det3['iv_rank']}</span></div>
                    <div class="cc-metric"><span class="cm-l">P/C Ratio</span><span class="cm-v">{_det3['pc']}</span></div>
                    <div class="cc-metric"><span class="cm-l">看空强度</span><span class="cm-v">{_det3['bear_score']:.1f}/10</span></div>
                    {f'<div class="cc-metric"><span class="cm-l">期权流向</span><span class="cm-v" style="color:{_det3["flow_color"]};font-weight:bold;">{_det3["flow_dir"]}</span></div>' if _det3["flow_dir"] != "-" else ""}
                    {f'<div class="cc-metric"><span class="cm-l">GEX</span><span class="cm-v">{_det3["gex"]}</span></div>' if _det3["gex"] != "-" else ""}
                </div>
                {_build_dim_dq_html(_det3['dim_dq'])}
                <ul class="cc-signals">
                    {_bhtml3}
                </ul>
                <div class="cc-footer">{_mlbtn3}</div>
            </div>
        </div>"""

    # Markdown → HTML 轻量渲染
    def _md2html(md_text: str) -> str:
        def _inline(s: str) -> str:
            """处理行内格式：加粗、斜体、代码、链接"""
            s = _html.escape(s)
            # 链接 [text](url) — 在 escape 之后处理（url 已被 escape）
            s = _re.sub(r'\[([^\]]+)\]\(([^)]+)\)',
                        r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
            # 内联代码 `code`
            s = _re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
            # 加粗 **text**
            s = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
            # 斜体 *text*（避免误伤 **)
            s = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', s)
            return s

        lines = md_text.split('\n')
        out = []
        in_ul = False      # 无序列表
        in_ol = False      # 有序列表
        in_sub = False     # 缩进嵌套列表
        ol_counter = 0
        in_table = False   # markdown 表格
        table_rows = []
        table_has_header = False

        def _close_lists():
            nonlocal in_ul, in_ol, in_sub, ol_counter
            if in_sub:  out.append('</ul>');  in_sub = False
            if in_ul:   out.append('</ul>');  in_ul = False
            if in_ol:   out.append('</ol>');  in_ol = False; ol_counter = 0

        for ln in lines:
            # ── 缩进嵌套列表（2/4 空格 + -/+/*)
            if _re.match(r'^( {2,4})[*+\-] ', ln):
                if not in_sub:
                    out.append('<ul class="sub-ul">')
                    in_sub = True
                out.append('<li>' + _inline(_re.sub(r'^ {2,4}[*+\-] ', '', ln)) + '</li>')
                continue
            if in_sub:
                out.append('</ul>')
                in_sub = False

            # ── 有序列表 1. 2. 3.
            _ol_m = _re.match(r'^(\d+)\. (.+)', ln)
            if _ol_m:
                if in_ul: out.append('</ul>'); in_ul = False
                if not in_ol:
                    out.append('<ol>')
                    in_ol = True
                out.append('<li>' + _inline(_ol_m.group(2)) + '</li>')
                continue

            # ── 无序列表 - / + / *
            if _re.match(r'^[*+\-] ', ln):
                if in_ol: out.append('</ol>'); in_ol = False; ol_counter = 0
                if not in_ul:
                    out.append('<ul>')
                    in_ul = True
                out.append('<li>' + _inline(ln[2:]) + '</li>')
                continue

            # 非列表行：关闭打开的列表
            if (in_ul or in_ol) and not ln.startswith(' '):
                _close_lists()

            # ── Markdown 表格 | col | col |
            _stripped = ln.strip()
            if _stripped.startswith('|') and _stripped.endswith('|') and _stripped.count('|') >= 3:
                cells = [c.strip() for c in _stripped.strip('|').split('|')]
                # 分隔行 |---|:---:|---:| → 标记 header
                if all(_re.match(r'^:?-+:?$', c) for c in cells if c):
                    table_has_header = True
                    continue
                if not in_table:
                    in_table = True
                    table_rows = []
                    table_has_header = False
                table_rows.append(cells)
                continue
            if in_table:
                # 非表格行 → 输出已收集的表格
                tbl = '<div class="table-wrap"><table class="md-table">'
                if table_has_header and len(table_rows) >= 1:
                    tbl += '<thead><tr>' + ''.join('<th>' + _inline(h) + '</th>' for h in table_rows[0]) + '</tr></thead>'
                    tbl += '<tbody>' + ''.join(
                        '<tr>' + ''.join('<td>' + _inline(c) + '</td>' for c in r) + '</tr>'
                        for r in table_rows[1:]
                    ) + '</tbody>'
                else:
                    tbl += '<tbody>' + ''.join(
                        '<tr>' + ''.join('<td>' + _inline(c) + '</td>' for c in r) + '</tr>'
                        for r in table_rows
                    ) + '</tbody>'
                tbl += '</table></div>'
                out.append(tbl)
                in_table = False
                table_rows = []
                table_has_header = False

            # ── 标题
            if ln.startswith('#### '):
                out.append('<h4>' + _inline(ln[5:]) + '</h4>')
            elif ln.startswith('### '):
                out.append('<h3>' + _inline(ln[4:]) + '</h3>')
            elif ln.startswith('## '):
                out.append('<h2>' + _inline(ln[3:]) + '</h2>')
            elif ln.startswith('# '):
                out.append('<h1>' + _inline(ln[2:]) + '</h1>')
            # ── 引用块 > text
            elif ln.startswith('> '):
                out.append('<blockquote>' + _inline(ln[2:]) + '</blockquote>')
            # ── 分隔线
            elif _re.match(r'^-{3,}$|^\*{3,}$|^_{3,}$', ln.strip()):
                out.append('<hr>')
            # ── 空行
            elif not ln.strip():
                if not (in_ul or in_ol or in_sub):
                    out.append('<br>')
            # ── 普通段落
            else:
                out.append('<p>' + _inline(ln) + '</p>')

        _close_lists()
        # 关闭残余表格（若表格是最后一段内容）
        if in_table and table_rows:
            tbl = '<div class="table-wrap"><table class="md-table">'
            if table_has_header and len(table_rows) >= 1:
                tbl += '<thead><tr>' + ''.join('<th>' + _inline(h) + '</th>' for h in table_rows[0]) + '</tr></thead>'
                tbl += '<tbody>' + ''.join(
                    '<tr>' + ''.join('<td>' + _inline(c) + '</td>' for c in r) + '</tr>'
                    for r in table_rows[1:]
                ) + '</tbody>'
            else:
                tbl += '<tbody>' + ''.join(
                    '<tr>' + ''.join('<td>' + _inline(c) + '</td>' for c in r) + '</tr>'
                    for r in table_rows
                ) + '</tbody>'
            tbl += '</table></div>'
            out.append(tbl)
        return '\n'.join(out)

    _rpt_body = ""
    _md_path3 = _Path(report_dir) / f"alpha-hive-daily-{date_str}.md"
    if _md_path3.exists():
        try:
            _rpt_body = _md2html(_md_path3.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError) as _rpt_err:
            _log.debug("报告 markdown 加载失败: %s", _rpt_err)
            _rpt_body = "<p>报告加载失败</p>"


    # ── Chart & Radar Data ──
    import json as _json

    _dir_counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    for _td in all_tickers_sorted:
        _drd = str(opp_by_ticker.get(_td, {}).get("direction") or
                   swarm_detail.get(_td, {}).get("direction", "neutral")).lower()
        if "多" in _drd:   _drd = "bullish"
        elif "空" in _drd: _drd = "bearish"
        elif _drd not in ("bullish","bearish","neutral"): _drd = "neutral"
        _dir_counts[_drd] += 1

    _all_scores = [
        (_td2, float(opp_by_ticker.get(_td2, {}).get("opp_score") or
                     swarm_detail.get(_td2, {}).get("final_score", 0)))
        for _td2 in all_tickers_sorted
    ]
    _avg_score = (sum(s for _, s in _all_scores) / len(_all_scores)) if _all_scores else 0

    # ── 升级 C: Hero 一句话 + 宏观事件倒计时 ──
    _hero_parts = []
    _hero_parts.append(f"今天市场{_fg_label}(F&G {_fg_str})")
    # 宏观事件倒计时（≤7天的最近事件）
    try:
        from economic_calendar import get_next_event as _get_next_event
        _macro_evt = _get_next_event()
        if _macro_evt and _macro_evt.get("days_until", 99) <= 7:
            _ed = _macro_evt["days_until"]
            _en = _macro_evt["event"]
            _hero_parts.append(f"距{_en}还有{_ed}天" if _ed > 0 else f"{_en}今天公布")
    except (ImportError, Exception):
        pass
    _opp_parts = []
    if _dir_counts["bullish"]:
        _opp_parts.append(f"{_dir_counts['bullish']}个看多")
    if _dir_counts["bearish"]:
        _opp_parts.append(f"{_dir_counts['bearish']}个看空")
    if _dir_counts["neutral"]:
        _opp_parts.append(f"{_dir_counts['neutral']}个中性")
    _hero_parts.append(f"扫描{n_tickers}只标的发现{'、'.join(_opp_parts)}机会")
    _hero_tldr = "，".join(_hero_parts)

    # ── 宏观指标数据（用于 Hero Stats）──
    _macro_vix = "—"
    _macro_10y = "—"
    _macro_yc = "—"
    _macro_yc_cls = ""
    _macro_gld = "—"
    _macro_gld_cls = ""
    _macro_sector_html = ""
    try:
        from fred_macro import get_macro_context as _get_macro_ctx
        _mctx = _get_macro_ctx()
        if _mctx.get("data_source") != "fallback":
            _macro_vix = f"{_mctx.get('vix', 0):.1f}"
            _macro_10y = f"{_mctx.get('treasury_10y', 0):.2f}%"
            _yc = _mctx.get("yield_curve", "unknown")
            _yc_map = {"normal": "正常", "flat": "趋平", "inverted": "⚠️倒挂"}
            _yc_cls_map = {"normal": "yc-ok", "flat": "yc-warn", "inverted": "yc-bad"}
            _macro_yc = _yc_map.get(_yc, "—")
            _macro_yc_cls = _yc_cls_map.get(_yc, "")
            # 黄金指标
            _gld_trend = _mctx.get("gold_trend", "stable")
            _gld_chg = _mctx.get("gold_change_pct", 0.0)
            if _gld_trend in ("surging", "rising", "falling"):
                _macro_gld = f"{_gld_chg:+.1f}%"
                _macro_gld_cls = "gld-up" if _gld_chg > 0 else "gld-dn"
            elif _mctx.get("gold_price"):
                _macro_gld = f"${_mctx['gold_price']:.0f}"
            # 板块轮动 HTML
            _sr = _mctx.get("sector_rotation", {})
            if _sr.get("hot") or _sr.get("cold"):
                _sec_parts = []
                for etf, name, chg in _sr.get("hot", [])[:3]:
                    _sec_parts.append(f'<span class="sec-hot">{name}{chg:+.1f}%</span>')
                for etf, name, chg in _sr.get("cold", [])[-2:]:
                    _sec_parts.append(f'<span class="sec-cold">{name}{chg:+.1f}%</span>')
                _macro_sector_html = (
                    '<div class="macro-sectors">'
                    '<span class="sec-label">板块轮动(5日)</span> '
                    + " ".join(_sec_parts)
                    + '</div>'
                )
    except ImportError:
        pass
    except Exception as e:
        logging.getLogger("alpha_hive.dashboard").debug("宏观指标加载失败: %s", e)

    def _radar_data(ticker):
        sd  = swarm_detail.get(ticker, {})
        dim = sd.get("dimension_scores", {})
        if dim:
            signal    = float(dim.get("signal",   5.0)) * 10
            catalyst  = float(dim.get("catalyst", 5.0)) * 10
            sentiment = float(dim.get("sentiment",5.0)) * 10
            odds      = float(dim.get("odds",     5.0)) * 10
            risk_adj  = float(dim.get("risk_adj", 5.0)) * 10
        else:
            ad = sd.get("agent_details", {})
            signal   = float(ad.get("ScoutBeeNova",     {}).get("self_score", 5.0)) * 10
            catalyst = float(ad.get("ChronosBeeHorizon",{}).get("self_score", 5.0)) * 10
            oracle_det = ad.get("OracleBeeEcho", {}).get("details", {})
            pc_r    = oracle_det.get("put_call_ratio", 1.0) or 1.0
            odds    = max(0.0, min(100.0, (2.0 - float(pc_r)) / 1.5 * 100))
            buzz_d  = ad.get("BuzzBeeWhisper", {}).get("discovery", "")
            sm3     = _re.search(r'情绪\s*([\d.]+)%', buzz_d)
            sentiment = float(sm3.group(1)) if sm3 else 50.0
            bear_s  = float(ad.get("BearBeeContrarian", {}).get("score", 5.0))
            risk_adj = max(0.0, (10.0 - bear_s) * 10)
        return [round(min(100, max(0, signal)),   1),
                round(min(100, max(0, catalyst)), 1),
                round(min(100, max(0, sentiment)),1),
                round(min(100, max(0, odds)),     1),
                round(min(100, max(0, risk_adj)), 1)]

    _scores_js  = _json.dumps([[t, round(s, 1)] for t, s in _all_scores])
    _dir_js     = _json.dumps([_dir_counts["bullish"], _dir_counts["bearish"], _dir_counts["neutral"]])
    _radar_js   = _json.dumps({t: _radar_data(t) for t in all_tickers_sorted})

    _DOMAINS = {
        "MSFT": "microsoft.com", "NVDA": "nvidia.com",  "TSLA": "tesla.com",
        "META": "meta.com",       "AMZN": "amazon.com",  "RKLB": "rocketlabusa.com",
        "BILI": "bilibili.com",   "VKTX": "vikingtherapeutics.com", "CRCL": "circle.com",
        "GOOGL": "google.com",    "AAPL": "apple.com",   "NFLX": "netflix.com",
    }


    # ── 升级 E: 快速预计算 Score Delta（对比昨天） ──
    _score_deltas = {}  # {ticker: {"delta": float, "html": str}}
    try:
        import glob as _glob_e
        _prev_jsons = sorted(
            _glob_e.glob(str(report_dir / "alpha-hive-daily-*.json")),
            reverse=True
        )
        _prev_scores = {}
        for _pjf in _prev_jsons:
            _pdate = _Path(_pjf).stem.replace("alpha-hive-daily-", "")
            if _pdate == date_str:
                continue  # 跳过今天
            try:
                with open(_pjf, encoding="utf-8") as _pfp:
                    _prpt = _json.load(_pfp)
                for _po in _prpt.get("opportunities", []):
                    _ptk = _po.get("ticker", "")
                    if _ptk and _ptk not in _prev_scores:
                        _prev_scores[_ptk] = float(_po.get("opp_score", 0))
                break  # 只看最近一天
            except Exception:
                continue
        for _tke in all_tickers_sorted:
            _cur_e = float(opp_by_ticker.get(_tke, {}).get("opp_score") or
                          swarm_detail.get(_tke, {}).get("final_score", 0))
            if _tke in _prev_scores:
                _d_e = round(_cur_e - _prev_scores[_tke], 1)
                if _d_e > 0:
                    _dcls_e = "delta-up"
                    _dtxt_e = f"↑+{_d_e}"
                elif _d_e < 0:
                    _dcls_e = "delta-dn"
                    _dtxt_e = f"↓{_d_e}"
                else:
                    _dcls_e = "delta-flat"
                    _dtxt_e = "→0"
                _score_deltas[_tke] = {
                    "delta": _d_e,
                    "html": f'<span class="score-delta {_dcls_e}">{_dtxt_e}</span>',
                }
            else:
                _score_deltas[_tke] = {
                    "delta": 0,
                    "html": '<span class="score-delta delta-new">NEW</span>',
                }
    except Exception as _de_err:
        _log.debug("Score delta 预计算失败: %s", _de_err)

    # ── Build new Top-6 cards ──
    new_cards_html = ""
    for _ci, _tc6 in enumerate(all_tickers_sorted[:6], 1):
        _oc6   = opp_by_ticker.get(_tc6, {})
        _sc6   = float(_oc6.get("opp_score") or swarm_detail.get(_tc6, {}).get("final_score", 0))
        _dr6   = str(_oc6.get("direction") or swarm_detail.get(_tc6, {}).get("direction", "neutral")).lower()
        if "多" in _dr6: _dr6 = "bullish"
        elif "空" in _dr6: _dr6 = "bearish"
        elif _dr6 not in ("bullish","bearish","neutral"): _dr6 = "neutral"
        _dlbl6 = {"bullish":"🟢 看多","bearish":"🔴 看空","neutral":"🟡 中性"}[_dr6]
        _dcls6 = {"bullish":"sdir-bull","bearish":"sdir-bear","neutral":"sdir-neut"}[_dr6]
        _scls6 = sc_cls(_sc6)
        _fcls6 = "fill-h" if _sc6 >= 7.0 else ("fill-m" if _sc6 >= 5.5 else "fill-l")
        _pct6  = int(_sc6 * 10)
        _dom6  = _DOMAINS.get(_tc6, "")
        _logo6 = (f'<img class="slogo" src="https://logo.clearbit.com/{_dom6}" loading="lazy" '
                  f'width="42" height="42" alt="{_html.escape(_tc6)}" onerror="this.style.display=\'none\';this.nextSibling.style.display=\'flex\'">'
                  f'<div class="slogo-fb" style="display:none">{_html.escape(_tc6[:2])}</div>') if _dom6 else \
                 f'<div class="slogo-fb">{_html.escape(_tc6[:2])}</div>'
        # 升级 A: 通俗一句话解读（替代原始 discovery 截断）
        _sd6 = swarm_detail.get(_tc6, {})
        _ins6 = _html.escape(_build_plain_insight(_tc6, _sd6))
        # 升级 B: 风险标签
        _risk6 = _risk_badge(_tc6, _sd6)
        # 升级 D: 催化剂倒计时
        _cat6 = _catalyst_countdown(_tc6, _sd6)
        # 升级 E: Score Delta
        _delta6 = _score_deltas.get(_tc6, {}).get("html", "")
        # 升级 F: 信号冲突预警
        _conf6 = _signal_conflicts(_tc6, _sd6)
        _ml6ex = _Path(report_dir / f"alpha-hive-{_tc6}-ml-enhanced-{date_str}.html").exists()
        _ml6   = (f'<a href="alpha-hive-{_tc6}-ml-enhanced-{date_str}.html" class="ml-btn">ML 详情 →</a>'
                  if _ml6ex else '<span style="font-size:.75em;color:var(--ts);">ML 报告生成中</span>')
        # 升级 G: 维度 mini-bars（增强版：数值 + tooltip）
        _dims6 = _sd6.get("dimension_scores", {})
        _dim_html6 = ""
        if _dims6:
            _dl6 = [("信号","signal"),("催化","catalyst"),("情绪","sentiment"),("赔率","odds"),("风险","risk_adj")]
            _db6 = ""
            for _dlbl6x, _dkey6 in _dl6:
                _dv6  = float(_dims6.get(_dkey6, 5.0))
                _dpct6 = max(5, int(_dv6 * 10))
                _dcol6 = "#22c55e" if _dv6 >= 7 else ("#f59e0b" if _dv6 >= 5.5 else "#ef4444")
                _tip6 = _DIM_TOOLTIPS.get(_dlbl6x, "")
                _db6 += (f'<div class="dim-b-item" title="{_html.escape(_tip6)}">'
                         f'<div class="dim-val" style="color:{_dcol6}">{_dv6:.0f}</div>'
                         f'<div class="dim-b" style="height:{_dpct6}%;background:{_dcol6}"></div>'
                         f'<span class="dim-lbl">{_dlbl6x}</span></div>')
            _dim_html6 = f'<div class="dim-bars">{_db6}</div>'
        # F10: 价格标注
        _det6 = _detail(_tc6)
        _price6_html = ""
        if _det6["price"] is not None:
            _p6 = _det6["price"]
            _m6 = _det6["momentum_5d"]
            _mstr6 = f"{_m6:+.1f}%" if _m6 is not None else ""
            _mcls6 = "sprice-up" if _m6 and _m6 > 0 else ("sprice-dn" if _m6 and _m6 < 0 else "sprice-flat")
            _price6_html = (f'<div class="sprice-row">'
                            f'<span class="sprice">${_p6:,.2f}</span>'
                            f'{f"""<span class="sprice-chg {_mcls6}">{_mstr6}</span>""" if _mstr6 else ""}'
                            f'</div>')
        new_cards_html += f"""
        <div class="scard" data-dir="{_dr6}" data-score="{_sc6:.1f}" onclick="scrollToDeep('{_html.escape(_tc6)}')">
          <button class="scard-share" onclick="event.stopPropagation();shareCard('{_html.escape(_tc6)}',{_sc6:.1f})">𝕏</button>
          <div class="scard-head">
            <div class="slogo-wrap">{_logo6}<span class="srank">#{_ci}</span></div>
            <div class="scard-badges"><span class="sdir {_dcls6}">{_dlbl6}</span>{_risk6}</div>
          </div>
          <div class="scard-body">
            <div class="sticker">{_html.escape(_tc6)}</div>
            <div class="score-row">
              <span class="score-big {_scls6}">{_sc6:.1f}</span>{_delta6}
              <div class="sbar-wrap">
                <div class="sbar-lbl"><span>综合分</span><span>/10</span></div>
                <div class="sbar"><div class="sbar-fill {_fcls6}" style="width:{_pct6}%"></div></div>
              </div>
            </div>
            {_dim_html6}
            {_cat6}
            {_price6_html}
            {_conf6}
            {f'<div class="sinsight">{_ins6}</div>' if _ins6 else ''}
            {_ml6}
          </div>
        </div>"""

    # ── Build Full Table rows ──
    new_rows_html = ""
    for _ri, _trt in enumerate(all_tickers_sorted, 1):
        _ort = opp_by_ticker.get(_trt, {})
        _srt = float(_ort.get("opp_score") or swarm_detail.get(_trt, {}).get("final_score", 0))
        _drt = str(_ort.get("direction") or swarm_detail.get(_trt, {}).get("direction","neutral")).lower()
        if "多" in _drt: _drt = "bullish"
        elif "空" in _drt: _drt = "bearish"
        elif _drt not in ("bullish","bearish","neutral"): _drt = "neutral"
        _dlrt = {"bullish":"看多","bearish":"看空","neutral":"中性"}[_drt]
        _dclrt = {"bullish":"dcell-bull","bearish":"dcell-bear","neutral":"dcell-neut"}[_drt]
        _scrt = sc_cls(_srt)
        _det_rt = _detail(_trt)
        _res_rt = swarm_detail.get(_trt,{}).get("resonance",{}).get("resonance_detected",False)
        _sup_rt = int(_ort.get("supporting_agents") or swarm_detail.get(_trt,{}).get("supporting_agents",0))
        _res_html_rt = (f'<span class="res-y">{_sup_rt}A</span>' if _res_rt else '<span class="res-n">无</span>')
        _ml_ex_rt = _Path(report_dir / f"alpha-hive-{_trt}-ml-enhanced-{date_str}.html").exists()
        _ml_rt = (f'<a href="alpha-hive-{_trt}-ml-enhanced-{date_str}.html" class="ml-btn-sm">查看</a>'
                  if _ml_ex_rt else "-")
        _pc_st_rt = (' style="color:var(--bull);font-weight:700"' if _det_rt["pc"] != "-" and float(_det_rt["pc"]) < 0.7
                     else (' style="color:var(--bear);font-weight:700"' if _det_rt["pc"] != "-" and float(_det_rt["pc"]) > 1.5 else ""))
        _prt = _det_rt["price"]
        _mrt = _det_rt["momentum_5d"]
        _ptd_rt = f'${_prt:,.2f}' if _prt is not None else '-'
        _mtd_rt = (f'<span class="{"sprice-up" if _mrt > 0 else "sprice-dn"}">{_mrt:+.1f}%</span>'
                   if _mrt is not None and _mrt != 0 else ('-' if _mrt is None else '<span class="sprice-flat">0.0%</span>'))
        # 升级 B/E: 表格行风险标签 + delta
        _risk_rt = _risk_badge(_trt, swarm_detail.get(_trt, {}))
        _delta_rt = _score_deltas.get(_trt, {}).get("html", "")
        new_rows_html += f"""
        <tr data-dir="{_drt}" data-score="{_srt:.1f}">
          <td>{_ri}</td>
          <td><strong>{_html.escape(_trt)}</strong></td>
          <td><span class="{_dclrt}">{_dlrt}</span> {_risk_rt}</td>
          <td class="{_scrt}"><strong>{_srt:.1f}</strong>/10 {_delta_rt}</td>
          <td>{_ptd_rt}</td>
          <td>{_mtd_rt}</td>
          <td>{_res_html_rt}</td>
          <td>{_det_rt['bullish']}/{_det_rt['bearish_v']}/{_det_rt['neutral_v']}</td>
          <td>{_det_rt['iv_rank']}</td>
          <td{_pc_st_rt}>{_det_rt['pc']}</td>
          <td style="color:var(--neut)">{_det_rt['bear_score']:.1f}</td>
          <td>{_ml_rt}</td>
        </tr>"""

    # ── Build Deep Analysis cards (with radar canvas) ──
    _dir_hdr3 = {"bullish":"#1a7a3a","bearish":"#8b1a1a","neutral":"#7a5c1a"}
    new_company_html = ""
    for _tkrd in all_tickers_sorted:
        _sdd = swarm_detail.get(_tkrd, {})
        _add = _sdd.get("agent_details", {})
        _scd = float(opp_by_ticker.get(_tkrd,{}).get("opp_score") or _sdd.get("final_score", 0))
        _drd = str(opp_by_ticker.get(_tkrd,{}).get("direction") or _sdd.get("direction","neutral")).lower()
        if "多" in _drd: _drd = "bullish"
        elif "空" in _drd: _drd = "bearish"
        elif _drd not in ("bullish","bearish","neutral"): _drd = "neutral"
        _dlbld = {"bullish":"看多 ↑","bearish":"看空 ↓","neutral":"中性 →"}[_drd]
        _hcd   = _dir_hdr3.get(_drd, "#1a3a7a")
        _detd  = _detail(_tkrd)
        # F10: 预计算价格 HTML（避免嵌套 f-string）
        _pd = _detd["price"]
        _md = _detd["momentum_5d"]
        if _pd is not None:
            _mhtml_d = ""
            if _md is not None:
                _mcls_d = "sprice-up" if _md > 0 else ("sprice-dn" if _md < 0 else "sprice-flat")
                _mhtml_d = f' <span class="sprice-chg {_mcls_d}">{_md:+.1f}%</span>'
            _price_metric_d = f'<div class="cc-metric"><span class="cm-l">当前价格</span><span class="cm-v">${_pd:,.2f}{_mhtml_d}</span></div>'
        else:
            _price_metric_d = ""
        _blstd = []
        for _discd, _icod, _lbd in [
            (_add.get("ScoutBeeNova",{}).get("discovery",""),       "📋","内幕"),
            (_add.get("OracleBeeEcho",{}).get("discovery",""),      "📊","期权"),
            (_add.get("BuzzBeeWhisper",{}).get("discovery",""),     "💬","情绪"),
            (_add.get("BearBeeContrarian",{}).get("discovery",""),  "🐻","风险"),
        ]:
            _fd = _discd.split("|")[0].strip()[:85] if _discd else ""
            if _fd:
                _blstd.append(f'<li>{_icod} <strong>{_lbd}：</strong>{_html.escape(_fd)}</li>')
        _bhtmld = "\n                    ".join(_blstd) if _blstd else "<li>数据采集中</li>"
        _ml_exd = _Path(report_dir / f"alpha-hive-{_tkrd}-ml-enhanced-{date_str}.html").exists()
        _mlbtnd = (f'<a href="alpha-hive-{_tkrd}-ml-enhanced-{date_str}.html" class="ml-btn-cc">ML 增强分析 →</a>'
                   if _ml_exd else '<span style="font-size:.78em;color:var(--ts)">ML 报告生成中</span>')
        # ── edgar_rss badge ──
        _rss_n = _add.get("ScoutBeeNova", {}).get("details", {}).get("insider", {}).get("rss_fresh_today", 0)
        _rss_badge = (f'<span class="rss-badge">📋 今日Form4 {_rss_n}份 🔴</span>' if _rss_n else "")
        # ── thesis break 面板（直接查询配置，不依赖 JSON 中转）──
        try:
            from thesis_breaks import ThesisBreakConfig as _TBC
            _tb_cfg = _TBC.get_breaks_config(_tkrd)
            _tb_l1 = [c["metric"] + "：" + c["trigger"]
                      for c in _tb_cfg.get("level_1_warning", {}).get("conditions", [])] if _tb_cfg else []
            _tb_l2 = [c["metric"] + "：" + c["trigger"]
                      for c in _tb_cfg.get("level_2_stop_loss", {}).get("conditions", [])] if _tb_cfg else []
        except (KeyError, TypeError, ImportError, AttributeError) as _tb_err:
            _log.debug("thesis-break 配置解析失败: %s", _tb_err)
            _tb_l1, _tb_l2 = [], []
        if _tb_l1 or _tb_l2:
            _tb_html = '<div class="thesis-break-box">'
            _tb_html += '<div class="tb-title">⚠️ 失效条件监控</div>'
            if _tb_l1:
                _tb_html += '<div class="tb-level tb-l1">Level 1 预警</div><ul class="tb-list">'
                for _c in _tb_l1[:3]:
                    _tb_html += f'<li>{_html.escape(str(_c))}</li>'
                _tb_html += '</ul>'
            if _tb_l2:
                _tb_html += '<div class="tb-level tb-l2">Level 2 止损</div><ul class="tb-list">'
                for _c in _tb_l2[:3]:
                    _tb_html += f'<li>{_html.escape(str(_c))}</li>'
                _tb_html += '</ul>'
            _tb_html += '</div>'
        else:
            _tb_html = ""
        # 升级 B/D/E/F: 深度卡片增强
        _risk_d = _risk_badge(_tkrd, _sdd)
        _delta_d = _score_deltas.get(_tkrd, {}).get("html", "")
        _cat_d = _catalyst_countdown(_tkrd, _sdd)
        _conf_d = _signal_conflicts(_tkrd, _sdd)
        _ins_d = _html.escape(_build_plain_insight(_tkrd, _sdd))
        new_company_html += f"""
        <div class="company-card" data-dir="{_drd}" data-score="{_scd:.1f}" id="deep-{_html.escape(_tkrd)}">
          <div class="cc-header" style="background:{_hcd};">
            <span class="cc-ticker">{_html.escape(_tkrd)}</span>
            <span class="cc-dir">{_dlbld}</span> {_risk_d}
            <span class="cc-score">{_scd:.1f}/10 {_delta_d}</span>
          </div>
          <div class="cc-body">
            {f'<div class="sinsight" style="margin-bottom:10px">{_ins_d}</div>' if _ins_d else ''}
            {_cat_d}
            {_conf_d}
            <div class="cc-two">
              <div class="cc-metrics-col">
                {_price_metric_d}
                <div class="cc-metric"><span class="cm-l">IV Rank</span><span class="cm-v">{_detd['iv_rank']}</span></div>
                <div class="cc-metric"><span class="cm-l">P/C Ratio</span><span class="cm-v">{_detd['pc']}</span></div>
                {f'<div class="cc-metric"><span class="cm-l">期权流向</span><span class="cm-v" style="color:{_detd["flow_color"]};font-weight:bold;">{_detd["flow_dir"]}</span></div>' if _detd["flow_dir"] != "-" else ""}
                {f'<div class="cc-metric"><span class="cm-l">GEX</span><span class="cm-v">{_detd["gex"]}</span></div>' if _detd["gex"] != "-" else ""}
                <div class="cc-metric"><span class="cm-l">看空强度</span><span class="cm-v">{_detd['bear_score']:.1f}/10</span></div>
                <div class="cc-metric"><span class="cm-l">投票</span><span class="cm-v">{_detd['bullish']}多/{_detd['bearish_v']}空</span></div>
              </div>
              <div class="radar-wrap"><div class="skeleton"><div class="skel-circle"></div></div><canvas id="radar-{_html.escape(_tkrd)}" width="160" height="160"></canvas></div>
            </div>
            <ul class="cc-signals">{_bhtmld}</ul>
            {_build_dim_dq_html(_detd['dim_dq'])}
            {_tb_html}
            <div class="cc-footer">{_rss_badge}{_mlbtnd}</div>
          </div>
        </div>"""

    # ── 历史简报回溯 + F&G 历史 + 评分趋势数据 ──
    _hist_entries = []
    _fg_history = [{"date": date_str, "value": _fv3}]  # 当天 F&G
    _trend_data = {}  # {ticker: [{date, score}, ...]}
    _hist_full = {}   # {date: [{ticker, score, direction}, ...]}  for diff
    # 当天趋势数据
    for _tt in all_tickers_sorted:
        _tts = float(opp_by_ticker.get(_tt, {}).get("opp_score") or swarm_detail.get(_tt, {}).get("final_score", 0))
        _trend_data.setdefault(_tt, []).append({"date": date_str, "score": round(_tts, 1)})
    _hist_full[date_str] = [
        {"ticker": _tt, "score": round(float(opp_by_ticker.get(_tt, {}).get("opp_score") or swarm_detail.get(_tt, {}).get("final_score", 0)), 1),
         "direction": str(opp_by_ticker.get(_tt, {}).get("direction") or swarm_detail.get(_tt, {}).get("direction", "neutral")).lower()}
        for _tt in all_tickers_sorted
    ]
    try:
        import glob as _glob
        _hist_files = sorted(
            _glob.glob(str(report_dir / "alpha-hive-daily-*.json")),
            reverse=True  # 最新在前
        )
        for _hf in _hist_files:
            _hdate = _Path(_hf).stem.replace("alpha-hive-daily-", "")
            if _hdate == date_str:
                continue  # 今天已在主面板展示
            try:
                with open(_hf, encoding="utf-8") as _hfp:
                    _hrpt = _json.load(_hfp)
                _hopps = _hrpt.get("opportunities", [])
                _hmeta = _hrpt.get("swarm_metadata", {})
                _hn    = _hmeta.get("tickers_analyzed", len(_hopps))
                # 全部 opps 数据（用于趋势 + diff）
                _hall_opps = [
                    {"ticker": o.get("ticker",""), "score": float(o.get("opp_score",0)),
                     "direction": str(o.get("direction","neutral")).lower()}
                    for o in _hopps if o.get("ticker")
                ]
                # 趋势数据：每个 ticker 每天的评分
                for _ho in _hall_opps:
                    _trend_data.setdefault(_ho["ticker"], []).append(
                        {"date": _hdate, "score": round(_ho["score"], 1)})
                # diff 全量数据
                _hist_full[_hdate] = _hall_opps
                # 按 opp_score 降序取 Top 3
                _htop3 = sorted(_hall_opps, key=lambda x: x["score"], reverse=True)[:3]
                _havg  = sum(o["score"] for o in _htop3) / len(_htop3) if _htop3 else 0
                # 可用的 ML 报告
                _hml   = [t for t in [o["ticker"] for o in _hopps]
                          if _Path(report_dir / f"alpha-hive-{t}-ml-enhanced-{_hdate}.html").exists()]
                # 提取 F&G 值（从 swarm_results）
                _hfg_val = None
                try:
                    _hsr_path = report_dir / f".swarm_results_{_hdate}.json"
                    if _hsr_path.exists():
                        with open(_hsr_path, encoding="utf-8") as _hsr_fp:
                            _hsr = _json.load(_hsr_fp)
                        for _htk in _hsr:
                            _hbuzz = _hsr[_htk].get("agent_details", {}).get("BuzzBeeWhisper", {}).get("discovery", "")
                            _hfg_m = _re.search(r'F&G\s*(\d+)', _hbuzz)
                            if _hfg_m:
                                _hfg_val = int(_hfg_m.group(1))
                                break
                except (KeyError, ValueError, IndexError, TypeError):
                    pass
                if _hfg_val is not None:
                    _fg_history.append({"date": _hdate, "value": _hfg_val})
                _hist_entries.append({
                    "date": _hdate, "n": _hn, "top3": _htop3,
                    "avg": _havg, "ml_tickers": _hml,
                    "has_md":   _Path(report_dir / f"alpha-hive-daily-{_hdate}.md").exists(),
                    "has_json": _Path(report_dir / f"alpha-hive-daily-{_hdate}.json").exists(),
                })
            except Exception as _he:
                _log.debug("历史报告 %s 解析失败: %s", _hdate, _he)
    except Exception as _hle:
        _log.debug("历史时间线加载失败: %s", _hle)

    # 排序 F&G 历史和趋势数据（按日期升序）
    _fg_history.sort(key=lambda x: x["date"])
    for _tk in _trend_data:
        _trend_data[_tk].sort(key=lambda x: x["date"])

    # 生成历史时间线 HTML
    _dir_icon = {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}
    _dir_cn   = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
    _hist_html = ""
    if _hist_entries:
        for _he in _hist_entries:
            _top3_html = ""
            for _ht in _he["top3"]:
                _hscls = "sc-h" if _ht["score"] >= 7.0 else ("sc-m" if _ht["score"] >= 5.5 else "sc-l")
                _hdir  = _ht["direction"] if _ht["direction"] in _dir_icon else "neutral"
                _top3_html += f"""<div class="htop-chip">
                  <span class="hticker">{_html.escape(_ht['ticker'])}</span>
                  <span class="hscore {_hscls}">{_ht['score']:.1f}</span>
                  <span class="hdir">{_dir_icon.get(_hdir,'🟡')}</span>
                </div>"""
            _hlinks = ""
            if _he["has_json"]:
                _hlinks += f'<a href="alpha-hive-daily-{_he["date"]}.json" target="_blank" rel="noopener" class="hlink hlink-json">📊 完整数据</a>'
            for _hmt in _he["ml_tickers"][:4]:
                _hlinks += f'<a href="alpha-hive-{_hmt}-ml-enhanced-{_he["date"]}.html" target="_blank" rel="noopener" class="hlink hlink-ml">{_html.escape(_hmt)}</a>'
            _hist_html += f"""
            <div class="hist-card">
              <div class="hist-left">
                <div class="hist-date">{_he['date']}</div>
                <div class="hist-meta">{_he['n']} 标的 · 均分 <span class="{'sc-h' if _he['avg']>=7 else ('sc-m' if _he['avg']>=5.5 else 'sc-l')}">{_he['avg']:.1f}</span></div>
              </div>
              <div class="hist-mid">{_top3_html}</div>
              <div class="hist-right">{_hlinks}</div>
            </div>"""
    else:
        _hist_html = '<div class="hist-empty">暂无历史记录，第一份历史简报将在明天出现 📅</div>'

    # ── Avg Score formatted ──
    _avg_score_str = f"{_avg_score:.1f}"
    _fg_str2 = _fg_str  # already computed above

    # ── 准确率 Dashboard 数据拼装 ──
    import json as _json

    # 方向图数据
    _dir_map_acc = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}

    # 个股行表格
    _acc_ticker_rows = ""
    for _tk, _tv in sorted(_acc_by_ticker.items(), key=lambda x: -x[1].get("accuracy", 0)):
        _tacc  = _tv.get("accuracy", 0)
        _tpill = "pill-green" if _tacc >= 0.6 else ("pill-red" if _tacc < 0.4 else "pill-gray")
        _tret  = _tv.get("avg_return", 0)
        _tret_color = "#16a34a" if _tret > 0 else "#dc2626"
        _acc_ticker_rows += (
            f'<tr><td><strong>{_tk}</strong></td>'
            f'<td>{_tv.get("total", 0)}</td>'
            f'<td>{_tv.get("correct", 0)}</td>'
            f'<td><span class="acc-pill {_tpill}">{_tacc*100:.0f}%</span></td>'
            f'<td style="color:{_tret_color}">{_tret:+.1f}%</td></tr>'
        )

    # 冷启动：统计 pending 预测数（直接查真实 DB）
    _acc_pending = 0
    try:
        from backtester import PredictionStore as _PS2
        _ps2 = _PS2()
        import sqlite3 as _sq3
        with _sq3.connect(_ps2.db_path) as _conn:
            _acc_pending = _conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    except Exception as _dbe:
        _log.debug("预测记录统计查询失败: %s", _dbe)

    # 准确率百分比（格式化）
    _acc_overall_pct = _acc_overall * 100

    # F11: 生成最佳/最差预测 HTML
    _dir_cn11 = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
    def _pred_list_html(preds, is_best=True):
        if not preds:
            return '<div style="font-size:.82em;color:var(--ts);padding:8px 0">数据积累中...</div>'
        h = '<ul class="pred-list">'
        for p in preds:
            _ret = p.get("return_t7", 0) or 0
            _rcls = "pred-ret-up" if _ret > 0 else "pred-ret-dn"
            _dir = _dir_cn11.get(p.get("direction", ""), p.get("direction", ""))
            h += (f'<li class="pred-item">'
                  f'<span class="pred-tk">{p.get("ticker","")}</span>'
                  f'<span class="pred-date">{p.get("date","")[:10]} · {_dir}</span>'
                  f'<span class="pred-ret {_rcls}">{_ret:+.1f}%</span>'
                  f'</li>')
        h += '</ul>'
        return h
    _best3_html = _pred_list_html(_acc_best3, True)
    _worst3_html = _pred_list_html(_acc_worst3, False)

    # F11: 额外指标行 HTML
    _acc_extra_metrics = (
        f'<div class="acc-metrics-row">'
        f'<div class="acc-metric-pill"><span class="mv">{_acc_sharpe:+.2f}</span><span class="ml">Sharpe Ratio</span></div>'
        f'<div class="acc-metric-pill"><span class="mv">{_acc_max_dd:.1f}%</span><span class="ml">最大回撤</span></div>'
        f'<div class="acc-metric-pill"><span class="mv">{_acc_win_streak}</span><span class="ml">当前连胜</span></div>'
        f'</div>'
    ) if _acc_total_checked > 0 else ""

    # F11: 增强准确率面板（胜率趋势 + 最佳/最差预测）
    _acc_enhanced_html = ""
    if _acc_total_checked > 0:
        _acc_enhanced_html = f"""
    {_acc_extra_metrics}
    <div class="acc-extra-row">
      <div class="acc-ext-box">
    <div class="acc-ext-title">📈 胜率走势（按周）</div>
    <div style="height:160px"><canvas id="accWinTrendChart"></canvas></div>
      </div>
      <div class="acc-ext-box">
    <div class="acc-ext-title">🏆 最佳预测 Top 3</div>
    {_best3_html}
    <div class="acc-ext-title" style="margin-top:14px">💀 最差预测 Top 3</div>
    {_worst3_html}
      </div>
    </div>"""

    # 生成准确率 HTML Section
    if _acc_total_checked > 0:
        _acc_section_html = f"""
  <!-- ── Accuracy Dashboard ── -->
  <div class="section" id="accuracy">
    <div class="acc-section-title">📈 预测准确率追踪（T+7 验证）</div>
    <div class="acc-kpi-row">
      <div class="acc-kpi"><div class="kv">{_acc_overall_pct:.0f}%</div><div class="kl">综合准确率</div></div>
      <div class="acc-kpi"><div class="kv">{_acc_total_checked}</div><div class="kl">已验证预测</div></div>
      <div class="acc-kpi"><div class="kv">{_acc_correct}</div><div class="kl">预测正确数</div></div>
      <div class="acc-kpi"><div class="kv">{_acc_avg_return:+.1f}%</div><div class="kl">平均收益率</div></div>
    </div>
    <div class="acc-two-col">
      <div class="acc-dir-box">
    <div class="acc-box-title">方向准确率分布</div>
    <div class="acc-canvas-wrap"><canvas id="accDirChart" width="300" height="200"></canvas></div>
      </div>
      <div class="acc-ticker-box">
    <div class="acc-box-title">个股准确率明细</div>
    <table class="acc-table" id="accTickerTable">
      <thead><tr>
        <th>标的</th><th>预测数</th><th>正确数</th><th>准确率</th><th>均收益%</th>
      </tr></thead>
      <tbody>
        {_acc_ticker_rows}
      </tbody>
    </table>
      </div>
    </div>
    {_acc_enhanced_html}
  </div>"""
    elif _acc_pending > 0:
        _acc_section_html = f"""
  <!-- ── Accuracy Dashboard (cold start) ── -->
  <div class="section" id="accuracy">
    <div class="acc-section-title">📈 预测准确率追踪（T+7 验证）</div>
    <div class="acc-dir-box acc-cold">
      <div class="cold-icon">🕐</div>
      <div class="cold-msg">系统正在积累预测记录，准确率数据将在 T+7 后自动显示<br>
      当前已保存 <strong>{_acc_pending}</strong> 条预测，等待价格验证中...</div>
    </div>
  </div>"""
    else:
        _acc_section_html = ""


    # F12: 搜索索引数据
    _dir_cn12 = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
    _search_index = []
    for _t12 in all_tickers_sorted:
        _s12 = float(opp_by_ticker.get(_t12, {}).get("opp_score") or swarm_detail.get(_t12, {}).get("final_score", 0))
        _d12 = str(opp_by_ticker.get(_t12, {}).get("direction") or swarm_detail.get(_t12, {}).get("direction", "neutral")).lower()
        if "多" in _d12: _d12 = "bullish"
        elif "空" in _d12: _d12 = "bearish"
        elif _d12 not in ("bullish", "bearish", "neutral"): _d12 = "neutral"
        _det12 = _detail(_t12)
        _search_index.append({
            "ticker": _t12,
            "score": round(_s12, 1),
            "direction": _dir_cn12.get(_d12, "中性"),
            "price": _det12["price"],
        })

    # ── Jinja2 渲染 ──
    _avg_score_str = f"{_avg_score:.1f}" if _all_scores else "0"

    # JS 数据（传给 window.__AH__）
    _data_obj = {
        "fv": _fv3,
        "fg_label": _fg_label,
        "scores": [[t, round(s, 1)] for t, s in _all_scores],
        "dir_counts": [_dir_counts["bullish"], _dir_counts["bearish"], _dir_counts["neutral"]],
        "radar": {t: _radar_data(t) for t in all_tickers_sorted},
        "acc_dir_labels": [_dir_map_acc.get(d, d) for d in ["bullish", "bearish", "neutral"]],
        "acc_dir_accs": [round(_acc_by_dir.get(d, {}).get("accuracy", 0) * 100, 1)
                         for d in ["bullish", "bearish", "neutral"]],
        "acc_dir_tots": [_acc_by_dir.get(d, {}).get("total", 0)
                         for d in ["bullish", "bearish", "neutral"]],
        "acc_weekly": _acc_weekly_trend,
        "fg_history": _fg_history,
        "trend_data": _trend_data,
        "hist_full": _hist_full,
        "search_index": _search_index,
    }

    # Sprint 4.1: 输出 dashboard-data.json 伴生文件（前端动态加载用）
    _data_obj["_generated_at"] = now_str
    _data_obj["_date"] = date_str
    _data_obj["_deploy_ts"] = int(_dt.now().timestamp())  # D5: Unix 秒级指纹
    try:
        _json_path = _Path_mod(report_dir) / "dashboard-data.json"
        _json_path.write_text(
            _json.dumps(_data_obj, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        _log.info("dashboard-data.json 已生成：%s", _json_path.name)
    except OSError as _je:
        _log.warning("dashboard-data.json 写入失败: %s", _je)

    _env = Environment(autoescape=False)
    _tpl = _env.from_string(_load_tpl("dashboard.html"))

    return _tpl.render(
        css=dashboard_css,
        js=_load_tpl("dashboard.js"),
        data_json=_json.dumps(_data_obj, ensure_ascii=False),
        n_tickers=n_tickers,
        now_str=now_str,
        n_agents=n_agents,
        avg_real=avg_real,
        n_resonance=n_resonance,
        fg_color=_fg_color,
        fg_str=_fg_str,
        avg_score_str=_avg_score_str,
        hero_tldr=_hero_tldr,
        macro_vix=_macro_vix,
        macro_10y=_macro_10y,
        macro_yc=_macro_yc,
        macro_yc_cls=_macro_yc_cls,
        macro_gld=_macro_gld,
        macro_gld_cls=_macro_gld_cls,
        macro_sector_html=_macro_sector_html,
        top_n=min(6, len(all_tickers_sorted)),
        scores_chart_height="{}px".format(max(160, len(all_tickers_sorted) * 28)),
        cards_html=new_cards_html,
        rows_html=new_rows_html,
        company_html=new_company_html,
        report_body=_rpt_body,
        hist_html=_hist_html,
        acc_section_html=_acc_section_html,
    )
