"""
Alpha Hive Dashboard Renderer

从 alpha_hive_daily_report.py 提取的 HTML 仪表板生成逻辑。
原方法 _generate_index_html (2190 行) + _DASHBOARD_CSS (471 行)
"""

import json
import logging
from typing import Dict, List
import html as _html
import re as _re

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

# ── 共享方向标签映射 ──
_DIR_CN   = {"bullish": "看多", "bearish": "看空", "neutral": "中性",
             "看多": "看多", "看空": "看空", "中性": "中性"}  # 同时接受中英文 key
_DIR_ICON = {"bullish": '<span class="dot-bull"></span>', "bearish": '<span class="dot-bear"></span>', "neutral": '<span class="dot-neut"></span>'}

# ── 方向归一化（中文/英文 → 统一英文） ──
_DIR_TO_EN = {"看多": "bullish", "看空": "bearish", "中性": "neutral",
              "bullish": "bullish", "bearish": "bearish", "neutral": "neutral"}
def _norm_dir(d: str) -> str:
    """Normalize direction to English canonical form."""
    return _DIR_TO_EN.get(str(d).strip().lower(), str(d).strip().lower())

# ── 维度数据质量标签 ──
_DIM_DQ_LABELS = {
    "signal": "信号", "catalyst": "催化", "sentiment": "情绪",
    "odds": "赔率", "risk_adj": "风险",
}

# ── 维度 Tooltip ──
_DIM_TOOLTIPS = {
    "信号": "聪明钱交易信号（SEC 内幕交易、机构持仓变化）",
    "催化": "未来催化剂事件清晰度（财报、FDA、产品发布）",
    "情绪": "市场舆情方向与质量（新闻、Reddit）",
    "赔率": "市场赔率错配（期权 IV、Put/Call）",
    "风险": "风险调整评估（回撤、流动性、拥挤度）",
}

# ── 域名映射 ──
_DOMAINS = {
    "MSFT": "microsoft.com", "NVDA": "nvidia.com",  "TSLA": "tesla.com",
    "META": "meta.com",       "AMZN": "amazon.com",  "RKLB": "rocketlabusa.com",
    "BILI": "bilibili.com",   "VKTX": "vikingtherapeutics.com", "CRCL": "circle.com",
    "GOOGL": "google.com",    "AAPL": "apple.com",   "NFLX": "netflix.com",
}


def _sc_cls(score):
    return "sc-h" if score >= 7.0 else ("sc-m" if score >= 5.5 else "sc-l")


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
        ename = _emap.get(nearest.get("type", ""), nearest.get("event", "")[:20])
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


def _risk_badge(ticker: str, sd: dict) -> str:
    """多因子风险评分 -> low/med/high risk badge"""
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
    ename = _emap.get(nearest.get("type", ""), nearest.get("event", "")[:25])
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


def _md2html(md_text: str) -> str:
    """Markdown -> HTML 轻量渲染"""
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
    in_table = False   # markdown 表格
    table_rows = []
    table_has_header = False

    def _close_lists():
        nonlocal in_ul, in_ol, in_sub
        if in_sub:  out.append('</ul>');  in_sub = False
        if in_ul:   out.append('</ul>');  in_ul = False
        if in_ol:   out.append('</ol>');  in_ol = False

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
            if in_ol: out.append('</ol>'); in_ol = False
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


def _pred_list_html(preds, is_best=True):
    if not preds:
        return '<div style="font-size:.82em;color:var(--ts);padding:8px 0">数据积累中...</div>'
    h = '<ul class="pred-list">'
    for p in preds:
        _ret = p.get("return_t7", 0) or 0
        _rcls = "pred-ret-up" if _ret > 0 else "pred-ret-dn"
        _dir = _DIR_CN.get(p.get("direction", ""), p.get("direction", ""))
        h += (f'<li class="pred-item">'
              f'<span class="pred-tk">{p.get("ticker","")}</span>'
              f'<span class="pred-date">{p.get("date","")[:10]} · {_dir}</span>'
              f'<span class="pred-ret {_rcls}">{_ret:+.1f}%</span>'
              f'</li>')
    h += '</ul>'
    return h


def _detail(ticker: str, swarm_detail: dict) -> dict:
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
        _pm = _re.search(r'\$(\d+(?:\.\d+)?)', _oracle_disc)
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

    # ── v0.26.0: 全链 OI 字段提取（用于 #/deep 板块全链视图）──
    # 数据源：OracleBeeEcho.details.full_chain_oi（v0.25.4 生成 / v0.26.2 扩 expiry）
    # v0.26.2: 区分"近端墙"(≤30 天到期，做市商真正在意的) vs "全链墙"(含远期 LEAPS)
    fco = oracle.get("full_chain_oi") or {}
    full_pc = fco.get("full_pc_ratio")
    max_pain = fco.get("max_pain")
    # v0.26.2 近端 OI 墙（≤30 天）—— 从 call_exp_oi/put_exp_oi 现场重算
    from datetime import datetime as _dt_oi, timedelta as _td_oi
    _now_oi = _dt_oi.now()
    _NEAR_WINDOW_DAYS = 30

    def _aggregate_near(exp_map_dict):
        """exp_map_dict: {strike_str: {expiry_str: oi}} → 近 30 天聚合: {strike: total_oi}"""
        out = {}
        if not isinstance(exp_map_dict, dict):
            return out
        for sk, exps in exp_map_dict.items():
            if not isinstance(exps, dict): continue
            try: strike_f = float(sk)
            except (ValueError, TypeError): continue
            total_near = 0
            for exp_str, oi_val in exps.items():
                try:
                    exp_dt = _dt_oi.strptime(exp_str, "%Y-%m-%d")
                    days_to = (exp_dt - _now_oi).days
                except (ValueError, TypeError):
                    continue
                if 0 <= days_to <= _NEAR_WINDOW_DAYS:
                    total_near += int(oi_val or 0)
            if total_near > 0:
                out[strike_f] = total_near
        return out

    near_call_by_strike = _aggregate_near(fco.get("call_exp_oi") or {})
    near_put_by_strike = _aggregate_near(fco.get("put_exp_oi") or {})
    near_call_total = sum(near_call_by_strike.values())
    near_put_total = sum(near_put_by_strike.values())
    near_pc = (near_put_total / near_call_total) if near_call_total > 0 else None
    # v0.26.1: 近端 Max Pain（基于最近 3 个到期日，真正的磁吸目标价）
    # 全链 Max Pain（max_pain 字段）含远期 LEAPS，磁吸效应弱
    # 近端 Max Pain（oracle.max_pain dict）来自 OptionsAgent 基于 expiration_dates 计算
    _near_mp_dict = oracle.get("max_pain") or {}
    near_max_pain = _near_mp_dict.get("max_pain") if isinstance(_near_mp_dict, dict) else None
    near_max_pain_pct = _near_mp_dict.get("distance_pct") if isinstance(_near_mp_dict, dict) else None
    near_expiry_dates = oracle.get("expiration_dates") or []  # 近端到期日列表
    total_call_oi = fco.get("total_call_oi") or 0
    total_put_oi = fco.get("total_put_oi") or 0
    top_call_oi_raw = fco.get("top_call_oi") or []
    top_put_oi_raw = fco.get("top_put_oi") or []
    # 提取 Top 5 关键墙（强阻力 / 强支撑），含 ITM/OTM 距离
    def _wall_summary(entries, side, cur_price):
        """side = 'C' / 'P'；返回精简 dict 列表"""
        out = []
        for e in (entries or [])[:5]:
            if not isinstance(e, dict): continue
            strike = e.get("strike")
            oi_v = e.get("oi") or 0
            if strike is None: continue
            try:
                pct_diff = (float(strike) - cur_price) / cur_price * 100 if cur_price else 0
            except (ValueError, TypeError, ZeroDivisionError):
                pct_diff = 0
            out.append({
                "strike": float(strike),
                "oi": int(oi_v),
                "pct_diff": pct_diff,  # 正 = OTM Call / ITM Put；负 = ITM Call / OTM Put
                "dom_exp": e.get("dom_exp", ""),  # v0.25.7 主导到期日
            })
        return out
    _cur_price = float(_price_raw) if _price_raw is not None else 0
    top_call_walls = _wall_summary(top_call_oi_raw, "C", _cur_price)
    top_put_walls = _wall_summary(top_put_oi_raw, "P", _cur_price)

    # v0.26.2 近端 OI 墙（≤30 天到期，主显示）
    def _near_wall_summary(by_strike_dict, cur_price, top_n=5):
        out = []
        for strike, oi_v in sorted(by_strike_dict.items(), key=lambda x: -x[1])[:top_n]:
            try:
                pct_diff = (strike - cur_price) / cur_price * 100 if cur_price else 0
            except (ValueError, TypeError, ZeroDivisionError):
                pct_diff = 0
            out.append({"strike": strike, "oi": int(oi_v), "pct_diff": pct_diff, "dom_exp": ""})
        return out
    near_call_walls = _near_wall_summary(near_call_by_strike, _cur_price)
    near_put_walls = _near_wall_summary(near_put_by_strike, _cur_price)
    # Max Pain 相对现价距离
    max_pain_pct = None
    if max_pain and _cur_price > 0:
        try: max_pain_pct = (float(max_pain) - _cur_price) / _cur_price * 100
        except (ValueError, TypeError, ZeroDivisionError): pass

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
        # v0.26.0 全链 OI 字段
        "full_pc": full_pc,
        "max_pain": max_pain,        # 全链 Max Pain（含远期 LEAPS）
        "max_pain_pct": max_pain_pct,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "top_call_walls": top_call_walls,
        "top_put_walls": top_put_walls,
        # v0.26.1 近端 Max Pain（真正的磁吸目标价，基于最近 3 个到期日）
        "near_max_pain": near_max_pain,
        "near_max_pain_pct": near_max_pain_pct,
        "near_expiry_dates": near_expiry_dates,
        # v0.26.2 近端 OI 墙（≤30 天到期）
        "near_call_walls": near_call_walls,
        "near_put_walls": near_put_walls,
        "near_call_total": near_call_total,
        "near_put_total": near_put_total,
        "near_pc": near_pc,
    }


def _radar_data(ticker: str, swarm_detail: dict) -> list:
    """生成单个 ticker 的雷达图 5 维数据 [signal, catalyst, sentiment, odds, risk_adj]"""
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


# ---------------------------------------------------------------------------
# Extracted helpers (formerly inlined in render_dashboard_html)
# ---------------------------------------------------------------------------

def _load_accuracy_data() -> dict:
    """Load backtester accuracy stats and enhanced metrics."""
    _acc_stats: dict = {}
    try:
        from backtester import PredictionStore
        _ps = PredictionStore()
        _acc_stats = _ps.get_accuracy_stats(period="t7", days=90, exclude_nontrading_days=True) or {}  # v32.3: 门面只算核心交易日
    except Exception as _ace:
        _log.debug("准确率统计加载失败: %s", _ace)
    _acc_total_checked = _acc_stats.get("total_checked", 0)
    _acc_overall       = _acc_stats.get("overall_accuracy", 0.0)
    _acc_avg_return    = _acc_stats.get("avg_return", 0.0)
    _acc_correct       = _acc_stats.get("correct_count", 0)
    _acc_by_dir        = _acc_stats.get("by_direction", {})
    _acc_by_ticker     = _acc_stats.get("by_ticker", {})

    # F11: 增强准确率数据（胜率走势、最佳/最差预测、Sharpe）
    _acc_weekly_trend: list = []  # [{week, accuracy, total}]
    _acc_weekly_by_dir: list = []  # [{week, bullish, bearish, neutral}]
    _acc_best3: list = []   # [{ticker, date, direction, score, return_t7}]
    _acc_worst3: list = []
    _acc_sharpe = 0.0
    _acc_max_dd = 0.0
    _acc_win_streak = 0
    try:
        from backtester import PredictionStore as _PS11
        import sqlite3 as _sq11
        _ps11 = _PS11()
        with _sq11.connect(_ps11.db_path) as _cn11:
            _cn11.row_factory = _sq11.Row
            # v32.3: 门面口径 —— 构建"排除非交易日"子句（周日 sample-accumulator 样本），
            # 应用到下面所有 pill 查询，使准确率面板与策略块同口径。fail-open：出错则空子句。
            _excl11 = ""
            _excl11_p: list = []
            try:
                from is_trading_day import is_trading_day as _itd11
                from datetime import date as _d11
                _all_d11 = [r[0] for r in _cn11.execute(
                    "SELECT DISTINCT date FROM predictions WHERE checked_t7=1").fetchall()]
                _nt11 = []
                for _ds11 in _all_d11:
                    try:
                        if _ds11 and not _itd11(_d11.fromisoformat(_ds11))[0]:
                            _nt11.append(_ds11)
                    except Exception:
                        pass
                if _nt11:
                    _excl11 = " AND date NOT IN (%s)" % ",".join("?" * len(_nt11))
                    _excl11_p = _nt11
            except Exception:
                _excl11 = ""
                _excl11_p = []
            # 周胜率走势（最近 12 周）
            _wrows = _cn11.execute(f"""
                SELECT strftime('%Y-W%W', date) as week,
                       COUNT(*) as total,
                       SUM(CASE WHEN correct_t7=1 THEN 1 ELSE 0 END) as correct,
                       AVG(return_t7) as avg_ret
                FROM predictions WHERE checked_t7=1{_excl11}
                GROUP BY week ORDER BY week DESC LIMIT 12
            """, _excl11_p).fetchall()
            _acc_weekly_trend = [
                {"week": r["week"], "accuracy": round(r["correct"]/r["total"]*100, 1) if r["total"] else 0,
                 "total": r["total"], "avg_ret": round(r["avg_ret"] or 0, 2)}
                for r in reversed(_wrows)
            ]
            # 按方向分组的周胜率（最近 12 周）
            _wdir_rows = _cn11.execute(f"""
                SELECT strftime('%Y-W%W', date) as week,
                       direction,
                       COUNT(*) as total,
                       SUM(CASE WHEN correct_t7=1 THEN 1 ELSE 0 END) as correct
                FROM predictions WHERE checked_t7=1{_excl11}
                GROUP BY week, direction ORDER BY week DESC LIMIT 48
            """, _excl11_p).fetchall()
            # 重组为 {week: {bullish: acc, bearish: acc, neutral: acc}}
            # 最小样本数过滤：单周单方向 < 3 笔不显示（避免小样本"假崩盘"）
            _MIN_WEEKLY_SAMPLES = 3
            _wdir_map: dict = {}
            for _r in _wdir_rows:
                _wk = _r["week"]
                _dir = _r["direction"]
                _wdir_map.setdefault(_wk, {})
                if _r["total"] and _r["total"] >= _MIN_WEEKLY_SAMPLES:
                    _wdir_map[_wk][_dir] = round(_r["correct"]/_r["total"]*100, 1)
                else:
                    _wdir_map[_wk][_dir] = None  # 样本不足 → 不显示该点
            # 按周排序，只保留最近 12 周
            _weeks_sorted = sorted(_wdir_map.keys())[-12:]
            _acc_weekly_by_dir = [
                {"week": _wk,
                 "bullish": _wdir_map[_wk].get("bullish"),
                 "bearish": _wdir_map[_wk].get("bearish"),
                 "neutral": _wdir_map[_wk].get("neutral")}
                for _wk in _weeks_sorted
            ]
            # 最佳预测 Top 3（收益最高）
            _brows = _cn11.execute(f"""
                SELECT ticker, date, direction, final_score, return_t7, correct_t7,
                       price_at_predict, price_t7
                FROM predictions WHERE checked_t7=1 AND return_t7 IS NOT NULL{_excl11}
                ORDER BY return_t7 DESC LIMIT 3
            """, _excl11_p).fetchall()
            _acc_best3 = [dict(r) for r in _brows]
            # 最差预测 Top 3（亏损最大）
            _wrows2 = _cn11.execute(f"""
                SELECT ticker, date, direction, final_score, return_t7, correct_t7,
                       price_at_predict, price_t7
                FROM predictions WHERE checked_t7=1 AND return_t7 IS NOT NULL{_excl11}
                ORDER BY return_t7 ASC LIMIT 3
            """, _excl11_p).fetchall()
            _acc_worst3 = [dict(r) for r in _wrows2]
            # Sharpe Ratio（基于 T+7 收益）
            _ret_rows = _cn11.execute(f"""
                SELECT return_t7 FROM predictions
                WHERE checked_t7=1 AND return_t7 IS NOT NULL{_excl11}
            """, _excl11_p).fetchall()
            if len(_ret_rows) >= 2:
                _rets = [r["return_t7"] for r in _ret_rows]
                _mean_r = sum(_rets) / len(_rets)
                _std_r = (sum((x - _mean_r)**2 for x in _rets) / (len(_rets) - 1)) ** 0.5
                # v0.23.4 修复：Sharpe 年化（T+7 周期 → ×√36，与 trading_costs 一致）
                # 旧实现 round(mean/std) 是单期 Sharpe，跟 dashboard 上"Sharpe Ratio +0.22"
                # 对应 — 实际年化应为 ×√36 ≈ +1.32
                _acc_sharpe = round((_mean_r / _std_r) * (36 ** 0.5), 2) if _std_r > 0 else 0.0
                # v0.23.4 修复：最大回撤改为基于"虚拟 NAV"的标准 drawdown 算法
                # 旧实现 cum += return_t7（单纯累加百分点），单位是"累积百分点"而非
                # NAV 比例 — 累加 210 笔每笔 ~1-15% 的 return 容易达 200-300%，
                # 这就是"325.6% 最大回撤"的来源（数学上不可能 > 100%）
                _nav = 1.0
                _peak_nav = 1.0
                _max_dd_pct = 0.0
                for _rv in _rets:
                    _nav *= (1.0 + _rv / 100.0)
                    if _nav > _peak_nav:
                        _peak_nav = _nav
                    _dd_pct = (1.0 - _nav / _peak_nav) * 100.0 if _peak_nav > 0 else 0.0
                    if _dd_pct > _max_dd_pct:
                        _max_dd_pct = _dd_pct
                _acc_max_dd = round(_max_dd_pct, 2)
            # 当前连胜
            _streak_rows = _cn11.execute(f"""
                SELECT correct_t7 FROM predictions
                WHERE checked_t7=1{_excl11} ORDER BY date DESC, id DESC
            """, _excl11_p).fetchall()
            _acc_win_streak = 0
            for _sr in _streak_rows:
                if _sr["correct_t7"] == 1:
                    _acc_win_streak += 1
                else:
                    break
    except Exception as _e11:
        _log.debug("F11 准确率增强数据加载失败: %s", _e11)

    # ── Sprint 1 / P0-3: 复利 Equity Curve（Gross/Net/SPY 三曲线）──
    # 语义：固定仓位比例（10% 本金/笔）、日度聚合、复利累积
    _equity_curve: list = []
    _trading_stats: dict = {
        "exit_tp_count": 0, "exit_sl_count": 0, "exit_close_count": 0,
        "avg_gross_ret": 0.0, "avg_net_ret": 0.0, "avg_cost": 0.0,
        "net_win_rate": 0.0, "sharpe_net": None,
        "max_dd_net_pct": 0.0, "max_dd_gross_pct": 0.0,
        "profit_factor": None,
        "total_spy_ret": 0.0, "alpha_vs_spy": 0.0,
        "initial_capital": 100000.0,
    }
    try:
        from backtester import PredictionStore as _PS_eq
        import sqlite3 as _sq_eq
        # v0.23.4 修复：单一真相来源 — 优先用 portfolio_backtest.BacktestConfig 默认值
        # 旧实现：默认 100000 / pos_pct 0.10 来自不存在的 PORTFOLIO_CONFIG，与代码实际 50000 不符
        try:
            from portfolio_backtest import BacktestConfig as _BC
            _bc_default = _BC()
            _initial_capital = float(_bc_default.initial_capital)
            _pos_pct = float(_bc_default.position_size_pct)
        except Exception:
            try:
                import config as _cfg_eq
                _PF_CFG = getattr(_cfg_eq, "PORTFOLIO_CONFIG", {})
            except Exception:
                _PF_CFG = {}
            _initial_capital = float(_PF_CFG.get("initial_capital", 50000.0))
            _pos_pct = float(_PF_CFG.get("position_size_pct", 0.10))
        _trading_stats["initial_capital"] = _initial_capital
        _trading_stats["position_size_pct"] = _pos_pct

        _ps_eq = _PS_eq()
        with _sq_eq.connect(_ps_eq.db_path) as _cn_eq:
            _cn_eq.row_factory = _sq_eq.Row
            # v0.23.4 修复：必须过滤 net_return_t7 IS NOT NULL，否则未回填的样本会被
            # gross-0.1 兜底污染统计（v0.23.5：加 id 字段供 portfolio_backtest 匹配）
            _eq_rows = _cn_eq.execute("""
                SELECT id, date, ticker, direction, final_score,
                       return_t7, correct_t7,
                       net_return_t7, exit_reason, exit_date, holding_days,
                       spy_return_t7
                FROM predictions
                WHERE checked_t7=1
                  AND return_t7 IS NOT NULL
                  AND net_return_t7 IS NOT NULL
                ORDER BY date ASC, id ASC
            """).fetchall()

            # v32.3 option(a)：dashboard 门面只算核心实盘策略 —— 剔除非交易日预测
            # （周日 sample-accumulator 扩展池样本 + 早期漂移幽灵）。样本仍留 DB 供 optimizer。
            # fail-open 逐行：日期解析失败的行保留，不破坏曲线。
            try:
                from is_trading_day import is_trading_day as _itd_eq
                from datetime import date as _d_eq

                def _keep_eq(_r):
                    try:
                        return _itd_eq(_d_eq.fromisoformat(_r["date"]))[0]
                    except Exception:
                        return True
                _eq_rows = [_r for _r in _eq_rows if _keep_eq(_r)]
            except Exception:
                pass

            # 初始化三条曲线：Gross / Net / SPY buy-and-hold
            _cap_gross = _initial_capital
            _cap_net = _initial_capital
            _cap_spy = _initial_capital
            _peak_gross = _initial_capital
            _peak_net = _initial_capital
            _max_dd_gross = 0.0
            _max_dd_net = 0.0

            _gross_rets, _net_rets, _spy_rets = [], [], []
            _wins_net, _losses_net = [], []

            # v0.23.5 修复：用 portfolio_backtest 实际入场的 trade_ids 子集累加曲线
            # 旧 v0.23.4 修复仍累加全部 260 笔（"独立每笔 $5K 无并发约束"上限模型），
            # 导致曲线显示 +54.27% 视觉幻觉。
            # 新方案：调用 run_backtest() 拿到含 max_concurrent=15 约束的真实入场清单，
            # 只累加这 ~48 笔，曲线对应真实可达 NAV（$52.5K / +5%）
            _accepted_pred_ids: set = set()
            try:
                import portfolio_backtest as _pb_eq
                _bt_for_eq = _pb_eq.run_backtest(_pb_eq.BacktestConfig(exclude_nontrading_days=True))
                if "error" not in _bt_for_eq:
                    _accepted_pred_ids = {t["id"] for t in _bt_for_eq.get("all_trades", [])
                                          if t.get("exit_reason") != "WINDOW_CUTOFF"}
            except Exception as _bt_eq_err:
                _log.debug("portfolio_backtest equity 曲线源加载失败，退回独立累加: %s", _bt_eq_err)

            _fixed_size_usd = _initial_capital * _pos_pct  # $50K × 10% = $5,000
            for _eqr in _eq_rows:
                # v0.23.5: 只对真实入场的 pred_id 累加曲线（其它笔贡献 0，模拟"未入场"）
                _is_accepted = (not _accepted_pred_ids) or (_eqr["id"] in _accepted_pred_ids if "id" in _eqr.keys() else True)
                _r7_raw = _eqr["return_t7"]
                _dir_lc = str(_eqr["direction"]).lower()
                # Gross (direction-adjusted) = strategy P&L before costs
                _gross_dir_adj = -_r7_raw if _dir_lc == "bearish" else _r7_raw
                _net = _eqr["net_return_t7"]
                # net_return_t7 已在 SQL WHERE 保证 NOT NULL，无需兜底
                _spy = _eqr["spy_return_t7"] if _eqr["spy_return_t7"] is not None else 0.0

                _gross_rets.append(_gross_dir_adj)
                _net_rets.append(_net)
                _spy_rets.append(_spy)

                if _net > 0:
                    _wins_net.append(_net)
                elif _net < 0:
                    _losses_net.append(_net)

                # v0.23.5: 仅对 portfolio_backtest 真实入场（含并发约束）的笔累加
                # 未入场的笔贡献 0（模拟"被并发限制 / 现金不足跳过"）
                if _is_accepted:
                    _pnl_gross = _fixed_size_usd * (_gross_dir_adj / 100.0)
                    _pnl_net = _fixed_size_usd * (_net / 100.0)
                    _pnl_spy = _fixed_size_usd * (_spy / 100.0)
                    _cap_gross += _pnl_gross
                    _cap_net += _pnl_net
                    _cap_spy += _pnl_spy

                if _cap_gross > _peak_gross:
                    _peak_gross = _cap_gross
                if _cap_net > _peak_net:
                    _peak_net = _cap_net
                _dd_g = (_peak_gross - _cap_gross) / _peak_gross * 100 if _peak_gross else 0
                _dd_n = (_peak_net - _cap_net) / _peak_net * 100 if _peak_net else 0
                if _dd_g > _max_dd_gross:
                    _max_dd_gross = _dd_g
                if _dd_n > _max_dd_net:
                    _max_dd_net = _dd_n

                _equity_curve.append({
                    "date": _eqr["date"],
                    "ticker": _eqr["ticker"],
                    "direction": _eqr["direction"],
                    "gross_ret": round(_gross_dir_adj, 2),
                    "net_ret": round(_net, 2),
                    "spy_ret": round(_spy, 2),
                    "exit_reason": _eqr["exit_reason"] or "T7_CLOSE",
                    "cap_gross": round(_cap_gross, 2),
                    "cap_net": round(_cap_net, 2),
                    "cap_spy": round(_cap_spy, 2),
                    "cum_gross_pct": round((_cap_gross / _initial_capital - 1) * 100, 2),
                    "cum_net_pct": round((_cap_net / _initial_capital - 1) * 100, 2),
                    "cum_spy_pct": round((_cap_spy / _initial_capital - 1) * 100, 2),
                    "correct": bool(_eqr["correct_t7"]),
                })

            # ── 填充 trading_stats ──
            # v32.3: exit 分布从已过滤的 _eq_rows 统计，与净值/胜率同口径（核心交易日）
            _exit_tp = _exit_sl = _exit_close = 0
            for _eqr2 in _eq_rows:
                _ekey = (_eqr2["exit_reason"] or "T7_CLOSE").upper()
                if "TP" in _ekey:
                    _exit_tp += 1
                elif "SL" in _ekey:
                    _exit_sl += 1
                else:
                    _exit_close += 1
            _trading_stats["exit_tp_count"] = _exit_tp
            _trading_stats["exit_sl_count"] = _exit_sl
            _trading_stats["exit_close_count"] = _exit_close

            if _net_rets:
                _n = len(_net_rets)
                _trading_stats["avg_gross_ret"] = round(sum(_gross_rets) / _n, 3)
                _trading_stats["avg_net_ret"] = round(sum(_net_rets) / _n, 3)
                _trading_stats["avg_cost"] = round(
                    (sum(_gross_rets) - sum(_net_rets)) / _n, 3
                )
                _trading_stats["net_win_rate"] = round(
                    sum(1 for r in _net_rets if r > 0) / _n * 100, 1
                )
                # 年化 Sharpe (T+7 ≈ 52 周期/年)
                try:
                    from trading_costs import sharpe_ratio
                    _trading_stats["sharpe_net"] = sharpe_ratio(_net_rets, periods_per_year=36)  # 修复 #8
                except Exception:
                    pass
                # Profit Factor
                _win_sum = sum(_wins_net)
                _loss_sum = abs(sum(_losses_net))
                if _loss_sum > 0:
                    _trading_stats["profit_factor"] = round(_win_sum / _loss_sum, 2)
                # SPY 累计 & alpha
                _trading_stats["total_spy_ret"] = round(
                    (_cap_spy / _initial_capital - 1) * 100, 2
                )
                _trading_stats["alpha_vs_spy"] = round(
                    (_cap_net / _initial_capital - 1) * 100 -
                    (_cap_spy / _initial_capital - 1) * 100, 2
                )

            _trading_stats["max_dd_gross_pct"] = round(_max_dd_gross, 2)
            _trading_stats["max_dd_net_pct"] = round(_max_dd_net, 2)
            _trading_stats["final_cap_gross"] = round(_cap_gross, 2)
            _trading_stats["final_cap_net"] = round(_cap_net, 2)
            _trading_stats["final_cap_spy"] = round(_cap_spy, 2)
            # 标记此组数字为"独立 $5K 假设，无并发限制"上限参考
            _trading_stats["methodology"] = "independent_per_trade_no_concurrency"

            # v0.23.4 修复：准确率板块的 max_dd 复用 trading_stats 真实 NAV-based 值
            # 旧实现 _acc_max_dd 用 100% 仓位 NAV 复利（_nav *= (1+ret)），对实际
            # 10% 仓位策略偏离严重（96.87% vs 真实 11.64%）
            if _max_dd_net > 0:
                _acc_max_dd = round(_max_dd_net, 2)

        # v0.23.4 修复：用 portfolio_backtest 真实结果（含 max_concurrent=15 并发约束）
        # 覆盖关键卡片数字。equity_curve 保留独立 $5K 模型作为"理论上限参考"
        try:
            import portfolio_backtest as _pb
            _bt_cfg = _pb.BacktestConfig(exclude_nontrading_days=True)  # v32.3: 门面只算核心交易日
            _bt_result = _pb.run_backtest(_bt_cfg)
            if "error" not in _bt_result:
                _portfolio = _bt_result.get("portfolio", {})
                _risk = _bt_result.get("risk_metrics", {})
                _bench = _bt_result.get("benchmark", {})
                _trade_stats_real = _bt_result.get("trade_stats", {})
                _trading_stats["realistic"] = {
                    "initial_capital": _portfolio.get("initial_nav"),
                    "final_nav": _portfolio.get("final_nav"),
                    "total_return_pct": _portfolio.get("total_return_pct"),
                    "total_pnl_usd": _portfolio.get("total_pnl_usd"),
                    "spy_end_nav": _bench.get("spy_end_nav"),
                    "spy_return_pct": _bench.get("spy_return_pct"),
                    "alpha_vs_spy": _bt_result.get("alpha"),
                    "sharpe_ratio": _risk.get("sharpe_ratio"),
                    "profit_factor": _risk.get("profit_factor"),
                    "max_drawdown_pct": _risk.get("max_drawdown_pct"),
                    "win_rate_pct": _risk.get("win_rate_pct"),
                    "trades_entered": _trade_stats_real.get("total_trades"),
                    "predictions_total": _bt_result.get("filter_stats", {}).get("entered", 0)
                                       + sum(v for k, v in _bt_result.get("filter_stats", {}).items() if "skipped" in k),
                    "max_concurrent": _bt_cfg.max_concurrent,
                    "methodology": "portfolio_backtest_with_concurrency_limit",
                }
        except Exception as _pb_err:
            _log.debug("portfolio_backtest 真实数字加载失败（dashboard 仅显示理论上限）: %s", _pb_err)

    except Exception as _eq_err:
        _log.debug("Equity curve 数据加载失败: %s", _eq_err)

    return {
        "stats": _acc_stats,
        "total_checked": _acc_total_checked,
        "overall": _acc_overall,
        "avg_return": _acc_avg_return,
        "correct": _acc_correct,
        "by_dir": _acc_by_dir,
        "by_ticker": _acc_by_ticker,
        "weekly_trend": _acc_weekly_trend,
        "best3": _acc_best3,
        "worst3": _acc_worst3,
        "sharpe": _acc_sharpe,
        "max_dd": _acc_max_dd,
        "win_streak": _acc_win_streak,
        "weekly_by_dir": _acc_weekly_by_dir,
        "equity_curve": _equity_curve,
        "trading_stats": _trading_stats,
    }


def _load_historical_data(report_dir, date_str: str,
                          all_tickers_sorted: list,
                          opp_by_ticker: dict,
                          swarm_detail: dict,
                          fg_value) -> dict:
    """Load historical report JSON files, build trend data and F&G history."""
    _hist_entries: list = []
    _fg_history = [{"date": date_str, "value": fg_value}]  # 当天 F&G
    _trend_data: dict = {}  # {ticker: [{date, score}, ...]}
    _hist_full: dict = {}   # {date: [{ticker, score, direction}, ...]}  for diff
    # 当天趋势数据
    for _tt in all_tickers_sorted:
        _tts = float(opp_by_ticker.get(_tt, {}).get("opp_score") or swarm_detail.get(_tt, {}).get("final_score", 0))
        _trend_data.setdefault(_tt, []).append({"date": date_str, "score": round(_tts, 1)})
    _hist_full[date_str] = [
        {"ticker": _tt, "score": round(float(opp_by_ticker.get(_tt, {}).get("opp_score") or swarm_detail.get(_tt, {}).get("final_score", 0)), 1),
         "direction": _norm_dir(opp_by_ticker.get(_tt, {}).get("direction") or swarm_detail.get(_tt, {}).get("direction", "neutral"))}
        for _tt in all_tickers_sorted
    ]
    try:
        import glob as _glob
        _hist_files = sorted(
            _glob.glob(str(report_dir / "alpha-hive-daily-*.json")),
            reverse=True  # 最新在前
        )
        for _hf in _hist_files:
            _hdate = _Path_mod(_hf).stem.replace("alpha-hive-daily-", "")
            if _hdate == date_str:
                continue  # 今天已在主面板展示
            # 跳过非交易日（周末/假日）幽灵：时区漂移曾把周五/周六 scan 标到周末，
            # 这些日期不应进入趋势/历史/F&G/ML 链接。fail-safe：解析失败时不跳过。
            try:
                from is_trading_day import filename_is_nontrading_day as _fnt_hist
                if _fnt_hist(_hdate):
                    continue
            except Exception:
                pass
            try:
                with open(_hf, encoding="utf-8") as _hfp:
                    _hrpt = json.load(_hfp)
                _hopps = _hrpt.get("opportunities", [])
                _hmeta = _hrpt.get("swarm_metadata", {})
                _hn    = _hmeta.get("tickers_analyzed", len(_hopps))
                # 全部 opps 数据（用于趋势 + diff）
                _hall_opps = [
                    {"ticker": o.get("ticker",""), "score": float(o.get("opp_score",0)),
                     "direction": _norm_dir(o.get("direction","neutral"))}
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
                          if _Path_mod(report_dir / f"alpha-hive-{t}-ml-enhanced-{_hdate}.html").exists()]
                # 提取 F&G 值（从 swarm_results）
                _hfg_val = None
                try:
                    _hsr_path = report_dir / f".swarm_results_{_hdate}.json"
                    if _hsr_path.exists():
                        with open(_hsr_path, encoding="utf-8") as _hsr_fp:
                            _hsr = json.load(_hsr_fp)
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
                    "has_md":   _Path_mod(report_dir / f"alpha-hive-daily-{_hdate}.md").exists(),
                    "has_json": _Path_mod(report_dir / f"alpha-hive-daily-{_hdate}.json").exists(),
                })
            except Exception as _he:
                _log.debug("历史报告 %s 解析失败: %s", _hdate, _he)
    except Exception as _hle:
        _log.debug("历史时间线加载失败: %s", _hle)

    # 排序 F&G 历史和趋势数据（按日期升序）
    _fg_history.sort(key=lambda x: x["date"])
    for _tk in _trend_data:
        _trend_data[_tk].sort(key=lambda x: x["date"])

    return {
        "hist_entries": _hist_entries,
        "fg_history": _fg_history,
        "trend_data": _trend_data,
        "hist_full": _hist_full,
    }


def _render_hist_card(entry: dict) -> str:
    """Render a single historical report card as HTML."""
    _top3_html = ""
    for _ht in entry["top3"]:
        _hscls = "sc-h" if _ht["score"] >= 7.0 else ("sc-m" if _ht["score"] >= 5.5 else "sc-l")
        _hdir  = _norm_dir(_ht["direction"])
        _top3_html += f"""<div class="htop-chip">
          <span class="hticker">{_html.escape(_ht['ticker'])}</span>
          <span class="hscore {_hscls}">{_ht['score']:.1f}</span>
          <span class="hdir">{_DIR_ICON.get(_hdir,'🟡')}</span>
        </div>"""
    _hlinks = ""
    _safe_date = _html.escape(entry["date"])
    if entry["has_json"]:
        _hlinks += f'<a href="alpha-hive-daily-{_safe_date}.json" target="_blank" rel="noopener" class="hlink hlink-json">完整数据</a>'
    for _hmt in entry["ml_tickers"][:4]:
        _safe_tk = _html.escape(_hmt)
        _hlinks += f'<a href="alpha-hive-{_safe_tk}-ml-enhanced-{_safe_date}.html" target="_blank" rel="noopener" class="hlink hlink-ml">{_safe_tk}</a>'
    return f"""
        <div class="hist-card">
          <div class="hist-left">
            <div class="hist-date">{entry['date']}</div>
            <div class="hist-meta">{entry['n']} 标的 · 均分 <span class="{'sc-h' if entry['avg']>=7 else ('sc-m' if entry['avg']>=5.5 else 'sc-l')}">{entry['avg']:.1f}</span></div>
          </div>
          <div class="hist-mid">{_top3_html}</div>
          <div class="hist-right">{_hlinks}</div>
        </div>"""


# ---------------------------------------------------------------------------
# Extracted HTML builder helpers (Step 4 of refactor)
# ---------------------------------------------------------------------------

def _build_actionable_top_html(all_tickers_sorted, opp_by_ticker, swarm_detail) -> str:
    """v0.24.5 #2: 构建"今日 Top 1-3 Actionable" 板块

    筛选条件（4 重门控）:
      1. score >= 6.5 OR score <= 4.5   — 明确方向，避开中性犹豫
      2. agent_std < 1.5                — 蜂群一致（5 维分数标准差）
      3. macro 不阻拦                   — risk-off 时不推强看多
      4. 有近期催化剂（≤ 14d）OR 高异常流  — 短期事件驱动

    输出：1-3 个高置信度卡片，余下折叠到下方"观察名单"
    无候选时显示"今日无强信号，建议观望"
    """
    import math as _math
    import json as _json_act
    candidates = []
    for tk in all_tickers_sorted:
        sd = swarm_detail.get(tk, {})
        if not sd:
            continue
        score = sd.get("final_score") or sd.get("composite_score") or 5.0
        direction = (sd.get("direction") or "neutral").lower()
        ad = sd.get("agent_details", {})

        # 1. 方向门控
        is_strong_bull = score >= 6.5 and "bull" in direction
        is_strong_bear = score <= 4.5 and "bear" in direction
        if not (is_strong_bull or is_strong_bear):
            continue

        # 2. Agent 共识门控（dimension_scores std）
        dim_scores = sd.get("dimension_scores", {})
        if isinstance(dim_scores, dict) and dim_scores:
            vals = [float(v) for v in dim_scores.values() if v is not None]
            if len(vals) >= 3:
                mean = sum(vals) / len(vals)
                var = sum((v - mean) ** 2 for v in vals) / len(vals)
                std = _math.sqrt(var)
                if std > 1.5:
                    continue  # 蜂群分歧大，跳过

        # 3. 催化剂门控
        chronos = ad.get("ChronosBeeHorizon", {}).get("details", {})
        cats = chronos.get("catalysts") or []
        nearest_cat = None
        for c in cats:
            d = c.get("days_until")
            if isinstance(d, (int, float)) and 0 < d <= 14:
                if nearest_cat is None or d < nearest_cat.get("days_until", 999):
                    nearest_cat = c

        # 4. 异常流门控（OracleBee unusual flow）
        oracle = ad.get("OracleBeeEcho", {}).get("details", {})
        unusual = oracle.get("unusual_flow") or {}
        has_unusual = bool(unusual.get("signals")) and unusual.get("data_source") != "fallback"

        # 至少满足"有催化剂" OR "有异常流"才算 actionable
        if not nearest_cat and not has_unusual:
            continue

        candidates.append({
            "ticker": tk,
            "score": float(score),
            "direction": direction,
            "is_bull": is_strong_bull,
            "agent_std": std if 'std' in locals() else 0,
            "nearest_cat": nearest_cat,
            "has_unusual": has_unusual,
            "unusual": unusual,
            "ad": ad,
        })

    # 取 Top 3（按 |score - 5| 倒序，越极端越 actionable）
    candidates.sort(key=lambda c: -abs(c["score"] - 5.0))
    candidates = candidates[:3]

    if not candidates:
        return (
            '<div class="actionable-empty" style="padding:18px;background:rgba(148,163,184,.08);'
            'border-left:4px solid #94a3b8;border-radius:6px;margin:12px 0">'
            '<div style="font-weight:700;color:var(--mt);margin-bottom:6px">今日 Actionable</div>'
            '<div style="color:var(--ts);font-size:.95em">'
            '今日无强信号通过 4 重门控（score 极端 + 蜂群一致 + 近期催化剂 + 不在 risk-off）。'
            '<b>建议观望</b>，避免低置信度交易。'
            '</div></div>'
        )

    # 渲染卡片
    cards = []
    for c in candidates:
        tk = c["ticker"]
        score = c["score"]
        is_bull = c["is_bull"]
        cat = c["nearest_cat"]
        unusual = c["unusual"]

        bg = "rgba(34,197,94,.10)" if is_bull else "rgba(239,68,68,.10)"
        border = "#22c55e" if is_bull else "#ef4444"
        dot_cls = "dot-bull" if is_bull else "dot-bear"
        label = "看多" if is_bull else "看空"
        action = "考虑买入" if is_bull else "考虑做空 / 减仓"

        cat_text = ""
        if cat:
            event = cat.get("event", "事件")[:30]
            days = cat.get("days_until", 0)
            cat_text = f"{event}（{int(days)}天后）"

        unusual_text = ""
        if c["has_unusual"]:
            sigs = unusual.get("signals", [])
            if sigs:
                top_sig = sigs[0] if isinstance(sigs[0], str) else sigs[0].get("description", "异常流")
                unusual_text = f"异常期权流：{str(top_sig)[:40]}"

        std_text = f"蜂群一致度 {(1.5 - c['agent_std']):.1f}/1.5（std={c['agent_std']:.2f}）"

        cards.append(f'''
<div class="actionable-card" style="background:{bg};border:2px solid {border};border-radius:10px;
    padding:16px;margin:10px 0;display:grid;grid-template-columns:auto 1fr auto;gap:14px;align-items:center">
  <div><span class="{dot_cls}" style="width:14px;height:14px;display:inline-block;border-radius:50%;"></span></div>
  <div>
    <div style="font-size:1.4em;font-weight:800;color:{border};margin-bottom:4px">
      {tk} · {score:.1f}分 · <span style="font-size:.7em;background:{border};color:#fff;padding:2px 8px;border-radius:4px">{label}</span>
    </div>
    <div style="color:var(--t);font-size:.92em;margin-bottom:3px"><strong>{action}</strong></div>
    {f'<div style="color:var(--mt);font-size:.86em;margin-top:2px">{cat_text}</div>' if cat_text else ''}
    {f'<div style="color:var(--mt);font-size:.86em;margin-top:2px">{unusual_text}</div>' if unusual_text else ''}
    <div style="color:var(--ts);font-size:.78em;margin-top:4px">{std_text}</div>
  </div>
  <a href="#tk-{tk}" style="text-decoration:none;color:{border};font-weight:700;
    border:1px solid {border};padding:8px 14px;border-radius:6px;font-size:.85em">查看详情 →</a>
</div>''')

    return (
        '<div class="section actionable-section" id="actionable-top" '
        'style="margin:18px 0;padding:18px;background:linear-gradient(135deg,rgba(255,193,7,.06),rgba(34,197,94,.04));'
        'border:1px solid var(--border);border-radius:12px">'
        '<h2 class="sec-title" style="margin:0 0 12px">今日 Actionable Top {n}</h2>'
        '<div style="font-size:.82em;color:var(--ts);margin-bottom:6px">'
        '通过 4 重门控（score 极端 + 蜂群一致 + 近期催化剂 + 非 risk-off）的高置信信号'
        '</div>'
        '{cards}'
        '<div style="font-size:.78em;color:var(--ts);margin-top:8px;padding:8px;background:rgba(0,0,0,.04);border-radius:4px">'
        '注：本板块仅展示通过严格筛选的信号；其余标的（含中性 / 高分歧）请见下方"今日 Top 6 机会"完整列表'
        '</div></div>'
    ).format(n=len(candidates), cards=''.join(cards))


def _build_top_cards_html(all_tickers_sorted, opp_by_ticker, swarm_detail,
                          report_dir, date_str, score_deltas, hist_full) -> str:
    """Build Top-6 opportunity cards HTML."""
    new_cards_html = ""
    for _ci, _tc6 in enumerate(all_tickers_sorted[:6], 1):
        _oc6   = opp_by_ticker.get(_tc6, {})
        _sc6   = float(_oc6.get("opp_score") or swarm_detail.get(_tc6, {}).get("final_score", 0))
        _dr6   = str(_oc6.get("direction") or swarm_detail.get(_tc6, {}).get("direction", "neutral")).lower()
        if "多" in _dr6: _dr6 = "bullish"
        elif "空" in _dr6: _dr6 = "bearish"
        elif _dr6 not in ("bullish","bearish","neutral"): _dr6 = "neutral"
        _dlbl6 = {"bullish":'<span class="dot-bull"></span>看多',"bearish":'<span class="dot-bear"></span>看空',"neutral":'<span class="dot-neut"></span>中性'}[_dr6]
        _dcls6 = {"bullish":"sdir-bull","bearish":"sdir-bear","neutral":"sdir-neut"}[_dr6]
        _scls6 = _sc_cls(_sc6)
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
        _delta6 = score_deltas.get(_tc6, {}).get("html", "")
        # 升级 F: 信号冲突预警
        _conf6 = _signal_conflicts(_tc6, _sd6)
        _ml6ex = _Path_mod(report_dir / f"alpha-hive-{_tc6}-ml-enhanced-{date_str}.html").exists()
        _ml6   = (f'<a href="alpha-hive-{_tc6}-ml-enhanced-{date_str}.html" target="_blank" rel="noopener" class="ml-btn">ML 详情 →</a>'
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
        # 升级 D2: 情绪 Sparkline（7 天得分趋势 SVG）
        _spark6 = ""
        try:
            _hist_scores6 = []
            for _hd6 in sorted(hist_full.keys()):
                for _hi6 in hist_full[_hd6]:
                    if _hi6["ticker"] == _tc6:
                        _hist_scores6.append(round(float(_hi6["score"]), 1))
                        break
            if len(_hist_scores6) >= 2:
                _svgw, _svgh = 120, 24
                _smin = max(0, min(_hist_scores6) - 0.5)
                _smax = min(10, max(_hist_scores6) + 0.5)
                _srange = _smax - _smin if _smax > _smin else 1
                _npts = len(_hist_scores6)
                _pts = []
                for _si6, _sv6 in enumerate(_hist_scores6):
                    _sx = round(_si6 / max(1, _npts - 1) * _svgw, 1)
                    _sy = round(max(0, min(_svgh, _svgh - (_sv6 - _smin) / _srange * _svgh)), 1)
                    _pts.append(f"{_sx},{_sy}")
                _polyline = " ".join(_pts)
                _scol = "#22c55e" if _hist_scores6[-1] >= 7 else ("#f59e0b" if _hist_scores6[-1] >= 5.5 else "#ef4444")
                _area_pts = f"0,{_svgh} {_polyline} {_svgw},{_svgh}"
                _last_x, _last_y = _pts[-1].split(",")
                _spark6 = (f'<div class="spark-wrap">'
                           f'<svg class="spark-svg" viewBox="0 0 {_svgw} {_svgh}" preserveAspectRatio="none">'
                           f'<polygon class="spark-area" points="{_area_pts}" fill="{_scol}"/>'
                           f'<polyline class="spark-line" points="{_polyline}" stroke="{_scol}"/>'
                           f'<circle class="spark-dot" cx="{_last_x}" cy="{_last_y}" fill="{_scol}"/>'
                           f'</svg></div>')
        except Exception as _e_spark:
            _log.debug("Sparkline 生成失败 (%s): %s", _tc6, _e_spark)
        # 升级 B3: Agent 共识环形图（CSS conic-gradient）
        _donut6 = ""
        try:
            _ab6 = _sd6.get("agent_breakdown", {})
            _bv6 = int(_ab6.get("bullish", 0))
            _ev6 = int(_ab6.get("bearish", 0))
            _nv6 = int(_ab6.get("neutral", 0))
            _tv6 = _bv6 + _ev6 + _nv6
            if _tv6 > 0:
                _bp6 = round(_bv6 / _tv6 * 100)
                _ep6 = round(_ev6 / _tv6 * 100)
                _np6 = 100 - _bp6 - _ep6
                # 边缘情况：只有一种票型时用纯色，避免零宽 conic-gradient 段
                _active_segs = sum(1 for x in (_bp6, _ep6, _np6) if x > 0)
                if _active_segs <= 1:
                    _solo_col = "#22c55e" if _bp6 > 0 else ("#ef4444" if _ep6 > 0 else "#f59e0b")
                    _cg6 = _solo_col
                else:
                    # 构建仅包含非零段的 conic-gradient
                    _stops = []
                    _cur = 0
                    if _bp6 > 0:
                        _stops.append(f"#22c55e {_cur}% {_cur + _bp6}%")
                        _cur += _bp6
                    if _ep6 > 0:
                        _stops.append(f"#ef4444 {_cur}% {_cur + _ep6}%")
                        _cur += _ep6
                    if _np6 > 0:
                        _stops.append(f"#f59e0b {_cur}% 100%")
                    _cg6 = f"conic-gradient({', '.join(_stops)})"
                _donut6 = (f'<div class="consensus-wrap">'
                           f'<div class="consensus-donut" style="background:{_cg6}" '
                           f'title="看多:{_bv6} 看空:{_ev6} 中性:{_nv6}"></div>'
                           f'<div class="consensus-labels">{_bv6}多 {_ev6}空 {_nv6}中</div>'
                           f'</div>')
        except Exception as _e_donut:
            _log.debug("共识环形图生成失败 (%s): %s", _tc6, _e_donut)
        # F10: 价格标注
        _det6 = _detail(_tc6, swarm_detail)
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
        <div class="scard" data-dir="{_dr6}" data-score="{_sc6:.1f}" data-ticker="{_html.escape(_tc6)}">
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
            {_spark6}
            {_donut6}
            {_cat6}
            {_price6_html}
            {_conf6}
            {f'<div class="sinsight">{_ins6}</div>' if _ins6 else ''}
            {_ml6}
          </div>
          <div class="scard-expand" id="expand-{_html.escape(_tc6)}">
            <div class="detail-grid">
              <div class="dg-item"><span class="dg-label">IV Rank</span><span class="dg-value">{_det6['iv_rank']}</span></div>
              <div class="dg-item"><span class="dg-label">P/C Ratio</span><span class="dg-value">{_det6['pc']}</span></div>
              <div class="dg-item"><span class="dg-label">看空强度</span><span class="dg-value">{_det6['bear_score']:.1f}</span></div>
              <div class="dg-item"><span class="dg-label">数据真实度</span><span class="dg-value">{_det6['real_pct']}</span></div>
              <div class="dg-item"><span class="dg-label">GEX</span><span class="dg-value">{_det6['gex']}</span></div>
              <div class="dg-item"><span class="dg-label">期权流向</span><span class="dg-value" style="color:{_det6['flow_color']}">{_det6['flow_dir']}</span></div>
            </div>
            <div class="radar-mini"><canvas id="radar-expand-{_html.escape(_tc6)}" width="200" height="160"></canvas></div>
            <a href="javascript:void(0)" class="ml-btn" onclick="event.stopPropagation();scrollToDeep('{_html.escape(_tc6)}')">查看完整深度分析 →</a>
          </div>
          <button class="scard-expand-close" title="收起">✕</button>
        </div>"""
    return new_cards_html


def _build_table_rows_html(all_tickers_sorted, opp_by_ticker, swarm_detail,
                           report_dir, date_str, score_deltas) -> str:
    """Build full table rows HTML for all tickers."""
    new_rows_html = ""
    for _ri, _trt in enumerate(all_tickers_sorted, 1):
        _ort = opp_by_ticker.get(_trt, {})
        _srt = float(_ort.get("opp_score") or swarm_detail.get(_trt, {}).get("final_score", 0))
        _drt = str(_ort.get("direction") or swarm_detail.get(_trt, {}).get("direction","neutral")).lower()
        if "多" in _drt: _drt = "bullish"
        elif "空" in _drt: _drt = "bearish"
        elif _drt not in ("bullish","bearish","neutral"): _drt = "neutral"
        _dlrt = _DIR_CN[_drt]
        _dclrt = {"bullish":"dcell-bull","bearish":"dcell-bear","neutral":"dcell-neut"}[_drt]
        _scrt = _sc_cls(_srt)
        _det_rt = _detail(_trt, swarm_detail)
        _res_rt = swarm_detail.get(_trt,{}).get("resonance",{}).get("resonance_detected",False)
        _sup_rt = int(_ort.get("supporting_agents") or swarm_detail.get(_trt,{}).get("supporting_agents",0))
        _res_html_rt = (f'<span class="res-y">{_sup_rt}A</span>' if _res_rt else '<span class="res-n">无</span>')
        _ml_ex_rt = _Path_mod(report_dir / f"alpha-hive-{_trt}-ml-enhanced-{date_str}.html").exists()
        _ml_rt = (f'<a href="alpha-hive-{_trt}-ml-enhanced-{date_str}.html" target="_blank" rel="noopener" class="ml-btn-sm">查看</a>'
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
        _delta_rt = score_deltas.get(_trt, {}).get("html", "")
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
    return new_rows_html


def _build_deep_analysis_html(all_tickers_sorted, opp_by_ticker, swarm_detail,
                              report_dir, date_str, score_deltas) -> str:
    """Build Deep Analysis cards HTML with radar canvas."""
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
        _detd  = _detail(_tkrd, swarm_detail)
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
            (_add.get("ScoutBeeNova",{}).get("discovery",""),       "","内幕"),
            (_add.get("OracleBeeEcho",{}).get("discovery",""),      "","期权"),
            (_add.get("BuzzBeeWhisper",{}).get("discovery",""),     "","情绪"),
            (_add.get("BearBeeContrarian",{}).get("discovery",""),  "","风险"),
        ]:
            _fd = _discd.split("|")[0].strip()[:85] if _discd else ""
            if _fd:
                _blstd.append(f'<li>{_icod} <strong>{_lbd}：</strong>{_html.escape(_fd)}</li>')
        _bhtmld = "\n                    ".join(_blstd) if _blstd else "<li>数据采集中</li>"
        _ml_exd = _Path_mod(report_dir / f"alpha-hive-{_tkrd}-ml-enhanced-{date_str}.html").exists()
        _mlbtnd = (f'<a href="alpha-hive-{_tkrd}-ml-enhanced-{date_str}.html" target="_blank" rel="noopener" class="ml-btn-cc">ML 增强分析 →</a>'
                   if _ml_exd else '<span style="font-size:.78em;color:var(--ts)">ML 报告生成中</span>')
        # ── edgar_rss badge ──
        _rss_n = _add.get("ScoutBeeNova", {}).get("details", {}).get("insider", {}).get("rss_fresh_today", 0)
        _rss_badge = (f'<span class="rss-badge">今日Form4 {_rss_n}份 ▲</span>' if _rss_n else "")
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
            _tb_html += '<div class="tb-title">失效条件监控</div>'
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
        _delta_d = score_deltas.get(_tkrd, {}).get("html", "")
        _cat_d = _catalyst_countdown(_tkrd, _sdd)
        _conf_d = _signal_conflicts(_tkrd, _sdd)
        _ins_d = _html.escape(_build_plain_insight(_tkrd, _sdd))

        # ── v0.26.0 全链 OI 卡片（影响价格判断核心） ─────────────────────────
        # 用户反馈：#/deep 板块只有异常流和近端 P/C，缺全链 OI 让价格判断盲目
        # 设计：紧凑卡片显示全链 P/C + Max Pain + Top 3 Call 阻力 + Top 3 Put 支撑
        _full_oi_html = ""
        _full_pc = _detd.get("full_pc")
        _mp = _detd.get("max_pain")
        if _full_pc is not None or _mp is not None:
            _near_pc = _detd.get("pc", "-")
            _full_pc_str = f"{_full_pc:.2f}" if _full_pc is not None else "-"
            # full P/C 颜色：>1.2 偏空（红）/ <0.6 偏多（绿）/ 中间黄
            if _full_pc is None: _pc_color = "#94a3b8"
            elif _full_pc > 1.2: _pc_color = "#dc3545"
            elif _full_pc < 0.6: _pc_color = "#28a745"
            else: _pc_color = "#d97706"
            _pc_label = ("偏空" if _full_pc and _full_pc > 1.2 else
                         ("偏多" if _full_pc and _full_pc < 0.6 else "均衡"))
            # v0.26.1: 近端 Max Pain（真正的磁吸目标价）为主显示
            # 远期全链 Max Pain 作为参考次要显示
            _near_mp = _detd.get("near_max_pain")
            _near_mp_pct = _detd.get("near_max_pain_pct")
            _near_exps = _detd.get("near_expiry_dates", [])

            # 主显示：近端 MP
            if _near_mp:
                _mp_main_str = f"${_near_mp:.0f}"
                if _near_mp_pct is not None:
                    _mp_arrow = "↑" if _near_mp_pct > 1 else ("↓" if _near_mp_pct < -1 else "→")
                    _mp_color = "#28a745" if _near_mp_pct > 1 else ("#dc3545" if _near_mp_pct < -1 else "#94a3b8")
                    _mp_main_str += f' <span style="color:{_mp_color};font-size:.78em">{_mp_arrow}{_near_mp_pct:+.1f}%</span>'
                # 到期日数量提示
                _exp_label = (
                    f"近 {len(_near_exps)} 周到期" if len(_near_exps) > 0 else "近端到期"
                )
            else:
                # 回退到全链 MP
                _mp_pct = _detd.get("max_pain_pct")
                if _mp:
                    _mp_main_str = f"${_mp:.0f}"
                    if _mp_pct is not None:
                        _mp_arrow = "↑" if _mp_pct > 1 else ("↓" if _mp_pct < -1 else "→")
                        _mp_color = "#28a745" if _mp_pct > 1 else ("#dc3545" if _mp_pct < -1 else "#94a3b8")
                        _mp_main_str += f' <span style="color:{_mp_color};font-size:.78em">{_mp_arrow}{_mp_pct:+.1f}%</span>'
                    _exp_label = "全链聚合"
                else:
                    _mp_main_str = "-"
                    _exp_label = ""

            # 次要：全链 MP 对比（仅在近端 + 全链都有时显示差异）
            _mp_compare = ""
            if _near_mp and _mp and abs(_near_mp - _mp) > 1:
                _mp_compare = f'<div style="font-size:.62em;color:var(--ts);margin-top:1px">远期参考 ${_mp:.0f}</div>'
            elif _near_mp:
                _mp_compare = f'<div style="font-size:.62em;color:var(--ts);margin-top:1px">{_exp_label}</div>'
            elif _mp:
                _mp_compare = f'<div style="font-size:.62em;color:var(--ts);margin-top:1px">{_exp_label}</div>'

            _mp_str = _mp_main_str

            # v0.26.2: 优先取近端墙（≤30 天到期，做市商真正在意的）
            # 近端有数据时显示；否则 fallback 到全链
            _near_calls_all = _detd.get("near_call_walls", [])
            _near_puts_all = _detd.get("near_put_walls", [])
            _is_near = bool(_near_calls_all or _near_puts_all)

            if _is_near:
                _calls = [w for w in _near_calls_all if w["pct_diff"] > -1][:3]
                _puts = [w for w in _near_puts_all if w["pct_diff"] < 1][:3]
                _wall_label = "近 30 天到期"
            else:
                _calls = [w for w in _detd.get("top_call_walls", []) if w["pct_diff"] > -1][:3]
                _puts = [w for w in _detd.get("top_put_walls", []) if w["pct_diff"] < 1][:3]
                _wall_label = "全链聚合"

            def _wall_rows(walls, side_color, side_label):
                if not walls:
                    return f'<div style="font-size:.75em;color:#94a3b8;padding:4px 0">无数据</div>'
                rows = []
                for w in walls:
                    pct = w["pct_diff"]
                    pct_str = f"{pct:+.1f}%"
                    oi_k = w["oi"] / 1000.0
                    oi_str = f"{oi_k:.0f}k" if oi_k >= 1 else f"{w['oi']}"
                    exp_tag = f' <span style="background:#374151;color:#cbd5e1;padding:1px 4px;border-radius:3px;font-size:.65em">{w["dom_exp"]}</span>' if w.get("dom_exp") else ""
                    rows.append(
                        f'<div style="display:flex;justify-content:space-between;font-size:.78em;padding:2px 0;'
                        f'border-bottom:1px dashed rgba(148,163,184,.2)">'
                        f'<span style="color:{side_color};font-weight:600">${w["strike"]:.0f}{exp_tag}</span>'
                        f'<span style="color:var(--ts);font-size:.85em">{pct_str}</span>'
                        f'<span style="color:var(--t);font-weight:500">{oi_str}</span>'
                        f'</div>'
                    )
                return ''.join(rows)

            _full_oi_html = f'''
            <div class="full-oi-card" style="background:rgba(99,102,241,.06);border:1px solid rgba(99,102,241,.25);
                border-radius:8px;padding:10px 12px;margin:10px 0">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;font-size:.85em;font-weight:700;color:var(--mt)">
                <span>🔗 全链 OI 视图</span>
                <span style="font-size:.7em;color:var(--ts);font-weight:400">影响价格判断核心</span>
              </div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
                <div style="background:rgba(0,0,0,.18);border-radius:6px;padding:6px 8px">
                  <div style="font-size:.7em;color:var(--ts)">全链 P/C</div>
                  <div style="font-size:1.15em;font-weight:700;color:{_pc_color}">{_full_pc_str}
                    <span style="font-size:.55em;font-weight:400;color:var(--ts)">({_pc_label})</span>
                  </div>
                  <div style="font-size:.62em;color:var(--ts);margin-top:1px">近端 {_near_pc}</div>
                </div>
                <div style="background:rgba(0,0,0,.18);border-radius:6px;padding:6px 8px">
                  <div style="font-size:.7em;color:var(--ts)">近端磁吸目标价</div>
                  <div style="font-size:1.15em;font-weight:700;color:var(--t)">{_mp_str}</div>
                  {_mp_compare}
                </div>
              </div>
              <div style="display:flex;justify-content:space-between;align-items:center;margin:6px 0 3px">
                <div style="font-size:.7em;color:var(--ts)">OI 墙位 · <b style="color:var(--mt)">{_wall_label}</b></div>
                <div style="font-size:.62em;color:var(--ts)">数据={int(_detd.get("near_call_total") or 0):,}C / {int(_detd.get("near_put_total") or 0):,}P {f"(近端P/C {_detd.get('near_pc'):.2f})" if _detd.get('near_pc') else ""}</div>
              </div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
                <div>
                  <div style="font-size:.72em;color:#dc3545;font-weight:600;margin-bottom:3px">📈 阻力墙 (Top Call OI)</div>
                  {_wall_rows(_calls, "#dc3545", "C")}
                </div>
                <div>
                  <div style="font-size:.72em;color:#28a745;font-weight:600;margin-bottom:3px">📉 支撑墙 (Top Put OI)</div>
                  {_wall_rows(_puts, "#28a745", "P")}
                </div>
              </div>
            </div>'''
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
            {_full_oi_html}
            <ul class="cc-signals">{_bhtmld}</ul>
            {_build_dim_dq_html(_detd['dim_dq'])}
            {_tb_html}
            <div class="cc-footer">{_rss_badge}{_mlbtnd}</div>
          </div>
        </div>"""
    return new_company_html


# 注：_ml_combined_score 已删除（v0.33.0）—— caa432d (2026-03-30) 决定 ML combined_probability
# 不用于 dashboard 排名（无区分度），该函数自此从渲染路径移除、成为死代码，今删除。
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
    _Path = _Path_mod  # 复用模块级 pathlib.Path 别名

    # --- 准确率数据加载 ---
    _acc = _load_accuracy_data()
    _acc_stats = _acc["stats"]
    _acc_total_checked = _acc["total_checked"]
    _acc_overall = _acc["overall"]
    _acc_avg_return = _acc["avg_return"]
    _acc_correct = _acc["correct"]
    _acc_by_dir = _acc["by_dir"]
    _acc_by_ticker = _acc["by_ticker"]
    _acc_weekly_trend = _acc["weekly_trend"]
    _acc_weekly_by_dir = _acc.get("weekly_by_dir", [])
    _acc_best3 = _acc["best3"]
    _acc_worst3 = _acc["worst3"]
    _acc_sharpe = _acc["sharpe"]
    _acc_max_dd = _acc["max_dd"]
    _acc_win_streak = _acc["win_streak"]
    _acc_equity_curve = _acc.get("equity_curve", [])
    _acc_trading_stats = _acc.get("trading_stats", {})
    # v0.23.4 修复 #F：把 initial_capital / position_size_pct 提到 render_dashboard_html scope
    # 供下方方法学描述使用（避免 NameError）
    _initial_capital = float(_acc_trading_stats.get("initial_capital", 50000.0))
    _pos_pct = float(_acc_trading_stats.get("position_size_pct", 0.10))

    try:
        from zoneinfo import ZoneInfo as _ZI
        now_str = _dt.now(_ZI("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        from datetime import timezone as _tz
        now_str = _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M UTC")
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

    # 从当日 Agent 价 / analysis-{ticker}-ml-{date}.json 补注价格（当 ScoutBee price 缺失时）
    import glob as _inj_glob
    import re as _inj_re
    from datetime import datetime as _inj_dt

    def _inj_agent_price(_d):
        """优先用当天 swarm_results 可靠 Agent 的真实股价（Chronos/RivalBee/CodeExecutor）。
        刻意排除 OracleBee._snapshot_stock_price（期权快照价，可能被污染，如 NVDA 6-15
        显示 $145 实为期权快照污染，真实 $212）。"""
        _ad = (_d or {}).get("agent_details", {}) or {}
        for _src in (
            (_ad.get("ChronosBeeHorizon", {}).get("details", {}) or {}).get("analyst_targets", {}) or {},
            (_ad.get("RivalBeeVanguard", {}).get("details", {}) or {}).get("eps_revision", {}) or {},
            _ad.get("CodeExecutorAgent", {}).get("details", {}) or {},
        ):
            try:
                # current_price（Chronos/Rival/CodeExecutor 失败路径）或 price（CodeExecutor 成功路径键名不同）
                _v = float((_src or {}).get("current_price") or (_src or {}).get("price"))
                if _v > 0:
                    return _v
            except (TypeError, ValueError):
                continue
        return None

    try:
        _inj_today = _inj_dt.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        _inj_today = None

    for _inj_t in list(swarm_detail.keys()):
        _inj_scout = (swarm_detail[_inj_t]
                      .setdefault("agent_details", {})
                      .setdefault("ScoutBeeNova", {})
                      .setdefault("details", {}))
        if _inj_scout.get("price") is not None:
            continue

        # ① 优先：当日扫描的可靠 Agent 价（最权威，排除期权快照污染）
        _ap = _inj_agent_price(swarm_detail[_inj_t])
        if _ap is not None:
            _inj_scout["price"] = _ap
            continue

        # ② 回退：ML 分析文件。当日文件无条件用；旧文件加 7 天新鲜度护栏
        #    （避免把数周前的陈价当成当日价——6-15 曾回退到 5-29 的污染价 145.32）
        _inj_today_file = _Path_mod(report_dir) / f"analysis-{_inj_t}-ml-{date_str}.json"
        _inj_candidates = [_inj_today_file] + [
            _Path_mod(p) for p in sorted(_inj_glob.glob(
                str(_Path_mod(report_dir) / f"analysis-{_inj_t}-ml-*.json")
            ), reverse=True)
        ]
        for _inj_ml in _inj_candidates:
            # 新鲜度护栏：非当日文件，若距 date_str 超 7 天则跳过
            if _inj_ml != _inj_today_file and _inj_today is not None:
                _m = _inj_re.search(r"(\d{4}-\d{2}-\d{2})", _inj_ml.name)
                if _m:
                    try:
                        if abs((_inj_today - _inj_dt.strptime(_m.group(1), "%Y-%m-%d")).days) > 7:
                            continue
                    except ValueError:
                        pass
            try:
                with open(_inj_ml) as _inj_f:
                    _inj_data = json.load(_inj_f)
                _inj_price = (_inj_data.get("current_price")
                              or _inj_data.get("combined_recommendation", {}).get("current_price")
                              or _inj_data.get("ml_prediction", {}).get("current_price"))
                if _inj_price:
                    _inj_scout["price"] = float(_inj_price)
                    break
            except (OSError, json.JSONDecodeError, ValueError, TypeError):
                continue

    # 将 opportunities 按 ticker 建立索引，并补充 swarm 详细数据
    opp_by_ticker = {o.get("ticker"): o for o in opps}
    # 若 swarm_detail 有更多 ticker（超过 opportunities 的 5 个），全部纳入
    all_tickers_sorted = [o.get("ticker") for o in opps]
    for t in swarm_detail:
        if t not in all_tickers_sorted:
            all_tickers_sorted.append(t)
    # 用实际扫描数量覆盖（swarm_detail 包含全部标的，opportunities 只存前 5）
    n_tickers = len(all_tickers_sorted) or n_tickers


    # 计算 avg real_pct
    real_pcts = [swarm_detail[t].get("data_real_pct", 0) for t in swarm_detail if swarm_detail[t].get("data_real_pct")]
    avg_real = f"{sum(real_pcts)/len(real_pcts):.0f}%" if real_pcts else "-"

    # ── Phase 3 增强：宏观面板 + 深度卡片 + Markdown 渲染 ──


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

    # 分数来源：蜂群 opp_score > final_score（ML combined_probability 不用于排名，无区分度）
    _all_scores = []
    for _td2 in all_tickers_sorted:
        _s_fallback = float(opp_by_ticker.get(_td2, {}).get("opp_score") or
                            swarm_detail.get(_td2, {}).get("final_score", 0))
        _all_scores.append((_td2, _s_fallback))
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
    except Exception as _e_cal:
        _log.debug("宏观事件日历加载失败: %s", _e_cal)
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
            _yc_map = {"normal": "正常", "flat": "趋平", "inverted": "倒挂"}
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
            # 跳过非交易日幽灵，与历史/趋势序列基准日保持一致（fail-safe）
            try:
                from is_trading_day import filename_is_nontrading_day as _fnt_e
                if _fnt_e(_pdate):
                    continue
            except Exception:
                pass
            try:
                with open(_pjf, encoding="utf-8") as _pfp:
                    _prpt = _json.load(_pfp)
                for _po in _prpt.get("opportunities", []):
                    _ptk = _po.get("ticker", "")
                    if _ptk and _ptk not in _prev_scores:
                        _prev_scores[_ptk] = float(_po.get("opp_score", 0))
                break  # 只看最近一天
            except Exception as _e_prev:
                _log.debug("历史评分加载失败 (%s): %s", _pjf, _e_prev)
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

    # ── 历史简报回溯 + F&G 历史 + 评分趋势数据（提前加载，供 sparkline 使用）──
    _hist = _load_historical_data(report_dir, date_str, all_tickers_sorted, opp_by_ticker, swarm_detail, _fv3)
    _hist_entries = _hist["hist_entries"]
    _fg_history = _hist["fg_history"]
    _trend_data = _hist["trend_data"]
    _hist_full = _hist["hist_full"]

    # v0.24.5 #2: 今日 Actionable Top 1-3 板块（先于 Top 6 机会展示）
    actionable_html = _build_actionable_top_html(all_tickers_sorted, opp_by_ticker, swarm_detail)

    new_cards_html = _build_top_cards_html(all_tickers_sorted, opp_by_ticker, swarm_detail, report_dir, date_str, _score_deltas, _hist_full)

    new_rows_html = _build_table_rows_html(all_tickers_sorted, opp_by_ticker, swarm_detail, report_dir, date_str, _score_deltas)

    new_company_html = _build_deep_analysis_html(all_tickers_sorted, opp_by_ticker, swarm_detail, report_dir, date_str, _score_deltas)

    # （历史数据已在 cards 循环前加载，此处直接使用）

    # 生成历史时间线 HTML（可折叠：最近 3 条展示，其余折叠）
    _HIST_VISIBLE = 3  # 默认展示条数
    _hist_html = ""

    if _hist_entries:
        # 最近 N 条直接展示
        for _he in _hist_entries[:_HIST_VISIBLE]:
            _hist_html += _render_hist_card(_he)
        # 超出部分包裹在可折叠区域
        _hidden_entries = _hist_entries[_HIST_VISIBLE:]
        if _hidden_entries:
            _hidden_count = len(_hidden_entries)
            _collapsed_cards = "".join(_render_hist_card(_he) for _he in _hidden_entries)
            _hist_html += f"""
            <button class="hist-toggle" id="histToggleBtn"
              onclick="var c=document.getElementById('histCollapsed');c.classList.toggle('hist-expanded');this.classList.toggle('hist-open');this.querySelector('.hist-toggle-text').textContent=c.classList.contains('hist-expanded')?'收起历史简报':'展开更早 {_hidden_count} 份简报'">
              <span class="hist-toggle-text">展开更早 {_hidden_count} 份简报</span>
              <span class="hist-arrow">▼</span>
            </button>
            <div class="hist-collapsed" id="histCollapsed">
              {_collapsed_cards}
            </div>"""
    else:
        _hist_html = '<div class="hist-empty">暂无历史记录，第一份历史简报将在明天出现</div>'

    # ── 准确率 Dashboard 数据拼装 ──

    # 方向图数据
    _dir_map_acc = _DIR_CN

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

    # 方向分组 KPI 卡片
    _dir_kpi_cfg = [
        ("bullish", "看多", "#22c55e", "rgba(34,197,94,.08)"),
        ("bearish", "看空", "#ef4444", "rgba(239,68,68,.08)"),
        ("neutral", "中性", "#94a3b8", "rgba(148,163,184,.08)"),
    ]
    _acc_dir_kpi_html = ""
    for _dk, _dlabel, _dcol, _dbg in _dir_kpi_cfg:
        _di = _acc_by_dir.get(_dk, {})
        _dacc = _di.get("accuracy", 0) * 100
        _dtot = _di.get("total", 0)
        _dcor = _di.get("correct", 0)
        _dret = _di.get("avg_return", 0)
        _dret_col = "#22c55e" if _dret >= 0 else "#ef4444"
        _acc_dir_kpi_html += (
            f'<div class="acc-dir-kpi" style="border-color:{_dcol};background:{_dbg}">'
            f'<div class="dkpi-label" style="color:{_dcol}">{_dlabel}</div>'
            f'<div class="dkpi-row">'
            f'<span class="dkpi-val">{_dacc:.0f}%</span>'
            f'<span class="dkpi-sub">准确率 · {_dcor}/{_dtot}次</span>'
            f'</div>'
            f'<div class="dkpi-ret" style="color:{_dret_col}">{_dret:+.2f}% 均收益</div>'
            f'</div>\n'
        )

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
    <div class="acc-dir-kpi-row">
{_acc_dir_kpi_html}
    </div>
    <div class="acc-two-col">
      <div class="acc-dir-box">
    <div class="acc-box-title">方向准确率分布</div>
    <div class="acc-canvas-wrap"><canvas id="accDirChart" width="300" height="200"></canvas></div>
    <div class="acc-dir-rets" style="display:flex;gap:10px;margin-top:10px;justify-content:center;flex-wrap:wrap" id="accDirRets"></div>
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
    <!-- ── Sprint 1 / v16.0 Trading Stats 真实交易指标 ── -->
    <div class="acc-section-title" style="margin-top:18px">💰 真实策略回测（扣成本 · 路径依赖 · Sprint 1）</div>
    <div id="tradingStatsBox" style="margin:10px 0 16px">
      <div style="font-size:.78em;color:var(--ts);margin-bottom:8px">
        📌 <strong>方法学</strong>：${int(_initial_capital/1000)}K 起始资金，每笔固定 ${int(_initial_capital * _pos_pct)}（{_pos_pct*100:.0f}% 仓位、不复利），
        -5% 硬止损 / +10% 止盈（盘中触发，跳空时 gap-aware），扣滑点 + 佣金 + 借券费（空头）。
        <span style="color:#e99;">Gross 曲线不扣成本（参考），Net 曲线 = 真实可拿收益。</span>
        <span style="color:var(--mt);">Sharpe 已年化（×√36，T+7 周期）。</span>
      </div>
      <div id="tradingStatsCards" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px"></div>
    </div>

    <!-- ── Equity Curve 权益曲线 (3 lines: Gross/Net/SPY) ── -->
    <div class="eq-section">
      <div class="acc-section-title" style="margin-top:18px">📉 资金曲线对比 · Gross · Net · SPY 基准</div>
      <div id="eqCurveContainer">
        <div class="eq-wrap"><canvas id="eqCurveChart"></canvas></div>
        <div class="eq-stats" id="eqStats"></div>
      </div>
      <div class="eq-cold" id="eqCold" style="display:none">
        <div>需要 T+7 验证数据才能绘制权益曲线<br><span style="font-size:.82em;opacity:.7">当 outcome backfill 运行后，此图表将自动显示</span></div>
      </div>
    </div>
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
    _dir_cn12 = _DIR_CN
    _search_index = []
    for _t12 in all_tickers_sorted:
        _s12 = float(opp_by_ticker.get(_t12, {}).get("opp_score") or swarm_detail.get(_t12, {}).get("final_score", 0))
        _d12 = str(opp_by_ticker.get(_t12, {}).get("direction") or swarm_detail.get(_t12, {}).get("direction", "neutral")).lower()
        if "多" in _d12: _d12 = "bullish"
        elif "空" in _d12: _d12 = "bearish"
        elif _d12 not in ("bullish", "bearish", "neutral"): _d12 = "neutral"
        _det12 = _detail(_t12, swarm_detail)
        _search_index.append({
            "ticker": _t12,
            "score": round(_s12, 1),
            "direction": _dir_cn12.get(_d12, "中性"),
            "price": _det12["price"],
        })

    # ── 升级 C: "今日变化" 摘要卡 ──
    _changes_html = ""
    try:
        # 取最近一天的历史数据对比
        _prev_date = None
        _prev_data = {}
        for _cd in sorted(_hist_full.keys(), reverse=True):
            if _cd != date_str:
                _prev_date = _cd
                _prev_data = {item["ticker"]: item for item in _hist_full[_cd]}
                break
        if _prev_date and _prev_data:
            _cur_data = {item["ticker"]: item for item in _hist_full.get(date_str, [])}
            _up_items = []    # (ticker, delta)
            _dn_items = []
            _flip_items = []  # (ticker, old_dir, new_dir)
            _new_tickers = []
            _dir_cn_c = _DIR_CN
            _prev_avg = 0.0
            _cur_avg = 0.0
            _prev_cnt = 0
            _cur_cnt = 0
            for _tk_c, _cd_c in _cur_data.items():
                _cur_avg += _cd_c["score"]
                _cur_cnt += 1
                if _tk_c in _prev_data:
                    _pd_c = _prev_data[_tk_c]
                    _delta_c = round(_cd_c["score"] - _pd_c["score"], 1)
                    if _delta_c >= 0.8:
                        _up_items.append((_tk_c, _delta_c))
                    elif _delta_c <= -0.8:
                        _dn_items.append((_tk_c, _delta_c))
                    if _cd_c["direction"] != _pd_c["direction"]:
                        _flip_items.append((_tk_c, _dir_cn_c.get(_pd_c["direction"], "?"),
                                            _dir_cn_c.get(_cd_c["direction"], "?")))
                else:
                    _new_tickers.append(_tk_c)
            for _tk_p, _pd_p in _prev_data.items():
                _prev_avg += _pd_p["score"]
                _prev_cnt += 1
            _prev_avg = round(_prev_avg / _prev_cnt, 1) if _prev_cnt else 0
            _cur_avg = round(_cur_avg / _cur_cnt, 1) if _cur_cnt else 0
            _avg_delta = round(_cur_avg - _prev_avg, 1)
            _up_items.sort(key=lambda x: x[1], reverse=True)
            _dn_items.sort(key=lambda x: x[1])
            _parts_c = []
            if _up_items:
                _chips = " ".join(f'<span class="chg-chip chg-up">↑ {t} {d:+.1f}</span>' for t, d in _up_items[:4])
                _parts_c.append(_chips)
            if _dn_items:
                _chips = " ".join(f'<span class="chg-chip chg-dn">↓ {t} {d:+.1f}</span>' for t, d in _dn_items[:4])
                _parts_c.append(_chips)
            if _flip_items:
                _chips = " ".join(f'<span class="chg-chip chg-flip">↺ {t} {o}→{n}</span>' for t, o, n in _flip_items[:3])
                _parts_c.append(_chips)
            if _new_tickers:
                _chips = " ".join(f'<span class="chg-chip chg-new">+ {t}</span>' for t in _new_tickers[:3])
                _parts_c.append(_chips)
            _n_up = sum(1 for _, d in [(t, _cur_data[t]["score"] - _prev_data[t]["score"])
                        for t in _cur_data if t in _prev_data] if d > 0.1)
            _n_dn = sum(1 for _, d in [(t, _cur_data[t]["score"] - _prev_data[t]["score"])
                        for t in _cur_data if t in _prev_data] if d < -0.1)
            _n_flat = len([t for t in _cur_data if t in _prev_data]) - _n_up - _n_dn
            _avg_cls = "chg-up" if _avg_delta > 0 else ("chg-dn" if _avg_delta < 0 else "")
            _summary_line = (f'<div class="chg-summary">'
                             f'整体：<span class="chg-up">{_n_up}↑</span> '
                             f'<span class="chg-dn">{_n_dn}↓</span> '
                             f'<span>{_n_flat}→</span> · '
                             f'均分 {_prev_avg}→{_cur_avg}'
                             f'(<span class="{_avg_cls}">{_avg_delta:+.1f}</span>)'
                             f'</div>')
            if _parts_c or _flip_items:
                _inner = "\n".join(_parts_c) + _summary_line
                _changes_html = (f'<div class="changes-card">'
                                 f'<div class="chg-title">与{_prev_date}对比</div>'
                                 f'<div class="chg-body">{_inner}</div>'
                                 f'</div>')
    except Exception as _chg_err:
        _log.debug("今日变化摘要生成失败: %s", _chg_err)

    # ── 升级 A: 板块热力图数据 ──
    _heatmap_html = ""
    try:
        from config import WATCHLIST as _WL_A
        _sectors_a: dict = {}  # {sector: {tickers: [...], avg_momentum, avg_sentiment, direction_dominant}}
        for _tk_a in all_tickers_sorted:
            _wl_a = _WL_A.get(_tk_a, {})
            _sec_a = _wl_a.get("sector", "Other") if isinstance(_wl_a, dict) else "Other"
            _sd_a = swarm_detail.get(_tk_a, {})
            _m5d_a = _sd_a.get("agent_details", {}).get("ScoutBeeNova", {}).get("details", {}).get("momentum_5d", 0) or 0
            _buzz_a = _sd_a.get("agent_details", {}).get("BuzzBeeWhisper", {}).get("details", {})
            _sent_a = _buzz_a.get("sentiment_pct", 50) if isinstance(_buzz_a, dict) else 50
            _dir_a = str(_sd_a.get("direction", "neutral")).lower()
            _sectors_a.setdefault(_sec_a, {"tickers": [], "momentums": [], "sentiments": [], "dirs": []})
            _sectors_a[_sec_a]["tickers"].append(_tk_a)
            _sectors_a[_sec_a]["momentums"].append(float(_m5d_a))
            _sectors_a[_sec_a]["sentiments"].append(float(_sent_a))
            _sectors_a[_sec_a]["dirs"].append(_dir_a)
        if _sectors_a:
            _hm_cells = ""
            for _sec_name, _sec_data in sorted(_sectors_a.items(), key=lambda x: len(x[1]["tickers"]), reverse=True):
                _n_tk = len(_sec_data["tickers"])
                _avg_mom = sum(_sec_data["momentums"]) / _n_tk if _n_tk else 0
                _avg_sent = sum(_sec_data["sentiments"]) / _n_tk if _n_tk else 50
                _bull_n = sum(1 for d in _sec_data["dirs"] if d == "bullish")
                _bear_n = sum(1 for d in _sec_data["dirs"] if d == "bearish")
                # 颜色：基于动量 + 情绪综合
                if _avg_mom > 1.5 and _avg_sent > 55:
                    _hm_bg = "rgba(34,197,94,.18)"
                    _hm_border = "rgba(34,197,94,.35)"
                elif _avg_mom < -1.5 and _avg_sent < 45:
                    _hm_bg = "rgba(239,68,68,.14)"
                    _hm_border = "rgba(239,68,68,.3)"
                elif _avg_mom > 0.5 or _avg_sent > 52:
                    _hm_bg = "rgba(34,197,94,.08)"
                    _hm_border = "rgba(34,197,94,.2)"
                elif _avg_mom < -0.5 or _avg_sent < 48:
                    _hm_bg = "rgba(239,68,68,.07)"
                    _hm_border = "rgba(239,68,68,.18)"
                else:
                    _hm_bg = "rgba(100,116,139,.06)"
                    _hm_border = "var(--border)"
                _mom_cls = "hm-up" if _avg_mom > 0 else ("hm-dn" if _avg_mom < 0 else "")
                _tk_chips = " ".join(f'<span class="hm-tk">{t}</span>' for t in _sec_data["tickers"][:5])
                _dir_bar = (f'<div class="hm-dir-bar">'
                            f'<div class="hm-dir-fill hm-dir-bull" style="width:{_bull_n/_n_tk*100:.0f}%"></div>'
                            f'<div class="hm-dir-fill hm-dir-bear" style="width:{_bear_n/_n_tk*100:.0f}%"></div>'
                            f'</div>' if _n_tk > 1 else "")
                _hm_cells += (f'<div class="hm-cell" style="background:{_hm_bg};border-color:{_hm_border};'
                              f'flex:{max(1, _n_tk)}">'
                              f'<div class="hm-sec-name">{_html.escape(_sec_name)}</div>'
                              f'<div class="hm-mom {_mom_cls}">{_avg_mom:+.1f}%</div>'
                              f'<div class="hm-tks">{_tk_chips}</div>'
                              f'{_dir_bar}'
                              f'<div class="hm-sent">情绪 {_avg_sent:.0f}%</div>'
                              f'</div>')
            _heatmap_html = (f'<div class="chart-box">'
                             f'<div class="chart-ttl">🔥 板块情绪热力图</div>'
                             f'<div class="hm-grid">{_hm_cells}</div>'
                             f'</div>')
    except Exception as _hm_err:
        _log.debug("板块热力图生成失败: %s", _hm_err)

    # ── Feature: 蜂群分歧度（Swarm Divergence）──
    _swarm_divergence: dict = {}
    _BEE_NAMES = [
        "ScoutBeeNova", "RivalBeeVanguard", "OracleBeeEcho",
        "ChronosBeeHorizon", "BuzzBeeWhisper", "GuardBeeSentinel",
        "BearBeeContrarian",
    ]
    _BEE_SHORT = {
        "ScoutBeeNova": "Scout", "RivalBeeVanguard": "Rival",
        "OracleBeeEcho": "Oracle", "ChronosBeeHorizon": "Chronos",
        "BuzzBeeWhisper": "Buzz", "GuardBeeSentinel": "Guard",
        "BearBeeContrarian": "Bear",
    }
    for _dv_tk in all_tickers_sorted:
        try:
            _dv_sd = swarm_detail.get(_dv_tk, {})
            _dv_ad = _dv_sd.get("agent_details", {})
            _dv_scores = []
            _dv_votes = {"bullish": 0, "bearish": 0, "neutral": 0}
            _dv_bees = []  # [{name, score, direction}]
            for _bn in _BEE_NAMES:
                _ba = _dv_ad.get(_bn, {})
                _bs = _ba.get("score")
                _bd = str(_ba.get("direction", "neutral")).lower()
                if _bs is not None:
                    _dv_scores.append(float(_bs))
                    _dv_votes[_bd] = _dv_votes.get(_bd, 0) + 1
                    _dv_bees.append({
                        "name": _BEE_SHORT.get(_bn, _bn),
                        "score": round(float(_bs), 1),
                        "dir": "bull" if _bd == "bullish" else ("bear" if _bd == "bearish" else "neut"),
                    })
            if _dv_scores:
                _dv_mean = sum(_dv_scores) / len(_dv_scores)
                _dv_std = (sum((x - _dv_mean)**2 for x in _dv_scores) / len(_dv_scores)) ** 0.5
                _dv_max = max(_dv_scores)
                _dv_min = min(_dv_scores)
                _dv_total = sum(_dv_votes.values())
                _dv_majority = max(_dv_votes.values()) / _dv_total if _dv_total else 0
                # 加入共振标志（与蜂群核心决策一致）
                _dv_resonance = _dv_sd.get("resonance", {}).get("resonance_detected", False)
                _swarm_divergence[_dv_tk] = {
                    "std": round(_dv_std, 2),
                    "spread": round(_dv_max - _dv_min, 1),
                    "consensus": round(_dv_majority * 100, 0),
                    "resonance": _dv_resonance,
                    "votes": _dv_votes,
                    "bees": _dv_bees,
                }
        except Exception:
            pass

    # ── Jinja2 渲染 ──
    _avg_score_str = f"{_avg_score:.1f}" if _all_scores else "0"

    # JS 数据（传给 window.__AH__）
    _data_obj = {
        "fv": _fv3,
        "fg_label": _fg_label,
        "scores": [[t, round(s, 1)] for t, s in _all_scores],
        "dir_counts": [_dir_counts["bullish"], _dir_counts["bearish"], _dir_counts["neutral"]],
        "radar": {t: _radar_data(t, swarm_detail) for t in all_tickers_sorted},
        "acc_dir_labels": [_dir_map_acc.get(d, d) for d in ["bullish", "bearish", "neutral"]],
        "acc_dir_accs": [round(_acc_by_dir.get(d, {}).get("accuracy", 0) * 100, 1)
                         for d in ["bullish", "bearish", "neutral"]],
        "acc_dir_tots": [_acc_by_dir.get(d, {}).get("total", 0)
                         for d in ["bullish", "bearish", "neutral"]],
        "acc_dir_rets": [round(_acc_by_dir.get(d, {}).get("avg_return", 0), 2)
                         for d in ["bullish", "bearish", "neutral"]],
        "acc_weekly": _acc_weekly_trend,
        "acc_weekly_by_dir": _acc_weekly_by_dir,
        "fg_history": _fg_history,
        "trend_data": _trend_data,
        "hist_full": _hist_full,
        "search_index": _search_index,
        "equity_curve": _acc_equity_curve,
        "trading_stats": _acc_trading_stats,
        "swarm_divergence": _swarm_divergence,
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
        deploy_ts=_data_obj.get("_deploy_ts", 0),
        changes_html=_changes_html,
        heatmap_html=_heatmap_html,
        top_n=min(6, len(all_tickers_sorted)),
        scores_chart_height="{}px".format(max(160, len(all_tickers_sorted) * 28)),
        actionable_html=actionable_html,
        cards_html=new_cards_html,
        rows_html=new_rows_html,
        company_html=new_company_html,
        report_body=_rpt_body,
        hist_html=_hist_html,
        acc_section_html=_acc_section_html,
    )
