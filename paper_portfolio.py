"""
Alpha Hive · PaperPortfolio v0.19.0
$50,000 股票现货模拟组合，用于透明展示蜂群方向信号的真实组合级别表现。

设计原则：
- 口径与 backtester.py 一致：股票现货 (bull→买入, bear→融券卖空)
- 不含期权模拟（避免 IV crush 还原误差）
- Defined Risk：-5% SL / +10% TP / T+10 强平
- 成本复用 trading_costs.py
- 审计日志 append-only：closed_trades.jsonl
- Bootstrap：从 report_snapshots 回放历史信号重建 equity curve
"""
from __future__ import annotations

import copy
import json
import math

from hive_logger import pdt_today  # v0.28.0: 美股交易日工具

# v0.45.50：本模块此前无 logger —— 所有降级（OHLC 取数失败、SPY 基准缺失）
# 都是静默的，连 debug 都没有。
try:
    from hive_logger import get_logger as _get_logger
    _log = _get_logger("paper_portfolio")
except Exception:  # pragma: no cover - 独立运行时退化到标准库
    import logging as _logging
    _log = _logging.getLogger("alpha_hive.paper_portfolio")
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 项目内依赖
try:
    from trading_costs import apply_costs, sharpe_ratio
except Exception:
    def apply_costs(gross, direction, ticker, holding_days, override_slippage_bps=None):
        return {"net_return_pct": gross - 0.12, "cost_pct": 0.12,
                "breakdown": {"slippage_pct": 0.06, "commission_pct": 0.02, "borrow_pct": 0.04}}
    def sharpe_ratio(rets, periods_per_year=36):  # 修复 #8：252/7=36
        if not rets:
            return 0.0
        m = sum(rets) / len(rets)
        v = sum((r - m) ** 2 for r in rets) / max(1, len(rets) - 1)
        return (m / math.sqrt(v)) * math.sqrt(periods_per_year) if v > 0 else 0.0

# ══════════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent
SNAPSHOT_DIR = BASE_DIR / "report_snapshots"
STATE_DIR = BASE_DIR / "paper_portfolio_state"
STATE_DIR.mkdir(exist_ok=True)

POSITIONS_FILE = STATE_DIR / "positions.jsonl"      # 当前持仓
CLOSED_FILE = STATE_DIR / "closed_trades.jsonl"     # 平仓记录（append-only）
EQUITY_FILE = STATE_DIR / "equity_curve.jsonl"      # 每日净值快照
META_FILE = STATE_DIR / "meta.json"                 # 组合元数据（启动日、现金等）

CONFIG = {
    "starting_capital": 50_000.0,
    "bootstrap_date": "2026-03-09",  # snapshot 最早可用日期（用户请求 01-02 但数据从 03-09 起）
    "max_positions": 15,
    # ── v0.39.0 资金利用率参数（用户拍板上线，2026-07-07）────────────────────
    # 依据 experiments/portfolio_capacity_report.md 36 组合全历史回放：
    # 拐点 = bull≥6.5/仓位×2/在场80%/TP15%（+6.24% vs 同口径基线 +1.99%，
    # MaxDD -1.90%，Calmar 10.75，通过前后半段稳健性检查）。
    # TP 10→15% 是最大红利（止盈太早砍赢单尾部 = "胜率高收益低"直接病因）；
    # ×3 仓位 Calmar 回落、门槛 6.0 风险调整后不划算，均不采用。
    # 样本 3.4 个月偏多头行情——上线后跑 4 周复盘（8 月初，与 bear-hypothesis 复盘同期）。
    "max_deployed_pct": 80.0,   # 30→80：上限用于不卡好信号（回放实测平均在场仅 ~26%）
    "size_pct_by_tier": {       # 基础仓位占 NAV %（×2）—— sizing_mode="tier" 时生效
        "high": 5.0,            # ⭐⭐⭐ 高置信（2.5→5.0）
        "mid": 3.0,             # ⭐⭐ 中置信（1.5→3.0）
        "low": 0.0,             # ⚠️ 低置信跳过
    },
    # ── v0.45.100 波动率目标仓位（替代 v0.39.0 的固定分档）────────────────────
    # 固定分档让 σ=15% 的 KO 和 σ=90% 的 MSTR 拿同样 5% NAV，单仓对组合的
    # 波动贡献差 6 倍——组合风险实际由几只高波动票决定。改为按标的自身
    # 20 日年化波动（pheromone.db signal_archive 的 price.volatility_20d，
    # 年化 %，与 data_pipeline 口径一致）反比定仓：size_pct = target / σ。
    # 用的是波动**水平**（横截面高度持久，IC +0.71），不是波动变化的预测。
    # `sizing_mode: "tier"` 逐字节还原旧算法，供 run_replay 对照历史。
    # 波动率缺失/非有限/≤0 时**显式**回落到分档算法，并把 tier_fallback 写进
    # 仓位记录的 sizing 字段与 rationale——静默换成默认数字是本项目的老病。
    "sizing_mode": "vol_target",          # "vol_target" | "tier"
    "vol_target": {
        "target_position_vol_pct": 1.75,  # 每仓对 NAV 的年化波动贡献目标（%）。校准：σ=35% 的中位标的 → 5% NAV，与旧 high 档持平
        # ⚠️ 这两个钳的是**置信乘数之前**的 target/σ 百分比，不是最终 NAV 占比。
        # 钳位在前、conf_multiplier 在后（顺序由 test_clamp_applies_before_conf_multiplier
        # 钉死，是有意的：先把 σ 极端值拉回可交易区间，再按置信缩放）。
        # 实测最终 NAV 占比：high(×1.0) 落 1.5–8.0%，mid(×0.6) 落 0.90–4.8%
        # （σ=200% 的票 mid 档只有 0.90% NAV）。别把下面两个数当 NAV 边界读。
        "size_pct_min": 1.5,              # 钳位下限（%，乘 conf 之前）：σ 极高的票也不至于小到没意义
        "size_pct_max": 8.0,              # 钳位上限（%，乘 conf 之前）：σ 极低的票不能无限放大
        "conf_multiplier": {"high": 1.0, "mid": 0.6, "low": 0.0},   # 保留置信分层的相对比例（旧 5:3）
        "vol_source_max_age_days": 5,     # 用 as_of 当日或最近 5 天内的 volatility_20d
    },
    # ⚠️ v0.45.12 (2026-08-25) 中性化 —— 与 TICKER_ACCURACY_FEEDBACK 同一前提被否。
    # 本表假设标的历史胜率可外推，但走查检验（experiments/ticker_winrate_persistence.py）
    # 显示 trailing 胜率对前向胜率无预测力、点估计方向相反。
    # 现实中它也几乎从未生效：closed_trades 仅 38 笔、单标的最多 7 笔，
    # min_samples_for_win_rate=5 使其历史上只有 3 次达标——属于"装着但没响的枪"，
    # 留着会在样本变多后按一个已被证伪的规则开始改仓位，故先中性化。
    # 原值 strong 1.2 / weak 0.5，恢复即回滚。
    "win_rate_multiplier": {    # 按 ticker 历史胜率修正
        "strong": 1.0,          # ≥ 60% + 样本 ≥ 10（原 1.2）
        "normal": 1.0,
        "weak": 1.0,            # < 45%（原 0.5）
    },
    "sl_pct": 7.0,              # 止损 7%（v0.19.1 参数优化：5%→7% 胜率 33%→50% Sharpe 1.27→2.73）
    "tp_pct": 15.0,             # 止盈 15%（v0.39.0：10→15，回放最大红利项）
    "time_stop_days": 10,       # T+10 强平
    "entry_conf_min": "mid",    # 最低置信 mid
    "entry_score_bull": 6.5,
    # v0.45.108：原为 3.5。
    # （v0.45.109 更正归因：这里原写「以设计上的中性点 5 为中心、两侧各留 1.5」，
    #   但 5 并不是独立的设计中性点——它就是当时的两闸中点 (6.5+3.5)/2。
    #   因果方向反了：不是「因为中性点是 5 所以闸门定在 3.5」，而是
    #   「6.5/3.5 这对闸门决定了排序中心是 5」。详见下方 candidate_sort_center。）
    # 但 final_score 的实际分布右移且下尾被压短——生产 predictions 1127 条里
    # 中位 5.52、81.5% 在 5 分以上、下尾最低只到 3.19，于是 `<= 3.5` 六个月
    # 只够到过 1 次（2026-03-13 TSLA，组合成立第 3 天），四月起每月 0 次。
    # 结果：纸面组合 60 笔（45 已平 + 15 在手）全是多头、零空单。
    # 这是「闸门恒假」，与 v0.45.71 记的「守卫恒真」是同一枚硬币的两面：
    # 代码路径没毛病、测试全绿，条件在真实数据上永远取不到。
    #
    # 现按**同向命中率**对齐，而不是在分数上几何对称：几何对称的 4.12
    # 只放行 191 条 bearish 里的 4 条（2.1%），等于没改；4.85 放行 25.1%，
    # 对上 bullish 侧 6.5 的 25.7%。
    # 依据窗口 2026-06-01~09-03（中位稳定在 5.25~5.37 的同一世代；四、五月
    # 中位 6.19/5.79 属另一世代，未混入）。逐月第 25.7 百分位 4.73/4.77/4.92/4.75，
    # 极差 0.19——不是拿单月凑出来的。
    #
    # ⚠️ 改这个值造成口径世代边界：之后的净值不能直接和之前比，
    #    要对比须 run_replay({"entry_score_bear": 3.5}) 重放旧口径。
    # ⚠️ 排序中心必须跟着这个值走——v0.45.109 已改为导出，见下方
    #    candidate_sort_center。改这里之前先读那段注释。
    "entry_score_bear": 4.85,

    # ── 候选排序中心（v0.45.109）────────────────────────────────────────────
    # None = 从两闸中点导出 (entry_score_bull + entry_score_bear) / 2。
    # 给数字 = 强制该中心（仅供 run_replay 做 A/B 重放旧口径；生产别写死）。
    #
    # 为什么必须导出而不是写死：排序键是 `abs(score - center)` 降序，两侧候选
    # 集合的边界就是两个闸门，所以「离中心多远」在 center=两闸中点时**恒等于**
    # 「超出自己那侧闸门多少」（已验证：2 万组随机配对零反例）。中心一旦偏离
    # 两闸中点，偏向的那一侧就获得一段与信号强度无关的排序补贴。
    #
    # 历史：此处原本硬编码 5，而 (6.5 + 3.5) / 2 = 5.0 —— 那个 5 从来不是
    # 「名义中性点」，它就是当时的两闸中点。v0.45.108 把 entry_score_bear
    # 3.5→4.85，两闸中点随之移到 5.675，硬编码的 5 却留在原地，成了一个
    # **输入变了却没跟着变的导出常数**（与 v0.45.71「守卫恒真」/ v0.45.108
    # 「闸门恒假」同族：代码没毛病、测试全绿，语义已经悄悄错位）。
    # 后果实测：C=5.0 下 bull 距心域 [1.50, 3.74]、bear 距心域 [0.16, 1.22]
    # **完全不重叠**——不是「多头天然优先」，是每一个多头候选都压过每一个
    # 空头候选（P(多头在前)=100.0%）。旧配置下这不成立也测不出来：
    # entry_score_bear=3.5 在全部 92 个快照日里放行的空头候选是 **0 条**，
    # 两方向从未同场竞争过，所以这个中心从未被真实检验。
    #
    # ⚠️ 不要改成 final_score 的实测中位。中位与两个闸门的位置无关，
    #    修不动这个偏置：06-01 世代中位 5.35 只把 P(多头在前) 从 100.0%
    #    降到 95.9%（等于没改），全期中位 5.60 也才到 77.9%；
    #    两闸中点 5.675 → 61.4%。同 v0.45.108「几何对称的 4.12 只放行 2.1%」
    #    的教训：要对齐的是**你真正在意的那个量**，不是看起来对称的那个数。
    "candidate_sort_center": None,

    "min_samples_for_win_rate": 5,  # 低于 5 样本不用胜率过滤

    # ── 两层模式（v0.19.1）──────────────────────────────────────────────────
    # bootstrap_date ～ live_start_date：回放所有 ticker（建立历史基准 + 胜率统计）
    # live_start_date 之后：只开 ticker_whitelist 里的新仓（跟实际报告对齐）
    # ticker_whitelist 留空 [] = 继续全标的
    "live_start_date": "2026-04-16",   # 今天之后进入实时追踪模式
    # P0-3 (v0.38.0): 原 ["NVDA"] 是深度报告时代的遗留——组合当时挂在
    # generate_deep_v2、只有 NVDA 跑深度报告。现已挂进日报主流程
    # （全 10 标的每日扫描），白名单放开，否则组合永远只有 NVDA 一仓。
    "ticker_whitelist": [],             # 空 = 全标的可入场
}


# ══════════════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    ticker: str
    direction: str          # "bullish" / "bearish"
    entry_date: str
    entry_price: float
    sl_price: float
    tp_price: float
    shares: float           # 允许小数股（按 $ 计算的模拟）
    size_usd: float         # 初始建仓市值
    time_stop_date: str     # T+10 强平日
    confidence: str         # high/mid/low
    score: float            # 蜂群评分
    rationale: str          # 入场依据简述
    # v0.45.100：仓位算法留痕（"tier" / "vol_target(σ=35.0%→5.0%)" /
    # "tier_fallback(no_vol)"）。必须带默认值且排最后——旧 positions.jsonl
    # 没有这个字段，Position(**p) 才能继续加载。
    sizing: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ClosedTrade:
    ticker: str
    direction: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    holding_days: int
    shares: float
    gross_return_pct: float
    net_return_pct: float
    cost_pct: float
    pnl_usd: float
    exit_reason: str        # "TP" / "SL" / "TIME"
    confidence: str
    score: float

    def to_dict(self) -> Dict:
        return asdict(self)


# ══════════════════════════════════════════════════════════════════════════════
# JSONL 读写工具
# ══════════════════════════════════════════════════════════════════════════════

def _load_jsonl(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _atomic_write_text(path: Path, content: str, mode: int = 0o644) -> None:
    """修复 Bug #20：tmp + fsync + os.replace 原子写，防止崩溃/断电损坏

    v0.23.7：增加 mode 参数。tempfile.mkstemp 默认 0o600（仅 owner 读写），
    会让 paper_portfolio_state/meta.json 等文件无法被其他工具/用户读取。
    显式设为 0o644 与正常文件权限一致。
    """
    import os as _os
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".tmp.", dir=str(path.parent)
    )
    try:
        with _os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            _os.fsync(f.fileno())
        _os.chmod(tmp_path, mode)  # 修正 mkstemp 默认 0o600
        _os.replace(tmp_path, path)
    except Exception:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_jsonl(path: Path, records: List[Dict]) -> None:
    """完整重写（用于 positions 这种会删减的）— 原子写"""
    content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    _atomic_write_text(path, content)


def _append_jsonl(path: Path, record: Dict) -> None:
    """追加（用于 closed_trades / equity_curve）— 单行追加对原子性需求低，保留 append"""
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_meta() -> Dict:
    if META_FILE.exists():
        return json.loads(META_FILE.read_text(encoding="utf-8"))
    return {
        "version": "0.19.0",
        "starting_capital": CONFIG["starting_capital"],
        "starting_date": CONFIG["bootstrap_date"],
        "cash": CONFIG["starting_capital"],
        "last_run_date": None,
        "config_snapshot": dict(CONFIG),
    }


def _save_meta(meta: Dict) -> None:
    # 修复 Bug #20：原子写
    _atomic_write_text(META_FILE, json.dumps(meta, ensure_ascii=False, indent=2))


# ══════════════════════════════════════════════════════════════════════════════
# 置信度 tier 推断（复用 generate_deep_v2 的 P0 逻辑）
# ══════════════════════════════════════════════════════════════════════════════

def _infer_confidence(snapshot: Dict) -> str:
    """根据 snapshot 推断置信度 tier (high/mid/low)。

    Snapshot 可能缺字段，尽量兜底：
    - agent_votes 分散度 → dim_std
    - 无 bear_signals 列表时视为 0
    """
    # v0.45.110：此处原有一行 `score = float(snapshot.get("composite_score") or 0)`，
    # 但函数体只用 dim_std 与 bear_sig_count，score 从未被读过——是死读
    # （ruff F841 能抓，本仓 pyproject 全局 ignore 了它，故一直没暴露）。
    # 连带后果：置信度**完全不看分数**，所以缺分快照从这里拿到的是 "high"。
    votes = snapshot.get("agent_votes") or {}
    if votes:
        vals = [float(v) for v in votes.values() if v is not None]
        if len(vals) >= 2:
            m = sum(vals) / len(vals)
            dim_std = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
        else:
            dim_std = 0.0
    else:
        dim_std = 0.0

    bear_sig_count = len(snapshot.get("bear_signals") or [])

    violations = 0
    if dim_std >= 1.5: violations += 1
    if bear_sig_count > 0: violations += 1

    if violations == 0:
        return "high"
    elif violations == 1:
        return "mid"
    return "low"


# ══════════════════════════════════════════════════════════════════════════════
# Ticker 历史胜率（从 closed_trades 自计算，避免依赖外部缓存）
# ══════════════════════════════════════════════════════════════════════════════

def _ticker_win_rate(ticker: str, closed: List[Dict]) -> Tuple[float, int]:
    trades = [t for t in closed if t["ticker"] == ticker]
    if not trades:
        return 0.0, 0
    wins = sum(1 for t in trades if t["pnl_usd"] > 0)
    return wins / len(trades), len(trades)


def _size_multiplier(ticker: str, closed: List[Dict]) -> float:
    wr, n = _ticker_win_rate(ticker, closed)
    if n < CONFIG["min_samples_for_win_rate"]:
        return CONFIG["win_rate_multiplier"]["normal"]
    if wr >= 0.60 and n >= 10:
        return CONFIG["win_rate_multiplier"]["strong"]
    if wr < 0.45:
        return CONFIG["win_rate_multiplier"]["weak"]
    return CONFIG["win_rate_multiplier"]["normal"]


# ══════════════════════════════════════════════════════════════════════════════
# 历史价格获取（yfinance，带缓存）
# ══════════════════════════════════════════════════════════════════════════════

_PRICE_CACHE: Dict[Tuple[str, str, str], Dict] = {}


# v0.38.2: 全区间 OHLC 存储（replay 用）——按 ticker 一次取全程，之后按日期切片，
# 避免 36 组合 × 每仓独立 (start,end) 组合键全部 miss 打爆 yfinance 限流
_OHLC_FULL: Dict[str, Dict[str, Dict]] = {}


def prefetch_ohlc(tickers: List[str], start: str, end: str) -> None:
    """一次性预取多标的全区间 OHLC 进 _OHLC_FULL。回放前调用；生产路径不依赖。"""
    try:
        import yfinance as yf
    except ImportError:
        return
    for tk in tickers:
        if tk in _OHLC_FULL:
            continue
        try:
            hist = yf.Ticker(tk).history(start=start, end=end, auto_adjust=False)
            out = {}
            for idx, row in hist.iterrows():
                out[idx.strftime("%Y-%m-%d")] = {
                    "Open": float(row["Open"]), "High": float(row["High"]),
                    "Low": float(row["Low"]), "Close": float(row["Close"]),
                }
            _OHLC_FULL[tk] = out
        except Exception:
            _OHLC_FULL[tk] = {}


def _fetch_ohlc(ticker: str, start: str, end: str) -> Dict[str, Dict]:
    """拉 [start, end] 区间的每日 OHLC。返回 {date_str: {Open, High, Low, Close}}"""
    # v0.38.2: 优先从全区间存储切片（prefetch_ohlc 预热后零网络调用）
    # 注意 end 排他（与 yfinance history(start,end) 语义一致，保证与直连路径行为完全相同）
    full = _OHLC_FULL.get(ticker)
    if full:
        return {d: bar for d, bar in full.items() if start <= d < end}

    key = (ticker, start, end)
    if key in _PRICE_CACHE:
        return _PRICE_CACHE[key]

    try:
        import yfinance as yf
        tkr = yf.Ticker(ticker)
        hist = tkr.history(start=start, end=end, auto_adjust=False)
        if hist is None or len(hist) == 0:
            _PRICE_CACHE[key] = {}
            return {}
        out = {}
        _dropped = 0
        for idx, row in hist.iterrows():
            date_str = idx.strftime("%Y-%m-%d")
            # ── v0.45.97：**含 NaN 的 bar 一律丢掉，不许流向下游。** ──
            # 2026-08-28 全站 HTTPS 断掉那天，TMUS 的 Close 是 NaN，
            # 却照样触发了 TIME 止损：`_check_exit` 返回 ("TIME", NaN, d)
            # → `gross_pct` NaN → `pnl_usd` NaN → `cash += size_usd + pnl`
            # → **cash 从此永久 NaN**，此后每天 NAV、每个新仓的 size_usd
            # 全是 NaN（NaN 会穿过 `size_usd <= 1` 和 `entry_price <= 0`
            # 两道守卫，因为 NaN 的任何比较都返回 False）。8/28→9/2 四个
            # 扫描日的净值就是这么烂掉的。
            # 在这里挡是因为它是唯一的源头：_check_exit / _mark_to_market
            # 都从这份 dict 取数，堵住出口就不必在每个消费者各补一遍。
            # 丢掉 bar 的后果是良性的：该日不触发出场，等到有真实价格的
            # 下一个交易日再触发（TIME 的判据是 `dt >= time_stop`，不会漏）。
            try:
                _o, _h, _l, _c = (float(row["Open"]), float(row["High"]),
                                  float(row["Low"]), float(row["Close"]))
            except (TypeError, ValueError):
                _dropped += 1
                continue
            if not all(math.isfinite(v) for v in (_o, _h, _l, _c)):
                _dropped += 1
                continue
            out[date_str] = {"Open": _o, "High": _h, "Low": _l, "Close": _c}
        if _dropped:
            _log.warning("_fetch_ohlc %s：丢弃 %d 根含非有限值的日线（该日不出场，顺延到有真实价格那天）",
                         ticker, _dropped)
        _PRICE_CACHE[key] = out
        return out
    except Exception as _e_ohlc:
        # ── v0.45.50：失败结果**不再写进缓存** ──
        # 旧实现把 {} 存进 _PRICE_CACHE，而取用侧用 `if key in _PRICE_CACHE` 判定
        # （空 dict 也命中）—— 于是同一进程内该 (ticker, start, end) **永不重试**，
        # 一次瞬时网络抖动就把整轮估值钉死。
        # 下游 _mark_to_market 拿到空 OHLC 会走 `cur_price = pos.entry_price`，
        # 渲染成「浮动盈亏恰好 0.00%」——与「今天这只票收平」完全同形。
        _log.warning("_fetch_ohlc %s 失败（不写入缓存，下次仍会重试）：%s: %s",
                     ticker, type(_e_ohlc).__name__, _e_ohlc)
        return {}


def _next_trading_date(ticker: str, after: str, max_lookahead_days: int = 5) -> Optional[str]:
    """找 after 之后的下一个交易日（有 OHLC 数据的日期）"""
    dt = datetime.strptime(after, "%Y-%m-%d")
    end_dt = dt + timedelta(days=max_lookahead_days + 5)
    ohlc = _fetch_ohlc(ticker, (dt + timedelta(days=1)).strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
    if not ohlc:
        return None
    sorted_dates = sorted(ohlc.keys())
    return sorted_dates[0] if sorted_dates else None


# ══════════════════════════════════════════════════════════════════════════════
# 核心逻辑：建仓 / 平仓 / mark-to-market
# ══════════════════════════════════════════════════════════════════════════════

def _snapshot_score(snapshot: Dict) -> Optional[float]:
    """取 snapshot 的 composite_score；缺失 / None / 非有限一律返回 None。

    v0.45.110：本函数存在的理由是原来那句 `float(snapshot.get("composite_score") or 0)`
    会把「拿不到分数」伪装成「分数是 0」，而 `_should_open` 的两道闸门都是
    **取反才拒绝**（`score < bull` 拒 / `score > bear` 拒），任何让比较返回 False
    的值都自动放行：

        缺键 / None / 0  → 0.0，`0 > 4.85` 为假 ⇒ **穿透看空侧**
        NaN             → 任何比较都是 False ⇒ **两侧同时穿透**
        +inf / -inf     → 各穿透一侧（+inf 以「满分看多」身份进场）

    而且三者都会带着 `conf=high` 出来——`_infer_confidence` 根本不看分数。
    这是 v0.45.93/97「NaN 穿透守卫」的第三种形态，也是 v0.45.3 记的
    「安全默认值」判据的正例：问「这个默认值会不会让下游误以为掌握了信息」——
    `or 0` 会，所以不给默认值，返回 None 让调用方显式拒绝。
    """
    raw = snapshot.get("composite_score")
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def _candidate_sort_center() -> float:
    """候选排序键的中心点。默认由两闸中点导出，不允许再出现硬编码副本。

    见 CONFIG["candidate_sort_center"] 上方的长注释：这个值必须跟着
    entry_score_bull / entry_score_bear 走，写死一份就会在改闸门时静默过期。
    """
    override = CONFIG.get("candidate_sort_center")
    if override is not None:
        return float(override)
    return (float(CONFIG["entry_score_bull"]) + float(CONFIG["entry_score_bear"])) / 2.0


def _sort_candidates(snapshots: List[Dict]) -> List[Dict]:
    """按「离排序中心多远」倒序——离得越远＝越超出自己那侧的闸门＝越优先吃资金。

    就地排序并返回同一个 list（调用方 run_for_date 依赖就地语义）。
    独立成函数是为了让回归测试打到**生产用的这一份**排序，而不是测试里
    再抄一遍 lambda（抄一遍就等于两个钟，改了生产那份测试照样全绿）。
    """
    center = _candidate_sort_center()

    def _dist(snapshot: Dict) -> float:
        # 取不到分 → 距离 0 → 排最后，与「没分数就别抢资金」的语义一致。
        #
        # v0.45.110：改走 _snapshot_score。原来写的是
        # `float(s.get("composite_score") or center)`，只挡住了缺键/None，
        # **挡不住 NaN**——NaN 是 truthy，`or` 短路不了，`abs(nan - center)`
        # 仍是 NaN，而 NaN 与任何数比较都返回 False ⇒ 名次未定义
        # （v0.45.93 记的「NaN 进排序函数」原型：2 条坏行让整张 IC 表位移 0.26）。
        # 排序发生在 _should_open **之前**，所以那边新加的守卫救不了这里，
        # 两处必须各自堵。
        score = _snapshot_score(snapshot)
        return 0.0 if score is None else abs(score - center)

    snapshots.sort(key=_dist, reverse=True)
    return snapshots


def _should_open(snapshot: Dict, existing_tickers: set, as_of: str = "") -> Tuple[bool, str]:
    """判断是否符合开仓条件。返回 (是否开, 原因说明)"""
    ticker = snapshot.get("ticker")
    if ticker in existing_tickers:
        return False, "已有持仓"

    # ── 两层模式：live_start_date 之后只开白名单 ticker ──
    whitelist = CONFIG.get("ticker_whitelist") or []
    live_start = CONFIG.get("live_start_date") or ""
    if whitelist and live_start and as_of >= live_start:
        if ticker not in whitelist:
            return False, f"实时模式仅追踪 {whitelist}，跳过 {ticker}"
    # v0.45.110：拿不到分数就拒绝，不再用 0 顶替（见 _snapshot_score 的 docstring）。
    # 顺序上放在 direction 判定之前无所谓——两者都是无条件拒绝，不存在
    # "先判方向能少拒几条"的情况。
    score = _snapshot_score(snapshot)
    if score is None:
        return False, f"composite_score 缺失或非有限值（{snapshot.get('composite_score')!r}）"
    direction = (snapshot.get("direction") or "").lower()
    if "bull" in direction:
        if score < CONFIG["entry_score_bull"]:
            return False, f"bull 但 score {score:.1f} < {CONFIG['entry_score_bull']}"
    elif "bear" in direction:
        if score > CONFIG["entry_score_bear"]:
            return False, f"bear 但 score {score:.1f} > {CONFIG['entry_score_bear']}"
    else:
        return False, "非 bull/bear"

    conf = _infer_confidence(snapshot)
    tier_ranks = {"high": 3, "mid": 2, "low": 1}
    if tier_ranks[conf] < tier_ranks[CONFIG["entry_conf_min"]]:
        return False, f"置信 {conf} < {CONFIG['entry_conf_min']}"
    return True, f"{direction} score={score:.1f} conf={conf}"


# v0.45.100：(ticker, as_of, db_path) → 年化波动 % 或 None。同一次 run_for_date
# 每个候选标的只查一次库；测试的 autouse fixture 负责清空。
_VOL_ANN_CACHE: Dict[Tuple[str, str, str, int], Optional[float]] = {}

_VOL_SIGNAL = "price.volatility_20d"

# 合法的 sizing_mode。run_for_date 入口与 _compute_position_size 共用同一张表——
# 两处各写一遍字面量，迟早会有一处漏更（v0.45.104）。
_SIZING_MODES = ("vol_target", "tier")


def _lookup_vol_ann(ticker: str, as_of: str,
                    db_path: Optional[Path] = None) -> Optional[float]:
    """读 signal_archive 里 as_of 当日或最近 max_age_days 内的 20 日年化波动（%）。

    只取 date <= as_of 的行（禁止前视），窗口外 / 缺表 / 缺库 / 非有限 / <=0
    一律返回 None——由调用方显式降级，本函数不给默认数字。
    """
    import sqlite3

    db = Path(db_path) if db_path is not None else BASE_DIR / "pheromone.db"
    # v0.45.104：max_age 必须进 key。它是**查询条件的一部分**（下面的 since 由它算），
    # 却曾被漏在 key 之外：同进程里先用 max_age=1 查出 None，再改成 max_age=10
    # 重查，拿回的是缓存里那个 None——一次 run_replay 的窗口覆盖会污染下一次。
    # 实测：4 天前的一行，narrow(1)→None 之后 wide(10) 仍然 None（清缓存则 40.0）。
    max_age = int(CONFIG["vol_target"]["vol_source_max_age_days"])
    key = (ticker, as_of, str(db), max_age)
    if key in _VOL_ANN_CACHE:
        return _VOL_ANN_CACHE[key]

    try:
        since = (datetime.strptime(as_of, "%Y-%m-%d")
                 - timedelta(days=max_age)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        _log.warning("_lookup_vol_ann: as_of=%r 不是 YYYY-MM-DD，按缺失处理", as_of)
        return None

    val: Optional[float] = None
    # 同 vol_forecast.load_day 的判法：exists() 而非匹配错误文本——
    # 「文件不存在」与「权限被拒」的 OperationalError 消息一模一样。
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT value FROM signal_archive "
                "WHERE ticker = ? AND signal = ? AND date <= ? AND date >= ? "
                "ORDER BY date DESC LIMIT 1",
                (ticker, _VOL_SIGNAL, as_of, since),
            ).fetchone()
        except sqlite3.OperationalError as e:
            # 只吞「表不存在」（全新库 / 未 backfill）。`database is locked`
            # 是瞬时可重试状态，不是"没数据"，必须往上抛。
            if "no such table" not in str(e):
                raise
            row = None
        finally:
            con.close()
        if row is not None and row[0] is not None:
            try:
                v = float(row[0])
            except (TypeError, ValueError):
                v = float("nan")
            # bool(nan) is True，`<= 0` 也放 NaN 过去——先 isfinite
            if math.isfinite(v) and v > 0:
                val = v

    _VOL_ANN_CACHE[key] = val
    return val


def _tier_size_pct(conf: str) -> float:
    return float(CONFIG["size_pct_by_tier"].get(conf, 0.0))


def _compute_position_size(
    nav: float,
    conf: str,
    ticker: str,
    closed: List[Dict],
    low_conviction: bool = False,
    as_of: str = "",
    vol_ann: Optional[float] = None,
) -> Tuple[float, str]:
    """返回 (size_usd, sizing_note)。

    sizing_mode="tier"：v0.39.0 固定分档，逐字节等于旧算法，note="tier"。
    sizing_mode="vol_target"（v0.45.100）：
        size_pct = clamp(target / σ × 100, min, max) × conf_multiplier[conf]
      σ 取 vol_ann（年化 %），未给则查 signal_archive；查不到 → 退回分档，
      note="tier_fallback(no_vol)" 并打 warning——降级必须在仓位记录里可见。
    两种模式之后都再乘 _size_multiplier，低置信仍减半。
    """
    mode = CONFIG.get("sizing_mode", "tier")
    note = "tier"
    if mode == "vol_target":
        vt = CONFIG["vol_target"]
        if vol_ann is None:
            vol_ann = _lookup_vol_ann(ticker, as_of)
        # NaN / inf / <=0 与缺失同等对待（bool(nan) is True，不能靠真值判断）。
        # v0.45.104 删掉了这里的 `isinstance(vol_ann, bool)`：_lookup_vol_ann 只
        # 返回 float(row[0]) 或 None，生产上没有任何调用方传 vol_ann，这条永不触发。
        # 保留 `isinstance(..., (int, float))` 是另一件事——它挡的是下一行
        # math.isfinite 在非数字入参上直接 TypeError（vol_ann 是公开 kwarg）。
        if (not isinstance(vol_ann, (int, float))
                or not math.isfinite(vol_ann) or vol_ann <= 0):
            vol_ann = None
        if vol_ann is None:
            _log.warning("[仓位] %s @ %s 无可用 volatility_20d（≤%d 天内），"
                         "退回分档仓位", ticker, as_of or "?",
                         int(vt["vol_source_max_age_days"]))
            base_pct = _tier_size_pct(conf)
            note = "tier_fallback(no_vol)"
        else:
            raw_pct = float(vt["target_position_vol_pct"]) / float(vol_ann) * 100.0
            lo, hi = float(vt["size_pct_min"]), float(vt["size_pct_max"])
            clamped = min(max(raw_pct, lo), hi)
            base_pct = clamped * float(vt["conf_multiplier"].get(conf, 0.0))
            clamp_tag = ("" if clamped == raw_pct
                         else (",clamp_min" if clamped == lo else ",clamp_max"))
            note = f"vol_target(σ={vol_ann:.1f}%→{base_pct:.1f}%{clamp_tag})"
    elif mode == "tier":
        base_pct = _tier_size_pct(conf)
    else:
        raise ValueError(f"CONFIG['sizing_mode']={mode!r} 未知，"
                         f"只认 {' / '.join(repr(m) for m in _SIZING_MODES)}")

    mult = _size_multiplier(ticker, closed)
    size = nav * (base_pct / 100.0) * mult
    # 低方向置信（v0.22.0）：IV 偏高 + 共振未触发 + 分数模糊区间 → 仓位减半
    if low_conviction:
        size *= 0.5
    return size, note


def _open_position(
    snapshot: Dict,
    nav: float,
    as_of: str,
    ticker_ohlc: Dict[str, Dict],
    closed: List[Dict],
) -> Optional[Position]:
    """用 as_of 日的 Close 作为入场价（成本模型里的滑点会再扣一次）"""
    ticker = snapshot["ticker"]
    direction = snapshot["direction"]
    conf = _infer_confidence(snapshot)
    low_conv = bool(snapshot.get("low_conviction", False))

    # v0.45.111：分数拿不到就不开仓，与下面 size_usd / entry_price 两道守卫同款
    # （非法输入 → return None，调用方 continue）。
    #
    # 原来写的是 `score=_snapshot_score(snapshot) or 0.0`，注释还辩解说"上游
    # _should_open 已保证分数存在，这里只是类型收口"。两点都站不住：
    #   1. **它是个会落盘的伪造值**。score 从 Position 传进 ClosedTrade（见
    #      _close_position），写进 closed_trades.jsonl，再被 ibkr_sync（导出给
    #      用户下单的 actions）、alpha_hive_mcp、chart_engine 读走。缺分记成
    #      0.0 不是"收口"，是往账本里写一个没人发生过的分数——而 0.0 在这套
    #      量表里还恰好是"最强看空"，是所有可能的谎话里最糟的一个。
    #   2. **"上游保证过"不是不检查的理由**，是 v0.45.3 那条判据的反面教材：
    #      问"这个默认值会不会让下游误以为掌握了信息"——会。真要依赖上游，
    #      就该在依赖断掉时炸掉或降级，而不是无声地编一个数。
    #
    # 选 return None 而不是 raise：本函数已有两道同形态守卫，调用方 run_for_date
    # 对 None 的处理（跳过该候选、当天继续）是现成且正确的；为一个上游已挡住的
    # 状态引入新的异常路径，收益不抵风险。
    score = _snapshot_score(snapshot)
    if score is None:
        _log.warning("[PaperPortfolio] %s %s composite_score 缺失或非有限值（%r）——"
                     "跳过开仓。正常情况下 _should_open 已经挡住，走到这里说明"
                     "两处守卫不同步了。", as_of, ticker, snapshot.get("composite_score"))
        return None

    size_usd, sizing_note = _compute_position_size(
        nav, conf, ticker, closed, low_conviction=low_conv, as_of=as_of)
    # v0.45.97：`size_usd <= 1` 挡不住 NaN（NaN 的任何比较都返回 False），
    # 于是 NaN 会一路走到 `shares = size_usd / entry_price` 变成 NaN 仓位。
    if not isinstance(size_usd, (int, float)) or not math.isfinite(size_usd) or size_usd <= 1:
        return None

    # 入场价：用 snapshot 的 entry_price 或当日 Close
    entry_price = float(snapshot.get("entry_price") or 0)
    if (not math.isfinite(entry_price) or entry_price <= 0) and as_of in ticker_ohlc:
        entry_price = ticker_ohlc[as_of]["Close"]
    # v0.45.97：同上，`<= 0` 放 NaN 过去
    if not math.isfinite(entry_price) or entry_price <= 0:
        return None

    # SL / TP 价位
    if "bull" in direction:
        sl = entry_price * (1 - CONFIG["sl_pct"] / 100.0)
        tp = entry_price * (1 + CONFIG["tp_pct"] / 100.0)
    else:  # bear
        sl = entry_price * (1 + CONFIG["sl_pct"] / 100.0)
        tp = entry_price * (1 - CONFIG["tp_pct"] / 100.0)

    shares = size_usd / entry_price

    # T+10 日期（自然日 +14 粗略覆盖 10 个交易日）
    entry_dt = datetime.strptime(as_of, "%Y-%m-%d")
    time_stop_dt = entry_dt + timedelta(days=14)

    # v0.45.111：复用上面已校验的 score。此处原有一次独立的 _snapshot_score 调用
    # 和一个 `else "N/A"` 分支——加了顶部守卫后该分支不可达，留着会让人以为
    # "缺分也能开仓、只是显示 N/A"，与实际行为相反，故一并删掉。
    rationale = f"score={score:.1f} · {conf}"
    if low_conv:
        rationale += " · ⚠️低置信-减半仓"
    if sizing_note:
        rationale += f" · {sizing_note}"

    return Position(
        ticker=ticker,
        direction="bullish" if "bull" in direction else "bearish",
        entry_date=as_of,
        entry_price=entry_price,
        sl_price=round(sl, 4),
        tp_price=round(tp, 4),
        shares=round(shares, 4),
        size_usd=round(size_usd, 2),
        time_stop_date=time_stop_dt.strftime("%Y-%m-%d"),
        confidence=conf,
        score=score,
        rationale=rationale,
        sizing=sizing_note,
    )


def _check_exit(pos: Position, as_of: str, ohlc: Dict[str, Dict]) -> Optional[Tuple[str, float, str]]:
    """
    扫描 (entry_date, as_of] 区间的每日 OHLC，检查 SL/TP/TIME 触发。
    返回 (exit_reason, exit_price, exit_date) 或 None（仍持仓）。

    保守规则：同日同时触发 SL+TP → 先 SL（悲观）。
    """
    start = datetime.strptime(pos.entry_date, "%Y-%m-%d") + timedelta(days=1)
    end = datetime.strptime(as_of, "%Y-%m-%d")
    time_stop = datetime.strptime(pos.time_stop_date, "%Y-%m-%d")

    dates_in_range = sorted([d for d in ohlc.keys() if
                              start <= datetime.strptime(d, "%Y-%m-%d") <= end])

    for d in dates_in_range:
        bar = ohlc[d]
        lo, hi = bar["Low"], bar["High"]
        dt = datetime.strptime(d, "%Y-%m-%d")

        if pos.direction == "bullish":
            hit_sl = lo <= pos.sl_price
            hit_tp = hi >= pos.tp_price
            if hit_sl and hit_tp:
                return ("SL", pos.sl_price, d)     # 保守
            if hit_sl:
                return ("SL", pos.sl_price, d)
            if hit_tp:
                return ("TP", pos.tp_price, d)
        else:  # bearish
            hit_sl = hi >= pos.sl_price
            hit_tp = lo <= pos.tp_price
            if hit_sl and hit_tp:
                return ("SL", pos.sl_price, d)
            if hit_sl:
                return ("SL", pos.sl_price, d)
            if hit_tp:
                return ("TP", pos.tp_price, d)

        # 时间止损：持仓到 T+10 日，无论方向强平
        if dt >= time_stop:
            return ("TIME", bar["Close"], d)

    return None


def _close_position(pos: Position, exit_reason: str, exit_price: float, exit_date: str) -> Tuple[ClosedTrade, float]:
    """平仓，返回 (ClosedTrade, pnl_usd)"""
    if pos.direction == "bullish":
        gross_pct = (exit_price - pos.entry_price) / pos.entry_price * 100.0
    else:
        gross_pct = (pos.entry_price - exit_price) / pos.entry_price * 100.0

    holding_days = (datetime.strptime(exit_date, "%Y-%m-%d") -
                    datetime.strptime(pos.entry_date, "%Y-%m-%d")).days
    holding_days = max(1, holding_days)

    # 成本：SL/TIME 触发时出场滑点额外加（默认 10bp/边；SL 触发加到 20bp/边 = 止损穿透溢价）
    # ⚠️ override_slippage_bps 传的是"单边 bp"，apply_costs 内部 ×2 变双边
    extra_slip = 20.0 if exit_reason == "SL" else None
    cost_res = apply_costs(gross_pct, pos.direction, pos.ticker, holding_days,
                           override_slippage_bps=extra_slip)
    net_pct = cost_res["net_return_pct"]
    pnl_usd = pos.size_usd * (net_pct / 100.0)

    trade = ClosedTrade(
        ticker=pos.ticker,
        direction=pos.direction,
        entry_date=pos.entry_date,
        entry_price=pos.entry_price,
        exit_date=exit_date,
        exit_price=round(exit_price, 4),
        holding_days=holding_days,
        shares=pos.shares,
        gross_return_pct=round(gross_pct, 4),
        net_return_pct=round(net_pct, 4),
        cost_pct=cost_res["cost_pct"],
        pnl_usd=round(pnl_usd, 2),
        exit_reason=exit_reason,
        confidence=pos.confidence,
        score=pos.score,
    )
    # v0.40.0 (D): 三重屏障标签回流——SL/TP/TIME 出场结果写入 pheromone.db，
    # 为未来 meta-labeling（用屏障结果训练"该不该信/该下多大"）攒数据。
    # 本期只写不读：ML 训练启用需 ≥100 笔样本（当前 ~25 笔）。失败不影响平仓。
    # run_replay 沙盒回放不回写（_REPLAY_MODE 守卫，防实验数据污染生产表）。
    if not _REPLAY_MODE:
        try:
            _record_barrier_outcome(trade)
        except Exception as _e_bo:
            import logging as _lg
            _lg.getLogger("alpha_hive").debug("barrier_outcome 回写失败(非致命): %s", _e_bo)
    return trade, pnl_usd


# v0.40.0: 回放模式标志——run_replay 期间为 True，屏障结果不回写生产 DB
_REPLAY_MODE = False


def _record_barrier_outcome(trade: "ClosedTrade") -> None:
    """把三重屏障出场结果幂等写入 pheromone.db 的 barrier_outcomes 表。"""
    import sqlite3
    db_path = BASE_DIR / "pheromone.db"
    con = sqlite3.connect(str(db_path), timeout=10)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS barrier_outcomes (
                ticker TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                direction TEXT,
                exit_date TEXT,
                exit_reason TEXT,      -- SL / TP / TIME
                net_return_pct REAL,
                holding_days INTEGER,
                score REAL,
                confidence TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (ticker, entry_date)
            )
        """)
        con.execute("""
            INSERT INTO barrier_outcomes
                (ticker, entry_date, direction, exit_date, exit_reason,
                 net_return_pct, holding_days, score, confidence)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker, entry_date) DO UPDATE SET
                exit_date=excluded.exit_date,
                exit_reason=excluded.exit_reason,
                net_return_pct=excluded.net_return_pct,
                holding_days=excluded.holding_days
        """, (trade.ticker, trade.entry_date, trade.direction, trade.exit_date,
              trade.exit_reason, trade.net_return_pct, trade.holding_days,
              trade.score, trade.confidence))
        con.commit()
    finally:
        con.close()


def _mark_to_market(positions: List[Position], as_of: str) -> Tuple[float, List[Dict]]:
    """计算当前持仓 mark-to-market 未实现损益"""
    unrealized = 0.0
    details = []
    for pos in positions:
        ohlc = _fetch_ohlc(pos.ticker, pos.entry_date,
                            (datetime.strptime(as_of, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d"))
        cur_price = pos.entry_price
        if as_of in ohlc:
            cur_price = ohlc[as_of]["Close"]
        elif ohlc:
            # 回退到最近一日
            latest = max(ohlc.keys())
            cur_price = ohlc[latest]["Close"]

        if pos.direction == "bullish":
            u_pct = (cur_price - pos.entry_price) / pos.entry_price * 100.0
        else:
            u_pct = (pos.entry_price - cur_price) / pos.entry_price * 100.0
        u_usd = pos.size_usd * (u_pct / 100.0)
        unrealized += u_usd
        details.append({
            "ticker": pos.ticker,
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "current_price": round(cur_price, 2),
            "unreal_pct": round(u_pct, 2),
            "unreal_usd": round(u_usd, 2),
            "sl_price": pos.sl_price,
            "tp_price": pos.tp_price,
            "size_usd": pos.size_usd,
        })
    return unrealized, details


# ══════════════════════════════════════════════════════════════════════════════
# 主入口：run_daily（单日回放）/ bootstrap_from_history（首次启动）
# ══════════════════════════════════════════════════════════════════════════════

def _load_snapshots_for_date(date_str: str) -> List[Dict]:
    """读取指定日期所有 ticker snapshot"""
    out = []
    for f in SNAPSHOT_DIR.glob(f"*_{date_str}.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append(d)
        except Exception:
            continue
    return out


def _all_snapshot_dates() -> List[str]:
    dates = set()
    for f in SNAPSHOT_DIR.glob("*_*.json"):
        parts = f.stem.split("_")
        if len(parts) >= 2:
            dates.add(parts[-1])
    return sorted(dates)


def run_for_date(as_of: str, verbose: bool = False) -> Dict:
    """
    执行指定日期的 paper portfolio 操作：
    1. 扫描现有仓位 → 检查 SL/TP/TIME 触发 → 平仓
    2. 读取当日符合条件的报告 → 开新仓
    3. 更新 equity curve
    """
    # v0.45.104：模式名打错必须**当天**就炸，不能等到"今天恰好有候选要开仓"。
    # _compute_position_size 里的 ValueError 只在 _open_position 路径上抛，
    # 没有合格候选的日子会一路静默跑完（照常写 equity），一份错配置可以潜伏好几天。
    _mode = CONFIG.get("sizing_mode", "tier")
    if _mode not in _SIZING_MODES:
        raise ValueError(f"CONFIG['sizing_mode']={_mode!r} 未知，"
                         f"只认 {' / '.join(repr(m) for m in _SIZING_MODES)}")

    meta = _load_meta()
    positions = [Position(**p) for p in _load_jsonl(POSITIONS_FILE)]
    closed = _load_jsonl(CLOSED_FILE)
    cash = float(meta["cash"])

    # ── Step 1: 检查现有仓位是否触发出场 ──
    remaining = []
    pnl_today = 0.0
    for pos in positions:
        # 拉包含 as_of 的 OHLC 段
        ohlc = _fetch_ohlc(pos.ticker, pos.entry_date,
                           (datetime.strptime(as_of, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d"))
        exit_check = _check_exit(pos, as_of, ohlc)
        if exit_check:
            reason, ex_price, ex_date = exit_check
            trade, pnl = _close_position(pos, reason, ex_price, ex_date)
            _append_jsonl(CLOSED_FILE, trade.to_dict())
            cash += pos.size_usd + pnl   # 归还本金 + 净损益
            pnl_today += pnl
            closed.append(trade.to_dict())
            if verbose:
                print(f"  ← {reason} {pos.ticker} {pos.direction}  ${pos.entry_price:.2f}→${ex_price:.2f}  PnL ${pnl:+.2f}")
        else:
            remaining.append(pos)
    positions = remaining

    # ── Step 2: 开新仓 ──
    snapshots = _load_snapshots_for_date(as_of)
    existing_tix = {p.ticker for p in positions}
    nav_for_sizing = cash + sum(p.size_usd for p in positions)  # 简化：用 cost basis 做 NAV
    # v0.45.97：纵深防御。上面三处已堵住已知的 NaN 来源，但只要 cash 或任何
    # 一条 size_usd 再次变成非有限值，这里就会静默地把 NaN 传给每一个新仓。
    # 宁可当天不开仓并大声报错，也不要写出 NaN 仓位——NaN 一旦落盘，
    # 之后每一天的 NAV 都继承它，且无法从状态文件反推出原始数值。
    if not math.isfinite(nav_for_sizing):
        _log.error("[PaperPortfolio] %s NAV 非有限值（cash=%r，持仓 %d 条）——"
                   "本日不开新仓。请检查 closed_trades.jsonl 最近是否有 NaN 出场价。",
                   as_of, cash, len(positions))
        snapshots = []

    # 先按 score 排序，保证高分优先吃到资金（v0.45.109：中心由两闸中点导出）
    _sort_candidates(snapshots)

    opened_count = 0
    for snap in snapshots:
        if len(positions) >= CONFIG["max_positions"]:
            break
        deployed = sum(p.size_usd for p in positions)
        if deployed / nav_for_sizing * 100 >= CONFIG["max_deployed_pct"]:
            break

        ok, reason = _should_open(snap, existing_tix, as_of=as_of)
        if not ok:
            continue

        ticker = snap["ticker"]
        ohlc = _fetch_ohlc(ticker, as_of,
                           (datetime.strptime(as_of, "%Y-%m-%d") + timedelta(days=3)).strftime("%Y-%m-%d"))
        new_pos = _open_position(snap, nav_for_sizing, as_of, ohlc, closed)
        if new_pos is None:
            continue
        if new_pos.size_usd > cash:
            continue  # 现金不足
        positions.append(new_pos)
        existing_tix.add(ticker)
        cash -= new_pos.size_usd
        opened_count += 1
        if verbose:
            print(f"  → 开仓 {ticker} {new_pos.direction}  ${new_pos.entry_price:.2f}  size=${new_pos.size_usd:.0f} ({new_pos.confidence})")

    # ── Step 3: mark-to-market + 快照 ──
    unreal_usd, pos_details = _mark_to_market(positions, as_of)
    deployed_usd = sum(p.size_usd for p in positions)
    nav = cash + deployed_usd + unreal_usd

    equity_snapshot = {
        "date": as_of,
        "cash": round(cash, 2),
        "deployed": round(deployed_usd, 2),
        "unrealized": round(unreal_usd, 2),
        "nav": round(nav, 2),
        "positions_count": len(positions),
        "trades_closed_today": sum(1 for t in closed if t.get("exit_date") == as_of),
        "realized_pnl_today": round(pnl_today, 2),
    }

    # 去重：同一天多次运行时只保留最新快照
    existing_equity = _load_jsonl(EQUITY_FILE)
    existing_equity = [e for e in existing_equity if e.get("date") != as_of]
    existing_equity.append(equity_snapshot)
    existing_equity.sort(key=lambda x: x["date"])
    _write_jsonl(EQUITY_FILE, existing_equity)

    # 保存仓位快照 + meta
    _write_jsonl(POSITIONS_FILE, [p.to_dict() for p in positions])
    meta["cash"] = cash
    meta["last_run_date"] = as_of
    # v0.40.2: 每次运行刷新 config_snapshot——此前只在 bootstrap 首次建 meta.json
    # 时写入一次，之后再没更新过，导致 v0.39.0 改参数后 meta.json 里的
    # config_snapshot 仍显示 3/9 的旧值（tp_pct=10/仓位×1/白名单=[NVDA]），
    # 纯记录展示字段，不影响实际交易（交易一直用模块内最新 CONFIG）。
    meta["config_snapshot"] = {k: (dict(v) if isinstance(v, dict) else v)
                                for k, v in CONFIG.items()}
    _save_meta(meta)

    return {
        "as_of": as_of,
        "nav": nav,
        "cash": cash,
        "positions": pos_details,
        "opened_today": opened_count,
        "realized_pnl_today": pnl_today,
        "equity_snapshot": equity_snapshot,
    }


def bootstrap_from_history(verbose: bool = False) -> None:
    """首次启动：从 bootstrap_date 开始逐日回放所有历史 snapshot"""
    meta = _load_meta()
    if meta.get("last_run_date"):
        if verbose:
            print(f"Bootstrap 已完成到 {meta['last_run_date']}，跳过")
        return

    all_dates = _all_snapshot_dates()
    start = CONFIG["bootstrap_date"]
    all_dates = [d for d in all_dates if d >= start]
    if not all_dates:
        print(f"⚠️ 找不到 {start} 之后的 snapshot，跳过 bootstrap")
        return

    print(f"🔁 Bootstrap PaperPortfolio: {all_dates[0]} → {all_dates[-1]} ({len(all_dates)} 个交易日)")
    for d in all_dates:
        res = run_for_date(d, verbose=verbose)
        if verbose:
            print(f"   {d}  NAV=${res['nav']:,.2f}  持仓={len(res['positions'])}  今日开仓={res['opened_today']}")


def run_replay(config_overrides: Dict, state_dir: Path,
               dates: Optional[List[str]] = None) -> Dict:
    """v0.38.2: 沙盒回放——在隔离状态目录 + 覆盖参数下重放历史 snapshot。

    绝不触碰生产状态（paper_portfolio_state/）：临时重绑模块级状态路径
    与 CONFIG 条目，try/finally 恢复。复用 run_for_date 全部逻辑，零行为改动。

    Args:
        config_overrides: 要覆盖的 CONFIG 键值（如 entry_score_bull /
            size_pct_by_tier / max_deployed_pct / tp_pct）。
            v0.45.100 起默认 sizing_mode="vol_target"；要复现 v0.39.0～v0.45.99
            的旧数字必须显式传 {"sizing_mode": "tier"}。
        state_dir: 沙盒状态目录（调用方负责唯一性；已存在则续跑）
        dates: 要回放的日期列表；None = 全部 snapshot 日期（含 bootstrap_date 过滤）

    Returns:
        {"equity": [...], "closed": [...], "final_nav": float, "config": dict}
    """
    global POSITIONS_FILE, CLOSED_FILE, EQUITY_FILE, META_FILE, _REPLAY_MODE

    if dates is None:
        dates = [d for d in _all_snapshot_dates() if d >= CONFIG["bootstrap_date"]]

    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    _orig_paths = (POSITIONS_FILE, CLOSED_FILE, EQUITY_FILE, META_FILE)
    # v0.45.100：vol_target.conf_multiplier 是二层嵌套，一层 dict(v) 拷贝挡不住
    # 就地改内层的覆盖泄漏到生产 CONFIG——用 deepcopy 封死。
    _orig_config = copy.deepcopy(CONFIG)
    _REPLAY_MODE = True  # v0.40.0: 回放期间屏障结果不回写生产 pheromone.db
    # v0.45.104：σ 缓存是模块级的，`_VOL_ANN_CACHE.clear()` 此前在全模块里一次
    # 都没有出现过——同一进程里连跑多个沙盒（weekly_optimizer 的参数网格就是
    # 这么跑的）会互相串味：上一轮 vol_source_max_age_days 下查出的 None
    # 原样回给下一轮。进出各清一次，沙盒之间彻底隔开。
    _VOL_ANN_CACHE.clear()
    try:
        POSITIONS_FILE = state_dir / "positions.jsonl"
        CLOSED_FILE = state_dir / "closed_trades.jsonl"
        EQUITY_FILE = state_dir / "equity_curve.jsonl"
        META_FILE = state_dir / "meta.json"
        CONFIG.update(config_overrides)
        # 回放语义：白名单不参与（回放评估的是参数，不是标的选择历史）
        CONFIG["ticker_whitelist"] = config_overrides.get("ticker_whitelist", [])

        for d in dates:
            run_for_date(d, verbose=False)

        equity = _load_jsonl(EQUITY_FILE)
        closed = _load_jsonl(CLOSED_FILE)
        return {
            "equity": equity,
            "closed": closed,
            "final_nav": equity[-1]["nav"] if equity else CONFIG["starting_capital"],
            "config": {k: (dict(v) if isinstance(v, dict) else v) for k, v in CONFIG.items()},
        }
    finally:
        POSITIONS_FILE, CLOSED_FILE, EQUITY_FILE, META_FILE = _orig_paths
        CONFIG.clear()
        CONFIG.update(_orig_config)
        _VOL_ANN_CACHE.clear()   # 沙盒里攒的 σ 不得漏进生产 run_for_date
        _REPLAY_MODE = False


# ══════════════════════════════════════════════════════════════════════════════
# KPI 计算
# ══════════════════════════════════════════════════════════════════════════════

def compute_kpis(as_of: Optional[str] = None) -> Dict:
    eq = _load_jsonl(EQUITY_FILE)
    if not eq:
        return {"nav": CONFIG["starting_capital"], "total_return_pct": 0.0, "n_trades": 0}

    eq_sorted = sorted(eq, key=lambda x: x["date"])
    if as_of:
        eq_sorted = [e for e in eq_sorted if e["date"] <= as_of]
    if not eq_sorted:
        return {"nav": CONFIG["starting_capital"], "total_return_pct": 0.0, "n_trades": 0}

    latest = eq_sorted[-1]
    start_nav = CONFIG["starting_capital"]
    total_ret = (latest["nav"] - start_nav) / start_nav * 100

    # 日度回报序列
    daily_rets = []
    for i in range(1, len(eq_sorted)):
        prev = eq_sorted[i - 1]["nav"]
        cur = eq_sorted[i]["nav"]
        if prev > 0:
            daily_rets.append((cur - prev) / prev * 100.0)  # 百分比形式，匹配 sharpe_ratio 期望

    # Sharpe（按交易日 ~252/年）
    sharpe = sharpe_ratio(daily_rets, periods_per_year=252) if daily_rets else 0.0
    if sharpe is None:
        sharpe = 0.0

    # Max Drawdown
    peak = eq_sorted[0]["nav"]
    mdd = 0.0
    for e in eq_sorted:
        peak = max(peak, e["nav"])
        dd = (e["nav"] - peak) / peak * 100
        mdd = min(mdd, dd)

    # 胜率
    closed = _load_jsonl(CLOSED_FILE)
    if as_of:
        closed = [c for c in closed if c["exit_date"] <= as_of]
    wins = sum(1 for c in closed if c["pnl_usd"] > 0)
    total = len(closed)

    # SPY 基准对比
    spy_start_date = eq_sorted[0]["date"]
    spy_end_date = eq_sorted[-1]["date"]
    # v0.45.50：None 而非 0.0。spy_ret=0 会让下方 alpha_pct = total_ret - 0
    # = **组合收益本身**，读作「同期 SPY 零涨跌，全部收益都是超额 alpha」。
    # 与 portfolio_backtest 里已修的同形缺陷（v0.45.42）在另一条 KPI 链上。
    spy_ret = None
    try:
        spy_ohlc = _fetch_ohlc("SPY", spy_start_date,
                                (datetime.strptime(spy_end_date, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d"))
        if spy_ohlc:
            _dates = sorted(spy_ohlc.keys())
            spy_start = spy_ohlc[_dates[0]]["Close"]
            spy_end = spy_ohlc[min(spy_end_date, _dates[-1])]["Close"] if spy_end_date in spy_ohlc else spy_ohlc[_dates[-1]]["Close"]
            spy_ret = (spy_end - spy_start) / spy_start * 100
    except Exception as _e_spy:
        _log.warning("compute_kpis: SPY 基准取数失败，spy_return_pct/alpha 置 None"
                     "（不以 0%% 冒充大盘零涨跌）：%s", _e_spy)
        spy_ret = None

    return {
        "nav": latest["nav"],
        "cash": latest["cash"],
        "deployed": latest["deployed"],
        "unrealized": latest["unrealized"],
        "total_return_pct": round(total_ret, 2),
        "spy_return_pct": (round(spy_ret, 2) if spy_ret is not None else None),
        "alpha_pct": (round(total_ret - spy_ret, 2) if spy_ret is not None else None),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(mdd, 2),
        "win_rate_pct": round(wins / total * 100, 1) if total else 0.0,
        "trades_total": total,
        "trades_wins": wins,
        "positions_count": latest.get("positions_count", 0),
        "days_running": len(eq_sorted),
        "starting_date": eq_sorted[0]["date"],
        "latest_date": latest["date"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# HTML 渲染
# ══════════════════════════════════════════════════════════════════════════════

def _render_sparkline_svg(nav_series: List[Tuple[str, float]], width: int = 320, height: int = 60) -> str:
    if len(nav_series) < 2:
        return ""
    vals = [v for _, v in nav_series]
    lo, hi = min(vals), max(vals)
    rng = hi - lo if hi > lo else 1
    pts = []
    for i, (_, v) in enumerate(nav_series):
        x = i / (len(nav_series) - 1) * width
        y = height - ((v - lo) / rng) * height
        pts.append(f"{x:.1f},{y:.1f}")
    color = "#10b981" if vals[-1] >= vals[0] else "#ef4444"
    path = "M" + " L".join(pts)
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'style="display:block;margin:8px 0;">'
            f'<path d="{path}" stroke="{color}" stroke-width="2" fill="none"/>'
            f'</svg>')


def _sizing_rule_text() -> str:
    """组合卡片「规则」行里描述当前生效的仓位算法（v0.45.100）。"""
    if CONFIG.get("sizing_mode", "tier") == "vol_target":
        vt = CONFIG["vol_target"]
        cm = vt["conf_multiplier"]
        # v0.45.104：曾写作「钳位 1.5–8.0% NAV」——那不是 NAV 边界。钳位作用在
        # 乘置信乘数**之前**的 target/σ 百分比上，mid 档（×0.6）实测只落
        # 0.90–4.8% NAV。卡片不能声称一个代码并不执行的区间。
        return (f'波动率目标仓位：单仓年化波动贡献 {vt["target_position_vol_pct"]}% NAV'
                f'（仓位 = 目标/σ₂₀，置信乘数前钳位 {vt["size_pct_min"]}–{vt["size_pct_max"]}%）'
                f' · 高置信 ×{cm["high"]} / 中置信 ×{cm["mid"]} · 无 σ 时退回分档')
    t = CONFIG["size_pct_by_tier"]
    return f'高置信 {t["high"]}% NAV / 中置信 {t["mid"]}%'


def render_portfolio_card() -> str:
    """渲染 $50k PaperPortfolio 卡片 HTML（插入到 CH0 或顶层）"""
    kpi = compute_kpis()
    positions = _load_jsonl(POSITIONS_FILE)
    closed = _load_jsonl(CLOSED_FILE)
    eq = _load_jsonl(EQUITY_FILE)

    if not eq:
        return (
            '<div style="margin:16px 0;padding:16px;background:var(--bg2);border-radius:10px;'
            'border:1px solid var(--border1);color:var(--text3);">'
            '📊 PaperPortfolio 尚未启动。运行 <code>python3 paper_portfolio.py bootstrap</code> 初始化。'
            '</div>'
        )

    # Sparkline
    nav_series = [(e["date"], e["nav"]) for e in sorted(eq, key=lambda x: x["date"])]
    sparkline = _render_sparkline_svg(nav_series)

    # KPI 色
    ret_col = "#10b981" if kpi["total_return_pct"] >= 0 else "#ef4444"
    # v0.45.50：alpha_pct 现在可为 None（SPY 基准不可得）
    _alpha_known = isinstance(kpi.get("alpha_pct"), (int, float))
    alpha_col = ("#6b7280" if not _alpha_known
                 else ("#10b981" if kpi["alpha_pct"] >= 0 else "#ef4444"))
    # ⚠️ 这两个必须**预先算好**：隐式字符串拼接链里放行内 if/else，
    # 三元会绑定到前面**整条链**而不是单个片段 —— 语法合法、静默出错。
    _spy_txt = (format(kpi["spy_return_pct"], "+.2f") + "%"
                if isinstance(kpi.get("spy_return_pct"), (int, float)) else "—")
    _alpha_txt = ("Alpha " + format(kpi["alpha_pct"], "+.2f") + "%"
                  if _alpha_known else "Alpha 不可用")

    # v0.45.104：降级可见性。Position.sizing 此前只落在 positions.jsonl 与
    # rationale 里，卡片一个字都不渲染——"降级必须可见"只对会去开 jsonl 的人
    # 成立。把退回分档的仓数摆到人真正会看的地方。
    _n_fb = sum(1 for p in positions
                if str(p.get("sizing", "")).startswith("tier_fallback"))
    _fallback_txt = (f'⚠️ 今日 {_n_fb}/{len(positions)} 仓退回分档（无可用 σ₂₀，'
                     f'按置信档位定仓）<br>' if _n_fb else '')

    # 持仓表格
    pos_rows = ""
    if positions:
        pos_details = []
        for p in positions:
            p_obj = Position(**p)
            ohlc = _fetch_ohlc(p_obj.ticker, p_obj.entry_date,
                                (datetime.strptime(kpi["latest_date"], "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d"))
            cur = p_obj.entry_price
            if kpi["latest_date"] in ohlc:
                cur = ohlc[kpi["latest_date"]]["Close"]
            elif ohlc:
                cur = ohlc[max(ohlc.keys())]["Close"]
            if p_obj.direction == "bullish":
                u_pct = (cur - p_obj.entry_price) / p_obj.entry_price * 100.0
            else:
                u_pct = (p_obj.entry_price - cur) / p_obj.entry_price * 100.0
            u_usd = p_obj.size_usd * (u_pct / 100.0)
            pos_details.append((p_obj, cur, u_pct, u_usd))

        for p_obj, cur, u_pct, u_usd in pos_details:
            pnl_col = "#10b981" if u_usd >= 0 else "#ef4444"
            dir_icon = "🟢" if p_obj.direction == "bullish" else "🔴"
            pos_rows += (
                f'<tr>'
                f'<td style="padding:4px 8px;font-weight:600;">{p_obj.ticker}</td>'
                f'<td style="padding:4px 8px;">{dir_icon} {"Long" if p_obj.direction == "bullish" else "Short"}</td>'
                f'<td style="padding:4px 8px;text-align:right;">${p_obj.entry_price:.2f}</td>'
                f'<td style="padding:4px 8px;text-align:right;">${cur:.2f}</td>'
                f'<td style="padding:4px 8px;text-align:right;color:{pnl_col};font-weight:600;">'
                f'${u_usd:+.2f} ({u_pct:+.1f}%)</td>'
                f'<td style="padding:4px 8px;text-align:right;font-size:11px;color:var(--text3);">'
                f'${p_obj.sl_price:.2f} / ${p_obj.tp_price:.2f}</td>'
                f'<td style="padding:4px 8px;text-align:right;font-size:11px;color:var(--text3);">'
                f'{p_obj.entry_date}</td>'
                f'</tr>'
            )
    else:
        pos_rows = '<tr><td colspan="7" style="padding:10px;text-align:center;color:var(--text3);">当前无持仓</td></tr>'

    # 近 5 笔平仓
    closed_sorted = sorted(closed, key=lambda x: x["exit_date"], reverse=True)[:5]
    closed_rows = ""
    for t in closed_sorted:
        pnl_col = "#10b981" if t["pnl_usd"] >= 0 else "#ef4444"
        mark = "✅" if t["pnl_usd"] > 0 else ("❌" if t["pnl_usd"] < 0 else "➖")
        reason_label = {"TP": "止盈", "SL": "止损", "TIME": "T+10 强平"}.get(t["exit_reason"], t["exit_reason"])
        closed_rows += (
            f'<div style="padding:4px 0;font-size:12px;color:var(--text2);">'
            f'{mark} <strong>{t["ticker"]}</strong> '
            f'{"Long" if t["direction"] == "bullish" else "Short"} · {reason_label} · '
            f'${t["entry_price"]:.2f} → ${t["exit_price"]:.2f} · '
            f'<span style="color:{pnl_col};font-weight:600;">${t["pnl_usd"]:+.2f}</span> '
            f'({t["net_return_pct"]:+.1f}%) · '
            f'{t["holding_days"]}d'
            f'</div>'
        )

    deployed_pct = kpi["deployed"] / kpi["nav"] * 100 if kpi["nav"] > 0 else 0
    _closed_block = closed_rows if closed_rows else '<div style="font-size:12px;color:var(--text3);">暂无平仓记录</div>'

    return (
        '<div style="margin:16px 0;padding:20px;background:linear-gradient(135deg,rgba(34,197,94,0.06),rgba(16,185,129,0.03));'
        'border:1px solid rgba(34,197,94,0.3);border-radius:12px;">'

        # 标题行
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">'
        '<div style="font-size:14px;font-weight:700;color:#22c55e;letter-spacing:.05em;">'
        '📊 $50,000 策略模拟组合 · 透明账户</div>'
        f'<div style="font-size:10px;color:var(--text3);font-style:italic;">'
        f'自 {kpi["starting_date"]} · {kpi["days_running"]} 天 · v0.19.0</div>'
        '</div>'

        # 核心 KPI
        '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px;">'
        f'<div><div style="font-size:11px;color:var(--text3);">组合净值</div>'
        f'<div style="font-size:20px;font-weight:700;color:var(--text1);">${kpi["nav"]:,.2f}</div>'
        f'<div style="font-size:12px;color:{ret_col};font-weight:600;">{kpi["total_return_pct"]:+.2f}%</div></div>'

        f'<div><div style="font-size:11px;color:var(--text3);">vs SPY</div>'
        f'<div style="font-size:20px;font-weight:700;color:var(--text1);">{_spy_txt}</div>'
        f'<div style="font-size:12px;color:{alpha_col};font-weight:600;">{_alpha_txt}</div></div>'

        f'<div><div style="font-size:11px;color:var(--text3);">Sharpe / MDD</div>'
        f'<div style="font-size:20px;font-weight:700;color:var(--text1);">{kpi["sharpe"]:.2f}</div>'
        f'<div style="font-size:12px;color:#ef4444;font-weight:600;">MDD {kpi["max_drawdown_pct"]:.1f}%</div></div>'

        f'<div><div style="font-size:11px;color:var(--text3);">胜率 / 仓位</div>'
        f'<div style="font-size:20px;font-weight:700;color:var(--text1);">{kpi["win_rate_pct"]:.1f}%</div>'
        f'<div style="font-size:12px;color:var(--text3);">'
        f'{kpi["trades_wins"]}/{kpi["trades_total"]} · 持 {kpi["positions_count"]}/{CONFIG["max_positions"]}</div></div>'
        '</div>'

        # Equity curve
        f'{sparkline}'

        # 资金利用
        f'<div style="font-size:11px;color:var(--text3);margin-bottom:12px;">'
        f'💰 现金 ${kpi["cash"]:,.2f} · 已部署 ${kpi["deployed"]:,.2f} ({deployed_pct:.1f}% NAV) · '
        f'未实现 ${kpi["unrealized"]:+,.2f}</div>'

        # 当前持仓
        '<div style="margin-bottom:12px;">'
        '<div style="font-size:11px;font-weight:700;color:var(--text3);margin-bottom:4px;">当前持仓</div>'
        '<table style="width:100%;font-size:12px;border-collapse:collapse;">'
        '<thead><tr style="border-bottom:1px solid var(--border2);color:var(--text3);">'
        '<th style="padding:4px 8px;text-align:left;">标的</th>'
        '<th style="padding:4px 8px;text-align:left;">方向</th>'
        '<th style="padding:4px 8px;text-align:right;">入场</th>'
        '<th style="padding:4px 8px;text-align:right;">当前</th>'
        '<th style="padding:4px 8px;text-align:right;">未实现</th>'
        '<th style="padding:4px 8px;text-align:right;">SL / TP</th>'
        '<th style="padding:4px 8px;text-align:right;">开仓日</th>'
        '</tr></thead>'
        f'<tbody>{pos_rows}</tbody></table>'
        '</div>'

        # 近 5 笔平仓
        '<div style="margin-bottom:8px;">'
        '<div style="font-size:11px;font-weight:700;color:var(--text3);margin-bottom:4px;">近 5 笔平仓</div>'
        f'{_closed_block}'
        '</div>'

        # 规则说明
        '<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border2);'
        'font-size:10px;color:var(--text3);line-height:1.6;">'
        f'{_fallback_txt}'
        f'⚙️ 规则：{_sizing_rule_text()} · '
        f'−{CONFIG["sl_pct"]}% SL / +{CONFIG["tp_pct"]}% TP / T+{CONFIG["time_stop_days"]} 强平 · '
        f'最大并行 {CONFIG["max_positions"]} 仓位 · 最大部署 {CONFIG["max_deployed_pct"]}% NAV<br>'
        '📎 含 trading_costs.py 成本（滑点 + 佣金 + 借券费）· 股票现货模拟 · 不含期权'
        '</div>'
        '</div>'
    )


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    p = argparse.ArgumentParser(description="Alpha Hive PaperPortfolio")
    p.add_argument("cmd", choices=["bootstrap", "run", "kpi", "card", "reset"],
                   help="bootstrap=回放历史 / run=跑单日 / kpi=打印KPI / card=渲染HTML / reset=清空")
    p.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    if args.cmd == "reset":
        for f in [POSITIONS_FILE, CLOSED_FILE, EQUITY_FILE, META_FILE]:
            if f.exists():
                f.unlink()
        print("🗑  已清空 PaperPortfolio 状态")
        return

    if args.cmd == "bootstrap":
        bootstrap_from_history(verbose=args.verbose)
        kpi = compute_kpis()
        print(f"\n✅ Bootstrap 完成")
        print(f"   NAV:    ${kpi['nav']:,.2f}  ({kpi['total_return_pct']:+.2f}%)")
        if isinstance(kpi.get("alpha_pct"), (int, float)):
            print(f"   vs SPY: {kpi['spy_return_pct']:+.2f}%  Alpha: {kpi['alpha_pct']:+.2f}%")
        else:
            print("   vs SPY: 不可用（基准取数失败）  Alpha: 不可用")
        print(f"   胜率:   {kpi['win_rate_pct']:.1f}% ({kpi['trades_wins']}/{kpi['trades_total']})")
        print(f"   Sharpe: {kpi['sharpe']:.2f}  MDD: {kpi['max_drawdown_pct']:.1f}%")
        return

    if args.cmd == "run":
        as_of = args.date or pdt_today()  # v0.28.0: 美股交易日
        res = run_for_date(as_of, verbose=args.verbose)
        print(f"\n📊 {as_of}  NAV=${res['nav']:,.2f}  持仓={len(res['positions'])}  "
              f"今日开仓={res['opened_today']}  已实现P&L=${res['realized_pnl_today']:+.2f}")
        return

    if args.cmd == "kpi":
        kpi = compute_kpis(args.date)
        print(json.dumps(kpi, ensure_ascii=False, indent=2))
        return

    if args.cmd == "card":
        html = render_portfolio_card()
        out = BASE_DIR / "paper_portfolio_card.html"
        out.write_text(f'<!DOCTYPE html><html><head><meta charset="utf-8">'
                       f'<style>:root{{--bg2:#1a1d2e;--bg3:#252840;--border1:#2e3348;--border2:#3a4055;'
                       f'--text1:#e2e8f0;--text2:#94a3b8;--text3:#64748b;--green2:#10b981;--red2:#ef4444;--gold2:#f59e0b;}}'
                       f'body{{background:#0f1119;color:#e2e8f0;font-family:system-ui,sans-serif;padding:20px;}}</style>'
                       f'</head><body>{html}</body></html>', encoding="utf-8")
        print(f"✅ 已渲染 → {out}")
        return


if __name__ == "__main__":
    main()
