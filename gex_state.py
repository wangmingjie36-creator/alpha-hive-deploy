"""每只标的一份 GEX 状态（v0.45.362，GEX 第 2 层）：政体路由用的那一份，原样给展示与存档读。

为什么要有这一层（2026-09-27 根因调查 + 09-28 普查）：同一份 CBOE payload 被切成三个视图，各算各的「GEX」——

  1. **主链**（`options_analyzer.calculate_gamma_exposure`，`_select_expiries` ≤4 个到期日）
     ⇒ OracleBee `details.gamma_exposure` / 板上 `gex` / `gamma_squeeze_risk`。
     v0.45.349 起不进任何规则分；但网站、日报、深度报告一直把它当「GEX」展示。
  2. **全到期日视图**（`cboe_options.fetch_cboe_chain_for_gex` → `advanced_analyzer.DealerGEXAnalyzer`）
     ⇒ `RegimeWeightAdjuster` 的三值 regime —— **规则引擎下 GEX 进评分的唯一通道**。
  3. **原始合约视图**（`fetch_cboe_raw_contracts` → `sell_strike_levels`）⇒ 卖权选择器（不进评分）。

截断可以翻转净 GEX 的符号（`cboe_options` `_GEX_MAX_EXPIRIES` 上方的实测表：只取最近 4 个到期日，
26 只里最低 −73.3%）。1 号视图是同类截断（DTE≥8 日历日后取 4 个，且公式带 DTE 权重），翻转幅度未量化、**待验证**；
但 1 和 2 可以一正一负：网站上写「GEX −1.2M」，评分却按正 gamma 路由。
读者看到的数与进分的数不是同一个，而且两边都叫 GEX。

本模块把 **2 号视图** 收成一个带可得性标记的小字典，由 `QueenDistiller` 步骤 0 在路由的同一处、
从**同一个** `dealer_gex` 构造一次，落盘为 `swarm_results[tk]["gex_state"]`；展示与存档只读这一份。

**不改分数**：路由处的表达式原地不动，只是挪进 `routing_regime()`（在同一个 try 里调用 ⇒ 抛异常的
走向也与之前相同）；状态里的 `regime` 由同一函数、同一输入算出（`build` 自身出错的兜底分支也是）。
`available` 只是**另外**记一笔，不参与路由。
两者不一致的情形（状态不可得、路由却拿到了正 / 负 regime）照旧按 regime 路由，步骤 0 打 error 日志。
已知两种，都是真 bug，修了都改分数，要另登世代边界，本版只让它们可见：
  · `non_finite_total_gex`：NaN ≥ 0 为假 ⇒ 判成负 gamma；
  · `chain_view:snapshot_main_chain`：快照模式补跑时 GEX 视图拿到的是 ≤4 个到期日的截断主链
    （`fetch_cboe_chain` 在快照模式忽略 `expiry_selector`），v0.45.197「不回退截断链」在这条路上没兑现。

**刻意不做的**：
  · 卖权选择器（3 号视图）不共用本状态：它的路由读 `le_45dte` 视图的重定价扫描 zero gamma，
    是冻结的预注册规则 v1（`experiments/sell_strike_routing_prereg.md` §9 / §10）——换输入就是协议变更；
    而且那是另一个量（≤45 天、重定价过零点），不是全书净 GEX。
  · 主链 `gamma_exposure` 照算、照落盘（`signal_archive` 的 `options.gamma_exposure`、`backtester` 列、
    LLM 模式 Oracle / Bear 的提示词都还在读，LLM 那条用户决定不动），只是**不再以「GEX」之名展示**。
  · 2 号视图本身也不是「整本书」：`fetch_cboe_chain` 的 ATM 带宽与每边行权价上限对它照样生效
    （见该函数）。那对符号有多大影响**待验证**；本版不改它的输入，改了就是改分数。

守卫：`tests/test_gex_state.py`。
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

SCHEMA_VERSION = 1

#: 本状态进评分的唯一去处（写进每条记录，读者不用再翻 CLAUDE.md 判断它进没进分）。
SCORE_CHANNEL = "regime_weight_routing"

_REGIMES = frozenset({"positive_gex", "negative_gex"})

#: 只有这个视图算「取到」：快照模式补跑时 DealerGEX 拿到的是截断主链（`chain_view="snapshot_main_chain"`，
#: 见 `cboe_options._gex_view_stats` 注释），数有、但不是全书净 GEX。
FULL_VIEW = "cboe_full_expiries"

#: 从 `DealerGEXAnalyzer.analyze` 的返回里原样带过来的展示字段（数值；缺就是 None）。
_NUM_FIELDS = ("total_gex", "gex_flip", "largest_call_wall", "largest_put_wall",
               "gex_normalized_pct", "stock_price")


def _finite(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def routing_regime(dealer_gex: Optional[Dict]) -> str:
    """`RegimeWeightAdjuster` 收到的 regime。**与 v0.45.361 之前步骤 0 的表达式逐字相同**——改它就是改分数。"""
    return dealer_gex.get("regime", "unknown") if dealer_gex else "unknown"


def build(dealer_gex: Optional[Dict], *, missing_reason: str = "not_computed") -> Dict:
    """`DealerGEXAnalyzer.analyze` 的返回（或 None / {}）→ GEX 状态。**不抛异常**（被步骤 0 的大 try 包着，
    抛了就会让权重退回基准 ⇒ 改分数）。

    `missing_reason`：`dealer_gex` 为空时写进 `reason` 的原因（调用方知道为什么没算：没有 Scout 价、抛了异常……）。
    """
    try:
        return _build(dealer_gex, missing_reason)
    except Exception as e:  # noqa: BLE001 - 见 docstring：构造失败不能连坐路由
        try:
            regime = routing_regime(dealer_gex)   # 路由照旧拿到它本来会拿到的值
        except Exception:  # noqa: BLE001
            regime = "unknown"
        return {"schema_version": SCHEMA_VERSION, "available": False,
                "reason": f"state_build_failed:{type(e).__name__}",
                "regime": regime, "score_channel": SCORE_CHANNEL, "routing_applied": False,
                **{k: None for k in _NUM_FIELDS}, "chain_view": None, "n_expiries": None}


def _build(dealer_gex: Optional[Dict], missing_reason: str) -> Dict:
    regime = routing_regime(dealer_gex)
    d = dealer_gex if isinstance(dealer_gex, dict) else {}
    nums = {k: _finite(d.get(k)) for k in _NUM_FIELDS}
    err = d.get("error")
    if not d:
        reason = missing_reason
    elif err:
        reason = str(err)
    elif regime not in _REGIMES:
        reason = f"bad_regime:{regime}"
    elif nums["total_gex"] is None:
        reason = "non_finite_total_gex"
    elif d.get("chain_view") != FULL_VIEW:
        reason = f"chain_view:{d.get('chain_view')}"
    else:
        reason = None
    available = reason is None
    exp = d.get("expiries_used")
    return {
        "schema_version": SCHEMA_VERSION,
        "available": available,
        "reason": reason,
        # 路由实际用的值（可得与否都照实记：不可得时是 "unknown"，异常形状时见 reason）
        "regime": regime,
        "score_channel": SCORE_CHANNEL,
        # 政体权重调整真的跑完了才由步骤 0 置 True（`adjust_weights` 抛异常 ⇒ 权重退回基准、这里保持 False）
        "routing_applied": False,
        # 不可得 ⇒ 数值一律 None（旧返回在出错时写 total_gex=0.0，那是哨兵值，不是「零 GEX」）
        **{k: (v if available else None) for k, v in nums.items()},
        "chain_view": d.get("chain_view") if available else None,
        "n_expiries": len(exp) if available and isinstance(exp, list) else None,
    }


def of(ticker_result: Optional[Dict]) -> Optional[Dict]:
    """从一只标的的蒸馏结果里取 GEX 状态；没有（v0.45.362 之前的记录 / 合成回退）返回 None。

    ⚠️ 读者**不要**在 None 时回退到 OracleBee `gamma_exposure`：那是另一个量（主链截断），
    同名混排正是本模块要消灭的东西。缺就显示「不可得」。
    """
    if not isinstance(ticker_result, dict):
        return None
    st = ticker_result.get("gex_state")
    return st if isinstance(st, dict) and st.get("schema_version") == SCHEMA_VERSION else None


REGIME_ZH = {"positive_gex": "正 gamma（压制波动）", "negative_gex": "负 gamma（放大波动）"}


def display_regime(ticker_result: Optional[Dict], dealer_gex: Optional[Dict] = None) -> str:
    """展示用 regime。**扫描记录里有状态就以它为准**（不可得 ⇒ "unknown"，不拿别处另算的顶上）；
    只有没有状态的旧记录，才退到同一视图的 `dealer_gex`（ML 报告 `advanced_analysis` 里另算的那份），
    并用与 `build` **同一套**可得性规则（出错 / 非有限 / 非全到期日视图都不算）。"""
    st = of(ticker_result)
    if st is None and isinstance(dealer_gex, dict) and dealer_gex:
        st = build(dealer_gex)
    if st is None:
        return "unknown"
    return st["regime"] if st.get("available") and st.get("regime") in _REGIMES else "unknown"


#: 「Gamma 压榨风险」展示分档：做市商净空 gamma（负 GEX）⇒ 追涨杀跌式对冲 ⇒ 压榨风险高。
#: ⚠️ 与 `options_analyzer` 的 `gamma_squeeze_risk` **方向相反**（那边主链 GEX > 0 ⇒ "high"，
#: 注释却写「正 GEX 压制波动」）。那个字段仍进 LLM 模式的提示词（用户决定不动 LLM），展示一律用这里的。
_SQUEEZE = {"negative_gex": "high", "positive_gex": "low"}


def squeeze_label(regime: str) -> str:
    return _SQUEEZE.get(regime, "unknown")


def display_line(state: Optional[Dict]) -> str:
    """一行中文展示：可得 ⇒「净 GEX：+12.30M$ · 正 gamma（压制波动）」；否则「净 GEX：不可得（原因）」。"""
    if not state:
        return "净 GEX：不可得（本记录无 GEX 状态）"
    if not state.get("available"):
        return f"净 GEX：不可得（{state.get('reason') or '原因未记录'}）"
    return f"净 GEX：{state['total_gex']:+.2f}M$ · {REGIME_ZH.get(state['regime'], state['regime'])}"
