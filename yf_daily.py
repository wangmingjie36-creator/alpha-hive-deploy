"""yfinance 日线的**唯一取数入口**（v0.45.652）：取到即去掉「公司行动空行」，并告诉调用方末行是不是空行。

为什么要有它（根因）
------------------
yfinance 1.2.0 的 `Ticker.history()` 缺省 `actions=True`，把分红 / 拆股并进日线。**越界**的事件（早于首根 K 线、或不早于
末根 + 1 天）会被 `utils.safe_merge_dfs` 补成一行「开高低收全空、成交量 0、分红 / 拆股非零」：

* 只传 `period=`（`start` 为 None）⇒ 窗口外边界日的分红可能漏成**首行**（实测 META 2026-09-21，`period="1mo"`）。
  传 `start=` 时 yfinance 把事件截到首根 K 线当天起，首行不会有；自定义 period（如 `"25d"`）内部走 start，同样不会有。
* 事件日期晚于末根 K 线（当天没有 K 线 / 盘前跑）⇒ **末行**。带不带 `start` 都会，只有 `end` 截得掉。
* 窗口中间没有 K 线的除息日 ⇒ 分红并进前一根，不造空行（只是缺一根）。日内（1m）的越界事件 yfinance 直接丢。

v0.45.645 只在 H1 行情三字段的四条取数路径上去掉了它。其余十几个调用点各自直调 `history()`，吃不吃得到全凭写法：
`Close.iloc[0]` ⇒ NaN；`diff().dropna()` ⇒ 碰巧没事。11 只 SPDR 板块 ETF 同一天除息，板块轮动一旦漏进来就整表变空，
经 GuardBee 宏观票改 `RegimeWeightAdjuster` 的维度权重 ⇒ 进生产 final_score（2026-10-25 只读审计，auto-memory
`alpha-hive-yf-event-row-consumers`）。所以根因不在哪一处，而在「每个调用点都得自己记得」：这里收成一个入口，
守卫 `tests/test_yf_daily.py::TestSingleEntry` 让「在别处直调 `.history(`」写不出来（存量例外双向登记，只许减）。

口径
----
* 判据只有一份：`price_metrics.event_only_positions`（645）。本模块不改它——`price_metrics.py` 整份在 H1 指纹 `price.fields` 里。
* 按行号删，不按索引标签：空行的时间戳与真 K 线同形，按标签会连真的一起删（同 645）。
* 返回 `(日线, 末行是不是空行)`。第二项为真 = 最近那根 K 线缺了：去掉空行后剩下的末根是**前一场**。用末根当「当天的值」的
  调用方必须据此当取不到，否则就是前一场冒充今天（645 代码评审 #1）；只按日期用整段的（蜡烛列表、按缺口校验的 HV）可以照用。
  **不用 `actions=False`**：yfinance 那样也会删掉末行空行，但一声不吭——正是要防的那种冒充。
* 计数独立于 645 的 `price_metrics.event_row_stats`：那份的「评分时快照」在评分预取之后拍，而这里的取数大多在那之后
  （Rival / Oracle / Guard 也属于评分），并进去会被标成「评分之后」。`stats()` ⇒ scan_timing `yf_daily` ⇒
  `alert_manager._check_yf_daily_event_rows`：末行 / 中间 ⇒ P2，首行只计数（窗口外的边界日，正常）。
* 不调限流闸门、不 import data_pipeline：调用方原来怎么排队照旧，本模块只管「取到之后」。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Tuple

import price_metrics

#: 调用点 → 说明。`history(source=...)` 只认这里的名字（告警按它点名）；守卫核对代码里用到的名字与本表**双向**一致。
SOURCES: Dict[str, str] = {
    "options_hv": "options_analyzer.fetch_historical_hv：1y 已实现波动率 → iv_rank（Oracle 期权分）",
    "rival_fallback": "swarm_agents.base._yf_history：预取包缺票时的直连回退（Rival RSI / MACD / BB）",
    "macro_quotes": "fred_macro._fetch_macro_data 实时分支：非 yfinance 源没供上的宏观符号（GuardBee 宏观票）",
    "macro_asof": "fred_macro._asof_history：补跑对齐目标日的宏观 / 板块 ETF 日线",
    "sector_rotation": "fred_macro._fetch_sector_rotation 实时分支：11 只板块 ETF 5 日涨跌（GuardBee 宏观票 → 权重路由）",
    "snapshot_entry_price": "alpha_hive_daily_report._post_scan_notify：快照入场价的 yfinance 回退",
    "synthetic_swarm": "alpha_hive_daily_report._generate_synthetic_swarm_results：合成蜂群结果的价格 / 5 日动量",
    "dashboard_price": "dashboard_renderer._detail：价格 / 5 日动量的最后一道回退（展示）",
    "mcp_price_history": "alpha_hive_mcp.alphahive_get_price_history：MCP 蜡烛（展示）",
    "ticker_validity": "swarm_agents.cache.check_ticker_validity：退市 / 低价检查",
    "spot_vix": "vix_term_structure._get_spot_vix：^VIX 现价兜底（指数无分红，入口照过）",
    "iv_crush": "iv_crush_scraper.get_iv_crush_data：历史财报涨跌（深度报告）",
    "crowding_cli": "crowding_detector.get_crowding_metrics：拥挤度示例（生产只被它自己的 __main__ 调用）",
    "risk_engine_cli": "risk_engine：收益序列 / 历史 VaR / 全量分析（生产只被它自己的 CLI 调用）",
}

_lock = threading.Lock()
_MAX_ITEMS = 20
_stats: Dict[str, Any] = {}


def _zero() -> Dict[str, Any]:
    return {"calls": 0, "dropped": 0, "first_row": 0, "last_row": 0, "middle": 0, "uninspectable": 0,
            "items_total": 0, "items": [], "by_source": {}}


_stats.update(_zero())


def history(tk, *, source: str, **request) -> Tuple[Any, bool]:
    """`tk.history(**request)`（`tk` = `yf.Ticker(...)` 或同形对象）去掉公司行动空行后返回 `(日线, 末行是不是空行)`。

    没有空行时第一项就是 yfinance 返回的同一个对象。`source` 必须是 `SOURCES` 里的名字。取数异常原样抛（调用方原有的
    except 照旧接）。返回的不是 DataFrame（只在测试桩里见过）⇒ 原样返回、计 `uninspectable`，不猜。"""
    if source not in SOURCES:
        raise ValueError(f"yf_daily.history: 未登记的 source {source!r}（先登记到 yf_daily.SOURCES）")
    hist = tk.history(**request)
    if not (hasattr(hist, "columns") and hasattr(hist, "iloc") and hasattr(hist, "__len__")):
        _record(source, tk, hist, [], inspectable=False)
        return hist, False
    pos = price_metrics.event_only_positions(hist)
    _record(source, tk, hist, pos)
    if not pos:
        return hist, False
    gone = set(pos)
    return hist.iloc[[i for i in range(len(hist)) if i not in gone]], pos[-1] == len(hist) - 1


def _record(source: str, tk, hist, pos, inspectable: bool = True) -> None:
    n = len(hist) if inspectable else 0
    with _lock:
        by = _stats["by_source"].setdefault(source, {"calls": 0, "first_row": 0, "last_row": 0, "middle": 0})
        _stats["calls"] += 1
        by["calls"] += 1
        if not inspectable:
            _stats["uninspectable"] += 1
            return
        _stats["dropped"] += len(pos)
        for i in pos:
            if i == 0 and n > 1:                     # 窗口外的边界日漏进来的事件：正常，只计数
                _stats["first_row"] += 1
                by["first_row"] += 1
                continue
            kind = "last_row" if i == n - 1 else "middle"
            _stats[kind] += 1
            by[kind] += 1
            _stats["items_total"] += 1
            if len(_stats["items"]) < _MAX_ITEMS:
                _stats["items"].append({"source": source, "ticker": str(getattr(tk, "ticker", "?")),
                                        "date": str(hist.index[i])[:10], "pos": i, "n": n, "last": i == n - 1})


def stats() -> Dict[str, Any]:
    """计数副本：``calls`` 调了几次；``dropped`` 去掉几行，其中 ``first_row`` 首行（正常）、``last_row`` 末行（最近那根 K 线缺了）、
    ``middle`` 窗口中间（yfinance 1.2.0 不会产生，出现 = yfinance 行为变了）；``items_total`` = 末行 + 中间的总数，``items`` 是
    前 20 条明细；``uninspectable`` = 返回的不是 DataFrame；``by_source`` 逐调用点。"""
    with _lock:
        return {**_stats, "items": [dict(x) for x in _stats["items"]],
                "by_source": {k: dict(v) for k, v in _stats["by_source"].items()}}


def _reset_stats() -> None:
    """测试用：清零（conftest 每条测试前后都清）。"""
    with _lock:
        _stats.clear()
        _stats.update(_zero())
