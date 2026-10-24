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
**不 import data_pipeline**（只用标准库 math 与传进来的 DataFrame 的方法）：cache.py 的旧回落正是在 data_pipeline 导入失败时才跑。

口径：逐位复刻 v0.45.639 之前 data_pipeline 三份的运算（表达式与顺序相同 ⇒ 正常数据逐位相同，`tests/test_price_metrics.py`
拿冻结的旧公式随机对拍）。⚠️ 刻意**不**在这里新增 NaN 守卫（当天成交量为 NaN ⇒ volume_ratio 为 NaN、收盘为 NaN ⇒ momentum 为 NaN）：
维度 IC 协议的冻结通道层 `buzz_v1_channels.price_fields` 原样复刻的就是这套 NaN 行为，生产单方面改会让 buzz_v1 自证在那种日子对不上。
要改须连冻结层一起走协议修订（v0.45.639 只读核过：生产全史 Buzz 的三个字段 NaN 0 次）。

⚠️ 冻结通道层 `buzz_v1_channels.price_fields` 是**刻意不共用**的副本（预注册检验的定义锚点，不得依赖生产代码），别改成调用本模块。

本文件的顶层定义提到三个行情字段名 ⇒ 在维度 IC 协议 H1 输入层指纹 `signal_code_fingerprints.UNITS["price.fields"]` 里（`mentions_price_fields`），
改公式会红、要用户确认登记类别。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

#: v0.45.564（用户 10-20 定）：`period="1mo"` 的窗口有时只有 19 个交易日，门槛 20 会让那些天所有标的整天缺波动率
VOLATILITY_MIN_BARS = 19


def history_price_metrics(hist) -> Dict[str, Optional[float]]:
    """由一段**已准备好**的日线（调用方已去掉未完成 K 线 / 截好补跑窗口；至少 1 根）算四个字段。

    返回 ``{"momentum_5d", "avg_volume", "volume_ratio", "volatility_20d"}``，算不出的是 None：
      · momentum_5d：≥ 5 根时 `(末根收盘 / 倒数第 5 根收盘 − 1) × 100`；
      · avg_volume / volume_ratio：均量（≥ 20 根取最近 20 根，否则全部）为 NaN 或 ≤ 0 ⇒ **两个都是 None**（不许拿 1.0 当分母）；
        否则 avg_volume = int(均量)、volume_ratio = 末根成交量 / 均量；
      · volatility_20d：≥ `VOLATILITY_MIN_BARS` 根时，逐日涨跌幅（`fill_method=None`：Close 为 NaN 的那根连同后一根的收益一起丢）
        的样本标准差 × √252 × 100；不足 2 个收益或结果非有限 ⇒ None。

    调用方判「均量算不出」请看 ``avg_volume is None``（volume_ratio 在末根成交量为 NaN 时可以是 NaN，见模块 docstring）。
    """
    out: Dict[str, Optional[float]] = {"momentum_5d": None, "avg_volume": None,
                                       "volume_ratio": None, "volatility_20d": None}
    n = len(hist)
    if n >= 5:
        out["momentum_5d"] = float((hist["Close"].iloc[-1] / hist["Close"].iloc[-5] - 1) * 100)

    recent_vol = float(hist["Volume"].iloc[-1])
    avg_vol = float(hist["Volume"].iloc[-20:].mean()) if n >= 20 else float(hist["Volume"].mean())
    if not (math.isnan(avg_vol) or avg_vol <= 0):
        out["avg_volume"] = int(avg_vol)
        out["volume_ratio"] = recent_vol / avg_vol

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
