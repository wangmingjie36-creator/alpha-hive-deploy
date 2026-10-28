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
前瞻收益在分析时从 `predictions` 联表（终点价 close_t7 / price_t30，见
`ic_diagnostics.FORWARD_CLOSE_COL`；**不是 price_t7**，那是离场价），不重复存储。

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
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

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


def _gex_state_sig(kind: str) -> Callable:
    """v0.45.362：蒸馏结果里的 `gex_state`（全到期日视图，就是政体路由用的那份；见 `gex_state` 模块）。

    · `gex.available`：有状态 ⇒ 1.0 / 0.0；没有状态（v0.45.362 之前的记录）⇒ None —— 覆盖率本身的观测点，
      不可得的日子**不**以缺行的形式消失。
    · `gex.total_gex` / `gex.negative`：仅可得时有值（不可得的 total 在状态里就是 None，不是 0.0 哨兵）。
    ⚠️ 与 `options.gamma_exposure` 不是同一个量：那是 OracleBee 主链（≤4 个到期日）的截断量，可与这里符号相反。
    """
    def _f(tr: Dict) -> Optional[float]:
        import gex_state as _gs
        st = _gs.of(tr)
        if st is None:
            return None
        if kind == "available":
            return 1.0 if st.get("available") else 0.0
        if not st.get("available"):
            return None
        if kind == "negative":
            return 1.0 if st.get("regime") == "negative_gex" else 0.0
        return _num(st.get("total_gex"))
    return _f


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
    # v0.45.362：政体路由用的那份 GEX（全到期日视图）。上面的 options.gamma_exposure 是主链截断量，另一个量
    "gex.available": _gex_state_sig("available"),
    "gex.total_gex": _gex_state_sig("total_gex"),
    "gex.negative": _gex_state_sig("negative"),

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

    # ── RivalBee 预期收益 ─────────────────────────────────────
    # v0.45.538：`ml.probability` 已摘除 —— 它出自 30 条 2023 年硬编码样本训的模型
    # （`ml_predictor_extended`，已退役），Rival 不再产出概率。库里的历史行不改写，只是不再写入新行。
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
    # ────────────────────────────────────────────────────────────────────────
    # v0.45.472（用户批准）：期权快照的「现价 / 整条链」不是当天真实观测的 (日期, 标的)。
    # 判据：快照 `_snapshot_stock_price` 对照 Twelve Data 日线收盘（当天与前后一个交易日）+ 快照内容，
    # 逐份分类（明细见 CHANGELOG v0.45.472）。只隔离市场观测五类（同上方条目），不动 Agent 分。
    # 盘后价（财报日收盘后 current_price，已由 official_price 修掉的另一类）不在此列：AMZN 07-30 / CRM 08-26 /
    # MSFT 07-29 / RKLB 07-22 / TSLA 07-23。
    # v0.45.476 二次检查撤回 6 对误判（行已从备份原样恢复）：CRWD 04-26 / 05-03 / 05-10 / 05-17——快照全在周日、
    # 对照的是前一交易日的**拆股调整后**收盘（Twelve Data），比值恒 ≈ 4，不是假价；VKTX 08-12（34.5）与 MU 08-26（952.0）——
    # 「行权价形状」判错：两者不在各自行权价网格的中点上（步长 2.5 / 5），不可能是行权价中位数；VKTX 那天就是调用方给的价。
    {
        "date": "2026-04-09",
        "tickers": ('CRCL', 'RKLB'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：CRCL, RKLB。",
        "evidence": ("cache/options_snapshot_{T}_2026-04-09.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-04-14",
        "tickers": ('AMZN',),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：AMZN。",
        "evidence": ("cache/options_snapshot_{T}_2026-04-14.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-04-15",
        "tickers": ('AMZN',),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：AMZN。",
        "evidence": ("cache/options_snapshot_{T}_2026-04-15.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-04-26",
        "tickers": ('AAPL', 'COST', 'DELL', 'HPE', 'RKLB'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：AAPL, COST, DELL, HPE, RKLB。",
        "evidence": ("cache/options_snapshot_{T}_2026-04-26.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-04-28",
        "tickers": ('CRCL',),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：CRCL。",
        "evidence": ("cache/options_snapshot_{T}_2026-04-28.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-04-29",
        "tickers": ('META', 'VKTX'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：META, VKTX。",
        "evidence": ("cache/options_snapshot_{T}_2026-04-29.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-05-03",
        "tickers": ('AAPL', 'META', 'SQ'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：AAPL, META, SQ。",
        "evidence": ("cache/options_snapshot_{T}_2026-05-03.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-05-06",
        "tickers": ('BILI',),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：BILI。",
        "evidence": ("cache/options_snapshot_{T}_2026-05-06.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-05-07",
        "tickers": ('BILI',),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：BILI。",
        "evidence": ("cache/options_snapshot_{T}_2026-05-07.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-05-10",
        "tickers": ('ASML', 'UPST'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：ASML, UPST。",
        "evidence": ("cache/options_snapshot_{T}_2026-05-10.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-05-15",
        "tickers": ('AMZN',),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：AMZN。",
        "evidence": ("cache/options_snapshot_{T}_2026-05-15.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-05-17",
        "tickers": ('BILI', 'CRCL', 'CRM', 'ICLN', 'SQ'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：BILI, CRCL, CRM, ICLN, SQ。",
        "evidence": ("cache/options_snapshot_{T}_2026-05-17.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-05-18",
        "tickers": ('CRCL',),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：CRCL。",
        "evidence": ("cache/options_snapshot_{T}_2026-05-18.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-05-24",
        "tickers": ('MSFT', 'NVDA', 'QCOM', 'TSLA', 'VKTX'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：MSFT, NVDA, QCOM, TSLA, VKTX。",
        "evidence": ("cache/options_snapshot_{T}_2026-05-24.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-06-08",
        "tickers": ('VKTX',),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：VKTX。",
        "evidence": ("cache/options_snapshot_{T}_2026-06-08.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-06-10",
        "tickers": ('AMZN', 'BILI', 'CRCL', 'META', 'MSFT', 'NVDA', 'QCOM', 'RKLB', 'TSLA', 'VKTX'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：现价是常数 145.0 / 100.0（analyze 兜底常数或测试传入价）：AMZN, BILI, CRCL, MSFT, QCOM, RKLB, TSLA, VKTX；现价与当天及前后一天收盘都偏离 > 20%（测试写穿生产或坏链，整份不可信）：META, NVDA。",
        "evidence": ("cache/options_snapshot_{T}_2026-06-10.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-06-15",
        "tickers": ('AMZN', 'CRCL', 'MSFT', 'NVDA', 'QCOM', 'TSLA'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：NVDA；现价是常数 145.0 / 100.0（analyze 兜底常数或测试传入价）：AMZN, CRCL, MSFT, QCOM, TSLA。",
        "evidence": ("cache/options_snapshot_{T}_2026-06-15.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-06-17",
        "tickers": ('MSFT', 'NVDA', 'QCOM', 'TSLA'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：MSFT, NVDA, QCOM, TSLA。",
        "evidence": ("cache/options_snapshot_{T}_2026-06-17.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-06-22",
        "tickers": ('AMZN', 'BILI', 'CRCL', 'META', 'MSFT', 'NVDA', 'QCOM', 'RKLB', 'TSLA', 'VKTX'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：AMZN, BILI, CRCL, META, MSFT, NVDA, QCOM, RKLB, TSLA, VKTX。",
        "evidence": ("cache/options_snapshot_{T}_2026-06-22.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-06-24",
        "tickers": ('MSFT', 'NVDA', 'QCOM', 'TSLA'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：MSFT, NVDA, QCOM, TSLA。",
        "evidence": ("cache/options_snapshot_{T}_2026-06-24.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-06-25",
        "tickers": ('AMZN', 'BILI', 'CRCL', 'META', 'MSFT', 'NVDA', 'QCOM', 'RKLB', 'TSLA', 'VKTX'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：AMZN, BILI, CRCL, META, MSFT, NVDA, QCOM, RKLB, TSLA, VKTX。",
        "evidence": ("cache/options_snapshot_{T}_2026-06-25.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-06-26",
        "tickers": ('AMZN', 'META', 'MSFT', 'NVDA', 'QCOM', 'RKLB', 'TSLA', 'VKTX'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：AMZN, BILI, META, MSFT, NVDA, QCOM, RKLB, TSLA, VKTX。",
        "evidence": ("cache/options_snapshot_{T}_2026-06-26.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-06-27",
        "tickers": ('AMZN', 'BILI', 'CRCL', 'META', 'MSFT', 'NVDA', 'QCOM', 'RKLB', 'TSLA', 'VKTX'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：AMZN, BILI, CRCL, META, MSFT, NVDA, QCOM, RKLB, TSLA, VKTX。",
        "evidence": ("cache/options_snapshot_{T}_2026-06-27.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-06-29",
        "tickers": ('AMD', 'META', 'MSFT', 'NVDA', 'QCOM', 'TSLA', 'VKTX'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：现价是 0.5 整数倍、偏离收盘 > 1%（行权价中位数兜底；形状判，≈2% 概率是恰好整数的真价）：AMD, META, MSFT, NVDA, QCOM, TSLA, VKTX。",
        "evidence": ("cache/options_snapshot_{T}_2026-06-29.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-06-30",
        "tickers": ('AMZN', 'BILI', 'CRCL', 'META', 'MSFT', 'NVDA', 'QCOM', 'RKLB', 'TSLA', 'VKTX'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：现价是常数 145.0 / 100.0（analyze 兜底常数或测试传入价）：META, MSFT, NVDA, QCOM, TSLA, VKTX；现价是 0.5 整数倍、偏离收盘 > 1%（行权价中位数兜底；形状判，≈2% 概率是恰好整数的真价）：AMZN, BILI, CRCL, RKLB。",
        "evidence": ("cache/options_snapshot_{T}_2026-06-30.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-07-01",
        "tickers": ('MSFT', 'NVDA', 'QCOM', 'TSLA'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：MSFT, NVDA, QCOM, TSLA。",
        "evidence": ("cache/options_snapshot_{T}_2026-07-01.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-07-02",
        "tickers": ('MSFT', 'NVDA', 'QCOM', 'TSLA'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：样本链（总 OI 85200 / P/C 0.54 / 到期 2026-03-21 的硬编码样本，整份期权观测是假的）：MSFT, NVDA, QCOM, TSLA。",
        "evidence": ("cache/options_snapshot_{T}_2026-07-02.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-07-08",
        "tickers": ('NVDA',),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：现价是常数 145.0 / 100.0（analyze 兜底常数或测试传入价）：NVDA。",
        "evidence": ("cache/options_snapshot_{T}_2026-07-08.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-07-15",
        "tickers": ('NVDA', 'TSLA', 'VKTX'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：现价与当天及前后一天收盘都偏离 > 20%（测试写穿生产或坏链，整份不可信）：NVDA, TSLA, VKTX。",
        "evidence": ("cache/options_snapshot_{T}_2026-07-15.json", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-08-14",
        "tickers": ('QCOM',),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.472：现价是 0.5 整数倍、偏离收盘 > 1%（行权价中位数兜底；形状判，≈2% 概率是恰好整数的真价）：QCOM。",
        "evidence": ("cache/options_snapshot_{T}_2026-08-14.json", "CHANGELOG.md#v0.45.472"),
    },
    # v0.45.472：CBOE 主源时代（06-30 起）里退到 yfinance 备用链的日子——v0.45.468 前 yfinance 链补 BS gamma
    # 用「OI>50 的看涨行权价中位数」当 S（analyze 调 fetch_options_chain 从不传价），GEX 与 CBOE 链不是同一个量。
    # 判据：快照 quote_set.error == "chain source is not cboe"（quote_set 09-04 起才有；06-30 ~ 09-03 的同类日子认不出，待验证）。
    # 只隔离 GEX：analyze 那天拿到的是调用方真价，IV / P/C / OI 不受影响。
    {
        "date": "2026-09-04",
        "tickers": ('TMUS',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-09-04.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-09-10",
        "tickers": ('BILI',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-09-10.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-09-11",
        "tickers": ('BILI', 'DE', 'TMO'),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-09-11.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-09-14",
        "tickers": ('BILI', 'DE', 'TMO'),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-09-14.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-09-15",
        "tickers": ('BILI',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-09-15.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-09-16",
        "tickers": ('BILI', 'DE', 'TMO'),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-09-16.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-09-22",
        "tickers": ('BILI', 'TMO'),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-09-22.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-09-23",
        "tickers": ('ABBV', 'AMC', 'AMZN', 'BILI', 'BRK-B', 'COST', 'CRCL', 'CRM', 'CVX', 'DE', 'DELL', 'JNJ', 'META', 'MSFT', 'NVDA', 'QCOM', 'RKLB', 'T', 'TSLA', 'VKTX', 'VZ', 'XOM'),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-09-23.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-09-28",
        "tickers": ('BILI',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-09-28.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-09-29",
        "tickers": ('BILI',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-09-29.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-09-30",
        "tickers": ('BILI',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-09-30.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-10-01",
        "tickers": ('BILI',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-10-01.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-10-02",
        "tickers": ('BILI',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-10-02.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-10-06",
        "tickers": ('BILI',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-10-06.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-10-08",
        "tickers": ('BILI',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-10-08.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    {
        "date": "2026-10-09",
        "tickers": ('BILI',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.472：当天这些标的的期权链来自 yfinance 备用链，BS gamma 的 S 是行权价中位数而非股价，GEX 不是同一个量（v0.45.468 已修）。",
        "evidence": ("cache/options_snapshot_{T}_2026-10-09.json（quote_set.error）", "CHANGELOG.md#v0.45.468", "CHANGELOG.md#v0.45.472"),
    },
    # v0.45.475：上面那批 yfinance 备用链只认 `quote_set.error`（09-04 起才有），06-30 ~ 09-03 认不出；而且
    # `_refill_empty_quote_set` 事后会用 CBOE 回填空的 quote_set、把 error 抹掉（10-05 BILI 实测，回填于 23:33）。
    # 改用期权链数据本身的厂商指纹：快照 key_levels 的 IV——CBOE 给 4 位小数，Yahoo 是二进制长浮点。
    # 在已知来源的快照上核过：CBOE 581/582、yfinance 44/44、06-30 前 yfinance 主源时代 541/542 分对（例外：CBOE 那份是
    # 10-05 BILI 本身——证据被回填抹掉的那份；yfinance 时代那份是测试夹具写穿生产、早已隔离）〔v0.45.476 更正措辞〕；
    # 06-30 ~ 09-03 共 548 份：样本链 8（已隔离）、测试夹具 1（07-08 NVDA，已隔离）、按指纹分出 CBOE 538、yfinance 1（09-01 BILI）。
    {
        "date": "2026-09-01",
        "tickers": ("BILI",),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.475：当天 BILI 的期权链来自 yfinance 备用链（key_levels IV 为 Yahoo 长浮点指纹；该日没有 quote_set 可认），BS gamma 的 S 是行权价中位数，GEX 不是同一个量。",
        "evidence": ("cache/options_snapshot_BILI_2026-09-01.json（key_levels.iv）", "CHANGELOG.md#v0.45.475"),
    },
    {
        "date": "2026-10-05",
        "tickers": ("BILI",),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.475：当天 BILI 的期权链来自 yfinance 备用链（key_levels IV 为 Yahoo 长浮点指纹）；quote_set 当晚 23:33 被 _refill_empty_quote_set 用 CBOE 回填，原来的 error 被抹掉，v0.45.472 按 quote_set.error 认时漏掉。",
        "evidence": ("cache/options_snapshot_BILI_2026-10-05.json（key_levels.iv / _quote_set_refilled_at）", "CHANGELOG.md#v0.45.475"),
    },
    # ────────────────────────────────────────────────────────────────────────
    # v0.45.478（用户批准）：v0.45.471 普查出的 `options.iv_current == 25.0`（兜底常数）。共 18 行，
    # 12 行已随 v0.45.472 的假现价 / 假链隔离删掉，这里是剩下的 6 行。只隔离 iv_current：
    # 同日的 iv_rank / P/C / OI / GEX 不由这个常数产生（CRCL 06-09、BILI / META 06-15 的 iv_rank
    # 可能由旧代码拿 25.0 当分子算出，**待验证**，未隔离）。
    {
        "date": "2026-06-09",
        "tickers": ("CRCL",),
        "signals": ("options.iv_current",),
        "reason": "v0.45.478（用户 2026-10-12 批准）：OptionsAgent 当日 ATM IV 无效且无 last_valid_iv 缓存时把 iv_current 兜底成常数 25.0（v0.45.471 起改记 None、不入库）。这里的 25.0 不是观测；同日其余期权观测不是这个兜底的产物，不隔离。",
        "evidence": (".swarm_results_2026-06-09.json（OracleBeeEcho.details.iv_current = 25.0，读的是 options_snapshot_CRCL_2026-06-10.json 槽位）",
                     "CHANGELOG.md#v0.45.471", "CHANGELOG.md#v0.45.478"),
    },
    {
        "date": "2026-06-15",
        "tickers": ("BILI", "META"),
        "signals": ("options.iv_current",),
        "reason": "v0.45.478（用户 2026-10-12 批准）：OptionsAgent 当日 ATM IV 无效且无 last_valid_iv 缓存时把 iv_current 兜底成常数 25.0（v0.45.471 起改记 None、不入库）。这里的 25.0 不是观测；同日其余期权观测不是这个兜底的产物，不隔离。",
        "evidence": (".swarm_results_2026-06-15.json（OracleBeeEcho.details.iv_current = 25.0；BILI 的快照文件之后被 02:12 重写成 12.5，入库值来自扫描结果）",
                     "CHANGELOG.md#v0.45.471", "CHANGELOG.md#v0.45.478"),
    },
    {
        "date": "2026-09-15",
        "tickers": ("VKTX",),
        "signals": ("options.iv_current",),
        "reason": "v0.45.478（用户 2026-10-12 批准）：OptionsAgent 当日 ATM IV 无效且无 last_valid_iv 缓存时把 iv_current 兜底成常数 25.0（v0.45.471 起改记 None、不入库）。这里的 25.0 不是观测；同日其余期权观测不是这个兜底的产物，不隔离。",
        "evidence": (".swarm_results_2026-09-15.json", "cache/options_snapshot_VKTX_2026-09-15.json", "CHANGELOG.md#v0.45.478"),
    },
    {
        "date": "2026-09-16",
        "tickers": ("VKTX",),
        "signals": ("options.iv_current",),
        "reason": "v0.45.478（用户 2026-10-12 批准）：OptionsAgent 当日 ATM IV 无效且无 last_valid_iv 缓存时把 iv_current 兜底成常数 25.0（v0.45.471 起改记 None、不入库）。这里的 25.0 不是观测；同日其余期权观测不是这个兜底的产物，不隔离。",
        "evidence": (".swarm_results_2026-09-16.json", "cache/options_snapshot_VKTX_2026-09-16.json", "CHANGELOG.md#v0.45.478"),
    },
    {
        "date": "2026-09-17",
        "tickers": ("VKTX",),
        "signals": ("options.iv_current",),
        "reason": "v0.45.478（用户 2026-10-12 批准）：OptionsAgent 当日 ATM IV 无效且无 last_valid_iv 缓存时把 iv_current 兜底成常数 25.0（v0.45.471 起改记 None、不入库）。这里的 25.0 不是观测；同日其余期权观测不是这个兜底的产物，不隔离。",
        "evidence": (".swarm_results_2026-09-17.json", "cache/options_snapshot_VKTX_2026-09-17.json", "CHANGELOG.md#v0.45.478"),
    },
    # ────────────────────────────────────────────────────────────────────────
    # v0.45.479：按 `.swarm_results_<日期>.json` 的 OracleBee details（signal_archive 的真实来源，5343 个值逐位相同）重判，
    # 补上 v0.45.472 按 cache 快照判漏掉的（同一天常是另一次运行）。判据与产出见
    # `experiments/reclassify_option_quarantine_from_swarm_20261012.py`；同批撤回的见 CHANGELOG v0.45.479。
    {
        "date": "2026-03-31",
        "tickers": ('AMZN',),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.479：归档那次运行（swarm_results）的期权观测是假的——AMZN：样本链（总 OI 85200 / P/C 0.54）。",
        "evidence": (".swarm_results_2026-03-31.json（OracleBeeEcho.details）", "CHANGELOG.md#v0.45.479"),
    },
    {
        "date": "2026-04-02",
        "tickers": ('AMZN',),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.479：归档那次运行（swarm_results）的期权观测是假的——AMZN：样本链（总 OI 85200 / P/C 0.54）。",
        "evidence": (".swarm_results_2026-04-02.json（OracleBeeEcho.details）", "CHANGELOG.md#v0.45.479"),
    },
    {
        "date": "2026-04-06",
        "tickers": ('AMZN',),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.479：归档那次运行（swarm_results）的期权观测是假的——AMZN：样本链（总 OI 85200 / P/C 0.54）。",
        "evidence": (".swarm_results_2026-04-06.json（OracleBeeEcho.details）", "CHANGELOG.md#v0.45.479"),
    },
    {
        "date": "2026-06-09",
        "tickers": ('AMZN', 'BILI', 'CRCL', 'META', 'MSFT', 'NVDA', 'QCOM', 'RKLB', 'TSLA', 'VKTX'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.479：归档那次运行（swarm_results）的期权观测是假的——AMZN：空链（总 OI 0，Yahoo 限流垃圾链）；BILI：空链（总 OI 0，Yahoo 限流垃圾链）；CRCL：空链（总 OI 0，Yahoo 限流垃圾链）；META：现价偏离收盘 > 20%；MSFT：现价常数 145 / 100；NVDA：现价偏离收盘 > 20%；QCOM：空链（总 OI 0，Yahoo 限流垃圾链）；RKLB：空链（总 OI 0，Yahoo 限流垃圾链）；TSLA：现价常数 145 / 100；VKTX：现价常数 145 / 100。",
        "evidence": (".swarm_results_2026-06-09.json（OracleBeeEcho.details）", "CHANGELOG.md#v0.45.479"),
    },
    {
        "date": "2026-06-15",
        "tickers": ('BILI', 'META'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.479：归档那次运行（swarm_results）的期权观测是假的——BILI：样本链（总 OI 85200 / P/C 0.54）；META：空链（总 OI 0，Yahoo 限流垃圾链）。",
        "evidence": (".swarm_results_2026-06-15.json（OracleBeeEcho.details）", "CHANGELOG.md#v0.45.479"),
    },
    {
        "date": "2026-06-17",
        "tickers": ('AMZN', 'META'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.479：归档那次运行（swarm_results）的期权观测是假的——AMZN：空链（总 OI 0，Yahoo 限流垃圾链）；META：样本链（总 OI 85200 / P/C 0.54）。",
        "evidence": (".swarm_results_2026-06-17.json（OracleBeeEcho.details）", "CHANGELOG.md#v0.45.479"),
    },
    {
        "date": "2026-06-18",
        "tickers": ('NVDA', 'VKTX'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.479：归档那次运行（swarm_results）的期权观测是假的——NVDA：样本链（总 OI 85200 / P/C 0.54）；VKTX：样本链（总 OI 85200 / P/C 0.54）。",
        "evidence": (".swarm_results_2026-06-18.json（OracleBeeEcho.details）", "CHANGELOG.md#v0.45.479"),
    },
    {
        "date": "2026-06-23",
        "tickers": ('AMZN', 'BILI'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.479：归档那次运行（swarm_results）的期权观测是假的——AMZN：样本链（总 OI 85200 / P/C 0.54）；BILI：样本链（总 OI 85200 / P/C 0.54）。",
        "evidence": (".swarm_results_2026-06-23.json（OracleBeeEcho.details）", "CHANGELOG.md#v0.45.479"),
    },
    {
        "date": "2026-06-26",
        "tickers": ('CRCL',),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.479：归档那次运行（swarm_results）的期权观测是假的——CRCL：样本链（总 OI 85200 / P/C 0.54）。",
        "evidence": (".swarm_results_2026-06-26.json（OracleBeeEcho.details）", "CHANGELOG.md#v0.45.479"),
    },
    {
        "date": "2026-06-29",
        "tickers": ('AMZN', 'BILI', 'CRCL', 'RKLB'),
        "signals": ("options.iv_current", "options.put_call_ratio",
                    "options.gamma_exposure", "options.total_oi", "bear.options_bear"),
        "reason": "v0.45.479：归档那次运行（swarm_results）的期权观测是假的——AMZN：空链（总 OI 0，Yahoo 限流垃圾链）；BILI：现价是落在行权价网格上的行权价中位数；CRCL：现价是落在行权价网格上的行权价中位数；RKLB：现价是落在行权价网格上的行权价中位数。",
        "evidence": (".swarm_results_2026-06-29.json（OracleBeeEcho.details）", "CHANGELOG.md#v0.45.479"),
    },
    {
        "date": "2026-08-31",
        "tickers": ('TMO',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.479：归档那次运行的期权链来自 yfinance 备用链（key_levels IV 为 Yahoo 长浮点指纹，审阅发现、v0.45.475 按快照判时漏掉），GEX 的 BS gamma 用行权价中位数当 S。",
        "evidence": (".swarm_results_2026-08-31.json（OracleBeeEcho.details.key_levels）", "CHANGELOG.md#v0.45.479"),
    },
    {
        "date": "2026-06-23",
        "tickers": ('NVDA',),
        "signals": ("options.iv_current",),
        "reason": "v0.45.479：归档那次运行的价、OI、P/C 都是真的（v0.45.472 按快照误判为常数 145，已撤回），但 iv_current 是垃圾常数 12.5——只挡这一项。",
        "evidence": (".swarm_results_2026-06-23.json（OracleBeeEcho.details.iv_current）", "CHANGELOG.md#v0.45.479"),
    },
    # ────────────────────────────────────────────────────────────────────────
    # v0.45.481（用户批准）：iv_current == 25.0（兜底常数）那些日子的 options.iv_rank / options.iv_percentile。
    # 08-14 前的代码拿 current_iv 直接算这两个值（`calculate_iv_rank(current_iv, hist_iv)`），常数进 ⇒ 常数函数出。
    # 15 对逐对核对过：.swarm_results 里 iv_current == 25.0、rank / pct 与归档逐位相等、无 iv_rank_source（旧代码）。
    # VKTX 09-15 ~ 17 不在此列：那时 iv_rank 走 hv_proxy（当前 HV 对 HV 分布），不读 current_iv。
    {
        "date": "2026-04-26",
        "tickers": ('AAPL', 'HPE', 'DELL'),
        "signals": ("options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.481（用户 2026-10-12 批准）：v0.43.19（08-14）之前 IV Rank / IV Percentile = 当前 IV 对「HV×premium」历史分布排名，当日 ATM IV 无效且无缓存时当前 IV 被兜底成常数 25.0 ⇒ 这两个值是常数的函数，不是观测。META / BILI 06-15 用公开日线重建 HV、由 rank 反解 premium k=1.25（代码缺省）后百分位独立吻合到小数点（CRCL 06-09 任意合法 k 都给 0，同吻合）。",
        "evidence": (".swarm_results_2026-04-26.json（OracleBeeEcho.details.iv_current = 25.0 / iv_rank / iv_percentile）",
                     "CHANGELOG.md#v0.45.481"),
    },
    {
        "date": "2026-05-03",
        "tickers": ('AAPL',),
        "signals": ("options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.481（用户 2026-10-12 批准）：v0.43.19（08-14）之前 IV Rank / IV Percentile = 当前 IV 对「HV×premium」历史分布排名，当日 ATM IV 无效且无缓存时当前 IV 被兜底成常数 25.0 ⇒ 这两个值是常数的函数，不是观测。META / BILI 06-15 用公开日线重建 HV、由 rank 反解 premium k=1.25（代码缺省）后百分位独立吻合到小数点（CRCL 06-09 任意合法 k 都给 0，同吻合）。",
        "evidence": (".swarm_results_2026-05-03.json（OracleBeeEcho.details.iv_current = 25.0 / iv_rank / iv_percentile）",
                     "CHANGELOG.md#v0.45.481"),
    },
    {
        "date": "2026-05-10",
        "tickers": ('UPST',),
        "signals": ("options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.481（用户 2026-10-12 批准）：v0.43.19（08-14）之前 IV Rank / IV Percentile = 当前 IV 对「HV×premium」历史分布排名，当日 ATM IV 无效且无缓存时当前 IV 被兜底成常数 25.0 ⇒ 这两个值是常数的函数，不是观测。META / BILI 06-15 用公开日线重建 HV、由 rank 反解 premium k=1.25（代码缺省）后百分位独立吻合到小数点（CRCL 06-09 任意合法 k 都给 0，同吻合）。",
        "evidence": (".swarm_results_2026-05-10.json（OracleBeeEcho.details.iv_current = 25.0 / iv_rank / iv_percentile）",
                     "CHANGELOG.md#v0.45.481"),
    },
    {
        "date": "2026-05-17",
        "tickers": ('ICLN',),
        "signals": ("options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.481（用户 2026-10-12 批准）：v0.43.19（08-14）之前 IV Rank / IV Percentile = 当前 IV 对「HV×premium」历史分布排名，当日 ATM IV 无效且无缓存时当前 IV 被兜底成常数 25.0 ⇒ 这两个值是常数的函数，不是观测。META / BILI 06-15 用公开日线重建 HV、由 rank 反解 premium k=1.25（代码缺省）后百分位独立吻合到小数点（CRCL 06-09 任意合法 k 都给 0，同吻合）。",
        "evidence": (".swarm_results_2026-05-17.json（OracleBeeEcho.details.iv_current = 25.0 / iv_rank / iv_percentile）",
                     "CHANGELOG.md#v0.45.481"),
    },
    {
        "date": "2026-06-09",
        "tickers": ('CRCL',),
        "signals": ("options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.481（用户 2026-10-12 批准）：v0.43.19（08-14）之前 IV Rank / IV Percentile = 当前 IV 对「HV×premium」历史分布排名，当日 ATM IV 无效且无缓存时当前 IV 被兜底成常数 25.0 ⇒ 这两个值是常数的函数，不是观测。META / BILI 06-15 用公开日线重建 HV、由 rank 反解 premium k=1.25（代码缺省）后百分位独立吻合到小数点（CRCL 06-09 任意合法 k 都给 0，同吻合）。",
        "evidence": (".swarm_results_2026-06-09.json（OracleBeeEcho.details.iv_current = 25.0 / iv_rank / iv_percentile）",
                     "CHANGELOG.md#v0.45.481"),
    },
    {
        "date": "2026-06-15",
        "tickers": ('QCOM', 'TSLA', 'NVDA', 'MSFT', 'BILI', 'AMZN', 'META', 'CRCL'),
        "signals": ("options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.481（用户 2026-10-12 批准）：v0.43.19（08-14）之前 IV Rank / IV Percentile = 当前 IV 对「HV×premium」历史分布排名，当日 ATM IV 无效且无缓存时当前 IV 被兜底成常数 25.0 ⇒ 这两个值是常数的函数，不是观测。META / BILI 06-15 用公开日线重建 HV、由 rank 反解 premium k=1.25（代码缺省）后百分位独立吻合到小数点（CRCL 06-09 任意合法 k 都给 0，同吻合）。",
        "evidence": (".swarm_results_2026-06-15.json（OracleBeeEcho.details.iv_current = 25.0 / iv_rank / iv_percentile）",
                     "CHANGELOG.md#v0.45.481"),
    },
    # ────────────────────────────────────────────────────────────────────────
    # v0.45.486（用户批准）：iv_current = 12.5 / 6.25——Yahoo 二分求解失败的减半阶梯值取中位数（v0.45.482 已在源头剔除）。
    # 13 对逐对核对过 .swarm_results 与归档逐位相等。08-14 前的 12 对连同由它算出的 iv_rank / iv_percentile；
    # TMO 08-31 只挡 iv_current（rank 走 hv_proxy）。06-23 NVDA 的 iv_current 已由 v0.45.479 隔离、不重复列。
    {
        "date": "2026-03-11",
        "tickers": ('NVDA',),
        "signals": ("options.iv_current", "options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.486（用户 2026-10-13 批准）：iv_current 是 Yahoo 求解失败值（减半阶梯 1/8、1/16 ×100 = 12.5 / 6.25）的中位数，不是观测（对照 CBOE：阶梯值 99.7% 相差 > 10 个波动率点，见 yahoo_iv）；08-14 前 iv_rank / iv_percentile 由同一个 current_iv 算出，一并隔离。",
        "evidence": (".swarm_results_2026-03-11.json（OracleBeeEcho.details.iv_current / iv_rank / iv_percentile）",
                     "CHANGELOG.md#v0.45.482", "CHANGELOG.md#v0.45.486"),
    },
    {
        "date": "2026-03-12",
        "tickers": ('NVDA',),
        "signals": ("options.iv_current", "options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.486（用户 2026-10-13 批准）：iv_current 是 Yahoo 求解失败值（减半阶梯 1/8、1/16 ×100 = 12.5 / 6.25）的中位数，不是观测（对照 CBOE：阶梯值 99.7% 相差 > 10 个波动率点，见 yahoo_iv）；08-14 前 iv_rank / iv_percentile 由同一个 current_iv 算出，一并隔离。",
        "evidence": (".swarm_results_2026-03-12.json（OracleBeeEcho.details.iv_current / iv_rank / iv_percentile）",
                     "CHANGELOG.md#v0.45.482", "CHANGELOG.md#v0.45.486"),
    },
    {
        "date": "2026-06-15",
        "tickers": ('RKLB',),
        "signals": ("options.iv_current", "options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.486（用户 2026-10-13 批准）：iv_current 是 Yahoo 求解失败值（减半阶梯 1/8、1/16 ×100 = 12.5 / 6.25）的中位数，不是观测（对照 CBOE：阶梯值 99.7% 相差 > 10 个波动率点，见 yahoo_iv）；08-14 前 iv_rank / iv_percentile 由同一个 current_iv 算出，一并隔离。",
        "evidence": (".swarm_results_2026-06-15.json（OracleBeeEcho.details.iv_current / iv_rank / iv_percentile）",
                     "CHANGELOG.md#v0.45.482", "CHANGELOG.md#v0.45.486"),
    },
    {
        "date": "2026-06-17",
        "tickers": ('BILI', 'CRCL', 'RKLB', 'VKTX'),
        "signals": ("options.iv_current", "options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.486（用户 2026-10-13 批准）：iv_current 是 Yahoo 求解失败值（减半阶梯 1/8、1/16 ×100 = 12.5 / 6.25）的中位数，不是观测（对照 CBOE：阶梯值 99.7% 相差 > 10 个波动率点，见 yahoo_iv）；08-14 前 iv_rank / iv_percentile 由同一个 current_iv 算出，一并隔离。",
        "evidence": (".swarm_results_2026-06-17.json（OracleBeeEcho.details.iv_current / iv_rank / iv_percentile）",
                     "CHANGELOG.md#v0.45.482", "CHANGELOG.md#v0.45.486"),
    },
    {
        "date": "2026-06-23",
        "tickers": ('CRCL', 'META', 'QCOM', 'RKLB', 'VKTX'),
        "signals": ("options.iv_current", "options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.486（用户 2026-10-13 批准）：iv_current 是 Yahoo 求解失败值（减半阶梯 1/8、1/16 ×100 = 12.5 / 6.25）的中位数，不是观测（对照 CBOE：阶梯值 99.7% 相差 > 10 个波动率点，见 yahoo_iv）；08-14 前 iv_rank / iv_percentile 由同一个 current_iv 算出，一并隔离。",
        "evidence": (".swarm_results_2026-06-23.json（OracleBeeEcho.details.iv_current / iv_rank / iv_percentile）",
                     "CHANGELOG.md#v0.45.482", "CHANGELOG.md#v0.45.486"),
    },
    {
        "date": "2026-08-31",
        "tickers": ('TMO',),
        "signals": ("options.iv_current",),
        "reason": "v0.45.486（用户 2026-10-13 批准）：iv_current 是 Yahoo 求解失败值 1/16 ×100 = 6.25 的中位数，不是观测（见 yahoo_iv）。当天 iv_rank 走 hv_proxy（当前 HV 对 HV 分布），不读 current_iv，不隔离。",
        "evidence": (".swarm_results_2026-08-31.json（OracleBeeEcho.details.iv_current = 6.25, iv_rank_source = hv_proxy）",
                     "CHANGELOG.md#v0.45.482", "CHANGELOG.md#v0.45.486"),
    },
    # ────────────────────────────────────────────────────────────────────────
    # v0.45.490：486 漏掉的 7 对——iv_current（12.5 / 6.25）早被 472 / 479 清掉，由它算出的 rank / pct 还在。
    # 判定以 .swarm_results 为准（不是以归档里现存的 iv_current 为准，那正是 486 的漏洞）。
    {
        "date": "2026-06-09",
        "tickers": ('RKLB',),
        "signals": ("options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.490（486 二次检查；属用户 10-13 批准的「12.5 / 6.25 及由它算出的 iv_rank / iv_percentile」范围）：这几对的 iv_current 是 Yahoo 求解失败阶梯值的中位数（12.5 / 6.25），已由 v0.45.472 / 479 隔离；08-14 前 iv_rank / iv_percentile 由同一个 current_iv 算出（全是 0 / 0），而 472 / 479 只挡五类市场观测、不含这两类，486 又只从归档里现存的 iv_current 行找、漏了它们。",
        "evidence": (".swarm_results_2026-06-09.json（OracleBeeEcho.details.iv_current ∈ {12.5, 6.25}, iv_rank = 0, iv_percentile = 0）",
                     "CHANGELOG.md#v0.45.490"),
    },
    {
        "date": "2026-06-17",
        "tickers": ('MSFT', 'TSLA', 'AMZN'),
        "signals": ("options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.490（486 二次检查；属用户 10-13 批准的「12.5 / 6.25 及由它算出的 iv_rank / iv_percentile」范围）：这几对的 iv_current 是 Yahoo 求解失败阶梯值的中位数（12.5 / 6.25），已由 v0.45.472 / 479 隔离；08-14 前 iv_rank / iv_percentile 由同一个 current_iv 算出（全是 0 / 0），而 472 / 479 只挡五类市场观测、不含这两类，486 又只从归档里现存的 iv_current 行找、漏了它们。",
        "evidence": (".swarm_results_2026-06-17.json（OracleBeeEcho.details.iv_current ∈ {12.5, 6.25}, iv_rank = 0, iv_percentile = 0）",
                     "CHANGELOG.md#v0.45.490"),
    },
    {
        "date": "2026-06-23",
        "tickers": ('NVDA', 'AMZN'),
        "signals": ("options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.490（486 二次检查；属用户 10-13 批准的「12.5 / 6.25 及由它算出的 iv_rank / iv_percentile」范围）：这几对的 iv_current 是 Yahoo 求解失败阶梯值的中位数（12.5 / 6.25），已由 v0.45.472 / 479 隔离；08-14 前 iv_rank / iv_percentile 由同一个 current_iv 算出（全是 0 / 0），而 472 / 479 只挡五类市场观测、不含这两类，486 又只从归档里现存的 iv_current 行找、漏了它们。",
        "evidence": (".swarm_results_2026-06-23.json（OracleBeeEcho.details.iv_current ∈ {12.5, 6.25}, iv_rank = 0, iv_percentile = 0）",
                     "CHANGELOG.md#v0.45.490"),
    },
    {
        "date": "2026-06-29",
        "tickers": ('AMZN',),
        "signals": ("options.iv_rank", "options.iv_percentile"),
        "reason": "v0.45.490（486 二次检查；属用户 10-13 批准的「12.5 / 6.25 及由它算出的 iv_rank / iv_percentile」范围）：这几对的 iv_current 是 Yahoo 求解失败阶梯值的中位数（12.5 / 6.25），已由 v0.45.472 / 479 隔离；08-14 前 iv_rank / iv_percentile 由同一个 current_iv 算出（全是 0 / 0），而 472 / 479 只挡五类市场观测、不含这两类，486 又只从归档里现存的 iv_current 行找、漏了它们。",
        "evidence": (".swarm_results_2026-06-29.json（OracleBeeEcho.details.iv_current ∈ {12.5, 6.25}, iv_rank = 0, iv_percentile = 0）",
                     "CHANGELOG.md#v0.45.490"),
    },
    # ────────────────────────────────────────────────────────────────────────
    # v0.45.499：10-12 重放（10-13 07:33 写入生产，代码 0993efd = v0.45.481，早于 482 的多数规则）里
    # BILI 的链来自 yfinance，ATM IV 全是 Yahoo 求解失败阶梯值 ⇒ iv_current = 6.25。形状同 v0.45.486 的 TMO 08-31。
    {
        "date": "2026-10-12",
        "tickers": ('BILI',),
        "signals": ("options.iv_current",),
        "reason": "v0.45.499（用户 2026-10-13 批准「删掉假 iv」）：iv_current 是 Yahoo 求解失败值 1/16 ×100 = 6.25 的中位数，不是观测（见 yahoo_iv；BILI 前后几日 IV 49–53）。来自 10-12 重放，重放代码早于 v0.45.482 的多数规则。当天 iv_rank 走 hv_proxy（当前 HV 对 HV 分布），不读 current_iv，不隔离；同一条 yfinance 链的 P/C 298.46 不属本次批准范围，未动。",
        "evidence": (".swarm_results_2026-10-12.json（OracleBeeEcho.details.iv_current = 6.25, iv_rank_source = hv_proxy, chain_source = yfinance）",
                     "cache/options_snapshot_BILI_2026-10-12.json（iv_raw_observed = 6.2509375）",
                     "CHANGELOG.md#v0.45.486", "CHANGELOG.md#v0.45.499"),
    },
    # v0.45.512：同一条 yfinance 链的 P/C。10-12（哥伦布日）Yahoo 全市场 OI 几乎清零，只剩几个月没成交的旧合约上
    # 冻结的旧数 ⇒ $5 put 22,065 ÷ $8 call 74 = 298.46（前后几天 0.64–1.08）。源头判据见 options_analyzer.assess_oi_staleness。
    {
        "date": "2026-10-12",
        "tickers": ('BILI',),
        "signals": ("options.put_call_ratio",),
        "reason": "v0.45.512（用户 2026-10-16 批准「隔离 BILI 10-12 那条 298.46 的 put/call」）：P/C 是 yfinance 链 OI 陈旧的产物，不是观测——当天 Yahoo 的 OI 几乎全被清零（30 只标的「最近有成交的合约里 OI>0」占比 0.00–0.04，正常日 0.81–0.99），BILI 只剩 2026-05/06 起没成交的 $5 put 22,065、$24 put 21 与 $8 call 74 ⇒ 22,086 ÷ 74 = 298.46；前后几天 1.03 / 1.02 / 1.08。归档里唯一一条 P/C > 10。同链的 total_oi 22,160、gamma_exposure −0.0611 同源，当时未获批准；v0.45.522 另立条目隔离。",
        "evidence": ("_manual_backups/2026-10-13_1012_replay_v0.45.484_r2/replay_artifacts/recording_union4（Yahoo v7 options 响应）",
                     "cache/options_snapshot_BILI_2026-10-12.json（put_call_ratio = 298.46, total_oi = 22160）",
                     "CHANGELOG.md#v0.45.503", "CHANGELOG.md#v0.45.512"),
    },
    # v0.45.522（用户 2026-10-16 批准「隔离并从库里删掉……不要脏数据污染历史库」）：坏链派生的期权观测。
    # ① 同一条 OI 陈旧的 BILI 10-12 yfinance 链上的另两个 OI 派生量（512 当时只批了 P/C）。
    {
        "date": "2026-10-12",
        "tickers": ('BILI',),
        "signals": ("options.total_oi", "options.gamma_exposure"),
        "reason": "v0.45.522（用户 2026-10-16 批准）：与 v0.45.512 隔离的 P/C 298.46 同一条 OI 陈旧的 yfinance 链——total_oi 22,160 只是冻结旧数（$5 put 22,065 + $24 put 21 + $8 call 74；前后几天 16.0–17.0 万），gamma_exposure −0.0611 由同一份 OI 加权（前后几天 −1.25 / +0.68）。都不是观测。",
        "evidence": ("cache/options_snapshot_BILI_2026-10-12.json（total_oi = 22160）", "CHANGELOG.md#v0.45.512",
                     "CHANGELOG.md#v0.45.522"),
    },
    # ② 06-17 VKTX / CRCL：call 侧 OI 全 0 的残链。P/C 是 `calculate_put_call_ratio` 的假中性 1.0（v0.45.516 已改返回 None），
    #    total_oi 13 / 97 与 GEX 0.0（v0.45.63 前「算不出」写成 0）同链。
    {
        "date": "2026-06-17",
        "tickers": ('CRCL', 'VKTX'),
        "signals": ("options.put_call_ratio", "options.total_oi", "options.gamma_exposure"),
        "reason": "v0.45.522（用户 2026-10-16 批准）：当天两只的链只剩残片——快照 key_levels 的 call OI 0/0/0、put OI VKTX 10/2/1、CRCL 96/1/0。P/C 1.0 是 calculate_put_call_ratio 在 call 侧为 0 时返回的假中性（v0.45.516 起改 None）；total_oi 13 / 97（前后几天 VKTX 2.3–2.4 万、CRCL 10–11 万）；gamma_exposure 0.0 是 v0.45.63 前「算不出」写成的 0（前后几天 VKTX 1.64 / −0.002、CRCL 2.21 / 0.99）。都不是观测。",
        "evidence": ("cache/options_snapshot_VKTX_2026-06-17.json", "cache/options_snapshot_CRCL_2026-06-17.json",
                     "CHANGELOG.md#v0.45.516", "CHANGELOG.md#v0.45.522"),
    },
    # ③ 样本链早退写的 total_oi = 0（v0.45.522 起改 None）：期权链取不到 ⇒ `OptionsAgent.analyze` 中性早退，其余期权字段
    #    全是 None、唯独 total_oi 写了 0 ⇒ 这一行是当天该标的唯一的期权「观测」。09-24 / 09-25 30/30、10-14 BILI。
    {
        "date": "2026-09-24",
        "tickers": ('ABBV', 'AMC', 'AMZN', 'BILI', 'BRK-B', 'COST', 'CRCL', 'CRM', 'CVX', 'DE', 'DELL', 'ENPH', 'JNJ', 'META', 'MSFT',
                    'MU', 'NEE', 'NFLX', 'NVDA', 'QCOM', 'RKLB', 'SNOW', 'T', 'TMO', 'TMUS', 'TSLA', 'VKTX', 'VZ', 'WMT', 'XOM'),
        "signals": ("options.total_oi",),
        "reason": "v0.45.522（用户 2026-10-16 批准）：yfinance 全挡日，期权链全部退到样本 ⇒ analyze 中性早退写 total_oi = 0（其余期权字段 None、当天这 30 只没有任何别的 options.* 行）。0 不是观测。",
        "evidence": ("~/.claude/logs/orchestrator-2026-09-24.log（降级为样本数据 / 中性早退 42 次）", "CHANGELOG.md#v0.45.522"),
    },
    {
        "date": "2026-09-25",
        "tickers": ('ABBV', 'AMC', 'AMZN', 'BILI', 'BRK-B', 'COST', 'CRCL', 'CRM', 'CVX', 'DE', 'DELL', 'ENPH', 'JNJ', 'META', 'MSFT',
                    'MU', 'NEE', 'NFLX', 'NVDA', 'QCOM', 'RKLB', 'SNOW', 'T', 'TMO', 'TMUS', 'TSLA', 'VKTX', 'VZ', 'WMT', 'XOM'),
        "signals": ("options.total_oi",),
        "reason": "v0.45.522（用户 2026-10-16 批准）：同 09-24——期权链全部退到样本 ⇒ 中性早退写 total_oi = 0，不是观测。",
        "evidence": ("~/.claude/logs/orchestrator-2026-09-25.log（降级为样本数据 / 中性早退 42 次）", "CHANGELOG.md#v0.45.522"),
    },
    {
        "date": "2026-10-14",
        "tickers": ('BILI',),
        "signals": ("options.total_oi",),
        "reason": "v0.45.522（用户 2026-10-16 批准）：yfinance 限流冷却中 BILI 期权链退到样本（14:15:51「降级为样本数据」「期权链为样本数据，跳过全部指标计算（中性早退）」）⇒ total_oi = 0 是早退分支的默认值，不是观测；当天 BILI 没有别的 options.* 行。",
        "evidence": ("~/.claude/logs/orchestrator-2026-10-14.log:377-378", "CHANGELOG.md#v0.45.522"),
    },
    # v0.45.525（用户 2026-10-16 批准「逐行核对那 52 行 GEX，假的也删掉」）：gamma_exposure = 0.0 全库 50 行（另 2 行 06-17 已由 522 清掉）
    # 逐行核对后**全部**是「算不出」分支写的 0，不是观测——三条路径，各自有当时代码与当天产物为证：
    # ① 03-10 ~ 03-13（10/10 每天）：yfinance 链不带 Greeks，BS gamma 注入 2026-03-16（d37cda98）才加 ⇒ 每个合约 gamma 0 ⇒ 0.0。
    {
        "date": "2026-03-10",
        "tickers": ('AMZN', 'BILI', 'CRCL', 'META', 'MSFT', 'NVDA', 'QCOM', 'RKLB', 'TSLA', 'VKTX'),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.525（用户 2026-10-16 批准）：BS gamma 注入 2026-03-16（d37cda98）才加，此前 yfinance 链不带 gamma ⇒ `c.get(\"gamma\", 0)` 全为 0 ⇒ GEX 恒 0.0（当天 10/10 只皆 0；注入当天 03-16 起 10/10 皆非 0）。不是观测。",
        "evidence": ("git show d37cda98~1:options_analyzer.py（calculate_gamma_exposure 读 gamma，fetch 不注入）", "CHANGELOG.md#v0.45.525"),
    },
    {
        "date": "2026-03-11",
        "tickers": ('AMZN', 'BILI', 'CRCL', 'META', 'MSFT', 'NVDA', 'QCOM', 'RKLB', 'TSLA', 'VKTX'),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.525（用户 2026-10-16 批准）：BS gamma 注入 2026-03-16（d37cda98）才加，此前 yfinance 链不带 gamma ⇒ `c.get(\"gamma\", 0)` 全为 0 ⇒ GEX 恒 0.0（当天 10/10 只皆 0；注入当天 03-16 起 10/10 皆非 0）。不是观测。",
        "evidence": ("git show d37cda98~1:options_analyzer.py（calculate_gamma_exposure 读 gamma，fetch 不注入）", "CHANGELOG.md#v0.45.525"),
    },
    {
        "date": "2026-03-12",
        "tickers": ('AMZN', 'BILI', 'CRCL', 'META', 'MSFT', 'NVDA', 'QCOM', 'RKLB', 'TSLA', 'VKTX'),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.525（用户 2026-10-16 批准）：BS gamma 注入 2026-03-16（d37cda98）才加，此前 yfinance 链不带 gamma ⇒ `c.get(\"gamma\", 0)` 全为 0 ⇒ GEX 恒 0.0（当天 10/10 只皆 0；注入当天 03-16 起 10/10 皆非 0）。不是观测。",
        "evidence": ("git show d37cda98~1:options_analyzer.py（calculate_gamma_exposure 读 gamma，fetch 不注入）", "CHANGELOG.md#v0.45.525"),
    },
    {
        "date": "2026-03-13",
        "tickers": ('AMZN', 'BILI', 'CRCL', 'META', 'MSFT', 'NVDA', 'QCOM', 'RKLB', 'TSLA', 'VKTX'),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.525（用户 2026-10-16 批准）：BS gamma 注入 2026-03-16（d37cda98）才加，此前 yfinance 链不带 gamma ⇒ `c.get(\"gamma\", 0)` 全为 0 ⇒ GEX 恒 0.0（当天 10/10 只皆 0；注入当天 03-16 起 10/10 皆非 0）。不是观测。",
        "evidence": ("git show d37cda98~1:options_analyzer.py（calculate_gamma_exposure 读 gamma，fetch 不注入）", "CHANGELOG.md#v0.45.525"),
    },
    # ② AMC 08-04 ~ 08-27：`calculate_gamma_exposure` 的 `if stock_price < 5: return 0.0`（v0.45.63 于 08-29 删除）；AMC 股价约 2.6。
    {
        "date": "2026-08-04",
        "tickers": ('AMC',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.525（用户 2026-10-16 批准）：当时 calculate_gamma_exposure 有 `if stock_price < 5: return 0.0`（v0.45.63 于 08-29 删除），AMC 股价约 $2.6 ⇒ 恒 0.0。同日快照的 gamma 日历 net_gamma 数十万到上千万（链上有 gamma），删守卫后 09-01 起 GEX ≈ 6–17。不是观测。",
        "evidence": ("cache/options_snapshot_AMC_2026-08-04.json（gamma_calendar.net_gamma 非零、gamma_exposure 0.0）", "CHANGELOG.md#v0.45.63", "CHANGELOG.md#v0.45.525"),
    },
    {
        "date": "2026-08-10",
        "tickers": ('AMC',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.525（用户 2026-10-16 批准）：当时 calculate_gamma_exposure 有 `if stock_price < 5: return 0.0`（v0.45.63 于 08-29 删除），AMC 股价约 $2.6 ⇒ 恒 0.0。同日快照的 gamma 日历 net_gamma 数十万到上千万（链上有 gamma），删守卫后 09-01 起 GEX ≈ 6–17。不是观测。",
        "evidence": ("08-10 无期权快照：同一守卫（v0.45.63 前 `stock_price < 5 ⇒ 0.0`）、同一股价区间（$2.5–2.7），前后 08-04 / 08-12 快照为证", "CHANGELOG.md#v0.45.63", "CHANGELOG.md#v0.45.525", "CHANGELOG.md#v0.45.544"),
    },
    {
        "date": "2026-08-12",
        "tickers": ('AMC',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.525（用户 2026-10-16 批准）：当时 calculate_gamma_exposure 有 `if stock_price < 5: return 0.0`（v0.45.63 于 08-29 删除），AMC 股价约 $2.6 ⇒ 恒 0.0。同日快照的 gamma 日历 net_gamma 数十万到上千万（链上有 gamma），删守卫后 09-01 起 GEX ≈ 6–17。不是观测。",
        "evidence": ("cache/options_snapshot_AMC_2026-08-12.json（gamma_calendar.net_gamma 非零、gamma_exposure 0.0）", "CHANGELOG.md#v0.45.63", "CHANGELOG.md#v0.45.525"),
    },
    {
        "date": "2026-08-13",
        "tickers": ('AMC',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.525（用户 2026-10-16 批准）：当时 calculate_gamma_exposure 有 `if stock_price < 5: return 0.0`（v0.45.63 于 08-29 删除），AMC 股价约 $2.6 ⇒ 恒 0.0。同日快照的 gamma 日历 net_gamma 数十万到上千万（链上有 gamma），删守卫后 09-01 起 GEX ≈ 6–17。不是观测。",
        "evidence": ("cache/options_snapshot_AMC_2026-08-13.json（gamma_calendar.net_gamma 非零、gamma_exposure 0.0）", "CHANGELOG.md#v0.45.63", "CHANGELOG.md#v0.45.525"),
    },
    {
        "date": "2026-08-25",
        "tickers": ('AMC',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.525（用户 2026-10-16 批准）：当时 calculate_gamma_exposure 有 `if stock_price < 5: return 0.0`（v0.45.63 于 08-29 删除），AMC 股价约 $2.6 ⇒ 恒 0.0。同日快照的 gamma 日历 net_gamma 数十万到上千万（链上有 gamma），删守卫后 09-01 起 GEX ≈ 6–17。不是观测。",
        "evidence": ("cache/options_snapshot_AMC_2026-08-25.json（gamma_calendar.net_gamma 非零、gamma_exposure 0.0）", "CHANGELOG.md#v0.45.63", "CHANGELOG.md#v0.45.525"),
    },
    {
        "date": "2026-08-26",
        "tickers": ('AMC',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.525（用户 2026-10-16 批准）：当时 calculate_gamma_exposure 有 `if stock_price < 5: return 0.0`（v0.45.63 于 08-29 删除），AMC 股价约 $2.6 ⇒ 恒 0.0。同日快照的 gamma 日历 net_gamma 数十万到上千万（链上有 gamma），删守卫后 09-01 起 GEX ≈ 6–17。不是观测。",
        "evidence": ("cache/options_snapshot_AMC_2026-08-26.json（gamma_calendar.net_gamma 非零、gamma_exposure 0.0）", "CHANGELOG.md#v0.45.63", "CHANGELOG.md#v0.45.525"),
    },
    {
        "date": "2026-08-27",
        "tickers": ('AMC',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.525（用户 2026-10-16 批准）：当时 calculate_gamma_exposure 有 `if stock_price < 5: return 0.0`（v0.45.63 于 08-29 删除），AMC 股价约 $2.6 ⇒ 恒 0.0。同日快照的 gamma 日历 net_gamma 数十万到上千万（链上有 gamma），删守卫后 09-01 起 GEX ≈ 6–17。不是观测。",
        "evidence": ("cache/options_snapshot_AMC_2026-08-27.json（gamma_calendar.net_gamma 非零、gamma_exposure 0.0）", "CHANGELOG.md#v0.45.63", "CHANGELOG.md#v0.45.525"),
    },
    # ③ ICLN 04-26 / 05-10、BIIB 05-10：analyze 取链不传价，BS gamma 的 S 退到「OI > 50 的 call 行权价中位数」，
    #    当天一个这样的 call 都没有 ⇒ S = 0 ⇒ 不注入 gamma ⇒ 0.0（v0.45.462 删了这个兜底）。
    {
        "date": "2026-04-26",
        "tickers": ('ICLN',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.525（用户 2026-10-16 批准）：analyze 取链不传价，BS gamma 的 S 退到「OI > 50 的 call 行权价中位数」，当天没有这样的 call ⇒ S = 0、不注入 gamma ⇒ GEX 0.0（v0.45.462 删了这个兜底）。总 OI 119，快照前三档 call / put 合计 91、余 28 ⇒ 不可能有 call OI > 50；gamma 日历 net_gamma 全 0。不是观测。",
        "evidence": ("cache/options_snapshot_ICLN_2026-04-26.json", "git show 5fe6a3ea:options_analyzer.py（_atm_candidates = calls OI > 50）", "CHANGELOG.md#v0.45.525"),
    },
    {
        "date": "2026-05-10",
        "tickers": ('ICLN',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.525（用户 2026-10-16 批准）：analyze 取链不传价，BS gamma 的 S 退到「OI > 50 的 call 行权价中位数」，当天没有这样的 call ⇒ S = 0、不注入 gamma ⇒ GEX 0.0（v0.45.462 删了这个兜底）。总 OI 178，快照前三档合计 151、余 27 ⇒ 不可能有 call OI > 50；gamma 日历 net_gamma 全 0。不是观测。",
        "evidence": ("cache/options_snapshot_ICLN_2026-05-10.json", "git show 5fe6a3ea:options_analyzer.py（_atm_candidates = calls OI > 50）", "CHANGELOG.md#v0.45.525"),
    },
    {
        "date": "2026-05-10",
        "tickers": ('BIIB',),
        "signals": ("options.gamma_exposure",),
        "reason": "v0.45.525（用户 2026-10-16 批准）：analyze 取链不传价，BS gamma 的 S 退到「OI > 50 的 call 行权价中位数」，当天没有这样的 call ⇒ S = 0、不注入 gamma ⇒ GEX 0.0（v0.45.462 删了这个兜底）。总 OI 631；快照 gamma 日历 4 个到期日（OI 44–242）net_gamma 全为 0.0 ⇒ 整条链没注入 gamma；gamma 日历 net_gamma 全 0。不是观测。",
        "evidence": ("cache/options_snapshot_BIIB_2026-05-10.json", "git show 5fe6a3ea:options_analyzer.py（_atm_candidates = calls OI > 50）", "CHANGELOG.md#v0.45.525"),
    },
    # ────────────────────────────────────────────────────────────────────────
    # v0.45.508（用户 2026-10-16 批准）：10-14 生产扫描 yfinance 全挡（429）⇒ 历史字段全走出口回落，其中两类取到了
    # **前一交易日**的观测。只隔离观测层 `price.*`：`buzz.comp.*` 通道值是派生值（同 Agent 评分，不隔离），且维度 IC
    # 协议 §13.2 自证 ②「生产有分、重算却缺输入 = 0」会把隔离 / purge 掉的通道行算成自证失败。
    # 维度 IC H1 的信号表含输入层 `price.volume_ratio` ⇒ 这 29 行出局、当天只剩 JNJ（< 20）⇒ H1 当天无效；
    # H2 / 共振加成 / F&G 敞口门不读 `price.*`，不受影响。协议记录见 `experiments/dim_ic_preregistration.md` §13.8。
    {
        "date": "2026-10-14",
        "tickers": ("ABBV", "AMC", "AMZN", "BILI", "BRK-B", "COST", "CRCL", "CRM", "CVX", "DE",
                    "DELL", "ENPH", "META", "MSFT", "MU", "NEE", "NFLX", "NVDA", "QCOM", "RKLB",
                    "SNOW", "T", "TMO", "TMUS", "TSLA", "VKTX", "VZ", "WMT", "XOM"),
        "signals": ("price.volume_ratio",),
        "reason": "v0.45.508（用户 2026-10-16 批准）：yfinance 日线全挡，量比来自 Twelve Data 兜底；收盘后扫描里 TD 当日那根"
                  "被 `_drop_forming_bar` 丢掉 ⇒ 入档的是「最新一根 = 10-13、均量窗口 = 截至 10-13 的 20 根」的量比，不是 10-14 那场的"
                  "（v0.45.505 起这种值拒收）。复核（10-16）：Twelve Data 自己的日线 `fetch_volume_ratio(end_date=2026-10-13)` 重算，"
                  "27 只逐位相等、as_of 2026-10-13；RKLB / VKTX 只差在 10-13 那根成交量——其余 19 根取今天的值、10-13 换成 15,537,400 /"
                  " 5,707,600 股即逐位复现（Twelve Data 事后上调了那根，推断，原值已取不回）。与「截至 10-14」相去甚远（COST 0.62 vs"
                  " 1.31、CRCL 0.48 vs 1.03）。JNJ 当天 TD 限流超时、未入档，不在名单里。",
        "evidence": ("~/.claude/logs/orchestrator-2026-10-14.log（29 行「volume_ratio 由 Twelve Data 补上」，各配「丢弃盘中未完成的末根日线（2026-10-14）」）",
                     "CHANGELOG.md#v0.45.505", "CHANGELOG.md#v0.45.508"),
    },
    {
        "date": "2026-10-14",
        "tickers": ("BILI",),
        "signals": ("price.momentum_5d",),
        "reason": "v0.45.508（用户 2026-10-16 批准）：yfinance 全挡 ⇒ 自攒索引回落；BILI 的 CBOE 数据陈旧（last_trade=10-13）被弃用 ⇒ "
                  "没有已核实的 10-14 收盘 ⇒ 窗口末端 10-13，"
                  "动量 +0.40%（= 10-13 归档值）；yfinance「截至 10-14」为 −3.91%。其余 29 只的回落末端是 10-14、与 yfinance 一致，不隔离。",
        "evidence": ("~/.claude/logs/orchestrator-2026-10-14.log（「[BILI] momentum_5d 由自攒索引补上: +0.40%（截至 2026-10-13」）",
                     "CHANGELOG.md#v0.45.508"),
        # v0.45.530（用户 2026-10-17 批准「清 92 行、留这 1 行」）：照常隔离，但库里这一行**不删**
        "keep_rows": ("维度 IC 冻结评分器（buzz_v1 阶段 2）要用归档的原始动量重算背离调整，自证 ② 要求「生产有分、重算缺输入」= 0、"
                      "不许放宽；10-14 在自证窗口（09-28 起）内——删掉这一行，将来自证就多一条缺输入、判不过。"
                      "生产当天的分正是用这个（取错窗口的）值算的，重算也该用它才能逐位复现。H1 那天本来就因量比隔离而无效。"),
    },
    # ────────────────────────────────────────────────────────────────────────
    # v0.45.517（用户 2026-10-16 批准「全部已证实的」）：508 复核时顺带查出的、H1 窗口（10-12 起）之前的同类观测。
    # 判据同 508：只登「逐位 / 万分之一内复现出错误窗口」的；复现不出的（09-24 T）不登。只隔离观测层 `price.*`。
    {
        "date": "2026-09-15",
        "tickers": ("NVDA", "QCOM"),
        "signals": ("price.volume_ratio",),
        "reason": "v0.45.517（用户 2026-10-16 批准）：yfinance 失败 ⇒ Twelve Data 兜底，09-15 那根被 `_drop_forming_bar` 丢掉 ⇒ "
                  "入档的是「最新一根 = 09-14（上一场）、窗口 = 截至 09-14 的 20 根」的量比。复核：Twelve Data 截至 09-14 的日线、"
                  "只把 09-14 那根成交量换成 131,942,300 / 13,755,800 股（TD 事后上调了那根）即逐位复现 1.01080 / 1.21243；"
                  "截至 09-15 应为 NVDA 0.676 / QCOM 1.195。",
        "evidence": ("logs/alpha_hive.log.2（开发检出，09-15 段：14:14:20 NVDA 1.01x、14:15:31 QCOM 1.21x，各配「丢弃 2026-09-15 那根」）",
                     "CHANGELOG.md#v0.45.505", "CHANGELOG.md#v0.45.508", "CHANGELOG.md#v0.45.517"),
    },
    {
        "date": "2026-09-15",
        "tickers": ("NVDA", "QCOM"),
        "signals": ("price.momentum_5d",),
        "reason": "v0.45.517（用户 2026-10-16 批准）：v0.45.465 之前的自攒索引回落，窗口末端是上一场 09-14、锚点取 ≥5 场前"
                  "（465 已记录）⇒ 实际窗口 09-04 → 09-14。用 Twelve Data 收盘与扫描价两种序列穷举，唯一同时复现两只（误差 < 1e-4）"
                  "的就是它。截至 09-15（4 场）应为 NVDA −5.14 / QCOM +6.46（归档 −8.42 / +6.76）。",
        "evidence": ("logs/alpha_hive.log.2（开发检出，09-15 段：「[NVDA] momentum_5d 由自攒索引补上: -8.42%」「[QCOM] … +6.76%」）",
                     "CHANGELOG.md#v0.45.465", "CHANGELOG.md#v0.45.508", "CHANGELOG.md#v0.45.517"),
    },
    {
        "date": "2026-09-24",
        "tickers": ("ABBV", "AMC", "AMZN", "BILI", "BRK-B", "COST", "CRCL", "CRM", "CVX", "DE",
                    "DELL", "ENPH", "JNJ", "META", "MSFT", "MU", "NEE", "NFLX", "NVDA", "QCOM",
                    "RKLB", "SNOW", "TMO", "TMUS", "TSLA", "VKTX", "VZ", "WMT", "XOM"),
        "signals": ("price.momentum_5d",),
        "reason": "v0.45.517（用户 2026-10-16 批准）：全源故障日（30 行 price_at_predict 全为 0），30/30 动量来自 465 之前的自攒索引回落；"
                  "上一个扫描日是 09-22（09-23 无扫描）⇒ 窗口末端滞后两场。这 29 只与 Twelve Data「末端 09-22、往前 5 场」逐一相符"
                  "（误差 < 0.02）。T 差 0.035、复现不出，**不登**（归档 −6.098，截至 09-24 的 4 场应为 +0.197，待验证）。",
        "evidence": ("logs/alpha_hive.log（开发检出，09-24 段：30 行「momentum_5d 由自攒索引补上」）",
                     "CHANGELOG.md#v0.45.465", "CHANGELOG.md#v0.45.508", "CHANGELOG.md#v0.45.517"),
    },
    {
        "date": "2026-09-25",
        "tickers": ("ABBV", "AMC", "AMZN", "BILI", "BRK-B", "COST", "CRCL", "CRM", "CVX", "DE",
                    "DELL", "ENPH", "JNJ", "META", "MSFT", "MU", "NEE", "NFLX", "NVDA", "QCOM",
                    "RKLB", "SNOW", "T", "TMO", "TMUS", "TSLA", "VKTX", "VZ", "WMT", "XOM"),
        "signals": ("price.momentum_5d",),
        "reason": "v0.45.517（用户 2026-10-16 批准）：又一个全源故障日（price_at_predict 全为 0）；30/30 动量与 09-24 归档**逐位相同**——"
                  "09-24 没有可用价，索引没有新点，窗口仍止于 09-22（滞后三场）。T 也在内：无论它出自哪个窗口，都不是 09-25 的"
                  "（归档 −6.098，截至 09-25 的 4 场应为 −0.275）。",
        "evidence": ("pheromone.db signal_archive：09-25 与 09-24 的 price.momentum_5d 30/30 相等",
                     "logs/alpha_hive.log（开发检出，09-25 段：30 行「momentum_5d 由自攒索引补上」）",
                     "CHANGELOG.md#v0.45.517"),
    },
]



# ────────────────────────────────────────────────────────────────────────────
# v0.45.494（用户 2026-10-13 批准）：08-14 前 iv_rank / iv_percentile 跟着 iv_current 一起隔离。
#
# 08-14（`f25e1fa3`，v0.43.19）之前 OptionsAgent 的 IV Rank / IV Percentile 是拿**当日 current_iv** 对历史分布排名
# （`calculate_iv_rank(current_iv, hist_iv)` / `calculate_iv_percentile(current_iv, hist_iv)`；03-19 ~ 08-14 在用的
# 14 个版本逐一核过）。所以凡是 current_iv 已被判为不可信、隔离了 `options.iv_current` 的 (日期, 标的)，同一份
# rank / pct 也是那个不可信值的函数。既有条目（v0.45.238 会话错位、472 样本链 / 假现价、479 重判…）按「五类市场观测」
# 写，不含这两类——本规则把它们补齐，而不是逐条复制 ~50 条字面量（那是同一个事实的 50 份副本，且新加的 08-14 前
# iv_current 条目会再漏一次）。
# 只补「源条目挡了 iv_current、没挡 iv_rank」且该 (日期, 标的) 尚未被别的条目挡 rank 的部分（478 那两条已由 481 覆盖）。
# 08-14 起 rank 走 hv_proxy / 真实 IV 历史，不读 current_iv（VKTX 09-15 ~ 17 即此例），不补。
_RANK_FROM_CURRENT_IV_UNTIL = "2026-08-14"
_RANK_MIRROR_TAG = "v0.45.494"


def _rank_mirror_entries(entries: List[Dict]) -> List[Dict]:
    def _rank_blocked(d, t):
        return any(q["date"] == d and "options.iv_rank" in q["signals"]
                   and (q.get("tickers") is None or t in q["tickers"]) for q in entries)
    out = []
    for q in entries:
        if (q["date"] >= _RANK_FROM_CURRENT_IV_UNTIL or "options.iv_current" not in q["signals"]
                or "options.iv_rank" in q["signals"]):
            continue
        tks = q.get("tickers")
        if tks is not None:
            tks = tuple(t for t in tks if not _rank_blocked(q["date"], t))
            if not tks:
                continue
        src = re.search(r"v\d+\.\d+\.\d+", q.get("reason", ""))
        out.append({
            "date": q["date"],
            **({"tickers": tks} if tks is not None else {}),
            "signals": ("options.iv_rank", "options.iv_percentile"),
            "reason": (f"{_RANK_MIRROR_TAG}（用户 2026-10-13 批准）：镜像 {src.group(0) if src else '既有条目'} 对 iv_current 的隔离——"
                       f"08-14 前 iv_rank / iv_percentile 由同一个 current_iv 算出。源条目理由：{q.get('reason', '')[:120]}"),
            "evidence": tuple(q.get("evidence") or ()) + ("CHANGELOG.md#v0.45.494",),
            "_mirror_of": q["date"],
        })
    return out


QUARANTINE.extend(_rank_mirror_entries(QUARANTINE))


#: 前瞻检验排除日（v0.45.484）—— 与 `QUARANTINE` **不是一回事**，别合并：
#:
#: `QUARANTINE` 管「取错日子的观测」，在**入库口**拦（archive / backfill 不写、purge 工具会删——带 `keep_rows` 的条目除外，见 `purge_due`），且划界原则是
#: 不隔离 Agent 评分。这里管的是「整天由事后重放补算、不再是生产当时实际产出」的日子：库里存的是**对的**
#: 重放值（要留着、要能被别的读者用），只是不进预注册前瞻检验——那两份协议的意向性分析口径是「系统当天
#: 实际产出什么就算什么」，事后重放的整天不属于这个口径。
#:
#: 只由读者问 `forward_test_excluded(date)`：`experiments/dim_ic_forward_test.load_rows`、
#: `experiments/resonance_boost_forward_test.load_reports`。登记须在该日任何结果（T+7）存在之前，
#: 并在对应预注册里写「修订（事后）」。守卫 `tests/test_forward_test_excluded_days.py`。
FORWARD_TEST_EXCLUDED_DAYS: Dict[str, Dict] = {
    "2026-10-12": {
        "version": "v0.45.484",
        "registered": "2026-10-13",
        "reason": (
            "生产扫描遇本机出站故障（编排器 14:00:08 代理探活失败 ⇒ 整轮直连 ⇒ 全部 TLS 握手 SSLEOFError）："
            "30/30 只 MultiSourceFetcher 全源 FALLBACK、现价 0、期权链 0 条、新闻/SEC/yfinance 全断。"
            "当日数据于 2026-10-13 用当天代码（0993efde）+ 云端同场链 + 真实收盘整日重放补算并写回"
            "（control 29/30 逐位复现生产存档）。用户决定：写回，但该日不进预注册前瞻检验。"
            "登记时该日 T+7 尚未到期（≥ 2026-10-21），不可能受结果影响。"
        ),
        "evidence": ("~/.claude/logs/orchestrator-2026-10-12.log", "~/.claude/logs/scan_coverage-2026-10-12.json",
                     "_manual_backups/2026-10-13_1012_replay_v0.45.484/"),
    },
}


def forward_test_excluded(date: str) -> bool:
    """该业务日是否排除在预注册前瞻检验之外（见 `FORWARD_TEST_EXCLUDED_DAYS`）。"""
    return str(date)[:10] in FORWARD_TEST_EXCLUDED_DAYS


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


def purge_due(date: str, ticker: str, signal: str, only_tag: Optional[str] = None) -> bool:
    """库里若有这一行，清库时该不该删（v0.45.530）。清库工具与整库恢复的报告共用这一处判据。

    先要 `is_quarantined` 为真；再要求命中某条**不带 `keep_rows`** 的条目（给了 `only_tag` 还要求该条目
    reason 含它）。只被 `keep_rows` 条目命中的行照常隔离（入库口拦、分析排除），但库里已有的不删——
    现有唯一一条是 10-14 BILI `price.momentum_5d`，理由见该条目。同一格还被别的条目命中 ⇒ 照删。
    """
    if not is_quarantined(date, ticker, signal):
        return False
    return any(q["date"] == date and signal in q["signals"]
               and (q.get("tickers") is None or ticker in q["tickers"])
               and not q.get("keep_rows")
               and (only_tag is None or only_tag in q.get("reason", ""))
               for q in QUARANTINE)


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
    # v0.45.568：BuzzBee 的原始观测溯源与 buzz.comp.* **同一事务**写进 observation_links——同日重扫时两边同样「后者覆盖
    # 前者」，链接永远指向产出库里那组通道值的那一轮观测（阶段 2 的冻结通道层按它取 payload 重算）。定向回填（only）不碰链接
    import observation_archive
    links = observation_archive.link_rows(swarm_results, date) if only is None else []
    if not rows and not links:
        return 0
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO {TABLE} (date,ticker,signal,value) "
            f"VALUES (?,?,?,?)", rows)
        observation_archive.write_links(conn, links)
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
        import yf_daily
        # v0.45.658：经唯一入口（前瞻波动率按日期用：帧与 actions=False 时逐位相同）
        px = yf_daily.download(yf, list(tickers), source="forward_vol", start=start, end=end,
                               progress=False, auto_adjust=True)[0]["Close"]
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
#     每条边带生效区间（v0.45.561，`Edge`）：闭包按边界**当天**生效的边走，改表不再追溯改写历史世代。
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

class Edge(NamedTuple):
    """一条依赖边：下游读 `up`（fnmatch 模式）这个系统输出。

    `via` —— 这条边**为什么**存在，也决定谁来核它：
      · `"peer"`      读同伴条目（`BeeAgent._read_peer`）的某个字段；
      · `"census"`    读板普查（`get_live_signals` / `detect_resonance` / 拥挤度的看多同伴数）；
      · `"self"`      同一只蜂内部的合成（子分 → 总分、分量 → 拥挤度、预期收益 → 方向）；
      · `"formula"`   自算一份同公式的量，以入档的那份代表「公式换代」（Rival / Guard 的拥挤度）；
      · `"aggregate"` 聚合层（`swarm_agreement` 遍历全部方向）。
      `peer` / `census` 两类由 `tests/test_signal_archive_generations.py::TestPeerReadEdgesMatchCode`
      按 AST 与代码**双向**对账（读了没登记、登记了没读都红）；其余三类是人工维护的结构事实。

    `since` / `until` —— 生效区间 `[since, until)`，扫描日期（ISO）；`None` = 不设限。
      边界日期 D 落在区间内，这条边才把 D 的换代传下去（`_scope_closure(..., on=D)`）。
      **为什么要有时间维度**（v0.45.561）：此前闭包拿**当前**的表去推**全部历史**边界 ——
      删一条已不存在的边会让早先边界对旧数据的闭包悄悄缩小（v0.45.441 因此不敢删
      consensus_strength 那条），留着它又会让此后无关的边界白切下游（v0.45.445 改 Buzz
      新闻源 ⇒ Buzz 方向 ⇒ consensus_strength ⇒ … ⇒ `ml.expected_*`，而 `ml.expected_*`
      根本不读这些）。有了区间，两头都对。
      ⚠️ 区间端点就是下游自己换了定义（多读 / 少读了一个量）⇒ 那一天必须有一条边界的闭包盖住下游
      （`TestEdgeWindows::test_window_endpoints_are_boundaries` 钉着）。拿不准的端点写 `None`
      （＝保守地当它一直在），**不要猜日期**。
    """
    up: str
    via: str
    since: Optional[str] = None
    until: Optional[str] = None


EDGE_KINDS = frozenset({"peer", "census", "self", "formula", "aggregate"})


def _edges(ups: Iterable[str], via: str, since: Optional[str] = None,
           until: Optional[str] = None) -> Tuple[Edge, ...]:
    return tuple(Edge(u, via, since, until) for u in ups)


#: 依赖边区间端点（扫描日期）。每个都对应 `_COHORT_HISTORY` 里一条点名了下游的边界。
#: v0.44.1~0.44.3 首个扫描日：Rival 移到 Phase-1.5，催化剂档改读 ChronosBee 分（此前写死 "B+"），
#: 拥挤度改传真实值（此前写死 50.0）；`expected_returns` 去掉拥挤度项（v0.44.2 否决 v0.44.1 的倾斜项）。
_EDGE_V0_44 = "2026-08-17"
#: v0.45.441：Scout 拥挤度不再数看多同伴（`peer_census=False`）⇒ consensus_strength 恒 None。
_EDGE_V0_45_441 = "2026-10-09"
#: v0.45.538：Rival 退役合成样本模型 ⇒ 分数不再含概率，Bear 删 `ml_probability < 0.45` 分支。
_EDGE_V0_45_538 = "2026-10-19"

#: 已退役的 `ml.probability`（v0.45.538 起不写新行、抽取器已摘除 ⇒ 不在信号宇宙里）读过的系统输出。
#: 它不能当节点（历史行会从「不认识 ⇒ 受全部边界约束」变成按依赖切），所以读它的下游直接挂到它的上游，
#: 区间 = 它活着的那段：[v0.44.2/0.44.3 接上真实特征, v0.45.538 退役)。
#:   · 拥挤度：Rival 自算一份（同一个 CrowdingDetector ⇒ `crowding.score` 代表公式），其看多同伴数读板
#:     普查 —— v0.45.279 之前是排行榜、不按身份过滤，Scout 也会被数进去 ⇒ 保守取全部 Phase-1 方向；
#:   · 催化剂档：读 ChronosBee 的 self_score。
#:   · OracleBee 的 iv_rank / pc_ratio 是原始观测（同 Bear 读的内幕金额 / P/C），不是边。
_ML_PROBABILITY_READS = (
    (Edge("crowding.score", "formula", _EDGE_V0_44, _EDGE_V0_45_538),)
    + _edges(_PHASE1_DIRS, "census", _EDGE_V0_44, _EDGE_V0_45_538)
    + (Edge("agent.ChronosBeeHorizon.score", "peer", _EDGE_V0_44, _EDGE_V0_45_538),)
)

#: 下游信号（fnmatch 模式）→ 它读的**系统输出**（`Edge`）。只登记「读系统输出」的边；
#: 读原始市场数据（期权链 / SEC / 行情）不是边 —— 那些量不随系统逻辑换代。
#: v0.45.561：逐信号登记（此前一个 `ml.*` 通配把已删的 `ml.probability` 与 `ml.expected_*`
#: 捆在一起，后者因此被它根本不读的拥挤度 / Phase-1 方向反复白切）+ 每条边带生效区间。
SIGNAL_UPSTREAM: Dict[str, Tuple[Edge, ...]] = {
    # Scout 拥挤度的 consensus_strength = 读板那一刻已发布同伴的看多数
    # （`real_data_sources.get_bullish_agents_count`）。Phase-1 并行 ⇒ 读到谁取决于竞态，
    # 但读到的是同伴**方向**：生产库 03~09 月取值 0~5 只看多，不是常数。
    # v0.45.441（10-09）起 Scout 不再数（恒 None）⇒ 边止于 10-09；此前的边界照旧经它传递。
    "crowding.comp.consensus_strength": _edges(
        (d for d in _PHASE1_DIRS if "ScoutBeeNova" not in d), "census", until=_EDGE_V0_45_441),
    "crowding.score": (Edge("crowding.comp.*", "self"),),
    "crowding.signal": (Edge("crowding.score", "self"),),
    "crowding.adj_factor": (Edge("crowding.score", "self"),),
    "agent.ScoutBeeNova.*": _edges(("crowding.score", "crowding.adj_factor"), "self"),  # 方向按拥挤度阈值定
    # Rival 的预期收益 = 催化剂档幅度 × 5 日动量（`ml_predictor.expected_returns`；动量是原始行情、不是边）。
    # 08-17 之前：Rival 在 Phase-1、催化剂档写死 "B+"、拥挤度写死 50.0 ⇒ 闭式 8.0 + 0.8×动量，不读任何系统输出。
    # 08-17 起：催化剂档 = `catalyst_quality_from_score(ChronosBee.self_score)`。拥挤度 / Phase-1 方向只进过
    # 概率（`_ML_PROBABILITY_READS`），从来没进过预期收益（v0.44.2 否决拥挤度倾斜项，同一条边界内）。
    # 行为差分守卫：`TestRivalExpectedReturnsIgnorePeers`（只动同伴方向 / 拥挤度，expected_* 逐位不变）。
    "ml.expected_7d": (Edge("agent.ChronosBeeHorizon.score", "peer", since=_EDGE_V0_44),),
    "ml.expected_30d": (Edge("agent.ChronosBeeHorizon.score", "peer", since=_EDGE_V0_44),),
    # Rival 方向 = sign(expected_7d + expected_30d)（EPS 修正只把 neutral 翻开、预期收益不可用时退技术指标，
    # 两者读的都是原始行情）—— 概率从来不进方向（v0.45.538 CHANGELOG：方向逐位不变）
    "agent.RivalBeeVanguard.direction": _edges(("ml.expected_7d", "ml.expected_30d"), "self"),
    # Rival 分 = 概率×10（+技术指标微调）—— v0.45.538 起概率退役，分数 = 5.0 + 0.5×技术指标（原始行情）
    "agent.RivalBeeVanguard.score": _ML_PROBABILITY_READS,
    # Guard：avg_score / consistency 读 Phase-1 + Rival 的分与方向；自算一份拥挤度（看多数取自同一份普查）
    "guard.consistency_census": _edges(_PHASE1_DIRS + ("agent.RivalBeeVanguard.direction",), "census"),
    "guard.adj_factor": ((Edge("crowding.score", "formula"),)
                         + _edges(_PHASE1_DIRS + ("agent.RivalBeeVanguard.direction",), "census")),
    "agent.GuardBeeSentinel.*": (
        _edges(tuple(f"agent.{a}.*" for a in _PHASE1) + ("agent.RivalBeeVanguard.*",), "census")
        # v0.45.561 补登：`score = clamp(score + macro_adj)`（guard_bee.py）。凡点名 macro_adj 的边界都同时点名了
        # Guard（v0.43.24 除外，其后 Guard 另有更晚的边界）⇒ 补边不挪任何起点
        + _edges(("guard.consistency_census", "guard.adj_factor", "guard.macro_adj"), "self")),
    # v0.45.349：Bear 的两个子分读同伴的**方向**（系统输出），此前误登在 SIGNAL_LEAVES「不读任何系统输出」。
    # `_assess_insider_selling`：`scout_entry.direction == "bearish"` ⇒ insider_bear 下限 6.0；
    # `_assess_options_puts`：`oracle_entry.direction == "bearish"` ⇒ options_bear 下限 5.5（bear_bee.py）。
    # 同处读的内幕金额 / P/C / IV Rank / skew / gex 是原始观测，不是边。
    # 补边不挪任何已算出的世代起点（两者当前起点都由 v0.45.288 直接点名决定，其后再无边界触及
    # Scout/Oracle 方向）——`TestPeerReadEdgesMatchCode` 按 AST 双向核这两条边
    "bear.insider_bear": (Edge("agent.ScoutBeeNova.direction", "peer"),),
    "bear.options_bear": (Edge("agent.OracleBeeEcho.direction", "peer"),),
    # Bear 总分：四个入档子分 + 不入档的 news / catalyst / ml / guard 子分（读同伴 details）
    # + LLM 模式下普查看多同伴（`_generate_llm_bear_thesis`；生产恒 --no-llm，按代码出边保守列）。
    # v0.45.561 补登三条此前漏掉的 peer 边（双向对账时 AST 找出来的）：Buzz 方向（看空 ⇒ news_bear 下限 5.5）、
    # `sentiment.pct`（Buzz 的 sentiment_score 即同一个 bullish_pct）、`catalyst.nearest_days`（≤7 天 ⇒ 5.0）。
    # bear.score 的起点由表尾 v0.45.538 决定，补边不挪它。
    "bear.score": (
        _edges(("bear.insider_bear", "bear.overval_bear", "bear.options_bear", "bear.short_int_bear"), "self")
        + _edges(("ml.expected_7d", "guard.consistency_census", "agent.GuardBeeSentinel.*",
                  "agent.BuzzBeeWhisper.direction", "sentiment.pct", "catalyst.nearest_days",
                  # 只进理由文字的三个 self_score（LLM 模式下文字进提示词 ⇒ 按代码出边保守列）
                  "agent.ScoutBeeNova.score", "agent.OracleBeeEcho.score", "agent.BuzzBeeWhisper.score"), "peer")
        + _edges(tuple(f"agent.{a}.*" for a in _PHASE1)
                 + ("agent.RivalBeeVanguard.*", "agent.GuardBeeSentinel.*"), "census")
        # v0.45.538 之前还读 Rival 的 `ml_probability`（< 0.45 ⇒ ml_bear 5.5）
        + _ML_PROBABILITY_READS),
    "agent.BearBeeContrarian.*": (Edge("bear.score", "self"),),
    # 多数派占比：遍历 agent_details 里**全部**蜂的方向（含 CodeExecutor / Guard / Bear）
    "composite.swarm_agreement": (Edge("agent.*.direction", "aggregate"),),
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
    # v0.45.362：CBOE 链 + Scout 价（同 price.* 按原始数据算）；只有本版上线后的记录才有，早于它的边界无从作用
    "gex.available", "gex.total_gex", "gex.negative",
    # ⚠️ 不是真叶子：= int(Buzz 合成值)，合成值是 buzz.comp.* 七个通道的加权和 ⇒ 通道换代它跟着换。
    # 留在这里是**刻意池化**（v0.45.565，用户 2026-10-20 定）：理由与复检条件见 `DELIBERATELY_POOLED`。
    # 复检决定切 ⇒ 移出本集合，在 SIGNAL_UPSTREAM 登 `Edge("buzz.comp.*", "self")`（结构修法，草稿见分支
    # claude/gifted-hawking-5866b0 的 72137325，未合入）。
    "sentiment.pct",
    # v0.45.349：bear.insider_bear / bear.options_bear 移出（读 Scout / Oracle 方向，见 SIGNAL_UPSTREAM）
    "bear.overval_bear", "bear.short_int_bear",
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
    # v0.45.472（补登）06-30：期权链主源 yfinance → CBOE，主链 GEX 的 gamma 来源与 S 都换了（见 _COHORT_HISTORY）。
    "v0.34.0": ("options.gamma_exposure",),
    # v0.45.275（P1 补登，两条真实早于表的第一条 08-17，按实际部署日期插入）：
    # 08-15 Step2：GuardBee VIX 改走 CBOE，宏观整体降级但 CBOE 仍供得上真实 VIX 的
    # 日子里，VIX 从「跟着一起丢弃」变成「参与 regime_votes」⇒ macro_adj 改口径。
    "v0.43.24": ("guard.macro_adj",),
    # 08-15：ScoutBee momentum_5d 缺失哨兵 0.0（伪造持平）→ None（诚实缺失）。
    "v0.43.25": ("price.momentum_5d",),
    # 08-17：expected_returns 去偏 + probability 居中 + RivalBee 三特征接真实数据
    # v0.45.561：拆 `ml.*` 后显式点名 Rival —— 此前 Rival 分 / 方向经 `ml.*`（含 ml.probability）带出；
    # 本版同时修了 Rival 的 EPS 单向棘轮（方向）、概率居中（分）
    "v0.44.1~0.44.3": ("ml.*", "agent.RivalBeeVanguard.*"),
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
    # v0.45.561：② 与 Rival 自算拥挤度的 None 处理只进**概率**（训练集 / 特征），`expected_returns` 不读
    # 训练集也不读拥挤度 ⇒ `ml.*` 收窄为它真正指的 Rival 分（`ml.probability` 已不在信号宇宙里）。
    # ③ 0DTE `or 30` 在 `OptionsDataFetcher` 的 BS gamma 回填里，而主链 `_select_expiries`
    # 只取 DTE≥7 ⇒ `options.gamma_exposure` 走不到，不列
    "v0.45.50": ("crowding.score", "guard.adj_factor", "agent.RivalBeeVanguard.score",
                 "agent.GuardBeeSentinel.*"),
    # 09-05：① Oracle 期限结构 / 25Δ skew 的 IV 换 CBOE ⇒ options_score（`options.iv_current`
    # 本就取自 OptionsAgent，不变）；② Bear 估值项 P/E 复活
    "v0.45.128": ("agent.OracleBeeEcho.*", "bear.overval_bear"),
    # 09-07：Rival 读 Chronos 的 catalyst_quality 修复。probability 在当前 HGB 上 0/173 变，
    # 但那是模型性质不是定义性质（该条原文「定义变了就登记」）⇒ ml.* 整体列
    # v0.45.561：拆 `ml.*` 后显式点名 Rival 分（概率的催化剂特征同源）；方向经 ml.expected_* 带出
    "v0.45.151": ("ml.*", "agent.RivalBeeVanguard.score"),
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
    # 09-18（v0.45.314 补登）：Queen 层 data_quality 源分类补登 4 个标签，只动
    # data_real_pct → quality_factor；各蜂自身输出不变（同 v0.45.209）
    "v0.45.314": (),
    # 09-18（v0.45.315 补登）：删 Polymarket。Oracle 融合分逐位不变，只少了 data_quality
    # 里恒为 unavailable 的通道 → 仅 Queen 层 data_real_pct 变化（同 v0.45.314）
    "v0.45.315": (),
    # 09-28（原定 09-24，未赶上那次扫描）：断开 GexRegimeModifier 对 rule_score 的直接加减分 —— 只在 Queen 层方向投票
    # 之后，各蜂自身输出不变；SIGNAL_UPSTREAM 里没有信号读 final_score/rule_score ⇒ 只动
    # composite.final_score（ALWAYS_SLICED，不必写）
    "v0.45.334": (),
    # 09-28（原定 09-25，未赶上那次扫描）：Buzz 情绪动量改按扫描日回看归档（调整层，维度 IC 协议 buzz_v1 锚点）。
    # 只点名 score：方向由合成值决定、在调整之前，不变；通道改全精度只是记录精度，不换代 buzz.comp.*。
    # 下游（Guard 读 Buzz 分）由 _scope_closure 带出
    "v0.45.340": ("agent.BuzzBeeWhisper.score",),
    # 09-28（与 v0.45.334/340 同日）：中性化读 Oracle 主链 gamma_exposure 的两条进分通道 ——
    # ① options_analyzer 的 gex_signal 恒 1.0 ⇒ Oracle options_score ⇒ Oracle 分，且方向由分数带定
    #   （v0.45.201）会跟着翻 ⇒ `agent.OracleBeeEcho.*` 两个都点名；
    # ② BearBee `gex < 0 ⇒ options_bear ≥ 5.0` 下限删除 ⇒ `bear.options_bear` 直接点名。
    # `options.gamma_exposure` 本身的算法没动（照算、照落盘），仍是叶子、不切。
    # 下游经 SIGNAL_UPSTREAM：Oracle 方向 → consensus_strength → crowding.* → Scout → bear.insider_bear，
    # 以及 Rival 分（经概率，v0.45.538 前）/ Guard / bear.score / Bear / swarm_agreement —— 由 _scope_closure 带出，不手写
    # （v0.45.561 更正：`ml.expected_*` 与 Rival 方向不读 Oracle 方向 / 拥挤度，不在本条闭包里）
    "v0.45.349": ("agent.OracleBeeEcho.*", "bear.options_bear"),
    # 09-28（与 v0.45.334/340/349 同日）：日报 VIX 改当日收盘（收盘后延迟报价优先，CSV 兜底）+
    # 陈旧 VIX 不计 Guard 宏观票 ⇒ `_calc_macro_adjustment` 的输入换了量 ⇒ `guard.macro_adj`
    # （SIGNAL_LEAVES，只在被点名时换代）与 Guard 分（macro_adj 直接加进 risk_adj 维分）都点名。
    # Guard 的 macro_regime 还经 RegimeWeightAdjuster 改权重 ⇒ 只动 composite.final_score（ALWAYS_SLICED）。
    "v0.45.357": ("guard.macro_adj", "agent.GuardBeeSentinel.*"),
    # 09-28（与 v0.45.357 同日同集合）：补跑的 Guard 宏观票对齐目标日（VIX 取 CSV 的 D 行并计票、期限结构读快照、
    # FOMC 按 D 数、板块按 D 对齐）⇒ 同样只动 `_calc_macro_adjustment` 的输入 ⇒ 同样点名这两项。只改补跑行。
    "v0.45.366": ("guard.macro_adj", "agent.GuardBeeSentinel.*"),
    # 09-28（v0.45.369，新代码 09-29 上线，等价论证见 ic_rerun_readiness 同版条目）：Oracle 期权数据「可用」
    # 改按 OptionsAgent 的 data_quality 判 —— 只改 Oracle 的 data_quality 标签与置信度，分与方向不变；
    # 没有归档信号读 confidence / data_quality ⇒ 只动 Queen 层（同 v0.45.314 / 315）
    "v0.45.369": (),
    # 09-28（v0.45.383，新代码 09-29 上线，等价证据见 ic_rerun_readiness 同版条目）：日线完整性校验 ⇒ 缺口日的 iv_rank
    # 置空、Oracle 期权分的 iv_signal 走中性 ⇒ Oracle 分变、方向随分数带变（同 v0.45.349 的两条）⇒ `agent.OracleBeeEcho.*`
    # 点名；BearBee 的 options_bear 读 Oracle 方向 ⇒ `bear.options_bear` 点名（同 349）。只在缺口日×标的上变。
    "v0.45.383": ("agent.OracleBeeEcho.*", "bear.options_bear"),
    # 09-28（v0.45.423，新代码 10-08 上线，等价证据见 ic_rerun_readiness 同版条目：09-28~10-07 共 240 对全部等价）：
    # twelve_data 当日那根改按交易所收盘判（收完照收）。进分的只有两条 Twelve Data **兜底**路径，正常日一个分都不变：
    # ① yfinance 日线抛错时的成交量回落 ⇒ volume_ratio 的三个读者：Buzz volume_signal（分与方向 ⇒ `agent.BuzzBeeWhisper.*`）、
    #   拥挤度的 Google Trends 代理（`real_data_sources` 由 volume_ratio 映射；它本身是原始观测、不切，见
    #   `test_raw_observations_are_not_sliced` ⇒ 点名读它的第一个系统输出 `crowding.score`，下游 crowding.* → Scout / Guard /
    #   Rival 由 _scope_closure 带出）、Bear 的 momentum_bear（不单独入档，直接进 `bear.score`）；
    # ② 日线缺口第二源 ⇒ iv_rank ⇒ 同 v0.45.383 的两条（`agent.OracleBeeEcho.*`、`bear.options_bear`）。
    # 叶子 `price.volume_ratio` / `buzz.comp.volume_signal` / `crowding.comp.google_trends` / `options.iv_rank` 不点名
    #（同 383 不点名 iv_rank；前三个里两个在 `NEVER_SLICED_TODAY` 守卫名单上）：
    # 它们的口径（yfinance 主路径）没变，只是兜底那几天从「比 yfinance 少一根」对齐成一样；点名会白白切掉
    # 6 周以上口径一致的历史。
    "v0.45.423": ("agent.BuzzBeeWhisper.*", "crowding.score", "bear.score",
                  "agent.OracleBeeEcho.*", "bear.options_bear"),
    # v0.45.438：更正 v0.45.423 条目（同日、日期不动），照表头「更正条目」规则抄被更正那条的范围
    "v0.45.438": ("agent.BuzzBeeWhisper.*", "crowding.score", "bear.score",
                  "agent.OracleBeeEcho.*", "bear.options_bear"),
    # 10-09（v0.45.441）：Scout 拥挤度去掉 consensus_strength（Scout 与同伴并行、读板时同伴没发布 ⇒ 27/30 恒为 0）。
    # 该分量本身从此恒为 None（点名它）；其余经 SIGNAL_UPSTREAM 带出：crowding.score → crowding.* → agent.ScoutBeeNova.*
    # （分平均 −0.34、confidence −0.02、方向有翻）→ Guard / bear.*（v0.45.561 更正：不含 `ml.expected_*` / Rival 方向）。
    # ⚠️ 过切：Rival / Guard 自己数的拥挤度（Phase-1 之后读完整普查）没变，但依赖边把它们挂在 crowding.score 上
    # 「代表同一个 CrowdingDetector 公式换代」——宁多切不少切（同 v0.45.423 的 crowding.score）。
    # 那条 SIGNAL_UPSTREAM 边（consensus_strength ← Phase-1 同伴方向）**保留**：它描述的是旧世代数据的真实依赖，
    # 删了会让早先边界（如 v0.45.349 经 Oracle 方向）对历史行的闭包悄悄缩小。
    # v0.45.561：改为带止期 10-09 —— 早先边界照旧经它传递，此后的边界（如同日的 v0.45.445）不再经它白切下游。
    "v0.45.441": ("crowding.comp.consensus_strength", "agent.ScoutBeeNova.*"),
    # 10-09（v0.45.445）：新闻主源 AV → Massive（降级仍去 Yahoo）。改的是**通道**（news 的逐文章标签换了分类器），
    # 按维度 IC 协议 §6「按层声明」：点名 `buzz.comp.news_signal` **和** `agent.BuzzBeeWhisper.*`（通道值变 ⇒ 分跟着变）。
    # 下游（Guard 读 Buzz 分与方向、Bear 读 Buzz 方向与情绪、swarm_agreement 等）由 _scope_closure 带出。
    # v0.45.561 更正：Scout 的 consensus_strength 同日（441）起不再读同伴，`ml.expected_*` 从来不读 Buzz ⇒ 都不在闭包里。
    # ⚠️ 本条也改了 `sentiment.pct`（新闻通道权重 0.25），**刻意不点名**——见 `DELIBERATELY_POOLED`（v0.45.565）。
    "v0.45.445": ("buzz.comp.news_signal", "agent.BuzzBeeWhisper.*"),
    # 10-19：蜂后 ML 反馈两个消费者（维度权重、方向票重）停用，只记影子乘数。蜂后层 ⇒ 不改任何归档蜂信号。
    "v0.45.537": (),
    # 10-19：RivalBee 分数不再含合成模型概率（`prob·10` → 5.0 基线），ml_adjustment 退出 final_score。
    # 只点名 Rival 的分：方向与 `ml.expected_7d/30d` 逐位不变（不点名 `ml.*`，免得白切它们）；
    # Guard 读 Rival 分 ⇒ Guard / Bear / swarm_agreement 由 _scope_closure 带出。
    "v0.45.538": ("agent.RivalBeeVanguard.score",),
    # 10-19（v0.45.559，新代码 10-20 上线，等价论证见 ic_rerun_readiness 同版条目）：BuzzBee data_quality.volatility 改按
    # volatility_20d 是否为 None 上报 —— 只改标签，Buzz 分 / 方向 / 通道不变；没有归档信号读 data_quality ⇒ 只动蜂后层
    # （data_real_pct 压缩线、S5 冲突 DQ 票重）⇒ 只动 composite.final_score（ALWAYS_SLICED）。同 v0.45.209 / 369
    "v0.45.559": (),
    # 10-19（v0.45.639，新代码 10-26 上线，等价论证见 ic_rerun_readiness 同版条目）：BearBee data_quality.options 照抄 Oracle 板上判定 ——
    # 只改标签与 Bear confidence，Bear 分 / 方向不变；`bear.*`、`agent.BearBeeContrarian.*` 读分与方向 ⇒ 只动蜂后层 ⇒ 只切 composite.final_score
    "v0.45.639": (),
    # 10-19（v0.45.652，新代码 10-27 推送，等价论证见 ic_rerun_readiness 同版条目）：行情三字段以外的 yfinance 日线读者收进 `yf_daily`
    # （去掉公司行动空行）。进分的是板块轮动（Guard 宏观票 → 权重路由）与 Rival 直连回退，都只在空行日变 ⇒ 点名 Guard
    # （本来就从 10-19 起，0 个信号后移）；`guard.macro_adj` / Rival 刻意不点名：09-28 起归档里没有一天受空行影响（证据见该条目）
    "v0.45.652": ("agent.GuardBeeSentinel.*",),
    # 10-19（v0.45.654，等价论证见 ic_rerun_readiness 同版条目）：补跑宏观报价末根须是 D + 曲线只用观测到的 10Y ⇒ Guard 宏观票的输入。
    # 只在「补跑且 Yahoo 缺 D」/「没整份降级而 10Y 缺」的日子变，09-28 起归档里 0 行 ⇒ 同 652 只点名 Guard（0 个信号后移），`guard.macro_adj` 不点名
    "v0.45.654": ("agent.GuardBeeSentinel.*",),
    # 10-19（v0.45.657，等价论证见 ic_rerun_readiness 同版条目）：补跑时 FRED 序列按 D 的时点取 ⇒ 2Y 经曲线进 Guard 宏观票。
    # 只在补跑行变，四个历史补跑日重取逐项相同 ⇒ 同 654 只点名 Guard（0 个信号后移），`guard.macro_adj` 不点名
    "v0.45.657": ("agent.GuardBeeSentinel.*",),
    # 10-19（v0.45.660，等价论证见 ic_rerun_readiness 同版条目）：FRED 序列逐个解析 ⇒ 一个 "." 不再连带丢掉进曲线的 2Y。
    # 历史 0 次（缺月 11-10 起才落到旧代码读的位置）⇒ 同 654 / 657 只点名 Guard（0 个信号后移），`guard.macro_adj` 不点名
    "v0.45.660": ("agent.GuardBeeSentinel.*",),
    # 10-19（v0.45.658，652 二次检查）：`yf.download` 读者与带日期窗口的 history 读者也收进 `yf_daily`，末段按「最近一场已收盘」判。
    # 可能变的只有 refuse 读者（预取包 → Rival / Scout、政体均线 → Guard、供应链 → Scout），且只在「最近一场缺了」的日子 ⇒ 点名 Guard
    # （本来就从 10-19 起，0 个信号后移）；Rival / Scout 刻意不点名：证据窗口里没有一天受影响（见 ic_rerun_readiness 同版条目）
    "v0.45.658": ("agent.GuardBeeSentinel.*",),
    # 10-19（v0.45.663，等价论证见 ic_rerun_readiness 同版条目）：`fred_macro` 读 Yahoo 日线按日期配对、缺值不再顶数 ⇒ 经 Guard 的
    # 板块冷热 / 黄金趋势 / 曲线 10Y 只在「缺一根」或「缺一项」的日子变；10-19 起旧代码扫描日冻结证据逐日核过一天都没有 ⇒ 同 654 / 657 / 660
    # 只点名 Guard（0 个信号后移），`guard.macro_adj` 不点名
    "v0.45.663": ("agent.GuardBeeSentinel.*",),
    # 10-19（v0.45.669，等价论证见 ic_rerun_readiness 同版条目）：`fred_macro` 报价全部没取到时不再提前返回兜底常量 ⇒ 这些天
    # 板块轮动照取、Guard 的板块票可能出现；10-19 起旧代码扫描日一天都没有报价全空的 ⇒ 同 654 / 657 / 660 / 663 只点名 Guard
    # （0 个信号后移），`guard.macro_adj` 不点名
    "v0.45.669": ("agent.GuardBeeSentinel.*",),
}


#: 刻意池化（v0.45.565 起）：某条边界**确实**改了这个信号的值，但用户决定暂不切、先保留跨世代样本，
#: 到复检条件满足时再定。与 v0.45.256「实测没换 ⇒ 照证据不列」不同：这里**已知换了**，不切是取舍，不是证据。
#: 到了复检日，三处出声：① `ic_rerun_readiness --quiet` 摘要行末尾的 ⏰ 段（v0.45.589）——**周度任务只读这一行**，
#: SKILL 规定 ⏳ 以外的图标在周报里显著标出；② 同一工具外壳 `attention[]` 的 warn（每日扫描 Step 11 经 `--out` 读，
#: 进日志与 status.json，没有推送）；③ `analyze()` 世代报告标 ⏰。（v0.45.565 原注释写「周度任务读 attention」，是错的。）
#: `tests/test_signal_archive_generations.py::TestDeliberatelyPooled` 钉住「边界确实改了它、确实没切、三处到期都出声」。复检后：维持 ⇒ 更新 `recheck_on_or_after` 并在 CHANGELOG 记结论；
#: 切 ⇒ 删本条，按 `SIGNAL_LEAVES` 里 sentiment.pct 那段注释的结构修法改。
DELIBERATELY_POOLED: Dict[str, Dict[str, str]] = {
    "sentiment.pct": {
        "boundary": "v0.45.445",
        "changed_via": "buzz.comp.news_signal",      # 该边界经它改了本信号（守卫要求边界直接点名它）
        "why": ("10-09 新闻主源 AV → Massive 改了新闻通道（合成值权重 0.25）⇒ 改了 pct。切在 10-09 会让 analyze() 里 "
                "pct 的 t7 / t30 样本从 1674 / 1104 清零到 11 月初 / 12 月初；且旧世代本身就是 AV / Yahoo 两种新闻刻度按天混，"
                "切掉代码边界也去不掉那一层。pct 只用于探索性 IC 报告：生产评分不读世代，维度 IC 预注册检验（H1）"
                "读的是 10-12 起的 buzz_v1，两种做法下都不受影响。"),
        "known_counter_evidence": ("固定走 Yahoo 的 5 只（TMUS/ENPH/NFLX/NEE/SNOW）在 10-09 前后横截面位置变了："
                                   "新闻分相对其余 25 只的落差 −25.0 → −5.0（AV 窗 09-28~10-02 → Massive 窗 10-09~10-19），"
                                   "pct 落差 −6.1 → −2.9、日内分位 0.29 → 0.40；而 Massive 相对 AV 的整体水平几乎没动"
                                   "（均值 +1.5、中位 −1.0）⇒ 变的是这 5 只的**相对位置**，逐日横截面 rank IC 吃的正是它。"
                                   "（sentiment-pct-buzz 会话 10-20 实测，生产库只读副本、未做显著性检验、混有行情。）"
                                   "这 5 只每天走 Yahoo 是因为 AV 配额只够 25 只。"),
        "recheck_when": "10-09 之后 sentiment.pct 已结算的 t7 交易日 ≥ 10（逐日 IC 的出数门槛）",
        # 第 10 个交易日 10-22 + 7 个交易日 = 11-02 结算，次日回填（`is_trading_day.add_trading_days`；漏扫一天就顺延）
        "recheck_on_or_after": "2026-11-03",
        "how": ("临时把 sentiment.pct 加进 COHORT_SIGNAL_SCOPE['v0.45.445'] 跑 `--analyze`，得 10-09 起的新世代 IC，"
                "与缺省（池化）结果比；同时按上面 5 只是否进 / 出两端分位看排序。新世代与全史同号、且新世代自身不在"
                "噪音带外反号 ⇒ 维持池化；否则切。"),
    },
}


def _cohort_history() -> list:
    """边界表**调用时**读 —— 不在 import 期抄一份（日后追加、测试替换都要生效）。"""
    import ic_rerun_readiness
    return ic_rerun_readiness._COHORT_HISTORY


def _matching(names: Iterable[str], patterns: Iterable[str]) -> Set[str]:
    pats = tuple(patterns)
    return {n for n in names if any(fnmatchcase(n, p) for p in pats)}


def edge_active(edge: Edge, on: Optional[str]) -> bool:
    """边在扫描日期 `on` 是否生效（`[since, until)`）。`on=None` ⇒ 任何时候生效过就算（最保守的并集）。"""
    if on is None:
        return True
    return (edge.since is None or edge.since <= on) and (edge.until is None or on < edge.until)


def active_upstream(down: str, on: Optional[str]) -> Tuple[str, ...]:
    """`SIGNAL_UPSTREAM[down]` 在 `on` 当天生效的上游模式。"""
    return tuple(e.up for e in SIGNAL_UPSTREAM.get(down, ()) if edge_active(e, on))


def _scope_closure(direct: Iterable[str], universe: Set[str],
                   on: Optional[str] = None) -> Set[str]:
    """直接目标 + 沿 `SIGNAL_UPSTREAM` 往下游传递，直到不再扩张。

    `on` = 边界日期：只走那天生效的边（v0.45.561）。缺省 `None` 走全部边（任一时刻生效过即算）——
    只给不带日期的探查用；`generation_boundaries()` 一律按边界日期传。
    """
    hit = _matching(universe, direct)
    grew = True
    while grew:
        grew = False
        for down in SIGNAL_UPSTREAM:
            ups = active_upstream(down, on)
            if not ups or not _matching(hit, ups):
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
        hit = (_scope_closure(COHORT_SIGNAL_SCOPE[version], universe, on=date) if declared
               else set(universe))
        for s in (hit | ALWAYS_SLICED | unknown) & signals:
            out[s] = {"date": date, "version": version, "undeclared": not declared}
    return out


def _forward_close_col(horizon: str) -> str:
    """前瞻收益的终点价列。唯一真相 `ic_diagnostics.FORWARD_CLOSE_COL`，未登记即抛。"""
    sys.path.insert(0, str(Path(__file__).parent))
    import ic_diagnostics as icd
    if horizon not in icd.FORWARD_CLOSE_COL:
        # 不猜：按 f"price_{horizon}" 拼列名正是 v0.45.321 修掉的那个 bug
        raise ValueError(f"horizon={horizon!r} 未在 ic_diagnostics.FORWARD_CLOSE_COL 登记"
                         f"（已登记：{sorted(icd.FORWARD_CLOSE_COL)}）")
    return icd.FORWARD_CLOSE_COL[horizon]


def _truncation_share(con, end_col: str, checked_col: str) -> Tuple[int, float]:
    """终点价列的截断指纹 —— 委托 `ic_diagnostics.truncation_share`（阈值 `TRUNCATION_ALARM`）。

    v0.45.321 首设于此；v0.45.328 搬到 `FORWARD_CLOSE_COL` 旁边，让 `ic_diagnostics`
    自己的读者（维度表 / `--benchmark`）也能用同一个探测器，而不是再抄一份。
    名字保留：`experiments/dim_ic_forward_test.py` 按这个名字调用。
    """
    sys.path.insert(0, str(Path(__file__).parent))
    import ic_diagnostics as icd
    return icd.truncation_share(con, end_col, checked_col)


def load_panel(db_path: Optional[Path] = None, horizon: str = "t7",
               min_width: int = 5,
               with_ticker: bool = False,
               target_metric: str = "return") -> Dict[str, Dict[str, List]]:
    """联表取 {signal: {date: [(值, 前瞻收益)]}}。

    前瞻收益 = `(终点收盘价 − price_at_predict) / price_at_predict`，终点列查
    `ic_diagnostics.FORWARD_CLOSE_COL`（t7 → close_t7，t30 → price_t30）。

    ⚠️ v0.45.321 更正：此前这里用 `f"price_{horizon}"`，docstring 称之为「纯价格变动」，
    与 return_t7 的路径依赖收益相对。**这是 ic_diagnostics 在 v0.45.19 已更正过的同一个误解**：
    `price_t7` 存的是 `_simulate_trade_path` 的 `exit_price`，触 SL/TP 即被钉在档位上，
    与 return_t7 一样截断（2026-05 起 100% 等于 exit_price，与 close_t7 相差 >0.01 的行
    每月 39%~76%）。另有 CRWD 两行 price_at_predict 已按 07-02 的 4:1 拆股复权、price_t7
    仍是未复权价 ⇒ 旧口径读成 +325% / +340%。v0.45.321 之前的 `--analyze` 结论全部基于它。

    v0.45.347：取数改走 `ic_diagnostics.forward_return_sql` / `row_forward_return`
    （v0.45.328 立的「前瞻收益唯一入口」）。此前这里是第三份私有 SQL，与共用入口已有一处
    分叉：终点价为 0（「没有这个价」，不是零元）时这里算成 −100%，共用入口丢弃该行。
    生产库目前 0 行，是潜在分叉；但 v0.45.328 的教训正是「两份实现，修一份等于制造分叉」。

    ⚠️ 返回的是**整张表**，不按世代切 —— 切片在做聚合的 `analyze()` 里（v0.45.265）。
    自己拿这个面板算跨日统计量的，先过一遍 `generation_boundaries()`。
    """
    db_path = Path(db_path) if db_path else _db_path()   # v0.45.160：调用时求值
    end_col, checked_col = _forward_close_col(horizon), f"checked_{horizon}"
    import ic_diagnostics as icd   # _forward_close_col 已把仓库根放进 sys.path
    sel, where, end_col = icd.forward_return_sql("close", f"return_{horizon}", horizon)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rets = {}
        for r in con.execute(
            f"SELECT date, ticker, {sel} FROM predictions WHERE {checked_col}=1 AND {where}"
        ):
            ret = icd.row_forward_return(r, end_col)
            if ret is not None:
                rets[(r["ticker"], r["date"][:10])] = ret
        if not rets:
            return {}
        if target_metric == "return":
            icd.warn_if_truncated(con, end_col, checked_col)

        if target_metric == "vol":
            # 换目标：未来 N 日已实现波动。样本集沿用同一批 (ticker, date)，
            # 保证与收益率口径可直接对照。
            # v0.45.321 核对：波动值取自行情，从未用过 price_t7 的**值**；换终点列只改样本成员
            # （生产快照：多 8 行 price_t7 为空、close_t7 已有的样本，零减少）。
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
            draws: Optional[int] = None, target_metric: str = "return",
            pool_generations: bool = False):
    """对每个归档信号跑四口径 IC + 噪音地板对照。

    v0.45.265：每个信号只用它**当前世代**的样本（`generation_boundaries`）。
    不读系统输出、也没被边界直接点名的信号（options.* / insider 金额 / 行情 …）照旧用全史。

    Args:
        target_metric: "return" = 预测方向收益（现状）；
            "vol" = 预测未来已实现波动。后者的可学性高一个数量级
            （实测同宇宙同特征 IC 0.710 vs 0.012），见 `_forward_realized_vol` 注释。
        pool_generations: True = 旧行为（跨世代混算），只作对照；报告会标出。
        draws: 噪音地板抽样数；None = 调用时的 `ic_diagnostics.RANDOM_DRAWS`（唯一真相，
            v0.45.324 起 2000）。每行带 `noise_floor_lo/hi`（地板自身的 MC 不确定带）与
            `floor_position`；`beats_noise` 只在 |IC| 超出**带上沿**时为真，带内由报告标 ◐。

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
    if draws is None:
        draws = icd.RANDOM_DRAWS
    # 显式指定地板基准：`composite.final_score` 覆盖最全（每个标的每天都有）。
    # 不能依赖默认 fallback —— 地板对基准的日期覆盖高度敏感（实测 0.076 vs 0.116）。
    # 用**未切**的面板：地板只用骨架（日期 / 宽度 / 收益），随机化掉了信号取值，与世代无关。
    floor = icd.noise_floor(panel, lag, period, draws=draws,
                            base_key="composite.final_score")

    def _band(f: Dict) -> tuple:
        return (f.get("ic_p95", float("nan")), f.get("ic_p95_lo", float("nan")),
                f.get("ic_p95_hi", float("nan")), f.get("n_draws", 0))

    band_full = _band(floor)
    # 被切信号的地板按**逐字相同的骨架**（日期序 + 每天收益序）缓存：noise_floor 只读骨架、
    # 种子固定，同骨架必得同地板，缓存是精确的。生产快照 9 个被切信号只有 5 种骨架
    # （同一边界切出来的 agent.* / catalyst.count 骨架相同），×2000 总耗时 11.7s → 8.2s。
    sliced_floor: Dict[tuple, tuple] = {}

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
        if n == n_total:
            thr, lo, hi, nd = band_full
        else:
            key = tuple((d, tuple(ret for _, ret in pairs)) for d, pairs in by_day.items())
            if key not in sliced_floor:
                sliced_floor[key] = _band(icd.noise_floor({sig: by_day}, lag, period,
                                                          draws=draws, base_key=sig))
            thr, lo, hi, nd = sliced_floor[key]
        pos = icd.floor_position(abs(r["daily_ic"]), lo, hi)
        # 覆盖度：非并列值的比例——稀疏事件型信号（如 cluster buying）
        # 大量并列会让 rank-IC 失真，必须显式暴露
        vals = [a for v in by_day.values() for a, _ in v]
        distinct_ratio = len(set(vals)) / len(vals) if vals else 0.0
        r.update({
            "signal": sig, "n_samples": n, "n_days": len(by_day),
            "distinct_ratio": distinct_ratio,
            "noise_floor": thr,
            "noise_floor_lo": lo,
            "noise_floor_hi": hi,
            "noise_floor_draws": nd,
            "floor_position": pos,
            # v0.45.324：只认带上沿。点估计随抽样数漂移，贴它的判定会跟着翻（见 floor_position）
            "beats_noise": pos == "above",
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


def verdict_mark(r: Dict) -> str:
    """一行的判定标记。报告与测试共用这一处，免得两边各写一套。

    🟢 候选 = 超出地板带上沿且 ≥3/4 口径；🟡 = 超出带上沿但口径不足；
    ◐ 贴地板 = |IC| 落在地板的蒙特卡洛不确定带内（抽样数定不了，加 --draws 或按 ⚪ 对待，
    **不据此动任何东西**）；⚪ = 不超过带下沿。
    """
    pos = r.get("floor_position")
    if pos == "band":
        return "◐ 贴地板"
    if r["beats_noise"]:
        return "🟢 候选" if r["passed_methods"] >= 3 else "🟡 口径不足"
    return "⚪ 噪音带内"


def print_report(rows: List[Dict], floor: Dict, horizon: str,
                 target_metric: str = "return",
                 generations: Optional[Dict] = None) -> None:
    sys.path.insert(0, str(Path(__file__).parent))
    import ic_diagnostics as icd

    tgt = {"return": "方向收益", "vol": "已实现波动"}.get(target_metric, target_metric)
    if target_metric == "return":
        tgt += f"（终点 {_forward_close_col(horizon)}）"
    print("=" * 112)
    print(f"【单信号 IC 档案】horizon={horizon}  目标={tgt}   "
          f"共 {len(rows)} 个信号达到最小样本量")
    print("=" * 112)
    if generations and generations.get("pooled"):
        print("  ⚠️ --pool-generations：跨世代混算。系统输出在世代边界上换过量，")
        print("     下表的 IC 是几个不同量的混合，不描述任何一个世代 —— 只作对照，勿据此下结论")
    if floor:
        print(f"  🎯 噪音地板（随机 ×{floor['n_draws']}）：|日度IC| 95分位 = "
              f"{floor['ic_p95']:.3f}（{icd.floor_band_text(floor)}）"
              f" ｜ 通过口径数 95分位 = {floor['passed_p95']:.0f}/4")
        print("     判定为真信号需同时满足：|IC| > 地板带上沿 **且** 通过 ≥3/4；"
              "带内 = ◐ 贴地板（MC 分辨率定不了，不是 🟡 也不是 ⚪）")
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
        mark = verdict_mark(r)
        warn = " ⚠️稀疏" if r["distinct_ratio"] < 0.25 else ""
        own = {"ic_p95_lo": r.get("noise_floor_lo", float("nan")),
               "ic_p95_hi": r.get("noise_floor_hi", float("nan")),
               "n_draws": r.get("noise_floor_draws", 0)}
        gen = (f" ｜世代自 {r['gen_start']}，地板 {r['noise_floor']:.3f}"
               f"（{icd.floor_band_text(own)}）"
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
    today = datetime.date.today().isoformat()
    for s, rec in sorted(DELIBERATELY_POOLED.items()):
        if s not in sigs:
            continue
        due = "⏰ 已到复检日" if today >= rec["recheck_on_or_after"] else f"复检日 ≥ {rec['recheck_on_or_after']}"
        print(f"    ⚠️ 刻意池化：{s} 跨 {rec['boundary']} 混算（已知该边界改了它的值）—— {due}；"
              f"条件：{rec['recheck_when']}。详见 signal_archive.DELIBERATELY_POOLED")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Alpha Hive 单信号 IC 档案")
    ap.add_argument("--backfill", action="store_true", help="从历史 .swarm_results 回填")
    ap.add_argument("--only", type=str, default=None,
                    help="仅回填这些信号（逗号分隔）。⚠️ 不带时用当前抽取器重写全部信号，"
                         "没按口径取值的抽取器会把旧口径写回来——先 --dry-run，见 backfill() docstring")
    ap.add_argument("--dry-run", action="store_true",
                    help="（--backfill 的缺省行为，保留兼容）只报新增/改值/不变行数，不写库")
    ap.add_argument("--apply", action="store_true",
                    help="与 --backfill 连用：真写库（先在线备份到 _manual_backups/；缺省是 dry-run，v0.45.419）")
    ap.add_argument("--analyze", action="store_true", help="分析每个信号的 IC")
    ap.add_argument("--list", action="store_true", help="列出已归档信号与覆盖度")
    ap.add_argument("--horizon", choices=["t7", "t30"], default="t7")
    ap.add_argument("--min-samples", type=int, default=50)
    ap.add_argument("--min-width", type=int, default=5)
    ap.add_argument("--draws", type=int, default=None,
                    help="噪音地板抽样数（默认 ic_diagnostics.RANDOM_DRAWS）。报告给出地板自身的"
                         " MC 不确定带，贴带信号标 ◐——想把 ◐ 定下来就加大它，别改判定规则")
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
    if args.apply and not args.backfill:
        ap.error("--apply 只能与 --backfill 连用")
    if args.apply and args.dry_run:
        ap.error("--apply 与 --dry-run 互斥")
    if args.only and not args.backfill:
        ap.error("--only 只能与 --backfill 连用")
    if args.pool_generations and not args.analyze:
        ap.error("--pool-generations 只能与 --analyze 连用")
    if args.backfill:
        only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else None
        dry = not args.apply   # v0.45.419：回填缺省 dry-run（旧缺省直接写库且不备份）
        bk = None
        if not dry:
            from data_migrations.runner import backup_before_write
            if not db.exists():
                # 空数据根里从 swarm 文件从零建库（恢复演练的真用法）：没有库就没有东西可备份
                print("无库可备份（库尚不存在，将由回填新建）")
            else:
                try:
                    bk = backup_before_write(db, "signal-archive-backfill")
                except Exception as e:  # noqa: BLE001 —— 库在却备不了就不写
                    print(f"❌ 写库前备份失败（{type(e).__name__}: {e}），已中止未写库。", file=sys.stderr)
                    return 2
                print(f"已备份 → {bk}")
        st = backfill(db_path=db, only=only, dry_run=dry)
        if dry:
            print(f"🔍 dry-run（未写库）：{st['files']} 个文件（跳过 {st['skipped']}）"
                  f" → 新增 {st['new']} / 改值 {st['changed']} / 不变 {st['same']}")
            for s, c in sorted(st["by_signal"].items()):
                print(f"   {s:<34} 新增 {c['new']:>6}  改值 {c['changed']:>5}  不变 {c['same']:>6}")
        else:
            print(f"✅ 回填完成：{st['files']} 个文件 → {st['rows']} 行"
                  f"（跳过 {st['skipped']}）")
            from data_migrations.runner import record_tool_run
            record_tool_run("signal_archive.backfill", rows_affected=st["rows"], backup=str(bk) if bk else None,
                            args=f"--only {args.only}" if args.only else "", db_path=db)

    if args.list:
        if not db.exists():
            # --list 是只读查询，不许为了「看一眼」建库（v0.45.382）：原先 ensure_schema 读写模式连接 +
            # CREATE TABLE ⇒ 数据根找错时凭空造出一个 pheromone.db，此后别的模块「库不存在」的
            # 判定全部失效（变成「表不存在 / 没数据」）。表缺失的旧库照旧由 ensure_schema 补表。
            print(f"❓ 无法判定：样本库不存在：{db}（--list 只读、不建库；"
                  "未设 ALPHA_HIVE_HOME / ALPHA_HIVE_DB_PATH 时 PATHS.db 取缺省数据根 ~/alpha-hive-data 下的库）",
                  file=sys.stderr)
            return 3
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
