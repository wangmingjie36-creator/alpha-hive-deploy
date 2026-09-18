#!/usr/bin/env python3
"""
🐝 Alpha Hive — 单信号 IC 档案 (v0.43.2)
==========================================
把「哪块砖在承重」从每次重新考古，变成一张随时可查的表。

为什么需要
----------
系统有 7 只蜂、60+ 个原始字段，但只有 **5 个聚合维度**进入评估。
2026-07-30 的排查暴露了这个盲区——那天临时翻 `.swarm_results_*.json` 才发现：

  • 内幕情绪 IC = **−0.132**（方向是反的）
  • 内幕披露**笔数** IC = **+0.168**（唯一正向，但那是活跃度不是方向）
  • 蜂群一致度 IC = **+0.003**（零预测力，却被三重计入综合分）
  • cluster buying 样本 = **0 条**（学术上最强的信号，数据里一次没出现）

这些都是**原始信号层**的事实，聚合后就看不见了。没有持续档案，
每次讨论"该修哪里"都要从头挖一遍，而且样本永远停在临时抽取的那几十条。

设计
----
长表 `signal_archive(date, ticker, signal, value)`：新增信号无需改 schema。
前瞻收益在分析时从 `predictions` 联表（已有 price_t7/t30），不重复存储。

用法
----
    # 从历史 .swarm_results_*.json 回填（一次性）
    /usr/local/bin/python3 signal_archive.py --backfill

    # 分析：每个信号的四口径 IC + 噪音地板对照（系统输出只算当前世代，见「世代切片」）
    /usr/local/bin/python3 signal_archive.py --analyze
    /usr/local/bin/python3 signal_archive.py --analyze --min-samples 100
    /usr/local/bin/python3 signal_archive.py --analyze --pool-generations   # 跨世代混算（仅作对照）

    # 查看已归档的信号清单与覆盖度
    /usr/local/bin/python3 signal_archive.py --list

日常扫描会自动写入（挂在 `_post_scan_enrichment`）。
"""

from __future__ import annotations

import argparse
import datetime
import ast
import glob
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from collections import defaultdict
from fnmatch import fnmatchcase
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

# v0.45.160：`DB_PATH` 现在是**覆盖钩子**，默认 `None` ⇒ 运行时解析 `PATHS.db`。
# 保留这个名字是因为 `tests/` 有多处 `monkeypatch.setattr(<mod>, "DB_PATH", ...)`
# 依赖它做隔离；把它删掉会把那些测试打红。
DB_PATH = None

def _db_path() -> Path:
    """本模块用的 `pheromone.db` 路径。**调用时求值。**

    v0.45.160：这里原本是 `Path(__file__).parent / "pheromone.db"`。
    那个写法比「模块级常量冻在 import 期」更彻底——它**压根不读任何环境变量**：
    `ALPHA_HIVE_DB_PATH` / `ALPHA_HIVE_HOME` 设成什么都无效，连改成懒求值都救不了，
    只能显式改去读 `PATHS.db`。于是 `tests/conftest.py::_isolate_env` 对它完全无效。
    实测：无参 `ensure_schema()` 会在 **checkout 根目录**的 `pheromone.db` 里建出
    `signal_archive` 表；主 checkout 上那就是生产库（`signal_archive` 84839 行，
    喂 IC 闸 / 概率记分卡 / 权重学习闭环）。

    ⚠️ 下面所有默认参数一律写 `None`，**不要**写 `= DB_PATH` 或 `= _db_path()`——
    默认参数在 `def` 执行时（＝import 期）求值，等于换个地方冻同一个值
    （同型 v0.45.37 / v0.45.150）。
    """
    if DB_PATH is not None:
        return Path(DB_PATH)
    from hive_logger import PATHS
    return Path(PATHS.db)
TABLE = "signal_archive"


# ────────────────────────────────────────────────────────────────────────────
# 信号提取器（声明式：加一个信号 = 加一行）
# ────────────────────────────────────────────────────────────────────────────

def _dig(obj: Any, path: str) -> Any:
    """按点号路径取值，任一层缺失返回 None"""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _num(v: Any) -> Optional[float]:
    """转成 float；bool 与不可转换值返回 None（避免 True→1.0 混入数值信号）"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _notable_trades(tr: Dict) -> List[Dict]:
    """notable_trades 在部分历史文件里被存成 repr 字符串，需容错解析"""
    nt = _dig(tr, "agent_details.ScoutBeeNova.details.insider.notable_trades")
    if isinstance(nt, str):
        try:
            nt = ast.literal_eval(nt)
        except (ValueError, SyntaxError):
            return []
    return nt if isinstance(nt, list) else []


def _insider_buys(tr: Dict) -> List[Dict]:
    """只保留 code=P（公开市场买入）。

    SEC Form 4 交易代码：P=公开市场买入、S=卖出、A=授予、M=行权、F=代扣税。
    学术上只有 P 被认为含信息量；其余多为行权/税务/计划内减持。
    """
    return [t for t in _notable_trades(tr)
            if isinstance(t, dict) and t.get("code") == "P"]


def _distinct_buyers(tr: Dict) -> Optional[float]:
    buys = _insider_buys(tr)
    return float(len({t.get("insider") for t in buys})) if buys else 0.0


def _officer_buys(tr: Dict) -> Optional[float]:
    return float(sum(1 for t in _insider_buys(tr) if t.get("is_officer")))


def _sentiment_num(tr: Dict) -> Optional[float]:
    m = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}
    return m.get(_dig(tr, "agent_details.ScoutBeeNova.details.insider.sentiment"))


def _direction_num(agent: str) -> Callable:
    m = {"bullish": 1.0, "long": 1.0, "neutral": 0.0, "bearish": -1.0, "short": -1.0}

    def f(tr: Dict) -> Optional[float]:
        d = _dig(tr, f"agent_details.{agent}.direction")
        return m.get(str(d).lower()) if d else None
    return f


def _path(p: str) -> Callable:
    return lambda tr: _num(_dig(tr, p))


# ── v0.45.33: 维度**输入**提取器 ─────────────────────────────────────────
# 此前档案只存各蜂的**输出分数**，于是「改 crowding 公式会不会更准」这类
# 问题无法离线重放，只能前向累积 25 个不重叠周（约半年）才知道。
# 输入一旦落库，维度计算层的改动也能当场重放（见 replay_scoring.py）。
# 全部走 details 里已有的字段，扫描侧零额外开销。

def _crowding_comp(key: str) -> Callable:
    """拥挤度分量。v0.45.30 前后键名不同（stocktwits_volume → social_volume），
    两个都试 —— 历史回填必须能读旧名，否则等于丢掉改名前的全部样本。"""
    _legacy = {"social_volume": "stocktwits_volume"}

    def _f(tr: Dict) -> Optional[float]:
        comp = _dig(tr, "agent_details.ScoutBeeNova.details.components")
        if not isinstance(comp, dict):
            return None
        v = comp.get(key)
        if v is None and key in _legacy:
            v = comp.get(_legacy[key])
        return _num(v)
    return _f


def _buzz_comp(key: str) -> Callable:
    def _f(tr: Dict) -> Optional[float]:
        comp = _dig(tr, "agent_details.BuzzBeeWhisper.details.components")
        return _num(comp.get(key)) if isinstance(comp, dict) else None
    return _f


def _fear_greed_is_cnn(tr: Dict) -> Optional[float]:
    """1.0 = CNN **股票**市场 F&G；0.0 = Alternative.me **加密**市场 F&G（备用源）。

    两者不是一个量（2026-03-10~13 实测记录值 13/15 是加密 F&G，CNN 当日 17–22）。
    读 `market.fear_greed` 的任何分析都必须先按这一列切开，同 `options.iv_rank_is_real`。
    值不是真实观测（兜底）时返回 None —— 那一行 `market.fear_greed` 本来也不入档。
    """
    fg = _dig(tr, "agent_details.BuzzBeeWhisper.details.fear_greed")
    if not isinstance(fg, dict) or not fg.get("is_real_data") or fg.get("value") is None:
        return None
    return {"cnn": 1.0, "alternative_me": 0.0}.get(fg.get("source"))


# ⚠️ v0.45.35 修：权重表**从 ChronosBee 读**，不再复制第二份。
# 初版手抄了一份，漏了 6 个类型（split/dividend/dividendDate/analyst_day/
# conference/exDividendDate）且默认值写成 0.8（蜂内是 0.7）。后果不是小偏差：
# 实测最常见的催化剂正是 Dividend/Ex-Dividend（蜂内 0.4/0.3），
# 归档里全按默认 0.8 算，**高估一倍以上**，875 行已回填数据全错。
# 同款教训见 v0.45.30 的 CrowdingDetector 硬编码第二份权重。
# 惰性导入：signal_archive 被 swarm_agents 依赖会成环，故不在模块顶层导。
_CAT_TABLES: Optional[tuple] = None


def _cat_tables() -> tuple:
    """(type_weights, severity_mult, type_default)。取不到就抛 —— 归档一份
    与蜂内不一致的权重，比不归档更糟（重放会给出看似精确的错误结论）。"""
    global _CAT_TABLES
    if _CAT_TABLES is None:
        from swarm_agents.chronos_bee import ChronosBeeHorizon as _C
        _CAT_TABLES = (dict(_C.CATALYST_TYPE_WEIGHTS),
                       dict(_C.CATALYST_SEVERITY_MULT),
                       _C._CATALYST_TYPE_DEFAULT)
    return _CAT_TABLES


def _catalysts(tr: Dict) -> List[Dict]:
    c = _dig(tr, "agent_details.ChronosBeeHorizon.details.catalysts")
    return [x for x in c if isinstance(x, dict)] if isinstance(c, list) else []


def _cat_count(tr: Dict) -> Optional[float]:
    """催化剂条数。注意 0 与「来源不可得」不同 —— 后者自 v0.45.31 起
    ChronosBee 返回 error，details 缺失，本函数返回 None（诚实缺失）。"""
    if _dig(tr, "agent_details.ChronosBeeHorizon.details") is None:
        return None
    return float(len(_catalysts(tr)))


def _cat_nearest_days(tr: Dict) -> Optional[float]:
    days = [_num(c.get("days_until")) for c in _catalysts(tr)]
    days = [d for d in days if d is not None and d >= 0]
    return float(min(days)) if days else None


def _cat_max_weight(tr: Dict) -> Optional[float]:
    """最强催化剂的 type_w × sev_m —— 重放评分公式时的关键输入。
    权重表与 ChronosBee 同源（见 _cat_tables），复制第二份必然漂移。"""
    cats = _catalysts(tr)
    if not cats:
        return None
    tw, sm, tdef = _cat_tables()
    best = None
    for c in cats:
        w = tw.get(c.get("type", ""), tdef) * sm.get(c.get("severity", "medium"), 1.0)
        best = w if best is None else max(best, w)
    return best


def _iv_rank_is_real(tr: Dict) -> Optional[float]:
    """1.0 = 真实自攒 IV 历史；0.0 = hv_proxy（数学上等于 HV Rank，非 IV）。
    读 iv_rank 的任何分析都必须先看这一列，见 MEMORY alpha-hive-iv-rank。"""
    src = _dig(tr, "agent_details.OracleBeeEcho.details.iv_rank_source")
    if not isinstance(src, str) or not src:
        return None
    return 0.0 if src == "hv_proxy" else 1.0


def _code_exec_fetch(key: str) -> Callable:
    """CodeExecutorAgent 取数脚本（`CodeGenerator._generate_yfinance`）的输出字段。

    v0.45.250：同一个 `data` 在 `analyze()` 里有**两个位置**，按路径分派：
      · 技术分析跑通 ⇒ `details = {price, sma_20, rsi_signal, fetch_data: data, ...}`
      · 技术分析失败（兜底）⇒ `details = data`

    两种形状自 2026-02-25 就并存。本信号 2026-07-30 写成只读顶层时是对的——
    那时技术分析脚本撞 yfinance MultiIndex 每次必崩，100% 走兜底。v0.43.10
    （2026-08-12）修好它之后成功路径成了主路径，`fund.*` 每天 30 只只剩 0–4 只入档，
    静默断供近一个月。**同一个量换了位置 ⇒ 合并读**（同 `_crowding_comp` 的 `_legacy`，
    判据见下方 `guard.consistency_census` 注释；值跨形状连续、两形状互斥均已实测）。

    ⚠️ 按「有没有 `fetch_data`」分派，**不要**改成「嵌套取不到就回头读顶层」：
    成功路径的顶层是技术分析的命名空间，日后多出一个同名键就会被静默混读。
    守卫：`tests/test_signal_archive_code_executor_shapes.py`（驱动真实 `analyze()`）。
    """
    def _f(tr: Dict) -> Optional[float]:
        det = _dig(tr, "agent_details.CodeExecutorAgent.details")
        if not isinstance(det, dict):
            return None
        src = det.get("fetch_data") if "fetch_data" in det else det
        return _num(src.get(key)) if isinstance(src, dict) else None
    return _f


def _agent_score(agent: str) -> Callable:
    return lambda tr: _num(_dig(tr, f"agent_details.{agent}.score"))


def _swarm_agreement(tr: Dict) -> Optional[float]:
    """蜂群方向一致度 = 多数派占比。2026-07-30 实测 IC=+0.003（零预测力），
    却经 GuardBee 基础分 + queen 共振加成被三重计入综合分。"""
    dirs = []
    for _ag, d in (tr.get("agent_details") or {}).items():
        v = (d or {}).get("direction")
        if v:
            dirs.append(str(v).lower())
    if len(dirs) < 3:
        return None
    from collections import Counter
    c = Counter(dirs)
    return c.most_common(1)[0][1] / len(dirs)


def _guard_census_consistency(tr: Dict) -> Optional[float]:
    """GuardBee 普查口径的一致性。**只认 `census_source == "live_agent_view"`。**

    v0.45.256：v0.45.182 把名字改成 `guard.consistency_census`，抽取器却仍是无条件
    的 `_path(...consistency)`。新扫描不受影响（写入的都是新口径），但 `backfill()`
    用**当前**抽取器重写**全部**历史文件 ⇒ 全量回填把 v0.45.163 之前的旧口径写进新名字，
    改名拆开的两段定义被重新池化（生产库副本实测：1,394 行旧口径 vs 90 行新口径）。
    **改名只拆开了未来，没拆开回填。**

    白名单，不是「非空即可」：
      · 缺失 ⇒ v0.45.163 之前，分母是排行榜窗口条数；
      · `top_signals_fallback` ⇒ 窗口 24 的排行榜，第三种口径；
      · `unavailable` ⇒ 板读取失败，`consistency` 是兜底写的 0，不是观测值。
    判别照 `tests/test_distribution_invariants.py::census_coverage_offenders` 的先例。
    标记是与 `guard_bee._read_census` 的字符串契约，改名会让本信号静默停档 ——
    守卫：`tests/test_guard_census_eviction.py::test_archive_reads_real_census_output`。
    """
    det = _dig(tr, "agent_details.GuardBeeSentinel.details")
    if not isinstance(det, dict) or det.get("census_source") != "live_agent_view":
        return None
    return _num(det.get("consistency"))


#: 信号名 → 提取函数。命名约定 `来源.字段`，便于按前缀筛选。
SIGNAL_EXTRACTORS: Dict[str, Callable[[Dict], Optional[float]]] = {
    # ── 聚合层（对照基准）────────────────────────────────────────
    "composite.final_score": lambda tr: _num(tr.get("final_score")),
    "composite.swarm_agreement": _swarm_agreement,

    # ── 内幕 / 聪明钱（系统立身之本）──────────────────────────────
    "insider.sentiment": _sentiment_num,
    "insider.score": _path("agent_details.ScoutBeeNova.details.insider.score"),
    "insider.filings": _path("agent_details.ScoutBeeNova.details.insider.filings"),
    "insider.dollar_bought": _path("agent_details.ScoutBeeNova.details.insider.dollar_bought"),
    "insider.dollar_sold": _path("agent_details.ScoutBeeNova.details.insider.dollar_sold"),
    "insider.distinct_buyers": _distinct_buyers,      # cluster buying 的分子
    "insider.officer_buys": _officer_buys,
    "congress.score": _path("agent_details.ScoutBeeNova.details.congress.congress_score"),

    # ── 拥挤度 / 关注度 ─────────────────────────────────────────
    "crowding.score": _path("agent_details.ScoutBeeNova.details.crowding_score"),
    "crowding.signal": _path("agent_details.ScoutBeeNova.details.crowding_signal"),
    "crowding.adj_factor": _path("agent_details.ScoutBeeNova.details.adjustment_factor"),

    # ── 价格 / 动量 ────────────────────────────────────────────
    "price.momentum_5d": _path("agent_details.ScoutBeeNova.details.momentum_5d"),
    "price.volatility_20d": _path("agent_details.BuzzBeeWhisper.details.volatility_20d"),
    "price.volume_ratio": _path("agent_details.BuzzBeeWhisper.details.volume_ratio"),

    # ── 期权 ──────────────────────────────────────────────────
    "options.iv_current": _path("agent_details.OracleBeeEcho.details.iv_current"),
    "options.put_call_ratio": _path("agent_details.OracleBeeEcho.details.put_call_ratio"),
    "options.gamma_exposure": _path("agent_details.OracleBeeEcho.details.gamma_exposure"),
    "options.total_oi": _path("agent_details.OracleBeeEcho.details.total_oi"),

    # ── 情绪 ──────────────────────────────────────────────────
    "sentiment.pct": _path("agent_details.BuzzBeeWhisper.details.sentiment_pct"),

    # ── 看空侧（唯一勉强显著的方向）──────────────────────────────
    "bear.score": _path("agent_details.BearBeeContrarian.details.bear_score"),
    "bear.insider_bear": _path("agent_details.BearBeeContrarian.details.insider_bear"),
    "bear.overval_bear": _path("agent_details.BearBeeContrarian.details.overval_bear"),
    "bear.options_bear": _path("agent_details.BearBeeContrarian.details.options_bear"),
    "bear.short_int_bear": _path("agent_details.BearBeeContrarian.details.short_int_bear"),

    # ── v0.45.33: 维度输入（供离线重放维度计算层的改动）──────────
    "crowding.comp.social_volume":      _crowding_comp("social_volume"),
    "crowding.comp.google_trends":      _crowding_comp("google_trends"),
    "crowding.comp.consensus_strength": _crowding_comp("consensus_strength"),
    "crowding.comp.seeking_alpha_views": _crowding_comp("seeking_alpha_views"),
    "crowding.comp.short_squeeze_risk": _crowding_comp("short_squeeze_risk"),
    "catalyst.count":         _cat_count,
    "catalyst.nearest_days":  _cat_nearest_days,
    "catalyst.max_weight":    _cat_max_weight,
    "buzz.comp.momentum_signal":   _buzz_comp("momentum_signal"),
    "buzz.comp.volume_signal":     _buzz_comp("volume_signal"),
    "buzz.comp.volatility_signal": _buzz_comp("volatility_signal"),
    "buzz.comp.reddit_signal":     _buzz_comp("reddit_signal"),
    # v0.45.247：Buzz 合成的另外三个通道此前不在 details 里，sentiment 维度重放不了。
    "buzz.comp.news_signal":       _buzz_comp("news_signal"),
    "buzz.comp.yahoo_signal":      _buzz_comp("yahoo_signal"),
    "buzz.comp.fear_greed_signal": _buzz_comp("fear_greed_signal"),
    # 市场级常量：当天所有标的同值 ⇒ 横截面 IC 恒不可算（analyze 按全并列日跳过），
    # 存它是为了离线重放「F&G 政体调整」这类聚合层规则。先看 is_cnn 再用。
    "market.fear_greed":        _path("agent_details.BuzzBeeWhisper.details.fear_greed.value"),
    "market.fear_greed_is_cnn": _fear_greed_is_cnn,
    "options.iv_rank":         _path("agent_details.OracleBeeEcho.details.iv_rank"),
    "options.iv_percentile":   _path("agent_details.OracleBeeEcho.details.iv_percentile"),
    "options.iv_rank_is_real": _iv_rank_is_real,

    # ── 共振 / 一致性（回声源头）─────────────────────────────────
    # v0.45.182：`guard.consistency` → `guard.consistency_census`。**改名不是洁癖。**
    # v0.45.163 把 GuardBee 改走普查读法后，这个量的分母由「排行榜窗口条数(≤5)」
    # 变成「本轮发布过的蜂数」—— 逐扫描日实测 0.50~0.72 → 0.47~0.49，是**换了量**
    # 不是换了名字。而 `analyze()` 既不按日期、也不按 `_COHORT_HISTORY` 切片
    # （`load_panel` 直接拉整张表），同名延续 = 两段定义被池化。
    #
    # ⚠️ 判据：**名字变了，还是「量」变了？**
    #   · 同一个量换名字（`stocktwits_volume` → `social_volume`）⇒ 合并读，
    #     见上面 `_crowding_comp` 的 `_legacy` 映射。
    #   · 同一个名字换量（本条）⇒ 拆成两条序列。
    # 为什么不加一列口径标记：`value` 是 REAL 存不下字符串标签；且加列等于要求
    # 每个消费方**记得**去 join，忘了就退回同一个静默 bug。改名之后「忘了」
    # 在结构上不可能发生。退役名单钉在 `tests/test_signal_archive.py::RETIRED_SIGNAL_NAMES`。
    #
    # ⚠️ v0.45.256：改名只管得住**新写入**。`backfill()` 用当前抽取器重写全部历史，
    # 所以新名字必须**按口径取值**，否则一次全量回填就把旧口径写回来 —— 见
    # `_guard_census_consistency`。`adj_factor` / `macro_adj` 实测未换量，不门控。
    #
    # v0.45.265：`analyze()` 已按世代切片（见 `COHORT_SIGNAL_SCOPE`），但**改名仍必要**——
    # 直接读表的消费方（如 `experiments/misjudgment_pattern_walkforward.py`）不经过 `analyze()`。
    "guard.consistency_census": _guard_census_consistency,
    "guard.adj_factor": _path("agent_details.GuardBeeSentinel.details.adjustment_factor"),
    "guard.macro_adj": _path("agent_details.GuardBeeSentinel.details.macro_adj"),
    # v0.45.182：`guard.top_signals_count` 已摘除。v0.45.163 之后它恒等于「本轮
    # Guard 之前发布过的蜂数」，票内方差为零 —— 不是信号，只会被 `analyze()`
    # 年年判成「纯标签」占位。它仍是一条**会红**的健康探针（不再恒定 ⇒ 有蜂停发，
    # 或普查读法被改回排行榜），改挂
    # `tests/test_distribution_invariants.py::TestGuardCensusCoverage`
    # （期望值从 `agent_details` 导出，不写死 6 —— 关掉 CodeExecutor 时应得是 5）。
    # 库里的历史行不改写，只是不再写入新行。

    # ── ML 预测 ───────────────────────────────────────────────
    "ml.probability": _path("agent_details.RivalBeeVanguard.details.probability"),
    "ml.expected_7d": _path("agent_details.RivalBeeVanguard.details.expected_7d"),
    "ml.expected_30d": _path("agent_details.RivalBeeVanguard.details.expected_30d"),

    # ── 基本面快照 ─────────────────────────────────────────────
    # v0.45.250：原为 `_path("...CodeExecutorAgent.details.<键>")`，只认兜底路径的形状。
    "fund.pe_ratio": _code_exec_fetch("pe_ratio"),
    "fund.market_cap": _code_exec_fetch("market_cap"),

    # ── 各蜂原始分与方向 ───────────────────────────────────────
    **{f"agent.{a}.score": _agent_score(a) for a in (
        "ScoutBeeNova", "BuzzBeeWhisper", "OracleBeeEcho",
        "ChronosBeeHorizon", "RivalBeeVanguard", "GuardBeeSentinel",
        "BearBeeContrarian")},
    **{f"agent.{a}.direction": _direction_num(a) for a in (
        "ScoutBeeNova", "BuzzBeeWhisper", "OracleBeeEcho",
        "ChronosBeeHorizon", "RivalBeeVanguard", "GuardBeeSentinel",
        "BearBeeContrarian")},
}


# ────────────────────────────────────────────────────────────────────────────
# 存储
# ────────────────────────────────────────────────────────────────────────────

#: 数据隔离名单 —— 已证实取自错误交易日的观测，禁止入库。
#:
#: 为什么需要它：`backfill()` 从 `.swarm_results_*.json` 用 `INSERT OR REPLACE`
#: 重建，所以**光删库不够**，下一次回填会把污染原样带回来。原始 JSON 刻意不改
#: （它是"系统当天实际产出什么"的审计轨迹），改由本名单在入库口拦截。
#:
#: 划界原则：只隔离**当日市场观测**（取错日子 ⇒ 值本身就是错的）；
#: **不隔离 Agent 评分**——那些是系统当天的真实输出，属审计轨迹，
#: 抹掉它们会让"系统当时做了什么"这个问题永远查不清。
#: 代价是 OracleBee 等下游评分在该日仍基于坏输入，已在 reason 里写明。
QUARANTINE: List[Dict] = [
    {
        "date": "2026-08-24",
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi",
                    "bear.options_bear"),
        "reason": (
            "v0.45.16 补跑槽位 bug：8/24 的报告实际在 8/25 07:37–12:44 生成，"
            "期权快照键取 pdt_today() 而非 --date 目标日，于是 30 只标的全部"
            "拿到 8/25 的期权链。日志实证：24 只命中 options_snapshot_*_2026-08-25.json，"
            "5 只（CRM/JNJ/NEE/WMT/XOM）现拉后写入同一个 8/25 槽位，"
            "BRK-B 于 12:44 单独重跑同样写入 8/25 槽位。"
            "⚠️ 期权接口只有实时快照、无历史 ⇒ 8/24 的真实期权观测**永久丢失**，"
            "不可补，只能缺失。"
        ),
        "evidence": ("logs/backfill_2026-08-24.log", "logs/rerun_brkb.log"),
    },
    # ────────────────────────────────────────────────────────────────────────
    # v0.45.266：v0.45.238 期权快照会话错位普查（11 个坏槽位日，171 份）
    #
    # 与 08-24 那条不同：这批**同一天里有的标的坏、有的标的没坏**（跨午夜/盘前
    # 起跑的扫描只碰到了当时还没收盘的那几只，其余标的当天照常拿到自己的会话），
    # 故每条都用 `tickers` 精确点名，不做整天隔离，免得连累好数据。
    #
    # 判据与 CHANGELOG v0.45.238 一致：`_snapshot_timestamp` 推出的 ET 交易会话
    # （`cboe_options.session_date_at`）与槽位文件名日期不同，即该份快照装的是
    # **前一交易会话**的期权链。2026-09-15 用同一判据对 `cache/` 里这 11 天的
    # 快照重新逐份判定（不是凭 CHANGELOG 的日份摘要去猜标的），171 份全部复现，
    # 与 CHANGELOG 报的总数一致；09-03/09-09 两天另有当日日志佐证（24/29 只命中）。
    # 五类信号沿用 08-24 那条的范围（不含 iv_rank/iv_percentile/iv_rank_is_real——
    # 这两者是否同等受损**未验证**，留待日后需要时再评估，不在此处扩大隔离面）。
    #
    # ⚠️ 同 08-24：期权接口只有实时快照、无历史 ⇒ 这些标的当天的真实期权观测
    # **永久丢失**，不可补，只能缺失。
    {
        "date": "2026-06-10",
        "tickers": ("AMZN", "BILI", "CRCL", "META", "MSFT", "NVDA", "QCOM",
                    "RKLB", "TEST", "TSLA", "VKTX"),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": (
            "v0.45.238 期权快照会话错位：跨午夜/盘前起跑的扫描把前一交易会话的"
            "期权链写进了当日槽位。11/11 份快照复核为陈旧（当日无一只正常）。"
        ),
        "evidence": ("CHANGELOG.md#v0.45.238", "CHANGELOG.md#v0.45.266"),
    },
    {
        "date": "2026-07-08",
        "tickers": ("NVDA",),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": (
            "同上机制。10 份快照里仅 NVDA 一只陈旧（其余 9 只当日照常拿到自己的会话），"
            "故只隔离 NVDA。"
        ),
        "evidence": ("CHANGELOG.md#v0.45.238", "CHANGELOG.md#v0.45.266"),
    },
    {
        "date": "2026-07-15",
        "tickers": ("AMZN", "BILI", "CRCL", "META", "MSFT", "NVDA", "QCOM",
                    "RKLB", "TSLA", "VKTX"),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": (
            "v0.45.238 期权快照会话错位：跨午夜/盘前起跑的扫描把前一交易会话的期权链"
            "写进了当日槽位。10/10 份快照复核为陈旧（当日无一只正常）。"
        ),
        "evidence": ("CHANGELOG.md#v0.45.238", "CHANGELOG.md#v0.45.266"),
    },
    {
        "date": "2026-07-17",
        "tickers": ("AMZN", "BILI", "CRCL", "META", "MSFT", "NVDA", "QCOM",
                    "RKLB", "TSLA", "VKTX"),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": (
            "v0.45.238 期权快照会话错位：跨午夜/盘前起跑的扫描把前一交易会话的期权链"
            "写进了当日槽位。10/10 份快照复核为陈旧（当日无一只正常）。"
        ),
        "evidence": ("CHANGELOG.md#v0.45.238", "CHANGELOG.md#v0.45.266"),
    },
    {
        "date": "2026-07-22",
        "tickers": ("AMZN", "BILI", "CRCL", "META", "MSFT", "NVDA", "QCOM",
                    "RKLB", "TSLA", "VKTX"),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": (
            "v0.45.238 期权快照会话错位：跨午夜/盘前起跑的扫描把前一交易会话的期权链"
            "写进了当日槽位。10/10 份快照复核为陈旧（当日无一只正常）。"
        ),
        "evidence": ("CHANGELOG.md#v0.45.238", "CHANGELOG.md#v0.45.266"),
    },
    {
        "date": "2026-07-23",
        "tickers": ("AMZN", "BILI", "CRCL", "META", "MSFT", "NVDA", "QCOM",
                    "RKLB", "TSLA", "VKTX"),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": (
            "v0.45.238 期权快照会话错位：跨午夜/盘前起跑的扫描把前一交易会话的期权链"
            "写进了当日槽位。10/10 份快照复核为陈旧（当日无一只正常）。"
        ),
        "evidence": ("CHANGELOG.md#v0.45.238", "CHANGELOG.md#v0.45.266"),
    },
    {
        "date": "2026-07-24",
        "tickers": ("AMZN", "BILI", "CRCL", "META", "MSFT", "NVDA", "QCOM",
                    "RKLB", "TSLA", "VKTX"),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": (
            "v0.45.238 期权快照会话错位：跨午夜/盘前起跑的扫描把前一交易会话的期权链"
            "写进了当日槽位。10/10 份快照复核为陈旧（当日无一只正常）。"
        ),
        "evidence": ("CHANGELOG.md#v0.45.238", "CHANGELOG.md#v0.45.266"),
    },
    {
        "date": "2026-08-11",
        "tickers": ("ABBV", "AMC", "AMZN", "BILI", "COST", "CRCL", "CRM", "CVX",
                    "DE", "DELL", "ENPH", "JNJ", "META", "MSFT", "MU", "NEE",
                    "NFLX", "NVDA", "QCOM", "RKLB", "SNOW", "T", "TMO", "TMUS",
                    "TSLA", "VKTX", "VZ", "WMT", "XOM"),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": (
            "v0.45.238 期权快照会话错位：跨午夜/盘前起跑的扫描把前一交易会话的期权链"
            "写进了当日槽位。29/29 份快照复核为陈旧（当日无一只正常）。"
        ),
        "evidence": ("CHANGELOG.md#v0.45.238", "CHANGELOG.md#v0.45.266"),
    },
    {
        "date": "2026-08-14",
        "tickers": ("ABBV", "AMC", "AMZN", "BILI", "COST", "CRCL", "CRM", "CVX",
                    "DE", "DELL", "ENPH", "JNJ", "META", "MSFT", "NEE", "NFLX",
                    "NVDA", "RKLB", "SNOW", "T", "TMO", "TMUS", "TSLA", "VKTX",
                    "VZ", "WMT", "XOM"),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": (
            "v0.45.238 期权快照会话错位：跨午夜/盘前起跑的扫描把前一交易会话的期权链"
            "写进了当日槽位。27/29 份快照复核为陈旧，MU/QCOM 当日正常，不隔离。"
        ),
        "evidence": ("CHANGELOG.md#v0.45.238", "CHANGELOG.md#v0.45.266"),
    },
    {
        "date": "2026-09-03",
        "tickers": ("ABBV", "AMC", "AMZN", "BILI", "BRK-B", "COST", "CRCL",
                    "CRM", "CVX", "DE", "DELL", "JNJ", "META", "MSFT", "MU",
                    "NFLX", "NVDA", "QCOM", "RKLB", "T", "TSLA", "VKTX", "VZ", "XOM"),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": (
            "同上机制，本条另有当日日志佐证（CHANGELOG v0.45.238：命中 42 次 / "
            "写入 6 次）。24/30 份快照复核为陈旧，ENPH/NEE/SNOW/TMO/TMUS/WMT "
            "当日正常，不隔离。"
        ),
        "evidence": ("logs/2026-09-03.log", "CHANGELOG.md#v0.45.238", "CHANGELOG.md#v0.45.266"),
    },
    {
        "date": "2026-09-09",
        "tickers": ("ABBV", "AMC", "AMZN", "BILI", "BRK-B", "COST", "CRCL",
                    "CRM", "CVX", "DE", "DELL", "ENPH", "JNJ", "META", "MU",
                    "NEE", "NFLX", "NVDA", "QCOM", "RKLB", "SNOW", "T", "TMO",
                    "TMUS", "TSLA", "VKTX", "VZ", "WMT", "XOM"),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": (
            "同上机制，本条另有当日日志佐证（CHANGELOG v0.45.238：命中 45 次 / "
            "写入 1 次）。29/30 份快照复核为陈旧，MSFT 当日正常，不隔离。"
        ),
        "evidence": ("logs/2026-09-09.log", "CHANGELOG.md#v0.45.238", "CHANGELOG.md#v0.45.266"),
    },
]


def is_quarantined(date: str, ticker: str, signal: str) -> bool:
    """该 (日期, 标的, 信号) 是否在隔离名单内。入库与分析都应先问这一句。

    v0.45.266：加 `ticker` 参数——2026-08-24 那次污染是全体标的一起中招，
    但 v0.45.238 的期权快照会话错位普查显示同一天可能只有部分标的陈旧
    （例：2026-09-03 只有 24/30 只）。条目缺 `tickers` 键 ⇒ 对当天全部标的
    生效（08-24 那条的既有语义，未改）；给了 `tickers` 就只挡白名单内的标的。
    """
    for q in QUARANTINE:
        if q["date"] != date or signal not in q["signals"]:
            continue
        tickers = q.get("tickers")
        if tickers is None or ticker in tickers:
            return True
    return False


def ensure_schema(db_path: Optional[Path] = None) -> None:
    db_path = Path(db_path) if db_path else _db_path()   # v0.45.160：调用时求值
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                signal TEXT NOT NULL,
                value REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, ticker, signal)
            )
        """)
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_sigarch_signal "
                     f"ON {TABLE}(signal, date)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_sigarch_date "
                     f"ON {TABLE}(date)")
        conn.commit()


def extract(ticker_result: Dict) -> Dict[str, float]:
    """从单个标的的 swarm 结果里抽出全部数值信号"""
    out = {}
    for name, fn in SIGNAL_EXTRACTORS.items():
        try:
            v = fn(ticker_result)
        except Exception:  # noqa: BLE001 - 单个信号失败不应影响其余
            v = None
        if v is not None and math.isfinite(v):
            out[name] = float(v)
    return out


def _check_only(only) -> Optional[frozenset]:
    """`only` 里的名字必须都是现役信号。拼错一个 ⇒ 静默「回填 0 行」，
    看起来与「本来就不缺」同形，故直接抛。"""
    if only is None:
        return None
    only = frozenset(only)
    unknown = sorted(only - set(SIGNAL_EXTRACTORS))
    if unknown:
        raise ValueError(f"未知信号名：{unknown}（现役信号见 SIGNAL_EXTRACTORS / --list）")
    return only


def _rows_for(swarm_results: Dict, date: str, only: Optional[frozenset] = None) -> List[tuple]:
    """一次扫描会写入的 (date, ticker, signal, value) 行。`archive` 与 dry-run 共用，
    保证 dry-run 报的数就是真跑会写的数。"""
    rows = []
    for ticker, tr in (swarm_results or {}).items():
        if not isinstance(tr, dict):
            continue
        for sig, val in extract(tr).items():
            if only is not None and sig not in only:
                continue
            # v0.45.26：隔离名单在**入库口**拦截，而不是在分析时过滤——
            # 后者会让每个下游都得记得过滤一次，漏一个就前功尽弃。
            if is_quarantined(date, ticker, sig):
                continue
            rows.append((date, ticker, sig, val))
    return rows


def archive(swarm_results: Dict, date: str, db_path: Optional[Path] = None,
            only=None) -> int:
    """把一次扫描的全部原始信号写入档案。

    Args:
        date: **业务日期**（YYYY-MM-DD）。与 predictions 表同键，供联表。
        only: 只写这些信号名（None = 全部）。v0.45.250，供定向回填。

    Returns:
        写入的 (ticker, signal) 行数
    """
    db_path = Path(db_path) if db_path else _db_path()   # v0.45.160：调用时求值
    if not swarm_results:
        return 0
    only = _check_only(only)
    ensure_schema(db_path)
    rows = _rows_for(swarm_results, date, only)
    if not rows:
        return 0
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO {TABLE} (date,ticker,signal,value) "
            f"VALUES (?,?,?,?)", rows)
        conn.commit()
    return len(rows)


def backfill(pattern: str = ".swarm_results_*.json",
             db_path: Optional[Path] = None, only=None,
             dry_run: bool = False) -> Dict[str, Any]:
    """从历史 .swarm_results_*.json 回填。幂等（UNIQUE + REPLACE）。

    ⚠️ 不带 `only` 时用**当前**抽取器重写**全部**历史文件 ⇒ 任何一个「名字延续、
    但没按口径取值」的抽取器，都会借回填把旧口径写回来。补某几个信号时务必 `only=`
    限定，并先 `dry_run=True` 看 `by_signal`。

    实例（v0.45.250 发现、v0.45.256 已修）：`guard.consistency_census` 原先无条件取值，
    全量回填会新增 1,394 行 v0.45.163 之前的旧口径，把 v0.45.182 改名拆开的两段定义
    重新池化；现只认 `census_source == "live_agent_view"`（见 `_guard_census_consistency`）。
    修后生产库副本全量 dry-run（2026-09-15，63 个信号）：只有 `fund.*`（v0.45.250 本意）
    与 `guard.consistency_census` 的 90 行新口径会新增，改值 5 行全在 `fund.*`。
    ⇒ **改名不等于拆分**：新增或改名一个抽取器时，先问历史文件里这个字段有几种口径。

    Args:
        only: 只回填这些信号名（v0.45.250）。未知名字抛 ValueError。
        dry_run: 不写库（库不存在也不建），在 stats 里给出
            `new` / `changed` / `same` 与逐信号的 `by_signal`。
    """
    db_path = Path(db_path) if db_path else _db_path()   # v0.45.160：调用时求值
    only = _check_only(only)
    base = Path(db_path).parent
    files = sorted(glob.glob(str(base / pattern)))
    stats: Dict[str, Any] = {"files": 0, "rows": 0, "skipped": 0}
    existing: Dict[tuple, Optional[float]] = {}
    if dry_run:
        stats.update(new=0, changed=0, same=0, by_signal={})
        if Path(db_path).exists():
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                has_table = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (TABLE,)).fetchone()
                if has_table:
                    q = f"SELECT date, ticker, signal, value FROM {TABLE}"
                    existing = {(d, t, s): v for d, t, s, v in con.execute(q)
                                if only is None or s in only}
            finally:
                con.close()
    for f in files:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(f))
        if not m:
            stats["skipped"] += 1
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            stats["skipped"] += 1
            continue
        stats["files"] += 1
        if not dry_run:
            stats["rows"] += archive(data, m.group(1), db_path, only=only)
            continue
        for d, t, s, v in _rows_for(data, m.group(1), only):
            k = (d, t, s)
            kind = ("new" if k not in existing
                    else "same" if existing[k] is not None and math.isclose(
                        existing[k], v, rel_tol=1e-9, abs_tol=1e-12)
                    else "changed")
            stats[kind] += 1
            per = stats["by_signal"].setdefault(s, {"new": 0, "changed": 0, "same": 0})
            per[kind] += 1
    return stats


# ────────────────────────────────────────────────────────────────────────────
# 分析
# ────────────────────────────────────────────────────────────────────────────

def decompose_fixed_vs_timevarying(by_day: Dict[str, List]) -> Dict:
    """把信号拆成「标的固定效应」与「票内时变」两部分，分别算 IC。

    ## 为什么这个分解是必需的

    常规 IC 无法区分两种**性质完全不同**的信号，而它们长得一模一样：

    - **固定效应**：某些标的长期取值就高/低 —— 这是**选股标签**，
      不是时变信号。塞进每日评分等于每天重新发现"MSFT 是只大盘科技股"，
      并把这个身份当成新信息。正确用法是**筛选池**，不是打分。
    - **票内时变**：同一只标的的取值相对自身均值的波动 —— 这才是**择时信号**，
      适合做成每日评分。

    2026-07-30 实测的两个反例：
      • `risk_adj`：固定效应 IC=+0.006(t=0.19)、票内时变 −0.161(t=−3.75)
        ⇒ 100% 择时，用法正确
      • `crowding_score`：全样本看驼峰形显著（30~50 组超额 +1.17%，
        且样本外延续），但**票内去均值后效应完全消失**
        ⇒ 100% 固定效应，把它当每日信号用是错的

    Args:
        by_day: {date: [(value, ret, ticker), ...]}

    Returns:
        {ic_fixed, ic_within, ic_raw, nature} —— nature ∈
        {"择时", "选股标签", "混合", "无"}
    """
    sys.path.insert(0, str(Path(__file__).parent))
    import ic_diagnostics as icd

    vals_by_ticker: Dict[str, List[float]] = defaultdict(list)
    for rows in by_day.values():
        for v, _r, tk in rows:
            vals_by_ticker[tk].append(v)
    if len(vals_by_ticker) < 3:
        return {}
    tmean = {tk: mean(vs) for tk, vs in vals_by_ticker.items()}

    raw = {d: [(v, r) for v, r, _ in rows] for d, rows in by_day.items()}
    fixed = {d: [(tmean[tk], r) for _v, r, tk in rows] for d, rows in by_day.items()}
    within = {d: [(v - tmean[tk], r) for v, r, tk in rows] for d, rows in by_day.items()}

    def _mean_ic(panel):
        s = icd._ic_series_from_pairs(panel)
        return mean(s.values()) if len(s) >= 10 else float("nan")

    ic_raw, ic_f, ic_w = _mean_ic(raw), _mean_ic(fixed), _mean_ic(within)

    # 票内方差为 0 ⇒ 该信号在每只标的内部完全不变，是**纯标签**。
    # 此时 ic_within 必为 nan（常数无法算秩相关），但语义不是"未知"而是"无时变信息"，
    # 需与"天数不足导致的 nan"区分开。
    within_spread = max(
        (abs(v) for rows in within.values() for v, _ in rows), default=0.0)
    no_within_variation = within_spread < 1e-12

    af = abs(ic_f) if math.isfinite(ic_f) else 0.0
    aw = 0.0 if no_within_variation else (abs(ic_w) if math.isfinite(ic_w) else 0.0)

    # 判定门槛不能用固定绝对值 —— 票均值本身有抽样波动，标的越少波动越大。
    # 实测：8 只票 × 30 天的**纯随机**面板，ic_fixed 可达 0.037，
    # 若用固定 0.03 会把噪音判成"选股标签"。
    # 改用 1/√(有效横截面宽度) 标定：这正是零假设下单日 IC 的标准差量级
    # （与 ic_diagnostics 噪音地板同源的原理）。
    widths = [len(rows) for rows in by_day.values()]
    avg_w = mean(widths) if widths else 5.0
    n_days_eff = len(by_day)
    floor = (1.0 / math.sqrt(max(avg_w - 1, 1))) / math.sqrt(max(n_days_eff, 1)) * 2.0

    if max(af, aw) < floor:
        nature = "无"
    elif aw >= af * 1.5:
        nature = "择时"
    elif af >= aw * 1.5:
        nature = "选股标签"
    else:
        nature = "混合"
    return {"ic_raw": ic_raw, "ic_fixed": ic_f,
            "ic_within": 0.0 if no_within_variation else ic_w,
            "no_within_variation": no_within_variation,
            "nature": nature}


def split_stability(by_day: Dict[str, List], floor: float,
                    train_frac: float = 0.6) -> Dict:
    """按时间切训练/测试，分别算 IC —— 检测全样本 IC 是否只是异号平均。

    ## 为什么必须常驻

    2026-07-30 实测：综合分全样本 IC = **−0.09**，看起来是个稳定的弱负信号。
    按时间切开后：**训练期 −0.214(t=−5.28) / 测试期 +0.025(t=+0.46)**。

    符号相反。那个 −0.09 **描述的不是一个效应，而是两段异号数据的平均**
    —— 一个不存在的中间值。此前所有基于全样本 IC 的分析都在解释这个平均数，
    这解释了为什么每次深挖都得到互相矛盾的结论。

    全样本统计量在存在时间不稳定性时会系统性误导，而检测它只需一步：
    **先分段看，再决定要不要合并**。

    Returns:
        {ic_train, ic_test, n_train, n_test, stability}
        stability ∈ {"稳定", "衰减", "翻转", "均噪音", "样本不足"}
    """
    sys.path.insert(0, str(Path(__file__).parent))
    import ic_diagnostics as icd

    days = sorted(by_day)
    if len(days) < 16:
        return {"stability": "样本不足", "ic_train": float("nan"),
                "ic_test": float("nan"), "n_train": 0, "n_test": 0}
    cut = int(len(days) * train_frac)
    tr = {d: by_day[d] for d in days[:cut]}
    te = {d: by_day[d] for d in days[cut:]}

    def _ic(panel):
        s = icd._ic_series_from_pairs(panel)
        return (mean(s.values()) if len(s) >= 5 else float("nan")), len(s)

    ic_tr, n_tr = _ic(tr)
    ic_te, n_te = _ic(te)

    if not (math.isfinite(ic_tr) and math.isfinite(ic_te)):
        stab = "样本不足"
    elif abs(ic_tr) < floor and abs(ic_te) < floor:
        stab = "均噪音"
    elif (ic_tr > 0) != (ic_te > 0):
        # 符号相反 ⇒ 全样本 IC 是异号平均，不描述任何真实效应
        stab = "翻转"
    elif abs(ic_te) >= abs(ic_tr) * 0.5:
        stab = "稳定"
    else:
        stab = "衰减"
    return {"ic_train": ic_tr, "ic_test": ic_te,
            "n_train": n_tr, "n_test": n_te, "stability": stab}


#: 预测目标。收益率与波动率的可学性差 60 倍，见下方注释。
TARGET_METRICS = ("return", "vol")

_PX_CACHE: Dict = {}


def _px_cache_key(tickers, dates, fwd_days) -> tuple:
    """行情缓存的键。**独立成函数是为了让测试能直接引用它**——
    测试里重抄一遍键逻辑等于测试自己，生产代码改回旧写法也照样绿。

    ⚠️ 键必须包含**完整**日期集合的指纹，不能只用首尾两天。
    只用 (首日, 末日) 时，同进程内若两次调用首尾相同但中间日期不同
    （例如新预测被回填后重查），第二次会静默复用陈旧结果、丢掉中间日期。
    len(dates) 也不够：首尾与长度都相同、只有中间那天不同的情形仍会撞键。
    """
    fp = hashlib.sha1("|".join(dates).encode()).hexdigest()[:16] if dates else ""
    return (tuple(sorted(tickers)), fp, len(dates), fwd_days)


def _forward_realized_vol(tickers: List[str], dates: List[str],
                          fwd_days: int) -> Dict:
    """未来 N 个交易日的已实现波动（日收益标准差 %）。

    ## 为什么加这个目标

    2026-07-30 实测，同一宇宙（90 只）× 897 交易日 × 同样朴素的特征：

    | 预测目标 | 特征 | IC | t |
    |---|---|---|---|
    | 未来 7 日**收益率** | 20 日动量 | **+0.012** | +1.7 |
    | 未来 7 日**已实现波动** | 过去 60 日波动 | **+0.710** | **+288.6** |

    **波动率的 IC 是收益率的 60 倍**，且用的是一行 `rolling(60).std()`，无任何模型。
    这不是过拟合——是波动率聚集（volatility clustering），金融学里最稳固的经验规律之一。

    含义：系统当前在预测一个 IC≈0.01 的目标，**天花板就在那里**，
    与架构好坏、蜂群聪明与否无关。同一套基础设施对准波动率则有 0.71 可用。

    Returns: {(ticker, date): 未来 fwd_days 日的日收益 std × 100}
    """
    key = _px_cache_key(tickers, dates, fwd_days)
    if key in _PX_CACHE:
        return _PX_CACHE[key]
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import pandas as pd
        import yfinance as yf
        start = (datetime.date.fromisoformat(dates[0]) - datetime.timedelta(days=20)).isoformat()
        end = (datetime.date.fromisoformat(dates[-1]) + datetime.timedelta(days=fwd_days * 3 + 20)).isoformat()
        px = yf.download(list(tickers), start=start, end=end,
                         progress=False, auto_adjust=True)["Close"]
        if isinstance(px, pd.Series):
            px = px.to_frame(tickers[0])
        rets = px.pct_change()
        out = {}
        for t in px.columns:
            s = rets[t].dropna()
            for d in dates:
                ts = pd.Timestamp(d)
                fut = s.loc[s.index > ts].head(fwd_days)
                if len(fut) >= max(3, fwd_days - 2):
                    out[(t, d)] = float(fut.std() * 100)
        _PX_CACHE[key] = out
        return out
    except Exception as e:  # noqa: BLE001 - 波动率目标是可选能力，失败则降级
        print(f"⚠️  波动率目标构造失败（需联网）：{e}", file=sys.stderr)
        return {}


# ────────────────────────────────────────────────────────────────────────────
# 世代切片（v0.45.265）
# ────────────────────────────────────────────────────────────────────────────
#
# 抽取器处理不了的一类问题：**生产者换了口径、抽取器与归档一致**。v0.45.163 让
# `agent.GuardBeeSentinel.score` 换了量（risk_adj Δ 均值 −0.485、方向 26.2% 不一致），
# 抽取器还是那一行 `_agent_score`，全量 `--backfill --dry-run`「改值 0」—— 抽取层看不见，
# 归档也没写错。错在 `analyze()` 把边界前后两个量当成一条序列算 IC。
# 各蜂输出分每改一次逻辑就换一次量，逐次改名（v0.45.182 的解法）不可行
# ⇒ 判别放在做聚合的这一层：每个信号只用它**当前世代**的样本。
#
# 分工（边界日期只在 `ic_rerun_readiness._COHORT_HISTORY` 维护，这里不抄）：
#   · `COHORT_SIGNAL_SCOPE`：每条边界**直接**改了哪些归档信号；
#   · `SIGNAL_UPSTREAM`：哪些信号读别的系统输出 —— 上游换代，下游同一天换代（传递闭包）。
#     追加边界的人只写直接目标，不必自己推「Bear 读 Guard、Rival 读拥挤度……」；
#   · `SIGNAL_LEAVES`：不读任何系统输出的信号，只在边界**直接**点名时换代；
#   · `ALWAYS_SLICED`：`composite.final_score` 受全部边界约束 —— `_COHORT_HISTORY`
#     本来就是它的世代表，与 `ic_rerun_readiness.assess()` 用同一条边界。
#
# ⚠️ 判据：这条边界改的是**产生该值的函数**（算法 / 来源 / 哨兵语义 / 读的系统输出），
#   还是只修了**原始输入的测量误差**？前者换代；后者（v0.45.234 陈旧盘中价、v0.45.238
#   快照槽位错会话、v0.45.243 补跑收盘兜底）不换代，已知坏日子走 `QUARANTINE`。
#   有实测说某量没换（v0.45.256 对 `guard.adj_factor` 的核对）⇒ 照证据不列；
#   没测过 ⇒ 按代码出边保守列。宁可丢样本（报告里点名），不静默池化。
#
# ⚠️ 已知盲区（v0.45.275 更新）：`_COHORT_HISTORY` 原以为「始于 2026-08-17，此前
#   一律未登记」，P1 审计（2026-07-25~08-28 提交窗口、覆盖全部 7 只蜂）发现这话
#   不够准——`agent.ChronosBeeHorizon.*` / `agent.BuzzBeeWhisper.*` / `guard.macro_adj` /
#   `price.momentum_5d` / `price.volatility_20d` 五组信号在 2026-08-15/08-26
#   真实换过定义，只是当时没人登记，现已补（见 `_COHORT_HISTORY` 同版新增的 5 条）。
#   **仍然已知的盲区**：这次只查了 07-25~08-28 这段；7 只蜂在此之前（最早到
#   2026-02-23）还有 49 次未查的改动，`signal_archive` 归档最早回填到 2026-03-10，
#   理论上也可能藏着同类问题。本机制只照登记表切，不会主动替未查的历史补登。

#: 被入档信号读取、自身不入档的系统输出 —— 只为让边界与依赖边能指向它。
UNARCHIVED_NODES = frozenset({"agent.CodeExecutorAgent.score",
                              "agent.CodeExecutorAgent.direction"})

# Phase-1 并行发布的蜂（`alpha_hive_daily_report._init_scan_context`）；其后顺序是 Rival → Guard → Bear
_PHASE1 = ("ScoutBeeNova", "OracleBeeEcho", "BuzzBeeWhisper", "ChronosBeeHorizon", "CodeExecutorAgent")
_PHASE1_DIRS = tuple(f"agent.{a}.direction" for a in _PHASE1)

#: 下游信号模式 → 它读的**系统输出**（上游模式）。只登记「读系统输出」的边；
#: 读原始市场数据（期权链 / SEC / 行情）不是边 —— 那些量不随系统逻辑换代。
SIGNAL_UPSTREAM: Dict[str, Tuple[str, ...]] = {
    # Scout 拥挤度的 consensus_strength = 读板那一刻已发布同伴的看多数
    # （`real_data_sources.get_bullish_agents_count`）。Phase-1 并行 ⇒ 读到谁取决于竞态，
    # 但读到的是同伴**方向**：生产库 03~09 月取值 0~5 只看多，不是常数。
    "crowding.comp.consensus_strength": tuple(d for d in _PHASE1_DIRS if "ScoutBeeNova" not in d),
    "crowding.score": ("crowding.comp.*",),
    "crowding.signal": ("crowding.score",),
    "crowding.adj_factor": ("crowding.score",),
    "agent.ScoutBeeNova.*": ("crowding.score", "crowding.adj_factor"),    # 方向按拥挤度阈值定
    # Rival（Phase-1.4）：自算一份拥挤度当 ML 特征（此时 Phase-1 已全部发布），读 Chronos 的 self_score。
    # Rival / Guard 自算的拥挤度不入档，与 Scout 同一个 CrowdingDetector 公式 ⇒ 以
    # `crowding.score` 代表「公式换代」（下面 guard.adj_factor 同理）
    "ml.*": ("crowding.score", "agent.ChronosBeeHorizon.score") + _PHASE1_DIRS,
    "agent.RivalBeeVanguard.*": ("ml.*",),
    # Guard：avg_score / consistency 读 Phase-1 + Rival 的分与方向；自算一份拥挤度（看多数取自同一份普查）
    "guard.consistency_census": _PHASE1_DIRS + ("agent.RivalBeeVanguard.direction",),
    "guard.adj_factor": ("crowding.score",) + _PHASE1_DIRS + ("agent.RivalBeeVanguard.direction",),
    "agent.GuardBeeSentinel.*": tuple(f"agent.{a}.*" for a in _PHASE1) + (
        "agent.RivalBeeVanguard.*", "guard.consistency_census", "guard.adj_factor"),
    # Bear 读 Rival 的 ml_probability / expected_7d、Guard 的 consistency / conflict_type
    "bear.score": ("ml.*", "agent.GuardBeeSentinel.*", "guard.consistency_census",
                   "bear.insider_bear", "bear.overval_bear", "bear.options_bear", "bear.short_int_bear"),
    "agent.BearBeeContrarian.*": ("bear.score",),
    # 多数派占比：遍历 agent_details 里**全部**蜂的方向（含 CodeExecutor / Guard / Bear）
    "composite.swarm_agreement": ("agent.*.direction",),
}

#: 受**全部**边界约束的信号（见上方分工）。
ALWAYS_SLICED = frozenset({"composite.final_score"})

#: 不读任何系统输出的现役信号。**逐个列名，不用通配**：新增 `options.xxx` 若由系统输出
#: 算出，通配会把它静默归成叶子。新增抽取器必须二选一 —— 进本集合，或在 `SIGNAL_UPSTREAM`
#: 登记它读什么（`tests/test_signal_archive_generations.py` 会红）。
SIGNAL_LEAVES = frozenset({
    "insider.sentiment", "insider.score", "insider.filings", "insider.dollar_bought",
    "insider.dollar_sold", "insider.distinct_buyers", "insider.officer_buys", "congress.score",
    "price.momentum_5d", "price.volatility_20d", "price.volume_ratio",
    "options.iv_current", "options.put_call_ratio", "options.gamma_exposure", "options.total_oi",
    "options.iv_rank", "options.iv_percentile", "options.iv_rank_is_real",
    "sentiment.pct",
    "bear.insider_bear", "bear.overval_bear", "bear.options_bear", "bear.short_int_bear",
    "crowding.comp.social_volume", "crowding.comp.google_trends",
    "crowding.comp.seeking_alpha_views", "crowding.comp.short_squeeze_risk",
    "catalyst.count", "catalyst.nearest_days", "catalyst.max_weight",
    "buzz.comp.momentum_signal", "buzz.comp.volume_signal", "buzz.comp.volatility_signal",
    "buzz.comp.reddit_signal", "buzz.comp.news_signal", "buzz.comp.yahoo_signal",
    "buzz.comp.fear_greed_signal",
    "market.fear_greed", "market.fear_greed_is_cnn",
    "guard.macro_adj",                       # `_calc_macro_adjustment`，不走读板路径
    "fund.pe_ratio", "fund.market_cap",
    # 这三只蜂不读板：输入全是原始数据
    "agent.OracleBeeEcho.score", "agent.OracleBeeEcho.direction",
    "agent.BuzzBeeWhisper.score", "agent.BuzzBeeWhisper.direction",
    "agent.ChronosBeeHorizon.score", "agent.ChronosBeeHorizon.direction",
})

#: `_COHORT_HISTORY` 的 version → 该边界**直接**改了哪些归档信号（fnmatch 模式）。
#: 下游由 `SIGNAL_UPSTREAM` 自动推出；`composite.final_score` 恒在内，不必写。
#: 追加边界时必须同步在这里声明（空元组＝只动了 final_score）—— 漏了测试红，
#: 运行时则按「影响全部信号」处理并在报告里点名。
COHORT_SIGNAL_SCOPE: Dict[str, Tuple[str, ...]] = {
    # v0.45.275（P1 补登，两条真实早于表的第一条 08-17，按实际部署日期插入）：
    # 08-15 Step2：GuardBee VIX 改走 CBOE，宏观整体降级但 CBOE 仍供得上真实 VIX 的
    # 日子里，VIX 从「跟着一起丢弃」变成「参与 regime_votes」⇒ macro_adj 改口径。
    "v0.43.24": ("guard.macro_adj",),
    # 08-15：ScoutBee momentum_5d 缺失哨兵 0.0（伪造持平）→ None（诚实缺失）。
    "v0.43.25": ("price.momentum_5d",),
    # 08-17：expected_returns 去偏 + probability 居中 + RivalBee 三特征接真实数据
    "v0.44.1~0.44.3": ("ml.*",),
    # v0.45.275（P1 补登）08-26（早于同日 v0.45.30，按部署时间序插入）：
    # BuzzBee 背离检测 None 语义修复 + volatility_20d 缺失哨兵 0.0→None（同批
    # data_pipeline 改动，Buzz details 是直接透传）。
    "v0.45.2~0.45.15": ("agent.BuzzBeeWhisper.*", "price.volatility_20d"),
    # 08-26：拥挤度公式删 polymarket_volatility、缺失分量改在现存分量间重归一化。
    # Scout 与 Guard 各算一份、同走 CrowdingDetector ⇒ 两份都换代（Rival 那份经依赖边）。
    # 分量本身未变（stocktwits_volume → social_volume 是同一个量改名，见 `_crowding_comp`）
    "v0.45.30": ("crowding.score", "guard.adj_factor"),
    # v0.45.275（P1 补登）08-26（晚于同日 v0.45.30，按部署时间序插入）：ChronosBee
    # 催化剂抓取全失败时不再冒充 4.0/「无近期催化剂」，改返回 error。
    "v0.45.31": ("agent.ChronosBeeHorizon.*", "catalyst.count",
                 "catalyst.nearest_days", "catalyst.max_weight"),
    # v0.45.275（P1 补登）08-26：移除 catalysts.json / 硬编码 NVDA-VKTX 两条人工来源，
    # 催化剂来源集合本身改变（与上一条「失败语义」是两次独立变更）。
    "v0.45.32": ("agent.ChronosBeeHorizon.*", "catalyst.count",
                 "catalyst.nearest_days", "catalyst.max_weight"),
    # 08-27：① 拥挤度全分量不可得返回 None（旧：20.59 →「低拥挤」→ 1.2 加分）；
    # ② 训练集剔除维度缺失样本 ⇒ ml.*；⑥ 信息素坏值 1.0 → 0.5 ⇒ 板排序 ⇒ Guard 的 n=5 窗口。
    # ③ 0DTE `or 30` 在 `OptionsDataFetcher` 的 BS gamma 回填里，而主链 `_select_expiries`
    # 只取 DTE≥7 ⇒ `options.gamma_exposure` 走不到，不列
    "v0.45.50": ("crowding.score", "guard.adj_factor", "ml.*", "agent.GuardBeeSentinel.*"),
    # 09-05：① Oracle 期限结构 / 25Δ skew 的 IV 换 CBOE ⇒ options_score（`options.iv_current`
    # 本就取自 OptionsAgent，不变）；② Bear 估值项 P/E 复活
    "v0.45.128": ("agent.OracleBeeEcho.*", "bear.overval_bear"),
    # 09-07：Rival 读 Chronos 的 catalyst_quality 修复。probability 在当前 HGB 上 0/173 变，
    # 但那是模型性质不是定义性质（该条原文「定义变了就登记」）⇒ ml.* 整体列
    "v0.45.151": ("ml.*",),
    # 09-07：detect_resonance 改读定点视图 ⇒ Guard 自身的共振判定（risk_adj 维分）
    "v0.45.156": ("agent.GuardBeeSentinel.*",),
    # 09-07：Guard 改走普查。`guard.adj_factor` 的输入也跟着变（看多数改取普查），但 v0.45.256
    # 实测同一 30 只池 08-24~09-04 vs 09-08~09-14 三档占比几乎不动 ⇒ 照证据不列
    "v0.45.163": ("agent.GuardBeeSentinel.*", "guard.consistency_census"),
    # 09-09 / 09-10：权重（config 改写、adapt_weights 旁路断开）—— 纯聚合层
    "v0.45.172": (),
    "v0.45.176": (),
    # 09-10：CodeExecutor 兜底分支不再恒投 6.0/看多（未入档，经依赖边传给读它的信号）
    "v0.45.191": ("agent.CodeExecutorAgent.*",),
    # 09-11：Dealer GEX 换全到期日视图，只经政体调整进 final_score。
    # ⚠️ `options.gamma_exposure` 是 OptionsAgent 在主链上的 `calculate_gamma_exposure`，
    # 不是 advanced_analyzer 的 dealer GEX，本边界没碰它
    "v0.45.197": (),
    # 09-11：Oracle 方向去掉「数中文摘要关键词」那层投票（分数不变）
    "v0.45.201": ("agent.OracleBeeEcho.direction",),
    # 09-11 / 09-13：Guard 的 data_quality 上报、Bear/Guard 退出计票、豁免蜂 ML 乘数、
    # Guard 退出共振 —— 都只在 Queen 层，各蜂自身输出不变
    "v0.45.209": (),
    "v0.45.212": (),
    "v0.45.228": (),
    "v0.45.235": (),
    # 09-14：原始输入的测量误差修正，不是换定义（见上方判据）
    "v0.45.234": (),
    "v0.45.238": (),
    "v0.45.243": (),
    # 09-18：ScoutBee bullish_agents 计数改用普查读法 + 身份过滤 + 诚实 None
    # （v0.45.151/156/163 那个「排行榜当普查用」缺陷的第 4 个未迁地点）。
    # 直接点名 consensus_strength，经 _scope_closure 传给 crowding.score/
    # ml.*/agent.ScoutBeeNova.*/guard.*/bear.* 等全部下游——但它们各自已有更晚
    # 的边界覆盖，这条不会改变它们当前的世代起点，只对此前从未被点名过的
    # consensus_strength 本身生效
    "v0.45.279": ("crowding.comp.consensus_strength",),
    # 09-18：BearBee 读同伴条目改走定点索引（同一个「排行榜当普查用」缺陷的第 5、6 处）。
    # overval_bear / short_int_bear 不读板、没变——且它们是 bear.score 的上游，不在下游闭包里。
    # Bear 不进 final_score，final_score 的切分靠 ALWAYS_SLICED，与这里的范围无关
    "v0.45.288": ("bear.score", "bear.options_bear", "bear.insider_bear"),
}


def _cohort_history() -> list:
    """边界表**调用时**读 —— 不在 import 期抄一份（日后追加、测试替换都要生效）。"""
    import ic_rerun_readiness
    return ic_rerun_readiness._COHORT_HISTORY


def _matching(names: Iterable[str], patterns: Iterable[str]) -> Set[str]:
    pats = tuple(patterns)
    return {n for n in names if any(fnmatchcase(n, p) for p in pats)}


def _scope_closure(direct: Iterable[str], universe: Set[str]) -> Set[str]:
    """直接目标 + 沿 `SIGNAL_UPSTREAM` 往下游传递，直到不再扩张。"""
    hit = _matching(universe, direct)
    grew = True
    while grew:
        grew = False
        for down, ups in SIGNAL_UPSTREAM.items():
            if not _matching(hit, ups):
                continue
            new = _matching(universe, (down,)) - hit
            if new:
                hit |= new
                grew = True
    return hit


def generation_boundaries(signals: Iterable[str],
                          history: Optional[list] = None) -> Dict[str, Dict]:
    """每个信号**当前世代**的起点：`{signal: {"date", "version", "undeclared"}}`。

    没有任何边界约束的信号**不在**结果里（＝全史可用）。取「最后一条适用的边界」，
    按 `_COHORT_HISTORY` 的列表顺序而非日期大小 —— 与 `assess()` 的「判据取最后一条」同一语义。

    保守规则（两条都会在 `analyze()` 的世代报告里露面）：
      · 边界没在 `COHORT_SIGNAL_SCOPE` 声明 ⇒ 影响全部信号；
      · 信号不认识（退役名如 `guard.consistency`，或库里有、抽取器没有）⇒ 受全部边界约束。
    """
    history = _cohort_history() if history is None else history
    signals = set(signals)
    known = set(SIGNAL_EXTRACTORS) | UNARCHIVED_NODES
    universe = known | signals
    unknown = signals - known
    out: Dict[str, Dict] = {}
    for date, version, _reason in history:
        declared = version in COHORT_SIGNAL_SCOPE
        hit = (_scope_closure(COHORT_SIGNAL_SCOPE[version], universe) if declared
               else set(universe))
        for s in (hit | ALWAYS_SLICED | unknown) & signals:
            out[s] = {"date": date, "version": version, "undeclared": not declared}
    return out


def load_panel(db_path: Optional[Path] = None, horizon: str = "t7",
               min_width: int = 5,
               with_ticker: bool = False,
               target_metric: str = "return") -> Dict[str, Dict[str, List]]:
    """联表取 {signal: {date: [(值, 前瞻收益)]}}。

    前瞻收益用**纯价格变动**（price_{h} / price_at_predict），而非 return_t7 列
    ——后者是 `_simulate_trade_path` 的路径依赖收益，42.5% 的行被 SL/TP 档位截断，
    会制造大量并列值破坏 rank-IC 的尾部排序（详见 ic_diagnostics 模块注释）。

    ⚠️ 返回的是**整张表**，不按世代切 —— 切片在做聚合的 `analyze()` 里（v0.45.265）。
    自己拿这个面板算跨日统计量的，先过一遍 `generation_boundaries()`。
    """
    db_path = Path(db_path) if db_path else _db_path()   # v0.45.160：调用时求值
    price_col, checked_col = f"price_{horizon}", f"checked_{horizon}"
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rets = {}
        for r in con.execute(
            f"SELECT date, ticker, price_at_predict, {price_col} AS p1 "
            f"FROM predictions WHERE {checked_col}=1 AND {price_col} IS NOT NULL "
            f"  AND price_at_predict > 0"
        ):
            rets[(r["ticker"], r["date"][:10])] = (
                (r["p1"] - r["price_at_predict"]) / r["price_at_predict"] * 100.0)
        if not rets:
            return {}

        if target_metric == "vol":
            # 换目标：未来 N 日已实现波动。样本集沿用同一批 (ticker, date)，
            # 保证与收益率口径可直接对照。
            tks = sorted({k[0] for k in rets})
            dts = sorted({k[1] for k in rets})
            fwd = 7 if horizon == "t7" else 30
            vol = _forward_realized_vol(tks, dts, fwd)
            if not vol:
                return {}
            rets = {k: v for k, v in vol.items() if k in rets}
            if not rets:
                return {}
        panel: Dict[str, Dict[str, List]] = defaultdict(lambda: defaultdict(list))
        for r in con.execute(f"SELECT date, ticker, signal, value FROM {TABLE}"):
            key = (r["ticker"], r["date"][:10])
            if key not in rets or r["value"] is None:
                continue
            row = ((r["value"], rets[key], r["ticker"]) if with_ticker
                   else (r["value"], rets[key]))
            panel[r["signal"]][r["date"][:10]].append(row)
    finally:
        con.close()
    return {s: {d: v for d, v in bd.items() if len(v) >= min_width}
            for s, bd in panel.items()}


def analyze(db_path: Optional[Path] = None, horizon: str = "t7",
            min_samples: int = 50, min_width: int = 5,
            draws: int = 200, target_metric: str = "return",
            pool_generations: bool = False):
    """对每个归档信号跑四口径 IC + 噪音地板对照。

    v0.45.265：每个信号只用它**当前世代**的样本（`generation_boundaries`）。
    不读系统输出、也没被边界直接点名的信号（options.* / insider 金额 / 行情 …）照旧用全史。

    Args:
        target_metric: "return" = 预测方向收益（现状）；
            "vol" = 预测未来已实现波动。后者的可学性高一个数量级
            （实测同宇宙同特征 IC 0.710 vs 0.012），见 `_forward_realized_vol` 注释。
        pool_generations: True = 旧行为（跨世代混算），只作对照；报告会标出。

    Returns:
        面板为空时 `[]`；否则 `(rows, floor, generations)`。
        `generations = {"pooled", "undeclared_versions",
                        "signals": {信号: {gen_start, gen_version, n_total, n_in_generation, n_excluded}}}`
        —— 覆盖面板里**全部**信号，含被切到样本不足、没进 `rows` 的那些
        （否则「被切光」与「从来没有数据」长得一样）。
    """
    db_path = Path(db_path) if db_path else _db_path()   # v0.45.160：调用时求值
    sys.path.insert(0, str(Path(__file__).parent))
    import ic_diagnostics as icd

    # 带 ticker 取一次，供固定效应/时变分解；IC 计算仍用 (值, 收益) 二元组
    panel_t = load_panel(db_path, horizon, min_width, with_ticker=True,
                         target_metric=target_metric)
    if not panel_t:
        return []
    panel = {s: {d: [(v, r) for v, r, _ in rows] for d, rows in bd.items()}
             for s, bd in panel_t.items()}
    lag, period = (7, "周") if horizon == "t7" else (30, "月")
    # 显式指定地板基准：`composite.final_score` 覆盖最全（每个标的每天都有）。
    # 不能依赖默认 fallback —— 地板对基准的日期覆盖高度敏感（实测 0.076 vs 0.116）。
    # 用**未切**的面板：地板只用骨架（日期 / 宽度 / 收益），随机化掉了信号取值，与世代无关。
    floor = icd.noise_floor(panel, lag, period, draws=draws,
                            base_key="composite.final_score")
    thr_full = floor.get("ic_p95", float("nan"))

    history = _cohort_history()
    gens = {} if pool_generations else generation_boundaries(panel, history)
    report: Dict[str, Any] = {
        "pooled": pool_generations,
        "undeclared_versions": ([] if pool_generations else
                                [v for _d, v, _r in history if v not in COHORT_SIGNAL_SCOPE]),
        "signals": {},
    }

    out = []
    for sig, by_day_all in sorted(panel_t.items()):
        g = gens.get(sig)
        by_day_t = ({d: v for d, v in by_day_all.items() if d >= g["date"]} if g
                    else by_day_all)
        n_total = sum(len(v) for v in by_day_all.values())
        n = sum(len(v) for v in by_day_t.values())
        report["signals"][sig] = {
            "gen_start": g["date"] if g else None, "gen_version": g["version"] if g else None,
            "n_total": n_total, "n_in_generation": n, "n_excluded": n_total - n}
        if n < min_samples:
            continue
        by_day = panel[sig] if n == n_total else {d: panel[sig][d] for d in by_day_t}
        s = icd._ic_series_from_pairs(by_day)
        if len(s) < 10:
            continue
        r = icd.diagnose(s, lag, period)
        if not r:
            continue
        # 被切过 ⇒ 骨架（天数）变了，地板必须在本世代骨架上重算：天数少 ⇒ 地板高，
        # 沿用全史骨架的地板会系统性偏低 ⇒ 假阳性。未被切的沿用全表地板（既有行为）。
        thr = (thr_full if n == n_total else
               icd.noise_floor({sig: by_day}, lag, period, draws=draws,
                               base_key=sig).get("ic_p95", float("nan")))
        # 覆盖度：非并列值的比例——稀疏事件型信号（如 cluster buying）
        # 大量并列会让 rank-IC 失真，必须显式暴露
        vals = [a for v in by_day.values() for a, _ in v]
        distinct_ratio = len(set(vals)) / len(vals) if vals else 0.0
        r.update({
            "signal": sig, "n_samples": n, "n_days": len(by_day),
            "distinct_ratio": distinct_ratio,
            "noise_floor": thr,
            "beats_noise": bool(math.isfinite(thr) and abs(r["daily_ic"]) > thr),
            **{k: report["signals"][sig][k] for k in ("gen_start", "gen_version", "n_excluded")},
        })
        # 固定效应 vs 时变分解 —— 区分「选股标签」与「择时信号」
        r.update(decompose_fixed_vs_timevarying(by_day_t) or
                 {"nature": "?", "ic_fixed": float("nan"), "ic_within": float("nan")})
        # 训练/测试分段 —— 检测全样本 IC 是否只是异号平均
        r.update(split_stability(by_day, thr if math.isfinite(thr) else 0.077))
        out.append(r)
    out.sort(key=lambda x: -abs(x["daily_ic"]))
    return out, floor, report


def print_report(rows: List[Dict], floor: Dict, horizon: str,
                 target_metric: str = "return",
                 generations: Optional[Dict] = None) -> None:
    tgt = {"return": "方向收益", "vol": "已实现波动"}.get(target_metric, target_metric)
    print("=" * 112)
    print(f"【单信号 IC 档案】horizon={horizon}  目标={tgt}   "
          f"共 {len(rows)} 个信号达到最小样本量")
    print("=" * 112)
    if generations and generations.get("pooled"):
        print("  ⚠️ --pool-generations：跨世代混算。系统输出在世代边界上换过量，")
        print("     下表的 IC 是几个不同量的混合，不描述任何一个世代 —— 只作对照，勿据此下结论")
    if floor:
        print(f"  🎯 噪音地板（随机 ×{floor['n_draws']}）：|日度IC| 95分位 = "
              f"{floor['ic_p95']:.3f} ｜ 通过口径数 95分位 = {floor['passed_p95']:.0f}/4")
        print("     判定为真信号需同时满足：|IC| > 地板 **且** 通过 ≥3/4")
    print("-" * 132)
    print(f"{'信号':<32}{'样本':>6}{'日度IC':>9}{'t':>7}{'通过':>6}"
          f"{'训练IC':>9}{'测试IC':>9}{'稳定性':>9}"
          f"{'固定':>9}{'时变':>9}{'性质':>8}  判定")
    print("-" * 132)

    def _f(x):
        return f"{x:+.4f}" if isinstance(x, float) and math.isfinite(x) else "   n/a"

    STAB_MARK = {"稳定": "✅稳定", "衰减": "⚠️衰减", "翻转": "❌翻转",
                 "均噪音": "  噪音", "样本不足": "  不足"}
    for r in rows:
        real = r["beats_noise"] and r["passed_methods"] >= 3
        mark = "🟢 候选" if real else ("🟡 口径不足" if r["beats_noise"] else "⚪ 噪音带内")
        warn = " ⚠️稀疏" if r["distinct_ratio"] < 0.25 else ""
        gen = (f" ｜世代自 {r['gen_start']}，地板 {r['noise_floor']:.3f}"
               if r.get("gen_start") and r.get("n_excluded") else "")
        print(f"{r['signal']:<32}{r['n_samples']:>6}"
              f"{r['daily_ic']:>+9.4f}{r['daily_t']:>+7.2f}{r['passed_methods']:>4}/4"
              f"{_f(r.get('ic_train')):>9}{_f(r.get('ic_test')):>9}"
              f"{STAB_MARK.get(r.get('stability','?'),'?'):>9}"
              f"{_f(r.get('ic_fixed')):>9}{_f(r.get('ic_within')):>9}"
              f"{r.get('nature','?'):>8}  {mark}{warn}{gen}")
    print()
    if generations and not generations.get("pooled"):
        _print_generations(rows, generations)
    print("  ⚠️稀疏 = 取值离散度 <25%，多为事件型信号（大量并列），rank-IC 会失真，")
    print("     应改用事件研究（对比有/无事件两组的超额收益）而非 IC。")
    print()
    print("  【性质】区分两种在常规 IC 里长得一样、但用法完全不同的信号：")
    print("    择时     = 票内时变主导 → 同一标的相对自身均值的波动有预测力，")
    print("               **适合做每日评分**（例：risk_adj 固定+0.006 / 时变 −0.161）")
    print("    选股标签 = 固定效应主导 → 只是某类标的的身份标记，不随时间提供新信息，")
    print("               **只能做筛选池，塞进每日评分等于每天重新发现「MSFT 是大盘股」**")
    print("               （例：crowding_score 全样本看似显著且样本外延续，")
    print("                 但票内去均值后效应完全消失）")
    print()
    print("  【稳定性】按时间切 60/40，分别算 IC —— 检测全样本 IC 是否只是异号平均：")
    print("    ✅稳定 = 两期同号且测试期强度 ≥ 训练期一半")
    print("    ⚠️衰减 = 同号但测试期明显减弱（过拟合的典型形态）")
    print("    ❌翻转 = **两期符号相反** ⇒ 全样本 IC 不描述任何真实效应，只是中间值。")
    print("             实测综合分：训练 −0.214(t=−5.28) / 测试 +0.025(t=+0.46)，")
    print("             那个 −0.09 的全样本 IC 是假象 —— 勿据此下任何结论")


def _print_generations(rows: List[Dict], generations: Dict) -> None:
    """世代切片小节：谁被切了、切到哪天、切掉多少 —— 尤其是被切到样本不足、没进上表的。"""
    sigs = generations.get("signals", {})
    sliced = sorted((s, e) for s, e in sigs.items() if e["n_excluded"])
    shown = {r["signal"] for r in rows}
    print("  【世代切片】系统输出只算当前世代 —— 边界 ic_rerun_readiness._COHORT_HISTORY，"
          "影响面 signal_archive.COHORT_SIGNAL_SCOPE")
    print(f"    被切 {len(sliced)} 个信号；其余 {len(sigs) - len(sliced)} 个不读系统输出"
          f"或无边界约束，用全史")
    hidden = [(s, e) for s, e in sliced if s not in shown]
    if hidden:
        print(f"    其中本世代样本不足、未进上表的 {len(hidden)} 个：")
        for s, e in hidden:
            print(f"      {s:<34} 自 {e['gen_start']}（{e['gen_version']}）起 "
                  f"{e['n_in_generation']:>5} 条 ｜ 切掉旧世代 {e['n_excluded']} 条")
    if generations.get("undeclared_versions"):
        print(f"    ⚠️ 未声明影响面的边界（已按全部信号切，原始观测在白丢样本）："
              f"{', '.join(generations['undeclared_versions'])} —— "
              f"去 signal_archive.COHORT_SIGNAL_SCOPE 补声明")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Alpha Hive 单信号 IC 档案")
    ap.add_argument("--backfill", action="store_true", help="从历史 .swarm_results 回填")
    ap.add_argument("--only", type=str, default=None,
                    help="仅回填这些信号（逗号分隔）。⚠️ 不带时用当前抽取器重写全部信号，"
                         "没按口径取值的抽取器会把旧口径写回来——先 --dry-run，见 backfill() docstring")
    ap.add_argument("--dry-run", action="store_true",
                    help="与 --backfill 连用：只报新增/改值/不变行数，不写库")
    ap.add_argument("--analyze", action="store_true", help="分析每个信号的 IC")
    ap.add_argument("--list", action="store_true", help="列出已归档信号与覆盖度")
    ap.add_argument("--horizon", choices=["t7", "t30"], default="t7")
    ap.add_argument("--min-samples", type=int, default=50)
    ap.add_argument("--min-width", type=int, default=5)
    ap.add_argument("--draws", type=int, default=200)
    ap.add_argument("--target", choices=TARGET_METRICS, default="return",
                    help="预测目标：return=方向收益（现状）；vol=未来已实现波动"
                         "（实测可学性高一个数量级，需联网取行情）")
    ap.add_argument("--pool-generations", action="store_true",
                    help="与 --analyze 连用：不按世代切片（v0.45.265 前的行为），"
                         "系统输出跨世代混算，只作对照")
    # v0.45.160：default 不能是 str(DB_PATH)——argparse 的 default 在 import 期求值
    ap.add_argument("--db", type=str, default=None)
    args = ap.parse_args()
    db = Path(args.db) if args.db else _db_path()

    if args.dry_run and not args.backfill:
        ap.error("--dry-run 只能与 --backfill 连用")
    if args.only and not args.backfill:
        ap.error("--only 只能与 --backfill 连用")
    if args.pool_generations and not args.analyze:
        ap.error("--pool-generations 只能与 --analyze 连用")
    if args.backfill:
        only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else None
        st = backfill(db_path=db, only=only, dry_run=args.dry_run)
        if args.dry_run:
            print(f"🔍 dry-run（未写库）：{st['files']} 个文件（跳过 {st['skipped']}）"
                  f" → 新增 {st['new']} / 改值 {st['changed']} / 不变 {st['same']}")
            for s, c in sorted(st["by_signal"].items()):
                print(f"   {s:<34} 新增 {c['new']:>6}  改值 {c['changed']:>5}  不变 {c['same']:>6}")
        else:
            print(f"✅ 回填完成：{st['files']} 个文件 → {st['rows']} 行"
                  f"（跳过 {st['skipped']}）")

    if args.list:
        ensure_schema(db)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            f"SELECT signal, COUNT(*) n, COUNT(DISTINCT date) d, "
            f"       MIN(date) mn, MAX(date) mx FROM {TABLE} "
            f"GROUP BY signal ORDER BY n DESC").fetchall()
        con.close()
        print(f"{'信号':<34}{'行数':>7}{'天数':>6}  范围")
        for s, n, d, mn, mx in rows:
            print(f"{s:<34}{n:>7}{d:>6}  {mn[:10]} ~ {mx[:10]}")

    if args.analyze:
        res = analyze(db, args.horizon, args.min_samples, args.min_width,
                      args.draws, target_metric=args.target,
                      pool_generations=args.pool_generations)
        if not res:
            print("⏭  无足够数据，请先 --backfill")
            return 1
        rows, floor, gens = res
        print_report(rows, floor, args.horizon, args.target, generations=gens)

    if not (args.backfill or args.analyze or args.list):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
