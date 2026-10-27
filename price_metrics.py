"""
行情三字段（momentum_5d / volume_ratio / volatility_20d，外加 avg_volume）由一段日线算出来的唯一实现（v0.45.639）。
====================================================================================================

为什么单独成模块
----------------
同一套公式此前抄了四份：`data_pipeline` 的 `YFinanceSource.fetch`、`_fetch_history_metrics`、`_fetch_historical_stock_data`，
和 `swarm_agents/cache.py` 在 data_pipeline 不可用时的旧回落。修 bug 每次只修到眼前那一份：

  · 均量算不出时把分母换成 1.0（⇒ avg_volume = 1 股、volume_ratio = 当天原始成交量，几千万）——v0.45.54 修了
    `_fetch_history_metrics`，v0.45.473 发现 `YFinanceSource` 那份「一直漏着」，v0.45.639 才发现 cache.py 第三份；
  · v0.45.601 的 `fill_method=None` 只进了 data_pipeline 的三份，cache.py 的还是 pandas 缺省。

所以这里只放「怎么算」，四个调用方各自负责「拿哪段日线」（未完成 K 线护栏、补跑窗口）与「缺了怎么标」（来源标签、日志）。
**不 import data_pipeline**（只用标准库 math / threading 与传进来的 DataFrame 的方法）：cache.py 的旧回落正是在 data_pipeline 导入失败时才跑。

口径：逐位复刻 v0.45.639 之前 data_pipeline 三份的运算（表达式与顺序相同 ⇒ 有限的结果逐位相同，`tests/test_price_metrics.py`
拿冻结的旧公式随机对拍）。**算出来不是有限数就当取不到（v0.45.653）**：真 K 线缺收盘（2026-08-28 那种：当天成交了、Yahoo 还没填收盘）落在
末根 / 倒数第 5 根 ⇒ 动量 NaN，此前照传 ⇒ Buzz `max(-10, min(10, nan))` 夹成满分 100、来源还标 `5d_real`、出口回落只认 None 也绕过；
末根成交量为 NaN ⇒ 量比 NaN ⇒ 量能通道落最低档 20。现在 momentum_5d / volume_ratio 非有限 ⇒ None（交给出口回落，`_fill_history_fields`），
并在 `nonfinite_stats()` 计数（⇒ scan_timing `price_nonfinite` ⇒ P2）。冻结通道层 `buzz_v1_channels.price_fields` 同口径改（用户 10-27 选
「根治」；此前 v0.45.639 / 645 都刻意没动它，因为冻结层原样复刻 NaN 行为、只改一边会让桥接核对在那种日子对不上）。生产全史 Buzz 三个字段 NaN 0 次。

⚠️ 冻结通道层 `buzz_v1_channels.price_fields` 是**刻意不共用**的副本（预注册检验的定义锚点，不得依赖生产代码），别改成调用本模块。

本文件的顶层定义提到三个行情字段名 ⇒ 在维度 IC 协议 H1 输入层指纹 `signal_code_fingerprints.UNITS["price.fields"]` 里（`mentions_price_fields`），
改公式会红、要用户确认登记类别。

公司行动空行（v0.45.645）
------------------------
yfinance 把分红 / 拆股并进日线时，那一天若**没有价格 K 线**，会单独出一行：开高低收全空、成交量 0、`Dividends` 或
`Stock Splits` 非零。实测（2026-10-21 入档，95 份日线里唯一一例）META 2026-09-21：10-21 收盘后 `period="1mo"` 的窗口是
(09-21, 10-21]，09-21 那根 K 线在窗口外、它的分红却漏了进来，成了第一行。这行不是 K 线——留着它，落在末根 / 倒数第 5 根
⇒ 动量 NaN（Buzz 夹成满分看多）；落在最近 20 根 ⇒ 成交量 0 拉低均量、量比偏高约 5%。
`drop_event_only_rows` 在四个调用方取到日线后**第一步**去掉它（档案里照存原样，见 `observation_layer`）。判据只认这个
完整特征（用户 10-24 定）：真实交易日只缺收盘（2026-08-28 那种，成交量正常）不动，上面「NaN 照传」的口径对它仍成立。
**空行只会在首行或末行**：yfinance 1.2.0 `utils.safe_merge_dfs` 只给**越界**事件（早于首根 K 线、或不早于末根 + 1 天）补 NaN 行，窗口中间
没有 K 线的事件并进它前面那根，不造空行（10-25 读源码核实）。首行只在纯 `period=` 请求出现（`start=` 时事件被截到首根当天起）。
不在首行的一律计数并报 P2；中间位置是防御（yfinance 换版本才可能出现），去掉后是「缺一根」（见 auto-memory 位置窗口盘点）。
**末行**是空行 = 事件日期晚于末根 K 线：去掉它会让前一场的收盘与止于 D−1 的三个字段冒充当天（2026-08-29 那种「前一交易日冒充目标日」）
⇒ 调用方据第二个返回值把这一路的行情整体当取不到、交给出口回落（用户 10-25 定，代码评审 #1）。⚠️ 盘前跑（当天还没开盘、除息日是今天）也会出现
这种末行，那时前一场其实就是最近一场——这里照样当取不到，宁缺不错；生产 14:00 PDT 收盘后扫描碰不到。补跑路径原本就由「末行不是目标日」
那一支接住（改问云端快照）。冻结通道层 `buzz_v1_channels` 有一份逐例相同的独立判据（差分测试钉住）。
"""
from __future__ import annotations

import math
import threading
from typing import Any, Dict, List, Optional

#: v0.45.564（用户 10-20 定）：`period="1mo"` 的窗口有时只有 19 个交易日，门槛 20 会让那些天所有标的整天缺波动率
VOLATILITY_MIN_BARS = 19

#: v0.45.645：公司行动空行的判据（见模块说明）。四个价格列都不是有限数、成交量恰为 0、任一事件列是有限非零数
EVENT_ROW_PRICE_COLUMNS = ("Open", "High", "Low", "Close")
EVENT_ROW_EVENT_COLUMNS = ("Dividends", "Stock Splits")

#: 进程级计数（v0.45.645）：`event_row_stats()` ⇒ scan_timing 的 `event_rows` ⇒ `alert_manager._check_event_rows`
#: （不在首行的空行 ⇒ P2）。`at_scoring` 与 `data_pipeline.mark_history_fill_scoring` 同一时点
_event_lock = threading.Lock()
_EVENT_MAX_ITEMS = 20
_EVENT_ZERO: Dict[str, Any] = {"frames": 0, "dropped": 0, "first_row": 0, "last_row": 0, "elsewhere_total": 0, "elsewhere": []}
_event_stats: Dict[str, Any] = {**_EVENT_ZERO, "elsewhere": []}
_event_at_scoring: Optional[Dict[str, Any]] = None


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def event_only_positions(hist) -> List[int]:
    """公司行动空行在 `hist` 里的行号（0 起）。缺任一价格列 / 成交量列、或一个事件列都没有 ⇒ 认不出，返回空。"""
    if hist is None or len(hist) == 0:
        return []
    cols = set(hist.columns)
    if any(c not in cols for c in EVENT_ROW_PRICE_COLUMNS + ("Volume",)):
        return []
    events = [c for c in EVENT_ROW_EVENT_COLUMNS if c in cols]
    if not events:
        return []
    prices = [hist[c].tolist() for c in EVENT_ROW_PRICE_COLUMNS]
    vols = hist["Volume"].tolist()
    evs = [hist[c].tolist() for c in events]
    return [i for i in range(len(vols))
            if not any(_finite(p[i]) for p in prices) and _finite(vols[i]) and vols[i] == 0
            and any(_finite(e[i]) and e[i] != 0 for e in evs)]


def drop_event_only_rows(hist, ticker: str = "?"):
    """去掉公司行动空行（见模块说明）并登记计数，返回 ``(日线, 末行是不是空行)``；没有空行时第一项就是传进来的同一个对象。

    调用方在取到日线后、任何判空 / 末根判断之前调（冻结层在同一位置去掉，「≥ 3 根才看交易所时钟」数的是去掉之后的根数）。
    第二项为真 ⇒ 当天那根 K 线缺了：实时路径不许拿剩下的末根（前一场）当今天，整段当取不到（见模块说明）。
    按行号去掉，不按索引标签（空行与真 K 线同一时间戳时按标签会连真的一起删）。"""
    pos = event_only_positions(hist)
    last = bool(pos) and pos[-1] == len(hist) - 1
    with _event_lock:
        if hist is not None:
            _event_stats["frames"] += 1
        _event_stats["dropped"] += len(pos)
        _event_stats["last_row"] += int(last)
        for i in pos:
            if i == 0 and len(hist) > 1:
                _event_stats["first_row"] += 1
                continue
            _event_stats["elsewhere_total"] += 1
            if len(_event_stats["elsewhere"]) < _EVENT_MAX_ITEMS:
                _event_stats["elsewhere"].append({"ticker": ticker, "date": str(hist.index[i])[:10],
                                                  "pos": i, "n": len(hist), "last": i == len(hist) - 1})
    if not pos:
        return hist, False
    gone = set(pos)
    return hist.iloc[[i for i in range(len(hist)) if i not in gone]], last


def _copy_stats(d: Dict[str, Any]) -> Dict[str, Any]:
    return {**d, "elsewhere": [dict(x) for x in d["elsewhere"]]}


def event_row_stats() -> Dict[str, Any]:
    """计数副本：``{"frames", "dropped", "first_row", "last_row", "elsewhere_total", "elsewhere", "at_scoring"}``。
    `frames` = `drop_event_only_rows` 被调了几次（同一只票走两条路径 / 评分后重取会各算一次，不是标的数）；`first_row` = 首行的空行
    （窗口外边界日，正常）；`last_row` = 末行的空行（该路行情已整体当取不到）；`elsewhere_total` = 不在首行的空行总数，`elsewhere` 是其中前 20 条
    明细（``{"ticker", "date", "pos", "n", "last"}``）；`at_scoring` = 评分那一刻的同一组（没标记过 ⇒ None）。"""
    with _event_lock:
        out = _copy_stats(_event_stats)
        out["at_scoring"] = None if _event_at_scoring is None else _copy_stats(_event_at_scoring)
        return out


def mark_event_row_scoring() -> None:
    """评分用的那份行情此刻取完（`alpha_hive_daily_report._init_scan_context`，紧挨 `mark_history_fill_scoring`）：拍快照。"""
    global _event_at_scoring
    with _event_lock:
        _event_at_scoring = _copy_stats(_event_stats)


def _reset_event_row_stats() -> None:
    """测试用：清零计数与快照（conftest 每条测试前后都清）。"""
    global _event_at_scoring
    with _event_lock:
        _event_stats.clear()
        _event_stats.update({**_EVENT_ZERO, "elsewhere": []})
        _event_at_scoring = None


#: 进程级计数（v0.45.653）：`history_price_metrics` 把非有限的动量 / 量比当取不到的次数与明细（前 20 条）。`nonfinite_stats()` ⇒
#: scan_timing 的 `price_nonfinite` ⇒ `alert_manager._check_price_nonfinite`（评分时就有 ⇒ P2）。快照时点同上
_NONFINITE_ZERO: Dict[str, Any] = {"momentum_5d": 0, "volume_ratio": 0, "items": []}
_nonfinite_stats: Dict[str, Any] = {**_NONFINITE_ZERO, "items": []}
_nonfinite_at_scoring: Optional[Dict[str, Any]] = None


def _note_nonfinite(field: str, ticker: str, hist) -> None:
    """登记一次「算出非有限数、改给 None」：字段、标的、末根日期、两端收盘 / 末根成交量是不是有限数（只记有没有，不记数值）。"""
    try:
        last = str(hist.index[-1])[:10]
        closes = hist["Close"].tolist()
        flags = {"close_last": _finite(closes[-1]), "volume_last": _finite(hist["Volume"].tolist()[-1])}
        if len(closes) >= 5:
            flags["close_5th_last"] = _finite(closes[-5])
    except Exception:  # noqa: BLE001 —— 记明细失败不影响计数
        last, flags = "?", {}
    with _event_lock:
        _nonfinite_stats[field] += 1
        if len(_nonfinite_stats["items"]) < _EVENT_MAX_ITEMS:
            _nonfinite_stats["items"].append({"field": field, "ticker": ticker, "last_bar": last, **flags})


def _copy_nonfinite(d: Dict[str, Any]) -> Dict[str, Any]:
    return {**d, "items": [dict(x) for x in d["items"]]}


def nonfinite_stats() -> Dict[str, Any]:
    """计数副本：``{"momentum_5d", "volume_ratio", "items", "at_scoring"}``——两个字段各被改成 None 几次（总数，不封顶；同一只票走两条路径
    会各算一次）、前 20 条明细、评分那一刻的同一组（没标记过 ⇒ None）。"""
    with _event_lock:
        out = _copy_nonfinite(_nonfinite_stats)
        out["at_scoring"] = None if _nonfinite_at_scoring is None else _copy_nonfinite(_nonfinite_at_scoring)
        return out


def mark_nonfinite_scoring() -> None:
    """评分用的那份行情此刻取完：给非有限计数拍快照（与 `mark_event_row_scoring` 同一处调）。"""
    global _nonfinite_at_scoring
    with _event_lock:
        _nonfinite_at_scoring = _copy_nonfinite(_nonfinite_stats)


def _reset_nonfinite_stats() -> None:
    """测试用：清零计数与快照（conftest 每条测试前后都清）。"""
    global _nonfinite_at_scoring
    with _event_lock:
        _nonfinite_stats.clear()
        _nonfinite_stats.update({**_NONFINITE_ZERO, "items": []})
        _nonfinite_at_scoring = None


def history_price_metrics(hist, ticker: str = "?") -> Dict[str, Optional[float]]:
    """由一段**已准备好**的日线（调用方已去掉未完成 K 线 / 截好补跑窗口；至少 1 根）算四个字段。

    返回 ``{"momentum_5d", "avg_volume", "volume_ratio", "volatility_20d"}``，算不出的是 None，**返回的数一定是有限数**：
      · momentum_5d：≥ 5 根时 `(末根收盘 / 倒数第 5 根收盘 − 1) × 100`；结果非有限（两端收盘有一个缺 / 为 0）⇒ None 并计数；
      · avg_volume / volume_ratio：均量（≥ 20 根取最近 20 根，否则全部）非有限或 ≤ 0 ⇒ **两个都是 None**（不许拿 1.0 当分母）；
        否则 avg_volume = int(均量)、volume_ratio = 末根成交量 / 均量；量比非有限（末根成交量缺）⇒ volume_ratio = None 并计数，avg_volume 照给；
      · volatility_20d：≥ `VOLATILITY_MIN_BARS` 根时，逐日涨跌幅（`fill_method=None`：Close 为 NaN 的那根连同后一根的收益一起丢）
        的样本标准差 × √252 × 100；不足 2 个收益或结果非有限 ⇒ None。

    调用方判「均量算不出」请看 ``avg_volume is None``；量比另看 ``volume_ratio is None``（均量有、末根成交量缺时只有它是 None）。
    `ticker` 只用于计数明细。
    """
    out: Dict[str, Optional[float]] = {"momentum_5d": None, "avg_volume": None,
                                       "volume_ratio": None, "volatility_20d": None}
    n = len(hist)
    if n >= 5:
        mom = float((hist["Close"].iloc[-1] / hist["Close"].iloc[-5] - 1) * 100)
        if math.isfinite(mom):
            out["momentum_5d"] = mom
        else:
            _note_nonfinite("momentum_5d", ticker, hist)

    recent_vol = float(hist["Volume"].iloc[-1])
    avg_vol = float(hist["Volume"].iloc[-20:].mean()) if n >= 20 else float(hist["Volume"].mean())
    if math.isfinite(avg_vol) and avg_vol > 0:
        out["avg_volume"] = int(avg_vol)
        ratio = recent_vol / avg_vol
        if math.isfinite(ratio):
            out["volume_ratio"] = ratio
        else:
            _note_nonfinite("volume_ratio", ticker, hist)

    if n >= VOLATILITY_MIN_BARS:
        returns = hist["Close"].pct_change(fill_method=None).dropna()
        if len(returns) >= 2:
            vol = float(returns.std() * (252 ** 0.5) * 100)
            if not (math.isnan(vol) or math.isinf(vol)):
                out["volatility_20d"] = vol
    return out


def volatility_20d_from_closes(closes: List[float]) -> float:
    """收盘价**列表**（已截好窗口、全是有效价、≥ 3 根——2 根只有 1 个收益，样本标准差除以 0）的同一个估计量：逐日涨跌幅样本标准差 × √252 × 100。

    v0.45.557 的 Twelve Data 复权回落用它（那条路拿到的是列表，不是 DataFrame）。表达式逐字搬自
    `data_pipeline._fill_volatility_from_twelvedata`（v0.45.639 前写在那里）⇒ 逐位相同；与上面 pandas 版数学上是同一个量，
    浮点末位可能不同（求和顺序不同），所以**不**改成调用 pandas 版。非有限结果原样返回，由调用方拒收并记日志。
    """
    rets = [b / a - 1 for a, b in zip(closes[:-1], closes[1:])]
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252) * 100
