#!/usr/bin/env python3
"""
🐝 Alpha Hive 日报生成器 - 集成期权分析的完整版本
每日自动扫描 watchlist 并生成结构化投资简报 + X 线程版本
"""

import json
import argparse
import time
from datetime import datetime
from typing import Dict, List, Tuple
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

# 导入现有模块
from config import WATCHLIST
from hive_logger import get_logger, PATHS, set_correlation_id, SafeJSONEncoder

_log = get_logger("daily_report")

# Week 4: 指标收集器
try:
    from metrics_collector import MetricsCollector
except ImportError:
    MetricsCollector = None
from generate_ml_report import MLEnhancedReportGenerator
from pheromone_board import PheromoneBoard
from swarm_agents import (
    ScoutBeeNova, OracleBeeEcho, BuzzBeeWhisper,
    ChronosBeeHorizon, RivalBeeVanguard, GuardBeeSentinel,
    BearBeeContrarian,
    QueenDistiller, prefetch_shared_data, inject_prefetched
)
from concurrent.futures import as_completed
from agent_toolbox import AgentHelper

# Phase 2: Import memory store
try:
    from memory_store import MemoryStore
except ImportError:
    MemoryStore = None

# Phase 3 P2: Import Calendar integrator
try:
    from calendar_integrator import CalendarIntegrator
except ImportError:
    CalendarIntegrator = None

# Phase 3 P4: Import Code Execution Agent
try:
    from code_executor_agent import CodeExecutorAgent
    from config import CODE_EXECUTION_CONFIG
except ImportError:
    CodeExecutorAgent = None
    CODE_EXECUTION_CONFIG = {"enabled": False}

# Phase 3 P5: Import CrewAI 多 Agent 框架
try:
    from crewai_adapter import AlphaHiveCrew
    from config import CREWAI_CONFIG
except (ImportError, TypeError) as e:
    AlphaHiveCrew = None
    CREWAI_CONFIG = {"enabled": False}
    _log.info("CrewAI 模块导入失败: %s (降级到原始蜂群)", type(e).__name__)

# Phase 3 P6: Import Slack 报告通知器（替代 Gmail）
try:
    from slack_report_notifier import SlackReportNotifier
except ImportError:
    SlackReportNotifier = None

# 财报自动监控器
try:
    from earnings_watcher import EarningsWatcher
except ImportError:
    EarningsWatcher = None

# Phase 3 内存优化: 向量记忆层（Chroma 长期记忆）
try:
    from vector_memory import VectorMemory
    from config import VECTOR_MEMORY_CONFIG
except ImportError:
    VectorMemory = None
    VECTOR_MEMORY_CONFIG = {"enabled": False}

# Phase 6: 回测反馈循环
try:
    from backtester import Backtester, run_full_backtest
except ImportError:
    Backtester = None
    run_full_backtest = None


# 免责声明常量（去重，全局引用）
DISCLAIMER_FULL = (
    "本报告为蜂群 AI 分析，不构成投资建议，不替代持牌投顾。"
    "预测存在误差，所有交易决策需自行判断和风控。"
)
DISCLAIMER_SHORT = "非投资建议，仅数据分析与情景推演。"


@dataclass
class OpportunityItem:
    """机会项目结构"""
    ticker: str
    direction: str  # "看多" / "看空" / "中性"
    signal_score: float  # 0-10
    catalyst_score: float  # 0-10
    sentiment_score: float  # 0-10
    odds_score: float  # 0-10
    risk_score: float  # 0-10
    options_score: float  # 0-10 (新增)
    opportunity_score: float  # 0-10 (综合)
    confidence: float  # 0-100%
    key_catalysts: List[str]
    options_signal: str  # 期权信号摘要
    risks: List[str]
    thesis_break: str  # 失效条件


class AlphaHiveDailyReporter:
    """Alpha Hive 日报生成引擎"""

    # ── Dashboard CSS（从 _generate_index_html 提取，262 行 → 类常量）──
    _DASHBOARD_CSS = """
:root{--bg:#f0f4ff;--surface:#fff;--surface2:#f8f9fc;--border:#e8ecf3;
      --tp:#1a1f2e;--ts:#64748b;--acc:#F4A532;--acc2:#667eea;--acc3:#764ba2;
      --bull:#22c55e;--bear:#ef4444;--neut:#f59e0b;--nav-h:60px}
html.dark{--bg:#0A0F1C;--surface:#141928;--surface2:#1a2035;--border:#2a3050;--tp:#e2e8f0;--ts:#94a3b8}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:var(--bg);color:var(--tp);min-height:100vh;transition:background .3s,color .3s}
/* NAV */
.nav{position:fixed;top:0;left:0;right:0;z-index:1000;height:var(--nav-h);
     background:rgba(10,15,28,.96);backdrop-filter:blur(10px);
     border-bottom:1px solid rgba(244,165,50,.2);
     display:flex;align-items:center;justify-content:space-between;padding:0 28px}
.nav-logo{display:flex;align-items:center;gap:8px;font-weight:900;font-size:1.1em;color:var(--acc);text-decoration:none}
.nav-links{display:flex;gap:2px}
.nav-link{padding:7px 12px;border-radius:6px;font-size:.85em;font-weight:500;
          color:rgba(255,255,255,.7);text-decoration:none;transition:all .2s}
.nav-link:hover{background:rgba(244,165,50,.15);color:var(--acc)}
.dark-btn{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);
          color:#fff;padding:6px 14px;border-radius:8px;cursor:pointer;font-size:.82em;transition:all .2s}
.dark-btn:hover{background:rgba(244,165,50,.2);border-color:var(--acc)}
@media(max-width:768px){.nav-links{display:none}}
/* HERO */
.hero{background:linear-gradient(135deg,#0A0F1C 0%,#141928 55%,#1a1040 100%);
      padding:calc(var(--nav-h) + 36px) 32px 0;position:relative;overflow:hidden}
.hero-inner{max-width:1280px;margin:0 auto;display:flex;align-items:center;
            justify-content:space-between;padding-bottom:36px;gap:40px}
.hero-left{flex:1}
.hero-badge{display:inline-flex;align-items:center;gap:6px;
            background:rgba(244,165,50,.12);border:1px solid rgba(244,165,50,.3);
            color:var(--acc);padding:5px 14px;border-radius:20px;
            font-size:.82em;font-weight:700;margin-bottom:18px}
.hero-title{font-size:clamp(1.8em,3.5vw,2.8em);font-weight:900;color:#fff;
            line-height:1.15;margin-bottom:12px}
.hero-title span{background:linear-gradient(135deg,#F4A532,#f7c55a);
                 -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hero-sub{color:rgba(255,255,255,.55);font-size:1em;margin-bottom:18px}
.hero-meta{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.hero-time{color:rgba(255,255,255,.45);font-size:.85em}
.hero-dbadge{background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.3);
             color:#4ade80;padding:3px 12px;border-radius:12px;font-size:.8em;font-weight:700}
.hero-right{flex-shrink:0;width:260px}
.hero-svg{width:100%;height:auto}
@keyframes hive-float{0%,100%{transform:translateY(0) rotate(0deg)}50%{transform:translateY(-8px) rotate(2deg)}}
@keyframes hex-pulse{0%,100%{opacity:.6}50%{opacity:1}}
.hive-anim{animation:hive-float 4s ease-in-out infinite}
.hex-p{animation:hex-pulse 2s ease-in-out infinite}
/* HERO STATS ROW */
.hero-stats{max-width:1280px;margin:0 auto;
            display:grid;grid-template-columns:repeat(4,1fr);
            border-top:1px solid rgba(244,165,50,.12)}
.hstat{padding:22px;text-align:center;border-right:1px solid rgba(244,165,50,.08);transition:background .2s}
.hstat:last-child{border-right:none}
.hstat:hover{background:rgba(244,165,50,.04)}
.hstat-val{font-size:2.1em;font-weight:900;color:var(--acc);line-height:1}
.hstat-lbl{font-size:.78em;color:rgba(255,255,255,.45);margin-top:5px;text-transform:uppercase;letter-spacing:.05em}
/* MAIN */
.main{max-width:1280px;margin:0 auto;padding:36px 28px}
.section{background:var(--surface);border-radius:14px;padding:28px;margin-bottom:24px;border:1px solid var(--border)}
.sec-title{font-size:1.2em;font-weight:800;color:var(--tp);margin-bottom:20px;
           display:flex;align-items:center;gap:10px}
.sec-title::before{content:'';display:inline-block;width:4px;height:20px;
                   background:linear-gradient(135deg,var(--acc),var(--acc2));border-radius:2px}
/* TOP 6 CARDS */
.top6-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
@media(max-width:1024px){.top6-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:600px){.top6-grid{grid-template-columns:1fr}}
.scard{border:1px solid var(--border);border-radius:13px;overflow:hidden;
       background:var(--surface2);transition:transform .2s,box-shadow .2s,border-color .2s;position:relative}
.scard:hover{transform:translateY(-4px);box-shadow:0 12px 36px rgba(244,165,50,.14);border-color:var(--acc)}
.scard-head{padding:16px 16px 12px;display:flex;align-items:flex-start;justify-content:space-between}
.slogo-wrap{position:relative}
.slogo{width:42px;height:42px;border-radius:9px;object-fit:contain;
       background:#fff;padding:4px;border:1px solid var(--border)}
.slogo-fb{width:42px;height:42px;border-radius:9px;display:flex;align-items:center;
          justify-content:center;font-weight:900;font-size:.82em;color:#fff;
          background:linear-gradient(135deg,var(--acc2),var(--acc3))}
.srank{font-size:.7em;font-weight:800;background:var(--acc);color:#0A0F1C;
       padding:2px 7px;border-radius:5px;position:absolute;top:-5px;right:-5px}
.sdir{padding:4px 11px;border-radius:18px;font-size:.78em;font-weight:700}
.sdir-bull{background:rgba(34,197,94,.13);color:var(--bull)}
.sdir-bear{background:rgba(239,68,68,.13);color:var(--bear)}
.sdir-neut{background:rgba(245,158,11,.13);color:var(--neut)}
.scard-body{padding:0 16px 16px}
.sticker{font-size:1.4em;font-weight:900;color:var(--tp)}
.sname{font-size:.75em;color:var(--ts);margin-top:1px}
.score-row{display:flex;align-items:center;gap:10px;margin:12px 0 7px}
.score-big{font-size:1.9em;font-weight:900;line-height:1}
.score-big.sc-h{color:var(--bull)}.score-big.sc-m{color:var(--neut)}.score-big.sc-l{color:var(--bear)}
.sbar-wrap{flex:1}
.sbar-lbl{font-size:.7em;color:var(--ts);margin-bottom:3px;display:flex;justify-content:space-between}
.sbar{height:5px;background:var(--border);border-radius:3px;overflow:hidden}
.sbar-fill{height:100%;border-radius:3px}
.fill-h{background:linear-gradient(90deg,#22c55e,#4ade80)}
.fill-m{background:linear-gradient(90deg,#f59e0b,#fbbf24)}
.fill-l{background:linear-gradient(90deg,#ef4444,#f87171)}
.sinsight{font-size:.78em;color:var(--ts);line-height:1.5;border-top:1px solid var(--border);
          padding-top:9px;margin-top:4px;
          display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ml-btn{display:inline-flex;align-items:center;gap:4px;margin-top:11px;padding:5px 13px;
        background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;
        border-radius:7px;font-size:.76em;font-weight:700;text-decoration:none;transition:opacity .2s}
.ml-btn:hover{opacity:.85}
/* CHARTS */
.charts-grid{display:grid;grid-template-columns:1fr 2fr 1fr;gap:20px}
@media(max-width:900px){.charts-grid{grid-template-columns:1fr}}
.chart-box{background:var(--surface2);border-radius:12px;padding:22px;border:1px solid var(--border)}
.chart-ttl{font-size:.82em;font-weight:700;color:var(--ts);text-transform:uppercase;
           letter-spacing:.06em;margin-bottom:14px;text-align:center}
/* TABLE */
.tbl-search-row{display:flex;gap:12px;margin-bottom:14px;align-items:center}
.tbl-search{flex:1;max-width:260px;padding:9px 14px;background:var(--surface2);
            border:1px solid var(--border);border-radius:8px;color:var(--tp);
            font-size:.88em;outline:none}
.tbl-search:focus{border-color:var(--acc2)}
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:8px;border:1px solid var(--border)}
.full-table{width:100%;border-collapse:collapse;min-width:620px}
.full-table thead{position:sticky;top:0;z-index:5}
.full-table th{padding:11px 13px;text-align:left;font-size:.8em;font-weight:700;
               background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;
               letter-spacing:.04em;white-space:nowrap}
.full-table td{padding:10px 13px;font-size:.86em;border-bottom:1px solid var(--border);color:var(--tp)}
.full-table tbody tr:hover{background:var(--surface2)}
.dcell-bull{background:rgba(34,197,94,.12);color:var(--bull);font-weight:700;
            border-radius:4px;padding:2px 9px;font-size:.8em;display:inline-block}
.dcell-bear{background:rgba(239,68,68,.12);color:var(--bear);font-weight:700;
            border-radius:4px;padding:2px 9px;font-size:.8em;display:inline-block}
.dcell-neut{background:rgba(245,158,11,.12);color:var(--neut);font-weight:700;
            border-radius:4px;padding:2px 9px;font-size:.8em;display:inline-block}
.ml-btn-sm{display:inline-block;padding:3px 9px;background:linear-gradient(135deg,#667eea,#764ba2);
           color:#fff;border-radius:5px;font-size:.75em;font-weight:700;text-decoration:none}
.sc-h{color:var(--bull)}.sc-m{color:var(--neut)}.sc-l{color:var(--bear)}
/* COMPANY DEEP CARDS */
.company-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:20px}
@media(max-width:600px){.company-grid{grid-template-columns:1fr}}
.company-card{border:1px solid var(--border);border-radius:13px;overflow:hidden;background:var(--surface2)}
.cc-header{padding:14px 18px;color:#fff;display:flex;justify-content:space-between;align-items:center}
.cc-ticker{font-size:1.25em;font-weight:900}
.cc-dir{font-size:.78em;background:rgba(255,255,255,.18);padding:2px 10px;border-radius:10px}
.cc-score{font-size:.95em;font-weight:700}
.cc-body{padding:16px 18px}
.cc-two{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:start}
.cc-metric{display:flex;justify-content:space-between;padding:4px 0;
           border-bottom:1px solid var(--border);font-size:.82em}
.cc-metric:last-child{border-bottom:none}
.cm-l{color:var(--ts)}.cm-v{font-weight:700;color:var(--tp)}
.cc-signals{list-style:none;padding:0;margin:12px 0 0}
.cc-signals li{padding:4px 0;border-bottom:1px dashed var(--border);
               font-size:.8em;color:var(--ts);line-height:1.5}
.cc-signals li:last-child{border-bottom:none}
.cc-footer{margin-top:12px;text-align:right}
.ml-btn-cc{display:inline-flex;align-items:center;gap:4px;padding:5px 13px;
           background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;
           border-radius:7px;font-size:.76em;font-weight:700;text-decoration:none}
/* REPORT */
.report-body{max-height:750px;overflow-y:auto;padding-right:8px;font-size:.88em;
             line-height:1.8;color:var(--tp)}
.report-body h1{font-size:1.35em;color:var(--acc2);border-bottom:2px solid var(--acc2);
                padding-bottom:6px;margin:16px 0 8px}
.report-body h2{font-size:1.1em;color:var(--acc2);border-left:4px solid var(--acc2);
                padding-left:9px;margin:13px 0 5px}
.report-body h3{font-size:.98em;color:var(--acc3);font-weight:700;margin:9px 0 3px}
.report-body ul{margin:4px 0 8px 18px}.report-body li{margin:2px 0}
.report-body hr{border:none;border-top:1px solid var(--border);margin:11px 0}
.report-body p{margin:2px 0}.sub-ul{margin-top:4px;padding-left:16px}
/* MISC */
.res-y{background:rgba(34,197,94,.14);color:var(--bull);border-radius:7px;
       padding:2px 8px;font-size:.77em;font-weight:700;display:inline-block}
.res-n{background:rgba(239,68,68,.1);color:var(--bear);border-radius:7px;
       padding:2px 8px;font-size:.77em;font-weight:700;display:inline-block}
.bear-bar{height:5px;background:var(--border);border-radius:3px;margin-top:2px}
.bear-fill{height:100%;border-radius:3px;background:linear-gradient(90deg,#f59e0b,#ef4444)}
.footer{background:#0A0F1C;color:rgba(255,255,255,.45);text-align:center;
        padding:28px;font-size:.85em}
.footer p{margin:4px 0}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:rgba(102,126,234,.4);border-radius:3px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.status-dot{width:10px;height:10px;border-radius:50%;background:#22c55e;animation:pulse 2s infinite;display:inline-block}
/* HISTORY */
.hist-list{display:flex;flex-direction:column;gap:14px}
.hist-card{display:flex;align-items:center;gap:20px;padding:18px 20px;
           border:1px solid var(--border);border-radius:12px;background:var(--surface2);
           transition:border-color .2s,box-shadow .2s}
.hist-card:hover{border-color:var(--acc2);box-shadow:0 4px 16px rgba(102,126,234,.1)}
.hist-left{min-width:110px}
.hist-date{font-size:.95em;font-weight:800;color:var(--tp)}
.hist-meta{font-size:.78em;color:var(--ts);margin-top:3px}
.hist-mid{display:flex;flex-wrap:wrap;gap:8px;flex:1}
.htop-chip{display:flex;align-items:center;gap:5px;background:var(--bg);
           border:1px solid var(--border);border-radius:8px;padding:5px 10px}
.hticker{font-weight:800;font-size:.88em;color:var(--tp)}
.hscore{font-size:.82em;font-weight:700}
.hdir{font-size:.85em}
.hist-right{display:flex;flex-wrap:wrap;gap:6px;min-width:220px;justify-content:flex-end}
.hlink{display:inline-block;padding:4px 10px;border-radius:6px;font-size:.76em;
       font-weight:700;text-decoration:none;transition:opacity .2s}
.hlink:hover{opacity:.8}
.hlink-md{background:rgba(102,126,234,.15);color:var(--acc2)}
.hlink-json{background:rgba(118,75,162,.15);color:var(--acc3)}
.hlink-ml{background:rgba(244,165,50,.15);color:var(--acc)}
.hist-empty{text-align:center;color:var(--ts);padding:32px;font-size:.92em}
@media(max-width:768px){
  .hist-card{flex-direction:column;align-items:flex-start;gap:12px}
  .hist-right{justify-content:flex-start}
}
/* ── ACCURACY DASHBOARD ── */
#accuracy{margin:32px 0}
.acc-section-title{font-size:1.15em;font-weight:700;color:var(--tp);margin-bottom:16px;padding-bottom:6px;border-bottom:2px solid rgba(102,126,234,.2)}
.acc-kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.acc-kpi{background:var(--card);border-radius:12px;padding:16px 12px;text-align:center;border:1px solid rgba(102,126,234,.12)}
.acc-kpi .kv{font-size:1.8em;font-weight:800;color:var(--tp)}
.acc-kpi .kl{font-size:.75em;color:var(--ts);margin-top:2px}
.acc-two-col{display:grid;grid-template-columns:1fr 1.6fr;gap:16px;margin-bottom:16px}
.acc-dir-box,.acc-ticker-box{background:var(--card);border-radius:12px;padding:18px;border:1px solid rgba(102,126,234,.12)}
.acc-box-title{font-size:.85em;font-weight:600;color:var(--ts);margin-bottom:12px}
.acc-canvas-wrap{position:relative;height:140px}
.acc-table{width:100%;border-collapse:collapse;font-size:.82em}
.acc-table th{text-align:left;padding:6px 8px;color:var(--ts);font-weight:600;border-bottom:1px solid rgba(0,0,0,.08);cursor:pointer;user-select:none}
.acc-table th::after{content:' ↕';opacity:.25;font-size:.7em}
.acc-table th[data-sort="asc"]::after{content:' ↑';opacity:.8}
.acc-table th[data-sort="desc"]::after{content:' ↓';opacity:.8}
.acc-table td{padding:5px 8px;border-bottom:1px solid rgba(0,0,0,.05)}
.acc-table tr:last-child td{border-bottom:none}
.acc-pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:.75em;font-weight:600}
.pill-green{background:#dcfce7;color:#15803d}
.pill-red{background:#fee2e2;color:#b91c1c}
.pill-gray{background:#f1f5f9;color:#64748b}
.acc-cold{text-align:center;padding:40px 20px;color:var(--ts)}
.acc-cold .cold-icon{font-size:2.5em;margin-bottom:8px}
.acc-cold .cold-msg{font-size:.9em;line-height:1.6}
.thesis-break-box{margin-top:10px;padding:10px;border-radius:8px;
  background:rgba(239,68,68,.06);border:1px solid rgba(239,68,68,.18)}
.tb-title{font-size:.78em;font-weight:700;color:#dc2626;margin-bottom:6px}
.tb-level{font-size:.72em;font-weight:600;padding:2px 6px;border-radius:4px;
  display:inline-block;margin-bottom:4px}
.tb-l1{background:rgba(245,158,11,.15);color:#b45309}
.tb-l2{background:rgba(239,68,68,.15);color:#b91c1c}
.tb-list{padding-left:14px;margin:0 0 6px}
.tb-list li{font-size:.74em;color:var(--ts);margin-bottom:2px;line-height:1.4}
.rss-badge{display:inline-block;padding:2px 7px;border-radius:5px;font-size:.72em;
  font-weight:700;background:rgba(239,68,68,.12);color:#b91c1c;margin-left:6px}
/* DIMENSION MINI-BARS */
.dim-bars{display:flex;gap:3px;align-items:flex-end;height:38px;margin:8px 0 4px;padding:0 2px}
.dim-b-item{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;height:100%}
.dim-b{width:100%;min-height:3px;border-radius:2px 2px 0 0;transition:height .4s}
.dim-lbl{font-size:.58em;color:var(--ts);line-height:1;text-align:center}
/* TABLE SORT */
.full-table th{cursor:pointer;user-select:none}
.full-table th::after{content:' ↕';font-size:.7em;opacity:.25}
.full-table th[data-sort="asc"]::after{content:' ↑';opacity:.8}
.full-table th[data-sort="desc"]::after{content:' ↓';opacity:.8}
/* DIM DATA QUALITY BARS (#3) */
.dim-dq-row{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px;padding:6px 0 2px;
  border-top:1px solid rgba(0,0,0,.06)}
.dq-item{display:flex;align-items:center;gap:3px;flex:1 0 30%}
.dq-lbl{font-size:.65em;color:#888;min-width:22px}
.dq-bar{flex:1;height:5px;border-radius:3px;background:#eee;overflow:hidden}
.dq-fill{height:100%;border-radius:3px;transition:width .4s}
.dq-val{font-size:.65em;color:#666;min-width:28px;text-align:right}
"""

    def __init__(self):
        self.report_dir = PATHS.home
        self.timestamp = datetime.now()
        self.date_str = self.timestamp.strftime("%Y-%m-%d")

        # 初始化报告生成器
        self.ml_generator = MLEnhancedReportGenerator()

        # 初始化 Agent 工具集（新增）
        self.agent_helper = AgentHelper()

        # Phase 2: 初始化持久化记忆存储
        self.memory_store = None
        self._session_id = None
        if MemoryStore:
            try:
                self.memory_store = MemoryStore()
                self._session_id = self.memory_store.generate_session_id(run_mode="daily_scan")
            except (OSError, ValueError, RuntimeError) as e:
                _log.warning("MemoryStore 初始化失败，继续运行: %s", e)

        # 结果存储
        self.opportunities: List[OpportunityItem] = []
        self.observations: List[Dict] = []
        self.risks: List[Dict] = []

        # 线程安全锁（用于并行执行时保护共享数据）
        self._results_lock = Lock()

        # Phase 3 P2: 初始化 Google Calendar 集成（失败时降级）
        self.calendar = None
        if CalendarIntegrator:
            try:
                self.calendar = CalendarIntegrator()
            except (OSError, ValueError, RuntimeError) as e:
                _log.warning("Calendar 初始化失败: %s", e)

        # Phase 3 P4: 初始化代码执行 Agent（失败时降级）
        self.code_executor_agent = None
        if CodeExecutorAgent and CODE_EXECUTION_CONFIG.get("enabled"):
            try:
                self.code_executor_agent = CodeExecutorAgent(board=None)
                # board 在 run_swarm_scan 时注入
            except (OSError, ValueError, RuntimeError, TypeError) as e:
                _log.warning("CodeExecutorAgent 初始化失败: %s", e)

        # Phase 3 内存优化: 初始化向量记忆层（Chroma 长期记忆）
        self.vector_memory = None
        if VectorMemory and VECTOR_MEMORY_CONFIG.get("enabled"):
            try:
                self.vector_memory = VectorMemory(
                    db_path=VECTOR_MEMORY_CONFIG.get("db_path"),
                    retention_days=VECTOR_MEMORY_CONFIG.get("retention_days", 90)
                )
                if self.vector_memory.enabled:
                    if VECTOR_MEMORY_CONFIG.get("cleanup_on_startup"):
                        self.vector_memory.cleanup()
            except (ImportError, OSError, ValueError, RuntimeError) as e:
                _log.warning("向量记忆初始化失败: %s", e)

        # Week 4: 指标收集器
        self.metrics = None
        if MetricsCollector:
            try:
                self.metrics = MetricsCollector()
            except (OSError, ValueError, RuntimeError) as e:
                _log.warning("MetricsCollector 初始化失败: %s", e)

        # Phase 2: 共享线程池（替代所有 daemon 线程，退出时等待完成）
        import atexit
        self._bg_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="hive_bg")
        self._bg_futures = []
        atexit.register(self._shutdown_bg)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._shutdown_bg()

        # 财报自动监控器
        self.earnings_watcher = None
        if EarningsWatcher:
            try:
                self.earnings_watcher = EarningsWatcher()
            except (OSError, ValueError, RuntimeError) as e:
                _log.warning("EarningsWatcher 初始化失败: %s", e)

        # Phase 3 P6: 初始化 Slack 报告通知器（替代 Gmail）
        self.slack_notifier = None
        if SlackReportNotifier:
            try:
                self.slack_notifier = SlackReportNotifier()
            except (OSError, ValueError, RuntimeError, ConnectionError) as e:
                _log.warning("Slack 通知器初始化失败: %s", e)

    def _shutdown_bg(self) -> None:
        """atexit 处理器：等待后台任务完成"""
        from concurrent.futures import TimeoutError as FuturesTimeout, CancelledError
        for f in self._bg_futures:
            try:
                f.result(timeout=10)
            except (FuturesTimeout, CancelledError, OSError, RuntimeError) as e:
                _log.debug("Background task cleanup: %s", e)
        self._bg_executor.shutdown(wait=True)

    def _submit_bg(self, fn, *args) -> None:
        """提交后台任务到共享线程池（替代 daemon 线程）"""
        future = self._bg_executor.submit(fn, *args)
        self._bg_futures.append(future)
        # 清理已完成的 futures（防止内存泄漏）
        self._bg_futures = [f for f in self._bg_futures if not f.done()]

    def _analyze_ticker_safe(self, ticker: str, index: int, total: int) -> Tuple[str, OpportunityItem, str]:
        """
        分析单个标的（线程安全，可在并行上下文中调用）

        Args:
            ticker: 股票代码
            index: 当前索引（用于显示进度）
            total: 总数（用于显示进度）

        Returns:
            (ticker, opportunity_item_or_none, error_message_or_none)
        """
        try:
            # 构建最小化的实时数据结构
            realtime_metrics = {
                "ticker": ticker,
                "sources": {
                    "yahoo_finance": {
                        "current_price": 100.0,
                        "change_pct": 2.5
                    }
                }
            }

            # 生成 ML 增强报告
            ml_report = self.ml_generator.generate_ml_enhanced_report(
                ticker, realtime_metrics
            )

            # 解析为 OpportunityItem
            opportunity = self._parse_ml_report_to_opportunity(ticker, ml_report)

            # 线程安全地添加到结果列表
            with self._results_lock:
                self.opportunities.append(opportunity)

            return ticker, opportunity, None

        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            _log.error("Ticker analysis failed for %s: %s", ticker, e, exc_info=True)
            error_msg = str(e)
            # 线程安全地添加观察项
            with self._results_lock:
                self.observations.append({
                    "ticker": ticker,
                    "status": "error",
                    "error": error_msg
                })
            return ticker, None, error_msg

    def run_daily_scan(self, focus_tickers: List[str] = None) -> Dict:
        """
        执行每日扫描（并行版本）

        Args:
            focus_tickers: 重点关注标的（如为None则扫描全部watchlist）

        Returns:
            完整的日报数据结构
        """
        _log.info("Alpha Hive 日报 %s", self.date_str)

        targets = focus_tickers or list(WATCHLIST.keys())[:10]
        _log.info("标的：%s", " ".join(targets))

        start_parallel = time.time()

        with ThreadPoolExecutor(max_workers=len(targets)) as executor:
            futures = [
                executor.submit(self._analyze_ticker_safe, ticker, i + 1, len(targets))
                for i, ticker in enumerate(targets)
            ]

            for i, future in enumerate(futures, 1):
                ticker, opportunity, error = future.result()
                if error:
                    _log.warning("[%d/%d] %s 分析失败: %s", i, len(targets), ticker, error[:60])
                else:
                    _log.info("[%d/%d] %s: %.1f/10", i, len(targets), ticker, opportunity.opportunity_score)

        elapsed_parallel = time.time() - start_parallel
        _log.info("分析耗时：%.1fs", elapsed_parallel)

        # 排序机会
        self.opportunities.sort(key=lambda x: x.opportunity_score, reverse=True)

        # 构建报告
        report = self._build_report()

        # Phase 2: 异步保存会话（使用共享线程池，退出时等待完成）
        if self.memory_store and self._session_id:
            self._submit_bg(
                self.memory_store.save_session,
                self._session_id, self.date_str, "daily_scan",
                targets, {}, [], elapsed_parallel
            )

        return report

    def run_swarm_scan(self, focus_tickers: List[str] = None, progress_callback=None) -> Dict:
        """
        真正的蜂群协作扫描 - 7 个自治工蜂并行运行（6 核心 + BearBeeContrarian），实时通过信息素板交换发现

        Args:
            focus_tickers: 重点关注标的（如为None则扫描全部watchlist）

        Returns:
            完整的蜂群分析报告
        """
        # Week 4: 设置 correlation_id 追踪本次扫描
        set_correlation_id(self._session_id or f"swarm_{self.date_str}")
        _log.info("蜂群协作启动 %s", self.date_str)
        # 汇报可选模块降级状态
        try:
            from hive_logger import FeatureRegistry
            FeatureRegistry.log_status()
        except ImportError:
            pass

        targets = focus_tickers or list(WATCHLIST.keys())[:10]
        _log.info("标的：%s", " ".join(targets))

        start_time = time.time()

        # 创建共享的信息素板
        board = PheromoneBoard(memory_store=self.memory_store, session_id=self._session_id)

        # 实例化 Agent：第一阶段 6 个核心 Agent（可选+CodeExecutor），第二阶段 BearBeeContrarian（读取信息素板后分析）
        retriever = self.vector_memory if (self.vector_memory and self.vector_memory.enabled) else None
        phase1_agents = [
            ScoutBeeNova(board, retriever=retriever),
            OracleBeeEcho(board, retriever=retriever),
            BuzzBeeWhisper(board, retriever=retriever),
            ChronosBeeHorizon(board, retriever=retriever),
            RivalBeeVanguard(board, retriever=retriever),
            GuardBeeSentinel(board, retriever=retriever),
        ]
        # 看空对冲蜂：二阶段执行（等其他 Agent 写入信息素板后再分析）
        bear_agent = BearBeeContrarian(board, retriever=retriever)

        # Phase 3 P4: 动态注入 CodeExecutorAgent
        if self.code_executor_agent and CODE_EXECUTION_CONFIG.get("add_to_swarm"):
            self.code_executor_agent.board = board
            phase1_agents.append(self.code_executor_agent)

        # Phase 6: 自适应权重
        adapted_w = Backtester.load_adapted_weights() if Backtester else None
        queen = QueenDistiller(board, adapted_weights=adapted_w)

        all_agents = phase1_agents + [bear_agent]
        _log.info("%d Agent（含二阶段看空蜂）| 预取数据中...", len(all_agents))

        # ⚡ 优化 #1+#2: 批量预取 yfinance + VectorMemory（每 ticker 仅 1 次）
        prefetched = prefetch_shared_data(targets, retriever)
        inject_prefetched(all_agents, prefetched)
        prefetch_elapsed = time.time() - start_time
        _log.info("预取完成 (%.1fs) | 开始并行分析", prefetch_elapsed)

        # ⚡ 优化 #3: 单层线程池，按 ticker 串行、Agent 并行
        swarm_results = {}

        # Phase 2: 崩溃恢复 checkpoint
        checkpoint_file = self.report_dir / f".checkpoint_{self._session_id or 'default'}.json"
        completed_tickers = set()
        if checkpoint_file.exists():
            try:
                with open(checkpoint_file, "r") as f:
                    ckpt = json.load(f)
                    swarm_results = ckpt.get("results", {})
                    completed_tickers = set(swarm_results.keys())
                    if completed_tickers:
                        _log.info("恢复 checkpoint：%d 标的已完成", len(completed_tickers))
            except (json.JSONDecodeError, KeyError, OSError) as e:
                _log.warning("Checkpoint 恢复失败，重新开始: %s", e)

        for idx, ticker in enumerate(targets, 1):
            if ticker in completed_tickers:
                res = "✅" if swarm_results[ticker]["resonance"]["resonance_detected"] else "—"
                _log.info("[%d/%d] %s: %.1f/10 (已缓存) %s", idx, len(targets), ticker, swarm_results[ticker]['final_score'], res)
                continue

            # ── #18: Ticker 有效性检测（退市/停牌/拆股）──
            try:
                from swarm_agents import check_ticker_validity
                _validity = check_ticker_validity(ticker)
                if not _validity["valid"]:
                    _log.warning("[%d/%d] ⏭️ 跳过 %s（%s）", idx, len(targets), ticker, _validity["warning"])
                    continue
                if _validity.get("warning"):
                    _log.warning("[%d/%d] ⚠️ %s 异常：%s", idx, len(targets), ticker, _validity["warning"])
            except Exception as _ve:
                _log.debug("ticker validity check error for %s: %s", ticker, _ve)

            # 第一阶段：6 个核心 Agent 并行分析（含可选 CodeExecutorAgent）
            with ThreadPoolExecutor(max_workers=len(phase1_agents)) as executor:
                futures = {executor.submit(agent.analyze, ticker): agent for agent in phase1_agents}
                agent_results = []
                for future in as_completed(futures):
                    try:
                        agent_results.append(future.result(timeout=60))
                    except (TimeoutError, ValueError, KeyError, TypeError, RuntimeError) as e:
                        _log.warning("Agent future failed: %s", e)
                        agent_results.append(None)

            # 第二阶段：BearBeeContrarian 读取信息素板后分析（此时其他 Agent 数据已可用）
            try:
                bear_result = bear_agent.analyze(ticker)
                agent_results.append(bear_result)
                _log.info("  🐻 看空蜂: %s %s (%.1f分, %d信号)",
                          ticker, bear_result.get("direction", "?"),
                          bear_result.get("details", {}).get("bear_score", 0),
                          len(bear_result.get("details", {}).get("bearish_signals", [])))
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                _log.warning("BearBeeContrarian failed for %s: %s", ticker, e)
                agent_results.append(None)

            distilled = queen.distill(ticker, agent_results)
            swarm_results[ticker] = distilled

            res = "✅" if distilled["resonance"]["resonance_detected"] else "—"
            _log.info("[%d/%d] %s: %.1f/10 %s %s", idx, len(targets), ticker, distilled['final_score'], distilled['direction'], res)

            # 进度回调（供桌面 App 实时动画使用）
            if progress_callback:
                try:
                    progress_callback(idx, len(targets), ticker, distilled)
                except Exception as _cb_err:
                    _log.debug("Progress callback error: %s", _cb_err)

            # 写入 checkpoint（每个 ticker 完成后）
            try:
                with open(checkpoint_file, "w") as f:
                    json.dump({"results": swarm_results, "targets": targets}, f, cls=SafeJSONEncoder)
            except (OSError, TypeError) as e:
                _log.warning("Checkpoint 写入失败: %s", e)

        # 扫描完成，保存蜂群结果（合并当日已有结果，支持分批运行）
        try:
            swarm_json = self.report_dir / f".swarm_results_{self.date_str}.json"
            merged_swarm = {}
            if swarm_json.exists():
                try:
                    with open(swarm_json, encoding="utf-8") as _f:
                        merged_swarm = json.load(_f)
                except (OSError, json.JSONDecodeError):
                    pass
            merged_swarm.update(swarm_results)  # 新批次覆盖同名标的
            with open(swarm_json, "w") as f:
                json.dump(merged_swarm, f, cls=SafeJSONEncoder, ensure_ascii=False)
        except (OSError, TypeError) as e:
            _log.warning("Swarm results 保存失败: %s", e)
        # 清理 checkpoint
        try:
            checkpoint_file.unlink(missing_ok=True)
        except OSError as e:
            _log.debug("Checkpoint 清理失败: %s", e)

        elapsed = time.time() - start_time

        # LLM Token 使用统计
        try:
            import llm_service
            usage = llm_service.get_usage()
            if usage["call_count"] > 0:
                _log.info("蜂群耗时：%.1fs | LLM: %d调用 $%.4f", elapsed, usage['call_count'], usage['total_cost_usd'])
            else:
                _log.info("蜂群耗时：%.1fs | 规则引擎模式", elapsed)
        except (ImportError, AttributeError, KeyError) as e:
            _log.info("蜂群耗时：%.1fs (LLM stats unavailable: %s)", elapsed, e)

        # Week 4: 记录扫描指标 + SLO 检查
        if self.metrics:
            try:
                scores = [d.get("final_score", 5.0) for d in swarm_results.values()]
                agent_errors = sum(
                    1 for d in swarm_results.values()
                    if d.get("supporting_agents", 0) == 0
                )
                resonance_n = sum(
                    1 for d in swarm_results.values()
                    if d.get("resonance", {}).get("resonance_detected")
                )
                avg_real = (
                    sum(d.get("data_real_pct", 0) for d in swarm_results.values()) / len(swarm_results)
                    if swarm_results else 0
                )
                llm_c, llm_cost = 0, 0.0
                try:
                    import llm_service as _ls
                    _u = _ls.get_usage()
                    llm_c, llm_cost = _u.get("call_count", 0), _u.get("total_cost_usd", 0.0)
                except (ImportError, AttributeError, KeyError):
                    pass

                self.metrics.record_scan(
                    ticker_count=len(swarm_results),
                    duration_seconds=elapsed,
                    agent_count=len(all_agents),
                    prefetch_seconds=prefetch_elapsed,
                    avg_score=sum(scores) / len(scores) if scores else 5.0,
                    max_score=max(scores) if scores else 5.0,
                    min_score=min(scores) if scores else 5.0,
                    agent_errors=agent_errors,
                    agent_total=len(swarm_results) * len(all_agents),
                    data_real_pct=avg_real,
                    resonance_count=resonance_n,
                    llm_calls=llm_c,
                    llm_cost_usd=llm_cost,
                    session_id=self._session_id or "",
                    scan_mode="swarm",
                )
                for ticker, data in swarm_results.items():
                    self.metrics.record_ticker(
                        ticker=ticker,
                        final_score=data.get("final_score", 5.0),
                        direction=data.get("direction", "neutral"),
                        supporting_agents=data.get("supporting_agents", 0),
                        data_real_pct=data.get("data_real_pct", 0),
                        resonance_detected=data.get("resonance", {}).get("resonance_detected", False),
                        session_id=self._session_id or "",
                    )

                # SLO 检查
                violations = self.metrics.check_slo(days=1)
                if violations:
                    _log.warning("SLO 违规 %d 条: %s",
                                 len(violations),
                                 "; ".join(v["details"] for v in violations))
            except (OSError, ValueError, KeyError, TypeError) as e:
                _log.warning("指标收集异常: %s", e)

        # Phase 6: 回测反馈循环
        if Backtester:
            try:
                bt = Backtester()
                bt.save_predictions(swarm_results)
                bt.run_backtest()
                # 优先 T+7（更可靠，需 10 条样本），不足时降级到 T+1（需 5 条）
                adapted = bt.adapt_weights(min_samples=10, period="t7")
                if adapted is None:
                    adapted = bt.adapt_weights(min_samples=5, period="t1")
                    if adapted:
                        _log.info("自适应权重：T+7 样本不足，使用 T+1 数据（保守调整）")
            except (OSError, ValueError, KeyError, TypeError) as e:
                _log.warning("回测异常: %s", e)

        # Phase 6: Slack 推送高分机会 + 异常信号
        if self.slack_notifier and self.slack_notifier.enabled:
            for ticker, data in swarm_results.items():
                score = data.get("final_score", 0)
                direction = data.get("direction", "neutral")
                dir_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(direction, direction)

                # NA2：调整说明（附加到 Slack 推送）
                adj_note = self._format_score_adjustments(data)
                details_list = [f"评分 {score:.1f}/10"]
                if adj_note:
                    details_list.append(adj_note)
                cov = data.get("dimension_coverage_pct", 100.0)
                if cov < 100.0:
                    details_list.append(f"维度覆盖 {cov:.0f}%")

                # 高分机会推送（>= 7.5）
                if score >= 7.5:
                    self._submit_bg(
                        self.slack_notifier.send_opportunity_alert,
                        ticker, score, dir_cn,
                        data.get("discovery", "高分机会"),
                        details_list
                    )

                # 异常信号推送：强看空 或 内幕大额交易
                elif score <= 3.0:
                    self._submit_bg(
                        self.slack_notifier.send_risk_alert,
                        f"{ticker} 低分预警",
                        f"蜂群评分仅 {score:.1f}/10，方向 {dir_cn}" + (f" | {adj_note}" if adj_note else ""),
                        "HIGH"
                    )

        # 生成综合报告
        report = self._build_swarm_report(swarm_results, board, agent_count=len(all_agents))

        # ── 失效条件快照（仅有配置的标的：NVDA/VKTX/TSLA）──
        try:
            from thesis_breaks import ThesisBreakConfig
            for _opp in report.get("opportunities", []):
                _tk = _opp.get("ticker", "")
                _tb_cfg = ThesisBreakConfig.get_breaks_config(_tk)
                if _tb_cfg:
                    _l1 = [c["metric"] + "：" + c["trigger"]
                           for c in _tb_cfg.get("level_1_warning", {}).get("conditions", [])]
                    _l2 = [c["metric"] + "：" + c["trigger"]
                           for c in _tb_cfg.get("level_2_stop_loss", {}).get("conditions", [])]
                    _opp["thesis_break_l1"] = _l1
                    _opp["thesis_break_l2"] = _l2
                    # 同步写入 swarm_results 供 .swarm_results_*.json 持久化
                    if _tk in swarm_results:
                        swarm_results[_tk]["thesis_break_l1"] = _l1
                        swarm_results[_tk]["thesis_break_l2"] = _l2
        except Exception as _tbe:
            _log.warning("thesis_break 配置加载失败: %s", _tbe)

        # Phase 3 P2: 为高分机会添加日历提醒（后台线程池，退出时等待完成）
        if self.calendar and report.get('opportunities'):
            for opp in report['opportunities']:
                if opp.opportunity_score >= 7.5:
                    self._submit_bg(
                        self.calendar.add_opportunity_reminder,
                        opp.ticker, opp.opportunity_score, opp.direction,
                        f"{opp.key_catalysts[0] if opp.key_catalysts else '高分机会'}"
                    )

        # Phase 2: 异步保存会话（使用共享线程池，退出时等待完成）
        if self.memory_store and self._session_id:
            snapshot = board.compact_snapshot()  # 在主线程取快照（线程安全）
            self._submit_bg(
                self.memory_store.save_session,
                self._session_id, self.date_str, "swarm",
                targets, swarm_results, snapshot, elapsed
            )

        # Phase 3 内存优化: 将高价值发现存入向量记忆（长期记忆）
        if self.vector_memory and self.vector_memory.enabled:
            stored = 0
            # 1. 存储 Queen 的最终评分
            for ticker, data in swarm_results.items():
                if data.get("final_score", 0) >= 5.0:
                    self.vector_memory.store(
                        ticker=ticker,
                        agent_id="QueenDistiller",
                        discovery=f"评分{data['final_score']:.1f} {data['direction']} "
                                  f"支持{data.get('supporting_agents', 0)}Agent",
                        direction=data["direction"],
                        score=data["final_score"],
                        source="swarm_scan",
                        session_id=self._session_id or ""
                    )
                    stored += 1
            # 2. 存储信息素板上每个 Agent 的高价值发现
            for entry in board.snapshot():
                if entry.get("self_score", 0) >= 6.0:
                    self.vector_memory.store(
                        ticker=entry.get("ticker", ""),
                        agent_id=entry.get("agent_id", ""),
                        discovery=entry.get("discovery", "")[:300],
                        direction=entry.get("direction", "neutral"),
                        score=entry.get("self_score", 5.0),
                        source=entry.get("source", ""),
                        session_id=self._session_id or ""
                    )
                    stored += 1
            if stored > 0:
                _log.info("已存入 %d 条长期记忆 (Chroma)", stored)

        return report

    def run_crew_scan(self, focus_tickers: List[str] = None) -> Dict:
        """
        CrewAI 模式蜂群扫描 - 使用 Process.hierarchical 主-子 Agent 递归调度
        若 crewai 未安装，自动降级到 run_swarm_scan()

        Args:
            focus_tickers: 重点关注标的（如为None则扫描全部watchlist）

        Returns:
            完整的蜂群分析报告
        """
        # 检查 CrewAI 是否可用
        if not AlphaHiveCrew or not CREWAI_CONFIG.get("enabled"):
            _log.info("CrewAI 未安装或未启用，降级到标准蜂群模式")
            return self.run_swarm_scan(focus_tickers)

        _log.info("CrewAI 模式 %s", self.date_str)

        targets = focus_tickers or list(WATCHLIST.keys())[:10]
        _log.info("标的：%s", " ".join(targets))

        # 创建共享的信息素板
        board = PheromoneBoard(memory_store=self.memory_store, session_id=self._session_id)

        # 构建 CrewAI Crew
        crew = AlphaHiveCrew(board=board, memory_store=self.memory_store)
        crew.build(targets)

        _log.info("CrewAI %d Agent", crew.get_agents_count())

        swarm_results = {}
        start_time = time.time()

        # 使用 CrewAI 分析每个标的
        for i, ticker in enumerate(targets, 1):
            _log.info("[%d/%d] CrewAI 分析 %s", i, len(targets), ticker)

            try:
                result = crew.analyze(ticker)
                swarm_results[ticker] = result

                _log.info("  %s: %.1f/10 %s", ticker, result.get('final_score', 0), result.get('direction', 'neutral'))

            except (ValueError, KeyError, TypeError, RuntimeError, ConnectionError) as e:
                _log.warning("  %s CrewAI 分析失败: %s", ticker, str(e)[:80])
                swarm_results[ticker] = {
                    "ticker": ticker,
                    "final_score": 0.0,
                    "direction": "neutral",
                    "discovery": f"CrewAI 分析失败: {str(e)}",
                    "error": str(e)
                }

        elapsed = time.time() - start_time
        _log.info("CrewAI 耗时：%.1fs", elapsed)

        # 转换为标准报告格式（兼容 run_swarm_scan 输出）
        # CrewAI 模式：6 核心 BeeAgent + BearBeeContrarian = 7
        report = self._build_swarm_report(swarm_results, board, agent_count=7)

        # 异步保存会话（使用共享线程池，退出时等待完成）
        if self.memory_store and self._session_id:
            snapshot = board.compact_snapshot()
            self._submit_bg(
                self.memory_store.save_session,
                self._session_id, self.date_str, "crew_scan",
                targets, swarm_results, snapshot, elapsed
            )

        return report

    def _build_swarm_report(self, swarm_results: Dict, board: PheromoneBoard,
                            agent_count: int = 7) -> Dict:
        """
        将蜂群分析结果转换为标准报告格式

        Args:
            swarm_results: QueenDistiller 的所有汇总结果
            board: 信息素板（用于提取全局信息）
            agent_count: 实际运行的 Agent 总数（Phase-1 + BearBeeContrarian + 可选 CodeExecutor）

        Returns:
            标准报告格式
        """
        # 排序结果
        sorted_results = sorted(
            swarm_results.items(),
            key=lambda x: x[1]["final_score"],
            reverse=True
        )

        # 构建 OpportunityItem 列表（兼容现有报告格式）
        opportunities = []
        for ticker, swarm_data in sorted_results:
            opp = OpportunityItem(
                ticker=ticker,
                direction="看多" if swarm_data["direction"] == "bullish" else (
                    "看空" if swarm_data["direction"] == "bearish" else "中性"
                ),
                signal_score=swarm_data["final_score"],
                catalyst_score=swarm_data["final_score"] * 0.9,
                sentiment_score=swarm_data["final_score"] * 0.85,
                odds_score=swarm_data["final_score"] * 0.8,
                risk_score=swarm_data["final_score"] * 0.95,
                options_score=swarm_data["final_score"] * 0.88,
                opportunity_score=swarm_data["final_score"],
                confidence=min(95, swarm_data["final_score"] * 10) if swarm_data["final_score"] >= 7.5 else 60,
                key_catalysts=["多 Agent 共振信号"] if swarm_data["resonance"]["resonance_detected"] else ["待验证"],
                options_signal=f"共振信号 ({swarm_data['resonance']['supporting_agents']} Agent)",
                risks=["多头拥挤"] if swarm_data["resonance"]["resonance_detected"] else [],
                thesis_break="信号分散"
            )
            opportunities.append(opp)

        self.opportunities = opportunities

        # ── P4: 投资组合集中度分析（板块重叠 + 相关性矩阵）──
        concentration = {}
        try:
            from portfolio_concentration import analyze_concentration
            from config import WATCHLIST
            concentration = analyze_concentration(swarm_results, WATCHLIST)
            _log.info("P4 集中度分析：%s（风险=%s）",
                      concentration.get("summary", ""), concentration.get("concentration_risk", ""))
        except (ImportError, ValueError, KeyError, TypeError, AttributeError) as e:
            _log.debug("P4 portfolio_concentration 不可用: %s", e)

        # ── P5: 宏观环境快照（附加到报告元数据）──
        macro_snapshot = {}
        try:
            from fred_macro import get_macro_context
            macro_snapshot = get_macro_context()
            _log.info("P5 宏观环境：%s", macro_snapshot.get("summary", ""))
        except (ImportError, ConnectionError, TimeoutError, ValueError, KeyError) as e:
            _log.debug("P5 fred_macro 不可用: %s", e)

        # ── P3: 获取回测准确率统计（附加到报告）──
        backtest_stats = {}
        try:
            if Backtester:
                _bt = Backtester()
                backtest_stats = _bt.store.get_accuracy_stats("t7", days=30)
        except (OSError, ValueError, KeyError, TypeError) as e:
            _log.debug("Backtest stats unavailable: %s", e)

        # 构建标准报告
        report = {
            "date": self.date_str,
            "timestamp": self.timestamp.isoformat(),
            "system_status": "✅ 蜂群协作完成",
            "phase_completed": "完整蜂群流程 (Swarm Mode)",
            "swarm_metadata": {
                "total_agents": agent_count,
                "tickers_analyzed": len(swarm_results),
                "resonances_detected": sum(1 for r in swarm_results.values() if r["resonance"]["resonance_detected"]),
                "pheromone_board_entries": board.get_entry_count()
            },
            "concentration_analysis": concentration,
            "macro_context": macro_snapshot,
            "backtest_stats": backtest_stats,
            "markdown_report": self._generate_swarm_markdown_report(swarm_results, concentration, macro_snapshot, backtest_stats, agent_count=agent_count),
            "twitter_threads": self._generate_swarm_twitter_threads(swarm_results),
            "opportunities": [
                {
                    "rank": i + 1,
                    "ticker": opp.ticker,
                    "direction": opp.direction,
                    "opp_score": round(opp.opportunity_score, 1),
                    "confidence": f"{opp.confidence:.0f}%",
                    "resonance": swarm_results[opp.ticker]["resonance"]["resonance_detected"],
                    "supporting_agents": swarm_results[opp.ticker]["supporting_agents"],
                    "thesis_break": opp.thesis_break
                }
                for i, opp in enumerate(self.opportunities[:5])
            ]
        }

        return report

    @staticmethod
    def _format_score_adjustments(data: Dict) -> str:
        """
        NA2：将 distill() 返回的调整字段格式化为人类可读注释。
        返回空字符串表示无调整发生。

        示例输出：
          "⚠️ 反对蜂看空 8.5 → 封顶 9.25 | ⚠️ 数据质量 60% (×0.875) | 🤖 LLM蒸馏(0.8) 基础分8.3 | ❌ 维度覆盖64%"
        """
        parts = []

        # BearBee 封顶
        if data.get("bear_cap_applied"):
            bs = data.get("bear_strength", 0.0)
            rs = data.get("rule_score", data.get("final_score", 0.0))
            parts.append(f"⚠️ 反对蜂看空强度{bs:.1f} → 封顶{rs:.2f}")

        # GuardBee 风险折扣
        if data.get("guard_penalty_applied"):
            gp = data.get("guard_penalty", 0.0)
            parts.append(f"🛡️ 风控折扣-{gp:.2f}")

        # 数据质量折扣
        if data.get("dq_penalty_applied"):
            rp = data.get("data_real_pct", 0.0)
            qf = data.get("dq_quality_factor", 1.0)
            parts.append(f"⚠️ 数据质量{rp:.0f}%(×{qf:.3f})")

        # LLM 蒸馏
        if data.get("distill_mode") == "llm_enhanced":
            lc = data.get("llm_confidence", 0.0)
            rs = data.get("rule_score", data.get("final_score", 0.0))
            parts.append(f"🤖 LLM蒸馏(置信{lc:.1f}) 基础分{rs:.1f}")

        # 维度覆盖率不足
        cov = data.get("dimension_coverage_pct", 100.0)
        if cov < 80.0:
            dim_status = data.get("dimension_status", {})
            missing = (
                [dim for dim, st in dim_status.items() if st != "present"]
                if isinstance(dim_status, dict) else []
            )
            missing_str = "/".join(missing) if missing else ""
            parts.append(f"❌ 维度覆盖{cov:.0f}%({missing_str})")

        return " | ".join(parts)

    def _generate_swarm_markdown_report(self, swarm_results: Dict,
                                         concentration: Dict = None,
                                         macro_context: Dict = None,
                                         backtest_stats: Dict = None,
                                         agent_count: int = 7) -> str:
        """生成蜂群模式的 Markdown 报告（8 版块 + P4集中度 + P5宏观 + P3回测）"""

        md = []
        md.append(f"# 【{self.date_str}】Alpha Hive 蜂群协作日报")
        md.append("")
        md.append(f"**自动生成于**：{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        md.append(f"**系统模式**：完全去中心化蜂群协作 | {agent_count} 个自治工蜂（6 核心 + BearBeeContrarian）")
        md.append("")

        sorted_results = sorted(
            swarm_results.items(),
            key=lambda x: x[1]["final_score"],
            reverse=True
        )

        # ====== 版块 1：今日摘要 ======
        resonances = sum(1 for r in swarm_results.values() if r["resonance"]["resonance_detected"])
        md.append("## 1) 今日摘要")
        md.append("")
        md.append(f"- 扫描标的：{len(swarm_results)} 个 | 共振信号：{resonances}/{len(swarm_results)}")
        for i, (ticker, data) in enumerate(sorted_results[:3], 1):
            res = "共振" if data["resonance"]["resonance_detected"] else ""
            md.append(f"- **{ticker}** {data['direction'].upper()} {data['final_score']:.1f}/10 {res}")
        md.append("")

        # ====== 版块 2：今日聪明钱动向（ScoutBeeNova） ======
        md.append("## 2) 今日聪明钱动向")
        md.append("")
        for ticker, data in sorted_results:
            agent = data.get("agent_details", {}).get("ScoutBeeNova", {})
            discovery = agent.get("discovery", "")
            details = agent.get("details", {})
            insider = details.get("insider", {})
            md.append(f"### {ticker}")
            if discovery:
                md.append(f"- {discovery}")
            if insider:
                sentiment = insider.get("sentiment", "unknown")
                bought = insider.get("dollar_bought", 0)
                sold = insider.get("dollar_sold", 0)
                filings = insider.get("filings", 0)
                md.append(f"- 内幕交易情绪：**{sentiment}** | 申报数：{filings}")
                if bought > 0:
                    md.append(f"- 内幕买入金额：${bought:,.0f}")
                if sold > 0:
                    md.append(f"- 内幕卖出金额：${sold:,.0f}")
                notable = insider.get("notable_trades", [])
                for t in notable[:2]:
                    if isinstance(t, dict):
                        md.append(f"  - {t.get('insider', '?')}：{t.get('code_desc', '?')} {t.get('shares', 0):,.0f} 股")
            crowding = details.get("crowding_score", "")
            if crowding:
                md.append(f"- 拥挤度：{crowding:.0f}/100")
            md.append("")

        # ====== 版块 3：市场隐含预期（OracleBeeEcho） ======
        md.append("## 3) 市场隐含预期")
        md.append("")
        for ticker, data in sorted_results:
            agent = data.get("agent_details", {}).get("OracleBeeEcho", {})
            discovery = agent.get("discovery", "")
            details = agent.get("details", {})
            md.append(f"### {ticker}")
            if discovery:
                md.append(f"- {discovery}")
            if isinstance(details, dict) and details:
                iv = details.get("iv_rank")
                pc = details.get("put_call_ratio")
                gamma = details.get("gamma_exposure")
                if iv is not None:
                    md.append(f"- IV Rank：{iv}")
                if pc is not None:
                    pc_val = pc if isinstance(pc, (int, float)) else pc
                    md.append(f"- Put/Call Ratio：{pc_val}")
                if gamma is not None:
                    md.append(f"- Gamma Exposure：{gamma}")
                # 异常活动
                unusual = details.get("unusual_activity", [])
                if unusual:
                    md.append(f"- 异常活动：{len(unusual)} 个信号")
                    for u in unusual[:3]:
                        if isinstance(u, dict):
                            utype = u.get("type", "unknown").replace("_", " ")
                            strike = u.get("strike", "")
                            vol = u.get("volume", 0)
                            bull = "看涨" if u.get("bullish") else "看跌"
                            md.append(f"  - {bull} {utype} ${strike} ({vol:,.0f}手)")
                        elif isinstance(u, str):
                            md.append(f"  - {u}")
            md.append("")

        # ====== 版块 4：X 情绪汇总（BuzzBeeWhisper） ======
        md.append("## 4) X 情绪汇总")
        md.append("")
        for ticker, data in sorted_results:
            agent = data.get("agent_details", {}).get("BuzzBeeWhisper", {})
            discovery = agent.get("discovery", "")
            details = agent.get("details", {})
            md.append(f"### {ticker}")
            if discovery:
                md.append(f"- {discovery}")
            if isinstance(details, dict) and details:
                sent_pct = details.get("sentiment_pct")
                mom = details.get("momentum_5d")
                vol = details.get("volume_ratio")
                if sent_pct is not None:
                    md.append(f"- 看多情绪：{sent_pct}%")
                if mom is not None:
                    md.append(f"- 5 日动量：{mom:+.1f}%")
                if vol is not None:
                    md.append(f"- 量比：{vol:.1f}x")
                reddit = details.get("reddit_mentions") or details.get("reddit_rank")
                if reddit:
                    md.append(f"- Reddit 热度：{reddit}")
            md.append("")

        # ====== 版块 5：财报/事件催化剂（ChronosBeeHorizon） ======
        md.append("## 5) 财报/事件催化剂")
        md.append("")
        for ticker, data in sorted_results:
            agent = data.get("agent_details", {}).get("ChronosBeeHorizon", {})
            discovery = agent.get("discovery", "")
            details = agent.get("details", {})
            md.append(f"### {ticker}")
            if discovery:
                md.append(f"- {discovery}")
            if isinstance(details, dict) and details:
                earnings = details.get("next_earnings") or details.get("earnings_date")
                if earnings:
                    md.append(f"- 下次财报：{earnings}")
                events = details.get("upcoming_events") or details.get("catalysts", [])
                if isinstance(events, list):
                    for ev in events[:3]:
                        if isinstance(ev, dict):
                            md.append(f"  - {ev.get('date', '?')}：{ev.get('event', ev.get('description', '?'))}")
                        elif isinstance(ev, str):
                            md.append(f"  - {ev}")
                past = details.get("recent_events", [])
                if isinstance(past, list):
                    for ev in past[:2]:
                        if isinstance(ev, dict):
                            md.append(f"  - [已发生] {ev.get('description', ev)}")
            md.append("")

        # ====== 版块 6：竞争格局分析（RivalBeeVanguard） ======
        md.append("## 6) 竞争格局分析")
        md.append("")
        for ticker, data in sorted_results:
            agent = data.get("agent_details", {}).get("RivalBeeVanguard", {})
            discovery = agent.get("discovery", "")
            details = agent.get("details", {})
            md.append(f"### {ticker}")
            if discovery:
                md.append(f"- {discovery}")
            if isinstance(details, dict) and details:
                ml_pred = details.get("ml_prediction") or details.get("prediction")
                if isinstance(ml_pred, dict):
                    md.append(f"- ML 预测方向：{ml_pred.get('direction', '?')}")
                    md.append(f"- ML 置信度：{ml_pred.get('confidence', '?')}")
                peers = details.get("peer_comparison") or details.get("peers", [])
                if isinstance(peers, list) and peers:
                    md.append(f"- 同业对标：{', '.join(str(p) for p in peers[:5])}")
            md.append("")

        # ====== 版块 6.5：看空对冲观点（BearBeeContrarian） ======
        md.append("## 6.5) 看空对冲观点")
        md.append("")
        md.append("> BearBeeContrarian 专门寻找看空信号，平衡蜂群系统性看多偏差")
        md.append("")
        for ticker, data in sorted_results:
            agent = data.get("agent_details", {}).get("BearBeeContrarian", {})
            discovery = agent.get("discovery", "")
            details = agent.get("details", {})
            bear_score = details.get("bear_score", 0)
            signals = details.get("bearish_signals", [])
            direction = agent.get("direction", "neutral")

            if direction == "bearish":
                severity = "**看空警告**"
            elif direction == "neutral":
                severity = "需关注风险点"
            elif signals:
                severity = "风险提示"
            else:
                severity = "暂无看空信号"

            md.append(f"### {ticker} ({severity} | 看空强度 {bear_score:.1f}/10)")
            if signals:
                for sig in signals:
                    md.append(f"- {sig}")
            elif discovery:
                md.append(f"- {discovery}")
            else:
                md.append("- 未发现显著看空信号")
            # 数据来源标注
            sources = details.get("data_sources", {})
            if sources:
                src_labels = {"pheromone_board": "蜂群共享", "sec_api": "SEC直查",
                              "options_api": "期权直查", "finviz_api": "Finviz",
                              "yfinance": "yfinance", "unavailable": "不可用"}
                src_parts = [f"{k}={src_labels.get(v, v)}" for k, v in sources.items()]
                md.append(f"- *数据来源*：{' | '.join(src_parts)}")
            md.append("")

        # ====== 版块 7：综合判断 & 信号强度（GuardBeeSentinel + 全体投票） ======
        md.append("## 7) 综合判断 & 信号强度")
        md.append("")
        md.append("| 标的 | 方向 | 综合分 | 共振 | 投票(多/空/中) | 数据% | 失效条件 |")
        md.append("|------|------|--------|------|---------------|-------|---------|")
        for ticker, data in sorted_results:
            res = "Y" if data["resonance"]["resonance_detected"] else "N"
            ab = data["agent_breakdown"]
            data_pct = data.get("data_real_pct", 0)
            # 从 GuardBeeSentinel 获取交叉验证信息
            guard = data.get("agent_details", {}).get("GuardBeeSentinel", {})
            guard_discovery = guard.get("discovery", "")
            thesis_break = "信号分散" if not guard_discovery else guard_discovery[:30]
            md.append(
                f"| **{ticker}** | {data['direction'].upper()} | "
                f"{data['final_score']:.1f} | {res} | "
                f"{ab['bullish']}/{ab['bearish']}/{ab['neutral']} | "
                f"{data_pct:.0f}% | {thesis_break} |"
            )
        md.append("")

        # NA2：评分调整注释（bear_cap / dq_penalty / llm_enhanced / 低维度覆盖）
        adj_lines = []
        for ticker, data in sorted_results:
            adj = self._format_score_adjustments(data)
            if adj:
                adj_lines.append(f"- **{ticker}**：{adj}")
        if adj_lines:
            md.append("### 评分调整说明")
            md.append("")
            md.extend(adj_lines)
            md.append("")

        # GuardBeeSentinel 详细交叉验证
        md.append("### 交叉验证详情")
        md.append("")
        for ticker, data in sorted_results:
            guard = data.get("agent_details", {}).get("GuardBeeSentinel", {})
            discovery = guard.get("discovery", "")
            if discovery:
                md.append(f"- **{ticker}**：{discovery}")
        md.append("")

        # ====== 版块 P4：投资组合集中度风险 ======
        if concentration and concentration.get("sector_breakdown"):
            risk_level = concentration.get("concentration_risk", "low")
            risk_emoji = {"low": "✅", "medium": "⚠️", "high": "🚨"}.get(risk_level, "")
            md.append(f"## 📊 投资组合集中度分析 {risk_emoji}")
            md.append("")
            md.append(f"**集中度风险**：{risk_level.upper()} | **综合评分**：{concentration.get('risk_score', 0):.1f}/10")
            md.append("")

            # 板块分布
            md.append("**板块分布**：")
            for sector, info in concentration.get("sector_breakdown", {}).items():
                tickers_str = " / ".join(info.get("tickers", []))
                md.append(f"- {sector}：{info.get('pct', 0):.0f}%（{tickers_str}）")
            md.append("")

            # 相关性警告
            corr_warns = concentration.get("correlation_warnings", [])
            if corr_warns:
                md.append("**高相关对（≥0.70）**：")
                for w in corr_warns[:4]:
                    md.append(f"- {w['pair']}：相关系数 {w['correlation']:.2f} [{w['risk'].upper()}]")
                md.append("")

            # 建议
            md.append("**分散化建议**：")
            for rec in concentration.get("recommendations", []):
                md.append(f"- {rec}")
            md.append("")

        # ====== 版块 P5：宏观环境 ======
        if macro_context and macro_context.get("data_source") != "fallback":
            regime = macro_context.get("macro_regime", "neutral")
            regime_emoji = {"risk_on": "🟢", "risk_off": "🔴", "neutral": "🟡"}.get(regime, "")
            md.append(f"## 🌐 宏观环境 {regime_emoji}")
            md.append("")
            md.append(f"**宏观政体**：{regime.upper()} | **评分**：{macro_context.get('macro_score', 5):.1f}/10")
            md.append("")
            md.append(f"| 指标 | 数值 | 状态 |")
            md.append(f"|------|------|------|")
            md.append(f"| VIX | {macro_context.get('vix', 0):.1f} | {macro_context.get('vix_regime', '')} |")
            md.append(f"| 10Y利率 | {macro_context.get('treasury_10y', 0):.2f}% | {macro_context.get('rate_environment', '')} |")
            md.append(f"| 大盘(5日) | {macro_context.get('spx_change_pct', 0):+.2f}% | {macro_context.get('market_trend', '')} |")
            md.append(f"| 美元 | — | {macro_context.get('dollar_trend', '')} |")
            md.append("")
            headwinds = macro_context.get("macro_headwinds", [])
            tailwinds = macro_context.get("macro_tailwinds", [])
            if headwinds:
                md.append("**逆风**：" + " | ".join(headwinds[:3]))
                md.append("")
            if tailwinds:
                md.append("**顺风**：" + " | ".join(tailwinds[:3]))
                md.append("")

        # ====== 版块 P3：历史预测准确率（T+7 回测反馈）======
        if backtest_stats and backtest_stats.get("total_checked", 0) > 0:
            acc = backtest_stats["overall_accuracy"]
            total = backtest_stats["total_checked"]
            correct = backtest_stats["correct_count"]
            avg_ret = backtest_stats["avg_return"]
            md.append("## 📈 历史预测准确率（T+7，近30天）")
            md.append("")
            md.append(
                f"**样本**：{total} 条 | "
                f"**准确率**：{acc * 100:.1f}% ({correct}/{total}) | "
                f"**平均收益**：{avg_ret:+.2f}%"
            )
            md.append("")
            by_ticker = backtest_stats.get("by_ticker", {})
            if by_ticker:
                md.append("| 标的 | 方向准确率 | 预测次数 | 平均收益 |")
                md.append("|------|-----------|---------|---------|")
                for t, info in sorted(
                    by_ticker.items(), key=lambda x: x[1]["total"], reverse=True
                )[:6]:
                    md.append(
                        f"| {t} | {info['accuracy'] * 100:.0f}% "
                        f"| {info['total']} | {info['avg_return']:+.2f}% |"
                    )
                md.append("")
            by_dir = backtest_stats.get("by_direction", {})
            if by_dir:
                parts = []
                for d, label in [("bullish", "看多"), ("bearish", "看空"), ("neutral", "中性")]:
                    info = by_dir.get(d, {})
                    if info.get("total", 0) > 0:
                        parts.append(
                            f"{label}:{info['accuracy']*100:.0f}%({info['total']}次)"
                        )
                if parts:
                    md.append("**按方向**：" + " | ".join(parts))
                    md.append("")

        # ====== 版块 8：数据来源 & 免责声明 ======
        md.append("## 8) 数据来源 & 免责声明")
        md.append("")
        md.append("**蜂群分工**：")
        md.append("- ScoutBeeNova：聪明钱侦察（SEC Form 4/13F + 拥挤度）")
        md.append("- OracleBeeEcho：市场预期（期权 IV/P-C Ratio/Gamma）")
        md.append("- BuzzBeeWhisper：社交情绪（X/Reddit/Finviz）")
        md.append("- ChronosBeeHorizon：催化剂追踪（财报/事件日历）")
        md.append("- RivalBeeVanguard：竞争格局（ML 预测 + 行业对标）")
        md.append("- GuardBeeSentinel：交叉验证（共振检测 + 风险调整）")
        md.append("")
        md.append("**免责声明**：")
        md.append(DISCLAIMER_FULL)
        md.append("")

        return "\n".join(md)

    def _generate_swarm_twitter_threads(self, swarm_results: Dict) -> List[str]:
        """生成蜂群模式的 X 线程版本"""

        threads = []
        sorted_results = sorted(
            swarm_results.items(),
            key=lambda x: x[1]["final_score"],
            reverse=True
        )

        # 主线程
        main_thread = []
        main_thread.append(
            f"【Alpha Hive 蜂群日报 {self.date_str}】"
            f"7 个自治工蜂协作分析，多数投票共振信号。"
            f"{DISCLAIMER_SHORT}👇"
        )

        for i, (ticker, data) in enumerate(sorted_results[:3], 1):
            resonance_emoji = "✅" if data["resonance"]["resonance_detected"] else "❌"
            insight = data.get("key_insight", "")
            tweet = (
                f"{i}. **{ticker}** {data['direction'].upper()}\n"
                f"蜂群评分：{data['final_score']:.1f}/10 | 共振：{resonance_emoji}\n"
                f"Agent 投票：看多{data['agent_breakdown']['bullish']} vs 看空{data['agent_breakdown']['bearish']}"
            )
            if insight:
                tweet += f"\nAI洞察：{insight}"
            main_thread.append(tweet)

        main_thread.append(
            f"🐝 7 个工蜂独立分析（6 核心 + 看空对冲蜂）→ 信息素板实时交换 → 多数投票汇总\n"
            f"高共振信号优先级最高。风险提示：控制仓位。\n"
            f"下一步：T+1 验证，T+7 回看准确率。@igg_wang748"
        )

        threads.append("\n\n".join(main_thread))

        return threads

    def _parse_ml_report_to_opportunity(self, ticker: str, ml_report: Dict) -> OpportunityItem:
        """将 ML 报告解析为 OpportunityItem"""

        adv = ml_report.get("advanced_analysis", {})
        opts = adv.get("options_analysis")
        ml_pred = ml_report.get("ml_prediction", {})

        # 提取各维度评分（假设已标准化为 0-10）
        signal_score = adv.get("signal_strength", 5.0)
        catalyst_score = adv.get("catalyst_score", 5.0)
        sentiment_score = adv.get("sentiment_score", 5.0)
        odds_score = adv.get("odds_score", 5.0)
        risk_score = adv.get("risk_adjusted_score", 5.0)

        # 安全提取期权分数
        if opts and isinstance(opts, dict):
            options_score = float(opts.get("options_score", 5.0))
            options_signal = opts.get("signal_summary", "信号平衡")
        else:
            options_score = 5.0
            options_signal = "期权数据不可用"

        # 计算综合 Opportunity Score（与 CLAUDE.md 5 维公式一致）
        # options_score 合并入 odds 维度（取平均）
        odds_combined = (odds_score + options_score) / 2.0
        opp_score = (
            0.30 * signal_score +
            0.20 * catalyst_score +
            0.20 * sentiment_score +
            0.15 * odds_combined +
            0.15 * risk_score
        )

        # 判断方向
        if opp_score >= 7.5:
            direction = "看多" if signal_score > 5.0 else "看空"
            confidence = min(95, opp_score * 10)
        elif opp_score >= 6.0:
            direction = "中性"
            confidence = 60
        else:
            direction = "中性"
            confidence = 30

        return OpportunityItem(
            ticker=ticker,
            direction=direction,
            signal_score=signal_score,
            catalyst_score=catalyst_score,
            sentiment_score=sentiment_score,
            odds_score=odds_score,
            risk_score=risk_score,
            options_score=options_score,
            opportunity_score=opp_score,
            confidence=confidence,
            key_catalysts=adv.get("upcoming_catalysts", [])[:3] if adv.get("upcoming_catalysts") else [],
            options_signal=options_signal,
            risks=adv.get("key_risks", [])[:2] if adv.get("key_risks") else [],
            thesis_break=adv.get("thesis_break_conditions", "未定义")
        )

    def _build_report(self) -> Dict:
        """构建完整报告"""

        report = {
            "date": self.date_str,
            "timestamp": self.timestamp.isoformat(),
            "system_status": "✅ 完成",
            "phase_completed": "1-6 (完整蜂群流程)",
            "markdown_report": self._generate_markdown_report(),
            "twitter_threads": self._generate_twitter_threads(),
            "opportunities": [
                {
                    "rank": i + 1,
                    "ticker": opp.ticker,
                    "direction": opp.direction,
                    "opp_score": round(opp.opportunity_score, 1),
                    "confidence": f"{opp.confidence:.0f}%",
                    "options_signal": opp.options_signal,
                    "key_catalyst": opp.key_catalysts[0] if opp.key_catalysts else "N/A",
                    "thesis_break": opp.thesis_break
                }
                for i, opp in enumerate(self.opportunities[:5])
            ],
            "observation_list": self.observations
        }

        return report

    def _generate_markdown_report(self) -> str:
        """生成中文 Markdown 报告"""

        md = []
        md.append(f"# 【{self.date_str}】Alpha Hive 每日投资简报")
        md.append("")
        md.append(f"**自动生成于**：{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        md.append(f"**系统状态**：✅ 完全激活 | Phase 1-6 完成")
        md.append("")

        # 1. 今日摘要
        md.append("## 📊 今日摘要（Top 3）")
        md.append("")

        for i, opp in enumerate(self.opportunities[:3], 1):
            md.append(f"### {i}. **{opp.ticker}** - {opp.direction}")
            md.append(f"- **机会分数**：{opp.opportunity_score:.1f}/10 | **置信度**：{opp.confidence:.0f}%")
            md.append(f"- **期权信号**：{opp.options_signal}")
            if opp.key_catalysts:
                md.append(f"- **关键催化剂**：{', '.join(opp.key_catalysts[:2])}")
            md.append("")

        # 2. 机会清单
        md.append("## 🎯 完整机会清单")
        md.append("")
        md.append("| 排序 | 标的 | 方向 | 综合分 | 期权信号 | 置信度 |")
        md.append("|------|------|------|--------|---------|--------|")

        for i, opp in enumerate(self.opportunities[:5], 1):
            md.append(
                f"| {i} | **{opp.ticker}** | {opp.direction} | "
                f"{opp.opportunity_score:.1f} | {opp.options_signal[:12]}... | {opp.confidence:.0f}% |"
            )

        md.append("")

        # 3. 风险雷达
        md.append("## ⚠️ 风险雷达")
        md.append("")
        for opp in self.opportunities[:3]:
            if opp.risks:
                md.append(f"**{opp.ticker}**：{', '.join(opp.risks)}")

        md.append("")

        # 4. 数据来源与免责
        md.append("## 📝 数据来源 & 免责声明")
        md.append("")
        md.append("**数据源**：")
        md.append("- StockTwits 情绪（实时）")
        md.append("- Polymarket 赔率（每5分钟）")
        md.append("- Yahoo Finance / yFinance（实时）")
        md.append("- SEC 披露（每日更新）")
        md.append("- **期权链数据**（yFinance，每5分钟缓存）")
        md.append("")
        md.append("**免责声明**：")
        md.append(DISCLAIMER_FULL)
        md.append("")

        return "\n".join(md)

    def _generate_twitter_threads(self) -> List[str]:
        """生成 X 线程版本"""

        threads = []

        # 主线程
        main_thread = []
        main_thread.append(
            f"【Alpha Hive 日报 {self.date_str}】"
            f"{DISCLAIMER_SHORT}"
            f"今天最值得跟踪的 3 个机会 👇"
        )

        for i, opp in enumerate(self.opportunities[:3], 1):
            main_thread.append(
                f"{i}. **{opp.ticker}** {opp.direction}\n"
                f"综合分：{opp.opportunity_score:.1f}/10 | 期权信号：{opp.options_signal}\n"
                f"主催化剂：{opp.key_catalysts[0] if opp.key_catalysts else 'TBD'}"
            )

        main_thread.append(
            f"更多详情见完整日报。风险提示：高波动标的需控制仓位。"
            f"下一步跟踪：T+1 验证信号强度，T+7 回看预测偏差。@igg_wang748"
        )

        threads.append("\n\n".join(main_thread))

        return threads

    def auto_commit_and_notify(self, report: Dict) -> Dict:
        """
        自动提交报告到 Git + Slack 通知（Agent Toolbox 演示）

        新功能：使用 AgentHelper 自动执行 Git 提交和通知
        """
        _log.info("Auto-commit & Notify 启动")

        results = {}

        # 1. Git 提交报告（始终新 commit，不 amend，避免 GitHub Pages 部署冲突）
        #
        # ⚠️ 架构说明：
        #   - LLM 模式：commit 所有变更 → git push origin main → 生产页面更新
        #   - 测试模式：commit 所有变更 → 仅推 test remote（临时分支）→ git reset --hard origin/main
        #              local main 完全回滚，origin/main 不受任何影响
        #   - 禁止在测试模式外手动 `git add index.html && git push origin main`，
        #     生成物（index.html / md / json / ML html）只能通过 LLM 扫描进入 origin
        from datetime import datetime as _dt2
        import llm_service as _llm_check
        _using_llm = _llm_check.is_available()
        timestamp = _dt2.now().strftime("%H:%M")
        today_commit_msg = f"Alpha Hive 蜂群日报 {self.date_str} {timestamp}"
        _log.info("Git commit... (mode: new)")
        status = self.agent_helper.git.status()
        if status.get("modified_files"):
            commit_result = self.agent_helper.git.commit(today_commit_msg)
            results["git_commit"] = commit_result
            if commit_result["success"]:
                _log.info("Git commit 成功（new）")
            else:
                _log.warning("Git commit 失败：%s", commit_result.get('message'))
        else:
            _log.info("无需提交（工作目录干净）")

        # 2. Git 推送：LLM 模式 → 生产（origin main），规则模式 → 测试（test remote）
        #    规则模式使用临时分支，不污染本地 main，推完即删除
        env_label = "🧠 生产（LLM）" if _using_llm else "🔧 测试（规则引擎）"
        _log.info("Git push → [%s]", env_label)

        if _using_llm:
            # 生产模式：正常推送 origin main
            r = self.agent_helper.git.run_git_cmd("git push origin main")
            push_result = {"success": r["success"], "remote": "origin",
                           "output": r.get("stdout", "") or r.get("stderr", "")}
        else:
            # 测试模式：临时分支 → test remote → 删除临时分支 → 本地 main 回滚到 origin/main
            _remote_check = self.agent_helper.git.run_git_cmd("git remote")
            if "test" not in _remote_check.get("stdout", ""):
                _log.warning("test remote 不存在，跳过推送")
                push_result = {"success": False, "error": "test remote not configured"}
            else:
                _tmp = "_test_snapshot"
                # 从当前 HEAD 创建临时分支并推送到 test:main
                self.agent_helper.git.run_git_cmd(f"git branch -D {_tmp}")
                self.agent_helper.git.run_git_cmd(f"git checkout -b {_tmp}")
                r = self.agent_helper.git.run_git_cmd(f"git push test {_tmp}:main --force")
                push_result = {"success": r["success"], "remote": "test",
                               "output": r.get("stdout", "") or r.get("stderr", "")}
                # 回到 main 并删除临时分支，本地 main 恢复干净状态
                self.agent_helper.git.run_git_cmd("git checkout main")
                self.agent_helper.git.run_git_cmd(f"git branch -D {_tmp}")
                # 重置本地 main 到 origin/main，撤销测试数据对本地 main 的污染
                self.agent_helper.git.run_git_cmd("git fetch origin")
                self.agent_helper.git.run_git_cmd("git reset --hard origin/main")
                _log.info("本地 main 已恢复至 origin/main（测试数据不污染生产）")

        results["git_push"] = push_result
        results["deploy_env"] = "production" if _using_llm else "test"
        if push_result["success"]:
            _log.info("Git push 成功 → %s", push_result.get("remote"))
        else:
            _log.warning("Git push 失败：%s", push_result.get("error") or push_result.get("output", ""))

        # 3. Slack 通知（由 Claude Code MCP 工具推送，不用 webhook bot）
        _log.info("Slack 推送由 Claude Code 负责（用户账号）")
        results["slack_notification"] = {"skipped": "handled_by_claude_mcp"}

        _log.info("Auto-commit & Notify 完成")
        return results

    def check_earnings_updates(self, report_path: str = None, tickers: List[str] = None) -> Dict:
        """
        检查 watchlist 中今日是否有标的发布了财报，若有则自动抓取结果并更新简报

        Args:
            report_path: 简报文件路径（默认今日简报）
            tickers: 要检查的标的（默认 WATCHLIST 全部）

        Returns:
            {reporting_today: [], updated: [], earnings_data: {}, errors: []}
        """
        if not self.earnings_watcher:
            _log.info("EarningsWatcher 不可用，跳过财报检查")
            return {"reporting_today": [], "updated": [], "earnings_data": {}, "errors": ["EarningsWatcher not available"]}

        if tickers is None:
            tickers = list(WATCHLIST.keys())

        if report_path is None:
            # 查找今日简报
            candidates = [
                self.report_dir / "reports" / f"alpha_hive_daily_{self.date_str}.md",
                self.report_dir / f"alpha-hive-daily-{self.date_str}.md",
            ]
            for c in candidates:
                if c.exists():
                    report_path = str(c)
                    break

        if report_path is None:
            _log.warning("未找到今日简报文件，跳过财报更新")
            return {"reporting_today": [], "updated": [], "earnings_data": {}, "errors": ["no report file found"]}

        result = self.earnings_watcher.check_and_update(tickers, report_path)

        # 如果有更新，通过 Slack 发送通知
        if result.get("updated") and self.slack_notifier and self.slack_notifier.enabled:
            for ticker in result["updated"]:
                ed = result["earnings_data"].get(ticker, {})
                rev = ed.get("revenue_actual")
                eps = ed.get("eps_actual")
                yoy = ed.get("yoy_revenue_growth")

                msg_parts = [f"{ticker} 财报数据已自动更新"]
                if rev:
                    rev_str = f"${rev / 1e9:.1f}B" if abs(rev) >= 1e9 else f"${rev / 1e6:.0f}M"
                    msg_parts.append(f"营收 {rev_str}")
                if yoy is not None:
                    msg_parts.append(f"YoY {'+' if yoy > 0 else ''}{yoy * 100:.1f}%")
                if eps is not None:
                    msg_parts.append(f"EPS ${eps:.2f}")

                try:
                    self.slack_notifier.send_opportunity_alert(
                        ticker,
                        0,  # score placeholder
                        "财报更新",
                        " | ".join(msg_parts),
                        ["自动抓取", f"完整度: {ed.get('data_completeness', 'N/A')}"]
                    )
                except (OSError, ValueError, RuntimeError) as e:
                    _log.warning("Slack 财报通知发送失败: %s", e)

        # D1: 自动同步财报日期到催化剂日历
        try:
            auto_catalysts = self.earnings_watcher.get_catalysts_for_calendar(tickers)
            if auto_catalysts and hasattr(self, 'calendar') and self.calendar:
                # 合并自动获取的财报日期与 config.CATALYSTS
                from config import CATALYSTS
                merged = dict(CATALYSTS)
                for t, events in auto_catalysts.items():
                    if t in merged:
                        # 去重：只添加尚未存在的 earnings 事件
                        existing_dates = {e.get("scheduled_date") for e in merged[t]}
                        for ev in events:
                            if ev.get("scheduled_date") not in existing_dates:
                                merged[t].append(ev)
                    else:
                        merged[t] = events
                self.calendar.sync_catalysts(catalysts=merged, tickers=tickers)
                _log.info("已自动同步 %d 个标的的财报日期到催化剂日历", len(auto_catalysts))
        except (ImportError, OSError, ValueError, TypeError, AttributeError) as e:
            _log.debug("催化剂日历自动同步跳过: %s", e)

        return result

    def _generate_ml_reports(self, report: Dict) -> List[str]:
        """为扫描标的批量生成 ML 增强 HTML 报告（同步写入，供 _generate_index_html 检测到文件后添加链接）"""
        # 加载蜂群详细数据（save_report 已写入 .swarm_results_*.json）
        swarm_data: Dict = {}
        sr_path = self.report_dir / f".swarm_results_{self.date_str}.json"
        if sr_path.exists():
            try:
                with open(sr_path) as f:
                    swarm_data = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass

        # 用 swarm_data 所有标的（而非仅 opportunities 前几名），确保每个扫描标的都有 ML 报告
        opps = report.get("opportunities", [])
        opp_tickers = [o.get("ticker") for o in opps if o.get("ticker")]
        extra = [t for t in swarm_data if t not in opp_tickers]
        tickers = opp_tickers + extra
        if not tickers:
            return []

        generated = []
        for ticker in tickers:
            try:
                # 从 yfinance 获取当前价格
                real_price, real_change = 100.0, 0.0
                try:
                    import yfinance as _yf
                    _hist = _yf.Ticker(ticker).history(period="5d")
                    if not _hist.empty:
                        real_price = float(_hist["Close"].iloc[-1])
                        if len(_hist) >= 2:
                            real_change = (_hist["Close"].iloc[-1] / _hist["Close"].iloc[-2] - 1) * 100
                except Exception as _yfe:
                    _log.debug("yfinance 价格获取失败 %s: %s", ticker, _yfe)

                ticker_data = {
                    "ticker": ticker,
                    "sources": {
                        "yahoo_finance": {
                            "current_price": real_price,
                            "price_change_5d": real_change,
                            "change_pct": real_change,
                        }
                    },
                }

                # 生成 ML 增强分析
                enhanced = self.ml_generator.generate_ml_enhanced_report(ticker, ticker_data)

                # 注入蜂群数据
                if ticker in swarm_data:
                    enhanced["swarm_results"] = swarm_data[ticker]

                # 同步写入 HTML（必须在 _generate_index_html 前完成，以便文件存在性检测通过）
                html = self.ml_generator.generate_html_report(ticker, enhanced)
                html_path = self.report_dir / f"alpha-hive-{ticker}-ml-enhanced-{self.date_str}.html"
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html)

                generated.append(ticker)
                _log.info("ML 增强报告已生成：%s", html_path.name)

            except Exception as e:
                _log.warning("ML 报告生成失败 %s: %s", ticker, e)

        return generated

    def save_report(self, report: Dict) -> str:
        """保存报告到文件（MD / JSON / X线程 / index.html GitHub Pages）"""

        json_file = self.report_dir / f"alpha-hive-daily-{self.date_str}.json"
        md_file = self.report_dir / f"alpha-hive-daily-{self.date_str}.md"

        # 如果今日已有报告，合并 opportunities（支持分批运行，避免互相覆盖）
        if json_file.exists():
            try:
                with open(json_file, encoding="utf-8") as _f:
                    existing = json.load(_f)
                existing_by_ticker = {o.get("ticker"): o for o in existing.get("opportunities", [])}
                new_by_ticker = {o.get("ticker"): o for o in report.get("opportunities", [])}
                existing_by_ticker.update(new_by_ticker)  # 新批次结果优先
                merged_opps = sorted(existing_by_ticker.values(),
                                     key=lambda x: x.get("opportunity_score", 0), reverse=True)
                report["opportunities"] = merged_opps
                if "swarm_metadata" in report:
                    report["swarm_metadata"]["tickers_analyzed"] = len(merged_opps)
                _log.info("合并今日已有报告：共 %d 标的", len(merged_opps))
            except Exception as e:
                _log.warning("合并已有报告失败，使用新报告: %s", e)

        # 保存 JSON 版本
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, cls=SafeJSONEncoder)

        # 保存 Markdown 版本（新批次追加到已有文件末尾）
        if md_file.exists():
            existing_md = md_file.read_text(encoding="utf-8")
            combined_md = existing_md + "\n\n---\n\n" + report["markdown_report"]
            with open(md_file, "w", encoding="utf-8") as f:
                f.write(combined_md)
            report["markdown_report"] = combined_md
        else:
            with open(md_file, "w", encoding="utf-8") as f:
                f.write(report["markdown_report"])

        # 清理当天旧的 X 线程文件（防止多次运行时数量不同导致残留叠加）
        for old in self.report_dir.glob(f"alpha-hive-thread-{self.date_str}-*.txt"):
            old.unlink()

        # 保存 X 线程版本
        for i, thread in enumerate(report["twitter_threads"], 1):
            thread_file = self.report_dir / f"alpha-hive-thread-{self.date_str}-{i}.txt"
            with open(thread_file, "w", encoding="utf-8") as f:
                f.write(thread)

        # 生成 ML 增强 HTML 报告（必须在 _generate_index_html 前完成，以便 ML 链接自动出现）
        try:
            ml_tickers = self._generate_ml_reports(report)
            if ml_tickers:
                _log.info("ML 增强报告完成：%s", ml_tickers)
                print(f"   ML 报告     : ✅ {', '.join(ml_tickers)}")
        except Exception as e:
            _log.warning("ML 报告批量生成出错: %s", e)

        # 更新 GitHub Pages 仪表板
        try:
            html = self._generate_index_html(report)
            index_file = self.report_dir / "index.html"
            with open(index_file, "w", encoding="utf-8") as f:
                f.write(html)
            _log.info("index.html 已更新（GitHub Pages）")
        except Exception as e:
            _log.warning("index.html 生成失败: %s", e)

        _log.info("报告已保存：%s", md_file.name)

        return str(md_file)

    def _generate_index_html(self, report: Dict) -> str:
        """从 swarm report + .swarm_results_*.json 生成完整 GitHub Pages 仪表板"""
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

        now_str = _dt.now().strftime("%Y-%m-%d %H:%M PST")
        date_str = self.date_str
        opps = report.get("opportunities", [])
        meta = report.get("swarm_metadata", {})
        n_tickers = meta.get("tickers_analyzed", len(opps))
        n_agents = meta.get("total_agents", 7)
        n_resonance = meta.get("resonances_detected", 0)

        # 读取详细 swarm_results（含 IV Rank、P/C Ratio、内幕信号等）
        swarm_detail: Dict = {}
        try:
            sr_path = self.report_dir / f".swarm_results_{date_str}.json"
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
            ml_link = _Path(self.report_dir / f"alpha-hive-{ticker}-ml-enhanced-{date_str}.html")
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
            ml_link = _Path(self.report_dir / f"alpha-hive-{ticker}-ml-enhanced-{date_str}.html")
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

        # extra_css：用普通字符串（不用 f-string），避免 CSS 大括号转义问题
        extra_css = """
        .reports-list { display: flex; flex-direction: column; gap: 12px; }
        .report-item { border: 1px solid #eee; border-radius: 8px; padding: 12px; }
        .report-date { font-size: 0.85em; color: #666; margin-bottom: 8px; }
        .report-links { display: flex; flex-wrap: wrap; gap: 8px; }
        .rl { display: inline-block; padding: 5px 12px; border-radius: 15px; font-size: 0.82em;
              font-weight: bold; text-decoration: none; transition: opacity 0.2s; }
        .rl:hover { opacity: 0.85; }
        .rl.md { background: #667eea; color: white; }
        .rl.json { background: #764ba2; color: white; }
        .rl.ml-rl { background: #17a2b8; color: white; font-size: 0.78em; padding: 4px 10px; }
        .company-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 20px; }
        .company-card { border-radius: 12px; overflow: hidden; box-shadow: 0 4px 18px rgba(0,0,0,0.09); }
        .cc-header { display: flex; justify-content: space-between; align-items: center; padding: 14px 20px; color: white; }
        .cc-ticker { font-size: 1.4em; font-weight: bold; }
        .cc-dir { font-size: 0.88em; background: rgba(255,255,255,0.22); padding: 3px 12px; border-radius: 12px; }
        .cc-score { font-size: 1.1em; font-weight: bold; }
        .cc-score.sc-h { color: #90EE90; } .cc-score.sc-m { color: #FFD700; } .cc-score.sc-l { color: #FFB6C1; }
        .cc-body { padding: 16px 20px; background: white; }
        .cc-metrics { display: flex; gap: 12px; margin-bottom: 12px; padding-bottom: 12px; border-bottom: 1px solid #f0f0f0; }
        .cc-metric { flex: 1; text-align: center; background: #f8f9fa; border-radius: 8px; padding: 8px 4px; }
        .cm-l { display: block; font-size: 0.75em; color: #888; }
        .cm-v { display: block; font-size: 1em; font-weight: bold; color: #333; margin-top: 3px; }
        .cc-signals { list-style: none; padding: 0; margin: 0 0 14px 0; }
        .cc-signals li { padding: 5px 0; border-bottom: 1px dashed #f5f5f5; font-size: 0.87em; color: #444; line-height: 1.5; }
        .cc-signals li:last-child { border-bottom: none; }
        .cc-footer { text-align: right; margin-top: 4px; }
        .ml-btn { display: inline-block; padding: 6px 16px; background: linear-gradient(135deg,#667eea,#764ba2);
                  color: white; border-radius: 15px; font-size: 0.82em; font-weight: bold; text-decoration: none; }
        .ml-btn:hover { opacity: 0.88; }
        .ml-btn-na { font-size: 0.82em; color: #bbb; font-style: italic; }
        .report-body { font-size: 0.92em; line-height: 1.8; color: #333; max-height: 900px; overflow-y: auto; padding-right: 8px; }
        .report-body h1 { font-size: 1.5em; color: #667eea; border-bottom: 2px solid #667eea; padding-bottom: 8px; margin: 20px 0 12px; }
        .report-body h2 { font-size: 1.2em; color: #667eea; border-left: 4px solid #667eea; padding-left: 10px; margin: 16px 0 8px; }
        .report-body h3 { font-size: 1.05em; color: #764ba2; font-weight: bold; margin: 12px 0 5px; }
        .report-body h4 { font-size: 0.97em; color: #555; margin: 8px 0 4px; }
        .report-body ul { margin: 4px 0 8px 18px; }
        .report-body .sub-ul { margin-top: 4px; padding-left: 16px; }
        .report-body li { margin: 2px 0; }
        .report-body p { margin: 2px 0; }
        .report-body hr { border: none; border-top: 1px solid #eee; margin: 14px 0; }
        """

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
            if _Path(self.report_dir / f"alpha-hive-{_t3}-ml-enhanced-{date_str}.html").exists():
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
            _ml3ex = _Path(self.report_dir / f"alpha-hive-{_tkr3}-ml-enhanced-{date_str}.html").exists()
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
            return '\n'.join(out)

        _rpt_body = ""
        _md_path3 = _Path(self.report_dir) / f"alpha-hive-daily-{date_str}.md"
        if _md_path3.exists():
            try:
                _rpt_body = _md2html(_md_path3.read_text(encoding='utf-8'))
            except Exception:
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

        new_css = self._DASHBOARD_CSS

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
            _logo6 = (f'<img class="slogo" src="https://logo.clearbit.com/{_dom6}" '
                      f'alt="{_html.escape(_tc6)}" onerror="this.style.display=\'none\';this.nextSibling.style.display=\'flex\'">'
                      f'<div class="slogo-fb" style="display:none">{_html.escape(_tc6[:2])}</div>') if _dom6 else \
                     f'<div class="slogo-fb">{_html.escape(_tc6[:2])}</div>'
            # Insight: first non-empty discovery
            _ins6 = ""
            for _agt6 in ["ScoutBeeNova","OracleBeeEcho","BuzzBeeWhisper","ChronosBeeHorizon"]:
                _d6 = swarm_detail.get(_tc6,{}).get("agent_details",{}).get(_agt6,{}).get("discovery","")
                if _d6:
                    _ins6 = _html.escape(_d6.split("|")[0].strip()[:100])
                    break
            _ml6ex = _Path(self.report_dir / f"alpha-hive-{_tc6}-ml-enhanced-{date_str}.html").exists()
            _ml6   = (f'<a href="alpha-hive-{_tc6}-ml-enhanced-{date_str}.html" class="ml-btn">ML 详情 →</a>'
                      if _ml6ex else '<span style="font-size:.75em;color:var(--ts);">ML 报告生成中</span>')
            # Dimension mini-bars (uses dimension_scores 0-10 → height %)
            _dims6 = swarm_detail.get(_tc6, {}).get("dimension_scores", {})
            _dim_html6 = ""
            if _dims6:
                _dl6 = [("信号","signal"),("催化","catalyst"),("情绪","sentiment"),("赔率","odds"),("风险","risk_adj")]
                _db6 = ""
                for _dlbl6x, _dkey6 in _dl6:
                    _dv6  = float(_dims6.get(_dkey6, 5.0))
                    _dpct6 = max(5, int(_dv6 * 10))
                    _dcol6 = "#22c55e" if _dv6 >= 7 else ("#f59e0b" if _dv6 >= 5.5 else "#ef4444")
                    _db6 += (f'<div class="dim-b-item">'
                             f'<div class="dim-b" style="height:{_dpct6}%;background:{_dcol6}"></div>'
                             f'<span class="dim-lbl">{_dlbl6x}</span></div>')
                _dim_html6 = f'<div class="dim-bars">{_db6}</div>'
            new_cards_html += f"""
            <div class="scard">
              <div class="scard-head">
                <div class="slogo-wrap">{_logo6}<span class="srank">#{_ci}</span></div>
                <span class="sdir {_dcls6}">{_dlbl6}</span>
              </div>
              <div class="scard-body">
                <div class="sticker">{_html.escape(_tc6)}</div>
                <div class="score-row">
                  <span class="score-big {_scls6}">{_sc6:.1f}</span>
                  <div class="sbar-wrap">
                    <div class="sbar-lbl"><span>综合分</span><span>/10</span></div>
                    <div class="sbar"><div class="sbar-fill {_fcls6}" style="width:{_pct6}%"></div></div>
                  </div>
                </div>
                {_dim_html6}
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
            _ml_ex_rt = _Path(self.report_dir / f"alpha-hive-{_trt}-ml-enhanced-{date_str}.html").exists()
            _ml_rt = (f'<a href="alpha-hive-{_trt}-ml-enhanced-{date_str}.html" class="ml-btn-sm">查看</a>'
                      if _ml_ex_rt else "-")
            _pc_st_rt = (' style="color:var(--bull);font-weight:700"' if _det_rt["pc"] != "-" and float(_det_rt["pc"]) < 0.7
                         else (' style="color:var(--bear);font-weight:700"' if _det_rt["pc"] != "-" and float(_det_rt["pc"]) > 1.5 else ""))
            new_rows_html += f"""
            <tr>
              <td>{_ri}</td>
              <td><strong>{_html.escape(_trt)}</strong></td>
              <td><span class="{_dclrt}">{_dlrt}</span></td>
              <td class="{_scrt}"><strong>{_srt:.1f}</strong>/10</td>
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
            _ml_exd = _Path(self.report_dir / f"alpha-hive-{_tkrd}-ml-enhanced-{date_str}.html").exists()
            _mlbtnd = (f'<a href="alpha-hive-{_tkrd}-ml-enhanced-{date_str}.html" class="ml-btn-cc">ML 增强分析 →</a>'
                       if _ml_exd else '<span style="font-size:.78em;color:var(--ts)">ML 报告生成中</span>')
            # ── edgar_rss badge ──
            _rss_n = _add.get("ScoutBeeNova", {}).get("details", {}).get("insider", {}).get("rss_fresh_today", 0)
            _rss_badge = (f'<span class="rss-badge">📋 今日Form4 {_rss_n}份 🔴</span>' if _rss_n else "")
            # ── thesis break 面板（仅 NVDA/VKTX/TSLA 有配置）──
            _opp_d = opp_by_ticker.get(_tkrd, {})
            _tb_l1 = _opp_d.get("thesis_break_l1") or _sdd.get("thesis_break_l1", [])
            _tb_l2 = _opp_d.get("thesis_break_l2") or _sdd.get("thesis_break_l2", [])
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
            new_company_html += f"""
            <div class="company-card">
              <div class="cc-header" style="background:{_hcd};">
                <span class="cc-ticker">{_html.escape(_tkrd)}</span>
                <span class="cc-dir">{_dlbld}</span>
                <span class="cc-score">{_scd:.1f}/10</span>
              </div>
              <div class="cc-body">
                <div class="cc-two">
                  <div class="cc-metrics-col">
                    <div class="cc-metric"><span class="cm-l">IV Rank</span><span class="cm-v">{_detd['iv_rank']}</span></div>
                    <div class="cc-metric"><span class="cm-l">P/C Ratio</span><span class="cm-v">{_detd['pc']}</span></div>
                    {f'<div class="cc-metric"><span class="cm-l">期权流向</span><span class="cm-v" style="color:{_detd["flow_color"]};font-weight:bold;">{_detd["flow_dir"]}</span></div>' if _detd["flow_dir"] != "-" else ""}
                    {f'<div class="cc-metric"><span class="cm-l">GEX</span><span class="cm-v">{_detd["gex"]}</span></div>' if _detd["gex"] != "-" else ""}
                    <div class="cc-metric"><span class="cm-l">看空强度</span><span class="cm-v">{_detd['bear_score']:.1f}/10</span></div>
                    <div class="cc-metric"><span class="cm-l">投票</span><span class="cm-v">{_detd['bullish']}多/{_detd['bearish_v']}空</span></div>
                  </div>
                  <div class="radar-wrap"><canvas id="radar-{_html.escape(_tkrd)}" width="160" height="160"></canvas></div>
                </div>
                <ul class="cc-signals">{_bhtmld}</ul>
                {_build_dim_dq_html(_detd['dim_dq'])}
                {_tb_html}
                <div class="cc-footer">{_rss_badge}{_mlbtnd}</div>
              </div>
            </div>"""

        # ── 历史简报回溯 ──
        _hist_entries = []
        try:
            import glob as _glob
            _hist_files = sorted(
                _glob.glob(str(self.report_dir / "alpha-hive-daily-*.json")),
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
                    # 按 opp_score 降序取 Top 3
                    _htop3 = sorted(
                        [{"ticker": o.get("ticker",""), "score": float(o.get("opp_score",0)),
                          "direction": str(o.get("direction","neutral")).lower()}
                         for o in _hopps if o.get("ticker")],
                        key=lambda x: x["score"], reverse=True
                    )[:3]
                    _havg  = sum(o["score"] for o in _htop3) / len(_htop3) if _htop3 else 0
                    # 可用的 ML 报告
                    _hml   = [t for t in [o["ticker"] for o in _hopps]
                              if _Path(self.report_dir / f"alpha-hive-{t}-ml-enhanced-{_hdate}.html").exists()]
                    _hist_entries.append({
                        "date": _hdate, "n": _hn, "top3": _htop3,
                        "avg": _havg, "ml_tickers": _hml,
                        "has_md":   _Path(self.report_dir / f"alpha-hive-daily-{_hdate}.md").exists(),
                        "has_json": _Path(self.report_dir / f"alpha-hive-daily-{_hdate}.json").exists(),
                    })
                except Exception as _he:
                    _log.debug("历史报告 %s 解析失败: %s", _hdate, _he)
        except Exception as _hle:
            _log.debug("历史时间线加载失败: %s", _hle)

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
                if _he["has_md"]:
                    _hlinks += f'<a href="alpha-hive-daily-{_he["date"]}.md" class="hlink hlink-md">📄 简报</a>'
                if _he["has_json"]:
                    _hlinks += f'<a href="alpha-hive-daily-{_he["date"]}.json" class="hlink hlink-json">📊 JSON</a>'
                for _hmt in _he["ml_tickers"][:4]:
                    _hlinks += f'<a href="alpha-hive-{_hmt}-ml-enhanced-{_he["date"]}.html" class="hlink hlink-ml">{_html.escape(_hmt)}</a>'
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

        # 方向图数据（JSON 格式，直接注入 JS）
        _dir_map_acc = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
        _acc_dir_labels_js = _json.dumps([_dir_map_acc.get(d, d) for d in ["bullish", "bearish", "neutral"]])
        _acc_dir_accs_js   = _json.dumps([
            round(_acc_by_dir.get(d, {}).get("accuracy", 0) * 100, 1)
            for d in ["bullish", "bearish", "neutral"]
        ])
        _acc_dir_tots_js   = _json.dumps([
            _acc_by_dir.get(d, {}).get("total", 0)
            for d in ["bullish", "bearish", "neutral"]
        ])

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
        <div class="acc-canvas-wrap"><canvas id="accDirChart"></canvas></div>
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

        # JS 数据（安全转义，用于 f-string 外注入）
        _acc_dir_labels_js_safe = _acc_dir_labels_js
        _acc_dir_accs_js_safe   = _acc_dir_accs_js
        _acc_dir_tots_js_safe   = _acc_dir_tots_js

        return f"""<!DOCTYPE html>
<html lang="zh-CN" class="">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Alpha Hive 投资仪表板</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
{new_css}
</style>
</head>
<body>
<!-- ── Fixed Nav ── -->
<nav class="nav">
  <a href="#" class="nav-logo">🐝 Alpha Hive</a>
  <div class="nav-links">
    <a href="#today"   class="nav-link">今日简报</a>
    <a href="#charts"  class="nav-link">图表</a>
    <a href="#list"    class="nav-link">完整清单</a>
    <a href="#deep"    class="nav-link">个股深度</a>
    <a href="#report"  class="nav-link">完整简报</a>
    <a href="#history" class="nav-link">📅 历史简报</a>
    <a href="#accuracy" class="nav-link">📈 准确率</a>
  </div>
  <button class="dark-btn" id="darkBtn" onclick="toggleDark()">🌙 暗黑</button>
</nav>

<!-- ── Hero Banner ── -->
<section class="hero">
  <div class="hero-inner">
    <div class="hero-left">
      <div class="hero-badge">🐝 Alpha Hive Intelligence · 蜂群驱动</div>
      <h1 class="hero-title">去中心化<span>蜂群智能</span><br>投资研究平台</h1>
      <p class="hero-sub">{n_agents} 自治工蜂协作 · SEC EDGAR 真实数据 · 每日自动扫描</p>
      <div class="hero-meta">
        <span class="hero-time">🕐 {now_str}</span>
        <span class="hero-dbadge">📊 数据真实度 {avg_real}</span>
      </div>
    </div>
    <div class="hero-right">
      <svg class="hero-svg hive-anim" viewBox="0 0 280 260" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <path id="orb-i" d="M 215,101 A 75,45 0 1 0 65,101 A 75,45 0 1 0 215,101"/>
          <path id="orb-o" d="M 238,112 A 98,56 0 1 0 42,112 A 98,56 0 1 0 238,112"/>
        </defs>
        <polygon points="140,55 180,78 180,124 140,147 100,124 100,78" fill="#F4A532" opacity=".9"/>
        <text x="140" y="112" text-anchor="middle" font-size="40" fill="white">🐝</text>
        <polygon class="hex-p" points="140,5 170,22 170,57 140,74 110,57 110,22" fill="none" stroke="#F4A532" stroke-width="1.5" opacity=".55" style="animation-delay:.3s"/>
        <polygon class="hex-p" points="190,32 220,49 220,84 190,101 160,84 160,49" fill="rgba(244,165,50,.12)" stroke="#F4A532" stroke-width="1" opacity=".5" style="animation-delay:.7s"/>
        <polygon class="hex-p" points="190,107 220,124 220,159 190,176 160,159 160,124" fill="rgba(102,126,234,.18)" stroke="#667eea" stroke-width="1" opacity=".45" style="animation-delay:1.1s"/>
        <polygon class="hex-p" points="190,182 220,199 220,234 190,251 160,234 160,199" fill="none" stroke="#764ba2" stroke-width="1" opacity=".35" style="animation-delay:1.5s"/>
        <polygon class="hex-p" points="140,155 170,172 170,207 140,224 110,207 110,172" fill="rgba(244,165,50,.09)" stroke="#F4A532" stroke-width="1.5" opacity=".45" style="animation-delay:1.9s"/>
        <polygon class="hex-p" points="90,182 120,199 120,234 90,251 60,234 60,199" fill="none" stroke="#667eea" stroke-width="1" opacity=".35" style="animation-delay:2.3s"/>
        <polygon class="hex-p" points="90,107 120,124 120,159 90,176 60,159 60,124" fill="rgba(102,126,234,.13)" stroke="#667eea" stroke-width="1" opacity=".45" style="animation-delay:2.7s"/>
        <polygon class="hex-p" points="90,32 120,49 120,84 90,101 60,84 60,49" fill="none" stroke="#764ba2" stroke-width="1" opacity=".35" style="animation-delay:3.1s"/>
        <!-- Orbit rings -->
        <ellipse cx="140" cy="101" rx="75" ry="45" fill="none" stroke="#F4A532" stroke-width="0.7" stroke-dasharray="3,6" opacity="0.25"/>
        <ellipse cx="140" cy="112" rx="98" ry="56" fill="none" stroke="#667eea" stroke-width="0.7" stroke-dasharray="3,8" opacity="0.18"/>
        <!-- Orbiting bees (animateMotion along ellipse paths) -->
        <g><circle r="5.5" fill="#F4A532" opacity="0.9"/><circle r="2.8" fill="#fff8e7" opacity="0.75"/><animateMotion dur="9s" repeatCount="indefinite" begin="0s"><mpath href="#orb-i"/></animateMotion></g>
        <g><circle r="4.8" fill="#fbbf24" opacity="0.85"/><circle r="2.4" fill="#fff8e7" opacity="0.65"/><animateMotion dur="9s" repeatCount="indefinite" begin="-4.5s"><mpath href="#orb-i"/></animateMotion></g>
        <g><circle r="5" fill="#667eea" opacity="0.85"/><circle r="2.5" fill="#c7d2fe" opacity="0.7"/><animateMotion dur="14s" repeatCount="indefinite" begin="-5s"><mpath href="#orb-o"/></animateMotion></g>
      </svg>
    </div>
  </div>
  <!-- Stats Row -->
  <div class="hero-stats">
    <div class="hstat">
      <div class="hstat-val">{n_resonance}</div>
      <div class="hstat-lbl">共振信号</div>
    </div>
    <div class="hstat">
      <div class="hstat-val" style="color:{_fg_color}">{_fg_str2}</div>
      <div class="hstat-lbl">Fear & Greed</div>
    </div>
    <div class="hstat">
      <div class="hstat-val">{n_tickers}</div>
      <div class="hstat-lbl">扫描标的</div>
    </div>
    <div class="hstat">
      <div class="hstat-val">{_avg_score_str}</div>
      <div class="hstat-lbl">平均综合分</div>
    </div>
  </div>
</section>

<div class="main">
  <!-- ── Top 6 Cards ── -->
  <div class="section" id="today">
    <div class="sec-title">今日 Top {min(6, len(all_tickers_sorted))} 机会</div>
    <div class="top6-grid">
      {new_cards_html}
    </div>
  </div>

  <!-- ── Charts ── -->
  <div class="section" id="charts">
    <div class="sec-title">市场可视化</div>
    <div class="charts-grid">
      <div class="chart-box">
        <div class="chart-ttl">😨 Fear &amp; Greed 指数</div>
        <div class="chart-canvas-wrap" style="height:180px"><canvas id="fgChart"></canvas></div>
      </div>
      <div class="chart-box">
        <div class="chart-ttl">📊 各标的综合评分</div>
        <div class="chart-canvas-wrap" style="height:{'{}px'.format(max(160, len(all_tickers_sorted)*28))}"><canvas id="scoresChart"></canvas></div>
      </div>
      <div class="chart-box">
        <div class="chart-ttl">🗳 看多 / 看空 / 中性</div>
        <div class="chart-canvas-wrap" style="height:180px"><canvas id="dirChart"></canvas></div>
      </div>
    </div>
  </div>

  <!-- ── Full Table ── -->
  <div class="section" id="list">
    <div class="sec-title">完整机会清单</div>
    <div class="tbl-search-row">
      <input class="tbl-search" id="tableSearch" type="text" placeholder="🔍 搜索标的..." oninput="filterTable()">
    </div>
    <div class="tbl-wrap">
      <table class="full-table" id="oppTable">
        <thead><tr>
          <th>#</th><th>标的</th><th>方向</th><th>综合分</th><th>共振</th>
          <th>投票(多/空/中)</th><th>IV Rank</th><th>P/C</th><th>看空强度</th><th>ML 详情</th>
        </tr></thead>
        <tbody>{new_rows_html}</tbody>
      </table>
    </div>
  </div>

  <!-- ── Deep Analysis ── -->
  <div class="section" id="deep">
    <div class="sec-title">个股深度分析（含雷达图）</div>
    <div class="company-grid">{new_company_html}</div>
  </div>

  <!-- ── Markdown Report ── -->
  <div class="section" id="report">
    <div class="sec-title">完整蜂群简报</div>
    <div class="report-body">{_rpt_body}</div>
  </div>

  <!-- ── Historical Reports ── -->
  <div class="section" id="history">
    <div class="sec-title">📅 历史简报回溯</div>
    <div class="hist-list">{_hist_html}</div>
  </div>

{_acc_section_html}
</div>

<footer class="footer">
  <p>🐝 Alpha Hive — 去中心化蜂群智能投资研究平台</p>
  <p>更新：{now_str} | {n_tickers} 标的 | SEC 真实数据 | 真实度 {avg_real}</p>
  <p style="margin-top:8px;font-size:.82em;opacity:.6">
    声明：本报告由 AI 蜂群自动生成，仅供研究参考，不构成投资建议。所有决策请自行判断。
  </p>
</footer>

<script>
// ── Dark Mode ──
function toggleDark(){{
  var h=document.documentElement;
  h.classList.toggle('dark');
  localStorage.setItem('ahDark',h.classList.contains('dark')?'1':'0');
  document.getElementById('darkBtn').textContent=h.classList.contains('dark')?'☀️ 亮色':'🌙 暗黑';
}}
if(localStorage.getItem('ahDark')==='1'){{
  document.documentElement.classList.add('dark');
}}
document.addEventListener('DOMContentLoaded',function(){{
  var b=document.getElementById('darkBtn');
  if(b&&document.documentElement.classList.contains('dark'))b.textContent='☀️ 亮色';
}});

// ── Table Search ──
function filterTable(){{
  var q=document.getElementById('tableSearch').value.toLowerCase();
  document.querySelectorAll('#oppTable tbody tr').forEach(function(tr){{
    tr.style.display=tr.textContent.toLowerCase().includes(q)?'':'none';
  }});
}}

// ── Table Sort ──
document.querySelectorAll('#oppTable thead th').forEach(function(th,i){{
  th.addEventListener('click',function(){{
    var tbody=document.querySelector('#oppTable tbody');
    var rows=Array.from(tbody.rows).filter(function(r){{return r.style.display!=='none';}});
    var asc=th.getAttribute('data-sort')!=='asc';
    document.querySelectorAll('#oppTable thead th').forEach(function(t){{t.removeAttribute('data-sort');}});
    th.setAttribute('data-sort',asc?'asc':'desc');
    rows.sort(function(a,b){{
      var av=a.cells[i].textContent.trim();
      var bv=b.cells[i].textContent.trim();
      var an=parseFloat(av),bn=parseFloat(bv);
      if(!isNaN(an)&&!isNaN(bn)) return asc?an-bn:bn-an;
      return asc?av.localeCompare(bv,'zh'):bv.localeCompare(av,'zh');
    }});
    rows.forEach(function(r){{tbody.appendChild(r);}});
  }});
}});

// ── Charts ──
document.addEventListener('DOMContentLoaded',function(){{
  var dark=document.documentElement.classList.contains('dark');
  var tc=dark?'rgba(255,255,255,.65)':'rgba(0,0,0,.55)';
  var gc=dark?'rgba(255,255,255,.07)':'rgba(0,0,0,.06)';

  // F&G Gauge
  var fgCtx=document.getElementById('fgChart');
  if(fgCtx){{
    var fv={_fv3};
    var fc=fv<=25?'#ef4444':fv<=45?'#f97316':fv<=55?'#f59e0b':fv<=75?'#22c55e':'#16a34a';
    var fl='{_fg_label}';
    new Chart(fgCtx,{{
      type:'doughnut',
      data:{{datasets:[{{data:[fv,100-fv],backgroundColor:[fc,dark?'#2a3050':'#e8ecf3'],
                         borderWidth:0,circumference:180,rotation:-90}}]}},
      options:{{responsive:true,maintainAspectRatio:false,cutout:'72%',
               plugins:{{legend:{{display:false}},tooltip:{{enabled:false}}}}}},
      plugins:[{{id:'fgTxt',afterDraw:function(ch){{
        var cx=ch.ctx,w=ch.width,h=ch.height;
        cx.save();
        cx.font='bold 26px system-ui';cx.fillStyle=fc;cx.textAlign='center';cx.textBaseline='middle';
        cx.fillText(fv,w/2,h*.60);
        cx.font='11px system-ui';cx.fillStyle=tc;cx.fillText(fl,w/2,h*.60+20);
        cx.restore();
      }}}}]
    }});
  }}

  // Scores Bar
  var scCtx=document.getElementById('scoresChart');
  if(scCtx){{
    var sc={_scores_js};
    var clrs=sc.map(function(x){{return x[1]>=7?'rgba(34,197,94,.85)':x[1]>=5.5?'rgba(245,158,11,.85)':'rgba(239,68,68,.85)';}});
    new Chart(scCtx,{{
      type:'bar',
      data:{{labels:sc.map(function(x){{return x[0];}}),
             datasets:[{{data:sc.map(function(x){{return x[1];}}),backgroundColor:clrs,borderRadius:5,borderSkipped:false}}]}},
      options:{{indexAxis:'y',responsive:true,maintainAspectRatio:false,
               plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:function(c){{return' '+c.raw+'/10';}}}}}}}},
               scales:{{
                 x:{{min:0,max:10,grid:{{color:gc}},ticks:{{color:tc,font:{{size:10}}}}}},
                 y:{{grid:{{display:false}},ticks:{{color:tc,font:{{size:10,weight:'bold'}}}}}}
               }}}}
    }});
  }}

  // Direction Donut
  var dirCtx=document.getElementById('dirChart');
  if(dirCtx){{
    var dd={_dir_js};
    new Chart(dirCtx,{{
      type:'doughnut',
      data:{{labels:['看多','看空','中性'],
             datasets:[{{data:dd,
                         backgroundColor:['rgba(34,197,94,.85)','rgba(239,68,68,.85)','rgba(245,158,11,.85)'],
                         borderColor:[dark?'#141928':'#fff'],borderWidth:2}}]}},
      options:{{responsive:true,maintainAspectRatio:false,cutout:'58%',
               plugins:{{legend:{{position:'bottom',labels:{{color:tc,font:{{size:10}},boxWidth:11,padding:10}}}},
                         tooltip:{{callbacks:{{label:function(c){{return' '+c.label+': '+c.raw+' 只';}}}}}}}}}}
    }});
  }}

  // Radar per ticker
  var rd={_radar_js};
  var rl=['信号强度','催化剂','情绪','赔率','风险控制'];
  Object.keys(rd).forEach(function(tk){{
    var cv=document.getElementById('radar-'+tk);
    if(!cv)return;
    new Chart(cv,{{
      type:'radar',
      data:{{labels:rl,datasets:[{{data:rd[tk],fill:true,
               backgroundColor:'rgba(102,126,234,.13)',borderColor:'#667eea',
               pointBackgroundColor:'#667eea',pointBorderColor:'#fff',pointRadius:2,borderWidth:1.5}}]}},
      options:{{responsive:true,maintainAspectRatio:true,
               scales:{{r:{{min:0,max:100,beginAtZero:true,
                            grid:{{color:gc}},angleLines:{{color:gc}},
                            ticks:{{display:false}},
                            pointLabels:{{color:tc,font:{{size:8}}}}}}}},
               plugins:{{legend:{{display:false}}}}}}
    }});
  }});
}});

// ── Accuracy Direction Chart ──
(function(){{
  var ctx = document.getElementById('accDirChart');
  if (!ctx) return;
  var dirs  = {_acc_dir_labels_js_safe};
  var accs  = {_acc_dir_accs_js_safe};
  var tots  = {_acc_dir_tots_js_safe};
  new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: dirs,
      datasets: [{{
        label: '准确率 %',
        data: accs,
        backgroundColor: ['#22c55e','#ef4444','#94a3b8'],
        borderRadius: 6,
        maxBarThickness: 40,
      }}]
    }},
    options: {{
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }}, tooltip: {{
        callbacks: {{ label: function(c){{ return c.raw.toFixed(1)+'% ('+tots[c.dataIndex]+' 次)'; }} }}
      }} }},
      scales: {{
        x: {{ min:0, max:100, ticks:{{ callback: function(v){{ return v+'%'; }} }} }},
        y: {{ grid: {{ display: false }} }}
      }}
    }}
  }});
}})();

// ── Accuracy Ticker Table Sort ──
(function(){{
  var tbl = document.getElementById('accTickerTable');
  if (!tbl) return;
  tbl.querySelectorAll('thead th').forEach(function(th, i){{
    th.addEventListener('click', function(){{
      var tbody = tbl.querySelector('tbody');
      var rows  = Array.from(tbody.rows);
      var asc   = th.getAttribute('data-sort') !== 'asc';
      tbl.querySelectorAll('thead th').forEach(function(t){{ t.removeAttribute('data-sort'); }});
      th.setAttribute('data-sort', asc ? 'asc' : 'desc');
      rows.sort(function(a, b){{
        var av = a.cells[i].textContent.trim();
        var bv = b.cells[i].textContent.trim();
        var an = parseFloat(av), bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
        return asc ? av.localeCompare(bv, 'zh') : bv.localeCompare(av, 'zh');
      }});
      rows.forEach(function(r){{ tbody.appendChild(r); }});
    }});
  }});
}})();
</script>
</body>
</html>"""

def main():
    """主入口"""

    # 解析命令行参数
    parser = argparse.ArgumentParser(
        description="Alpha Hive 每日投资简报生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法：
  # 传统 ML 模式（默认）
  python3 alpha_hive_daily_report.py
  python3 alpha_hive_daily_report.py --tickers NVDA TSLA VKTX
  python3 alpha_hive_daily_report.py --all-watchlist

  # 蜂群协作模式（7 个自治工蜂：6 核心 + BearBeeContrarian）
  python3 alpha_hive_daily_report.py --swarm --tickers NVDA TSLA VKTX
  python3 alpha_hive_daily_report.py --swarm --all-watchlist
        """
    )
    parser.add_argument(
        '--tickers',
        nargs='+',
        default=["NVDA", "TSLA", "VKTX", "META", "MSFT", "RKLB", "BILI", "AMZN", "CRCL"],
        help='要扫描的股票代码列表（空格分隔，默认：NVDA TSLA VKTX META MSFT RKLB BILI AMZN CRCL）'
    )
    parser.add_argument(
        '--all-watchlist',
        action='store_true',
        help='扫描配置中的全部监控列表'
    )
    parser.add_argument(
        '--swarm',
        action='store_true',
        help='启用蜂群协作模式（7 个自治工蜂：6 核心并行 + BearBeeContrarian 看空对冲）'
    )
    parser.add_argument(
        '--check-earnings',
        action='store_true',
        help='检查今日财报并自动更新简报（可单独运行，不需要重新扫描）'
    )
    parser.add_argument(
        '--no-llm',
        action='store_true',
        help='跳过询问，直接使用规则引擎模式（不调用 Claude API）'
    )
    parser.add_argument(
        '--use-llm',
        action='store_true',
        help='跳过询问，直接使用 LLM 混合模式'
    )

    args = parser.parse_args()

    # ── LLM 模式选择（每次跑简报前询问）──
    import llm_service as _llm_svc
    _llm_key_exists = bool(_llm_svc._load_api_key())

    if args.no_llm:
        use_llm = False
    elif args.use_llm:
        use_llm = True
    elif _llm_key_exists:
        print("\n┌─────────────────────────────────────────┐")
        print("│        Alpha Hive — 分析模式选择        │")
        print("├─────────────────────────────────────────┤")
        print("│  [1] LLM 混合模式  Claude API（推荐）   │")
        print("│      QueenDistiller + BuzzBee 语义增强  │")
        print("│      耗时 ~100s / 9 标的，约 $0.10      │")
        print("│                                         │")
        print("│  [2] 规则引擎模式  纯规则（测试迭代）   │")
        print("│      耗时 ~26s，$0 API 费用             │")
        print("└─────────────────────────────────────────┘")
        choice = input("请选择 [1/2，默认 1]：").strip()
        use_llm = (choice != "2")
    else:
        use_llm = False
        print("⚠️  未检测到 API Key，使用规则引擎模式")

    if not use_llm:
        _llm_svc.disable()
        print("🔧 规则引擎模式\n")
    else:
        print("🧠 LLM 混合模式（Claude API）\n")

    # 创建报告生成器
    reporter = AlphaHiveDailyReporter()

    # 如果只是检查财报更新
    if args.check_earnings:
        focus_tickers = list(WATCHLIST.keys())[:10] if args.all_watchlist else args.tickers
        result = reporter.check_earnings_updates(tickers=focus_tickers)
        reporting = result.get("reporting_today", [])
        updated = result.get("updated", [])
        if reporting:
            _log.info("今日财报: %s | 已更新: %s", reporting, updated)
        else:
            _log.info("今日无 watchlist 标的发布财报")
        return result

    # 确定扫描标的
    focus_tickers = list(WATCHLIST.keys())[:10] if args.all_watchlist else args.tickers

    if args.swarm:
        report = reporter.run_swarm_scan(focus_tickers=focus_tickers)
    else:
        report = reporter.run_daily_scan(focus_tickers=focus_tickers)

    # 保存报告（Hive app 通过 .swarm_results_{date}.json 自动同步）
    report_path = reporter.save_report(report)
    _log.info("报告已保存：%s", report_path)

    # 三端同步：GitHub 提交推送 + Hive App + Slack
    print("\n📡 同步三端：GitHub / Hive App / Slack...")
    try:
        sync_results = reporter.auto_commit_and_notify(report)
        git_ok = sync_results.get("git_push", {}).get("success", False)
        deploy_env = sync_results.get("deploy_env", "production")
        remote_label = sync_results.get("git_push", {}).get("remote", "origin")
        if deploy_env == "test":
            print(f"   GitHub push : {'✅' if git_ok else '⚠️  失败'} → 🔧 测试环境 https://wangmingjie36-creator.github.io/alpha-hive-test/")
        else:
            print(f"   GitHub push : {'✅' if git_ok else '⚠️  失败'} → 🧠 生产环境 https://wangmingjie36-creator.github.io/alpha-hive-deploy/")
        print(f"   Hive App    : ✅ .swarm_results 已落盘，下次启动自动加载")
    except (OSError, ValueError, KeyError, RuntimeError) as e:
        _log.warning("三端同步部分失败: %s", e)
        print(f"   ⚠️  三端同步出错：{e}")

    return report


if __name__ == "__main__":
    main()
