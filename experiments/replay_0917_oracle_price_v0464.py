"""09-17 重算（v0.45.464，用户选「云端链整体重算」）：期权层 → OracleBee → Guard / Bear / Queen。

**为什么要重算。** 09-17 全源中断约 2 分钟，OracleBee 的 `_get_stock_data` 得 price=0.0，`OptionsAgent.analyze`
（v0.45.462 前）把现价兜底成行权价中位数，30/30 份快照的 GEX / 异动 / 偏斜 / 期限结构 / ATM IV 都按假价算，
进了 Oracle 分（odds 维）、Bear 的期权子分、final_score。v0.45.271 那次重放复用了存档的 Oracle 输出，假价还在。

**为什么用云端链。** 生产 21:35Z 取的那份 CBOE 链取不回（CBOE 收盘后 payload 持续漂移：AMC 总 OI 21:36→21:53
从 794k 掉到 697k）；`origin/cloud-snapshots` 有 21:03Z 的同场 CBOE 链（30/30，price_at_fetch = 官方收盘）。
用户选择用它整体重算：每个数都来自一次真实计算，代价是也带进了链的时点差（control 里 7 只 options_score 本就不同）。

**用当天的代码。** 生产 09-17 跑的是 `2bcd81a0`（21:00:14Z production_sync up_to_date；与当晚日报提交 1d36076d 无 .py 差异；
到 v0.45.271 重放所用代码只差 scout_bee.py，Scout 在本重放里原样复用）。本脚本必须在该提交的检出里跑：

    git worktree add --detach <dir> 2bcd81a0
    ALPHA_HIVE_HOME=<沙箱> python3 experiments/replay_0917_oracle_price_v0464.py <dir> <沙箱> <out.json> ~/alpha-hive-data <开发仓库>

**只读生产。** 存档从 `<prod>` 读；运行时数据根是沙箱、快照写入关闭；钟钉在 09-17 14:20:26 PDT（analyze 用 now() 判 DTE）。

**钉成存档值的输入（与价无关、取不回）：** iv_rank / iv_percentile（那天是 hv_proxy，与价无关）、RV30（Twelve Data
`realized_vol` 钉成存档 rv_30d，价差由真函数按新 IV 算）、quote_set（与价无关，沿用存档）、OracleBee 的异常期权流
（yfinance 现拉、另取价、未存档：方向取自存档 discovery 的摘要，unusual_score 由存档 Oracle 分反解——融合式是线性的，
control 必须逐位复现存档分，否则停下）、历史上下文 ctx（从存档 discovery 截出）、Guard 的宏观 / 周期 / 政体 / 拥挤调整
（同 v0.45.271）。

**control 判据（不过就不信 corrected）：**
1. Oracle：用存档期权结果 + 反解的异常流跑真 OracleBee，score / direction / discovery 必须与存档逐位相同；
2. Bear 期权子分：用存档 Oracle 板条目跑真 `_assess_options_puts`，必须等于存档 options_bear（那天读到板的标的）；
3. 整链：存档 Oracle 进板，Guard / Bear / Queen 真跑，final_score / direction / dimension_weights 与存档比
   （v0.45.271 已知残差：Guard / Bear 个别 0.01~0.08，如实报告）。
"""
import copy
import json
import os
import re
import sys
from datetime import datetime as _RealDT, timezone as _tz
from unittest.mock import patch

OLD, SANDBOX, OUT, PROD, DEV_REPO = sys.argv[1:6]
ONLY = sys.argv[6:]
sys.path.insert(0, OLD)
os.environ["ALPHA_HIVE_HOME"] = SANDBOX
os.environ["ALPHA_HIVE_CACHE_DIR"] = os.path.join(SANDBOX, "cache")
os.environ["OPTIONS_SNAPSHOT_DISABLE"] = "1"
os.makedirs(os.path.join(SANDBOX, "cache"), exist_ok=True)
os.chdir(SANDBOX)
# Queen 的 ML 调整按 ml_model_cache.json 的 OOS 精度缩放（<50% ⇒ 0）。沙箱里没有这个文件时按 1.0 算——
# 当天的是 46.3%（备份仓 ml_model_history/ml_model_cache-2026-09-17.json），不放进来 control 就差 0.06~0.09。
import subprocess as _sp  # noqa: E402
with open(os.path.join(SANDBOX, "ml_model_cache.json"), "wb") as _f:
    _f.write(_sp.run(["git", "-C", os.path.join(PROD, "_git_backup"), "show",
                      "HEAD:ml_model_history/ml_model_cache-2026-09-17.json"],
                     check=True, capture_output=True).stdout)
AS_OF = "2026-09-17"
FROZEN_LOCAL = _RealDT(2026, 9, 17, 14, 20, 26)
FROZEN_UTC = _RealDT(2026, 9, 17, 21, 20, 26, tzinfo=_tz.utc)


class _FrozenDT(_RealDT):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_LOCAL if tz is None else FROZEN_UTC.astimezone(tz)


import cloud_snapshot_loader as csl  # noqa: E402
import options_analyzer as oa  # noqa: E402
import market_intelligence as mi  # noqa: E402
import cboe_options as co  # noqa: E402
import twelve_data as td  # noqa: E402
import unusual_options  # noqa: E402
import llm_service  # noqa: E402
import real_data_sources  # noqa: E402
import advanced_analyzer  # noqa: E402
from crowding_detector import CrowdingDetector  # noqa: E402
from pheromone_board import PheromoneBoard, PheromoneEntry  # noqa: E402
from swarm_agents.oracle_bee import OracleBeeEcho  # noqa: E402
from swarm_agents.guard_bee import GuardBeeSentinel  # noqa: E402
from swarm_agents.bear_bee import BearBeeContrarian  # noqa: E402
from swarm_agents.queen_distiller import QueenDistiller  # noqa: E402
from swarm_agents._config import _AS  # noqa: E402
from swarm_agents.utils import clamp_score  # noqa: E402

for _m in (oa, mi, co):
    if hasattr(_m, "datetime"):
        _m.datetime = _FrozenDT

SWARM = json.load(open(os.path.join(PROD, f".swarm_results_{AS_OF}.json")))
_DEEP_KEYS = ("term_structure", "deep_skew", "max_pain")


# ─────────────────────────────────────────────────────────────── 期权层

def corrected_options(tk, det, real_px):
    """云端链 + 真实收盘价，跑当天的 analyze()；与价无关的输入钉成存档值。"""
    agent = oa.OptionsAgent()
    agent.fetcher.cache_dir = os.path.join(SANDBOX, "cache", "corrected")
    os.makedirs(agent.fetcher.cache_dir, exist_ok=True)
    agent.fetcher.fetch_historical_hv = lambda t, days=252: [20.0 + i * 0.1 for i in range(60)]
    agent.fetcher.hist_hv_untrusted = lambda: det.get("iv_rank") is None
    agent.analyzer.calculate_iv_rank = lambda cur, hist: (det.get("iv_rank"), cur)
    agent.analyzer.calculate_iv_percentile = lambda cur, hist: det.get("iv_percentile")
    agent.fetcher._read_last_valid_iv = lambda t: None
    agent.fetcher._save_last_valid_iv = lambda t, iv: None
    rv = det.get("rv_30d")
    with patch.object(td, "is_configured", lambda: rv is not None), \
         patch.object(td, "realized_vol", lambda *a, **k: rv):
        r = agent.analyze(tk, stock_price=real_px)
    if rv is None:   # 那天 RV 就不可得：照存档（不去 yfinance 现拉）
        for k in ("rv_30d", "iv_rv_spread", "iv_rv_signal", "iv_rv_detail"):
            r[k] = copy.deepcopy(det.get(k))
    r["quote_set"] = copy.deepcopy(det.get("quote_set"))   # 与价无关；快照模式下取不到
    return r


# ─────────────────────────────────────────────────────────────── OracleBee

def _skew_post(score0, skew):
    if skew is None:
        return score0
    if skew > 1.3:
        return clamp_score(score0 - min(1.0, (skew - 1.3) * 2.5))
    if skew < 0.8:
        return clamp_score(score0 + min(0.5, (0.8 - skew) * 1.5))
    return score0


def unusual_flow_from_archive(o):
    """存档 discovery 的「异常流:…」给方向；unusual_score 由存档 Oracle 分反解（融合式线性）。"""
    det = o["details"]
    m = re.search(r"异常流:([^|]+)", o["discovery"])
    summary = m.group(1).strip()
    direction = ("bullish" if summary.startswith("异常Call流") else
                 "bearish" if summary.startswith("异常Put流") else "neutral")
    ow, uw = _AS.get("oracle_options_weight", 0.55), _AS.get("oracle_unusual_weight", 0.10)
    od = max(0.0, min(10.0, (det.get("options_score") or 5.0)
                      + (det.get("term_structure") or {}).get("term_score_adj", 0)
                      + (det.get("deep_skew") or {}).get("skew_score_adj", 0)))
    target = o["score"]

    def fwd(u):
        s0 = clamp_score((od * ow + u * uw) / (ow + uw) + (u - 5.0) * 0.3)
        return round(_skew_post(s0, det.get("iv_skew_ratio")), 2)

    # 线性部分反解 + 两位小数网格逐位核（unusual_options 的分本身是 round(·, 2)）
    k = uw / (ow + uw) + 0.3
    hits = [u / 100 for u in range(100, 1001) if fwd(u / 100) == target]
    if not hits:
        raise AssertionError(f"{o['details'].get('ticker')} unusual_score 反解不出（target {target}）")
    u = min(hits, key=lambda x: abs(x - (5.0 + (target - od * ow / (ow + uw)) / k)))
    return {"unusual_score": u, "unusual_direction": direction, "summary": summary,
            "signals": [{"type": "unusual_flow_unrecoverable"}], "data_source": "yfinance_chain",
            "_u_candidates": len(hits)}


def run_oracle(tk, result, uf, ctx):
    board = PheromoneBoard()
    ob = OracleBeeEcho(board)
    ob._prefetched_context = {tk: ctx}
    ob._prefetched_stock = {tk: {"price": 0.0}}   # 09-17 那天 OracleBee 拿到的就是 0.0（全源中断）
    with patch.object(oa.OptionsAgent, "analyze", lambda self, t, stock_price=None, **k: copy.deepcopy(result)), \
         patch.object(unusual_options, "detect_unusual_flow", lambda t, stock_price=0.0: dict(uf)), \
         patch.object(llm_service, "is_available", lambda: False):
        out = ob.analyze(tk)
    return out, board


def oracle_board_details(board, tk):
    ents = [e for e in board._entries if e.ticker == tk]
    assert len(ents) == 1, f"{tk}: Oracle 应恰好发布一条，实得 {len(ents)}"
    return copy.deepcopy(ents[0].details)


# ─────────────────────────────────────────────────────────────── 整链（同 v0.45.271 方法）

# v0.45.271 的四个助手（窄字典重建 / 发布 / Guard 宏观存档 / Bear 逐子项重算）。不 import 那个文件：它在模块级读
# 已不存在的 scratchpad 文件、并把开发检出插到 sys.path 最前（会混进现行代码）。用 AST 只取这四个函数，跑在当天代码上。
import ast as _ast  # noqa: E402
_R_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replay_swarm_sequence_20260917.py")
_R_KEEP = {"reconstruct_pub_details", "publish", "guard_archived_macro", "build_bear_result"}
_r_tree = _ast.parse(open(_R_SRC, encoding="utf-8").read())
_r_defs = [n for n in _r_tree.body if isinstance(n, _ast.FunctionDef) and n.name in _R_KEEP]
assert {n.name for n in _r_defs} == _R_KEEP
_R_NS = {"SWARM": SWARM, "copy": copy, "json": json, "PheromoneBoard": PheromoneBoard,
         "PheromoneEntry": PheromoneEntry, "BearBeeContrarian": BearBeeContrarian, "_AS": _AS}
exec(compile(_ast.Module(body=_r_defs, type_ignores=[]), _R_SRC, "exec"), _R_NS)


class R:   # 与旧脚本同名调用
    publish = staticmethod(_R_NS["publish"])
    guard_archived_macro = staticmethod(_R_NS["guard_archived_macro"])
    build_bear_result = staticmethod(_R_NS["build_bear_result"])


def static(tk, name):
    rec = SWARM[tk]
    d = copy.deepcopy(rec["agent_details"][name])
    d["source"] = name
    d["data_quality"] = (rec.get("data_quality") or {}).get(name, {}) or {}
    if d.get("error") is None:
        d.pop("error", None)
    return d


def run_chain(tk, oracle_res, oracle_pub):
    rec = SWARM[tk]
    price = 0.0
    buzz = static(tk, "BuzzBeeWhisper")
    others = [static(tk, n) for n in ("ScoutBeeNova", "ChronosBeeHorizon", "RivalBeeVanguard")]
    code = static(tk, "CodeExecutorAgent") if "CodeExecutorAgent" in rec["agent_details"] else None
    board = PheromoneBoard()
    for d in [others[0]] + [None] + [buzz] + others[1:] + ([code] if code else []):
        if d is None:   # Oracle：发布它自己真算出的窄字典（不是从宽字典重建）
            board.publish(PheromoneEntry(agent_id="OracleBeeEcho", ticker=tk, discovery=oracle_res["discovery"],
                                         source="OracleBeeEcho", self_score=oracle_res["score"],
                                         direction=oracle_res["direction"], details=oracle_pub))
        else:
            R.publish(board, d["source"], tk, d)
    bd = buzz["details"]
    stock = {"price": price, "momentum_5d": bd.get("momentum_5d"), "volume_ratio": bd.get("volume_ratio"),
             "volatility_20d": None, "avg_volume": None,
             "_buzz_details_for_bear": {"sentiment_pct": bd.get("sentiment_pct"), "direction": buzz["direction"],
                                        "score": buzz["score"], "reddit_momentum": bd.get("reddit_momentum")}}
    g = GuardBeeSentinel(board)
    g._prefetched_stock = {tk: dict(stock)}
    g._prefetched_market = {"info": {tk: {"trailingPE": None}}}
    gd = rec["agent_details"]["GuardBeeSentinel"]["details"]
    with patch.object(GuardBeeSentinel, "_calc_macro_adjustment", lambda self, t: R.guard_archived_macro(t)), \
         patch.object(mi, "get_cycle_context", lambda *a, **k: gd["cycle_context"]), \
         patch.object(mi, "detect_market_regime", lambda *a, **k: gd["market_regime"]), \
         patch.object(llm_service, "is_available", lambda: False), \
         patch.object(real_data_sources, "get_real_crowding_metrics",
                      lambda *a, **k: {"social_messages_per_day": 0, "google_trends_percentile": 50,
                                       "bullish_agents": 0, "seeking_alpha_page_views": 0,
                                       "short_float_ratio": 0, "price_momentum_5d": 0, "data_quality": {}}), \
         patch.object(CrowdingDetector, "get_adjustment_factor", lambda self, c: gd["adjustment_factor"]):
        guard = g.analyze(tk)
    R.publish(board, "GuardBeeSentinel", tk, guard)

    # Bear：先按 271 的方法算（insider / news / guard / valuation / momentum / short_int 与文案），再补两处：
    # ① 期权子项：真 `_assess_options_puts` 读本板 Oracle 条目（那天读到板的标的；API 兜底那条只看 P/C 与
    #    IV Rank，与价无关，照存档）；
    # ② 催化剂 / ML 子项：存档 details 不存 chronos_bear / ml_bear，271 当 0 算——META / QCOM / TSLA 因此
    #    差 0.15~0.23（271 记的「未定位残差」）。两者只读板上 Chronos / Rival 的窄字典（与价无关），用真函数重算。
    bear = R.build_bear_result(tk, price, stock, guard, None)
    arch_bear = rec["agent_details"]["BearBeeContrarian"]["details"]
    ds_arch = arch_bear.get("data_sources") or {}
    b = BearBeeContrarian(board)
    _scratch, _ds = [], {}
    chronos_bear = b._assess_catalyst_risk(tk, _scratch, _ds) if ds_arch.get("catalyst") == "real" else 0.0
    ml_bear = b._assess_ml_prediction(tk, _scratch, _ds) if ds_arch.get("ml") == "real" else 0.0
    if os.environ.get("REPLAY_BEAR_271_COMPAT") == "1":   # 只供核对：复刻 271 把两项当 0 的做法，看 control 是否逐位回到存档
        chronos_bear = ml_bear = 0.0
    opt_recalc = None
    sigs_all = list(bear["details"]["bearish_signals"])
    options_bear = arch_bear["options_bear"]
    if ds_arch.get("options") == "real":
        osigs, ods = [], {}
        options_bear, _ = b._assess_options_puts(tk, price, osigs, ods)
        # 读板失败会无声落到 API 兜底——那条在当天代码里会再调 analyze()（假价兜底还在），必须停下
        assert ods.get("options") == "real", f"{tk}: Bear 没读到本板的 Oracle 条目（{ods}）"
        opt_recalc = {"options_bear": options_bear, "signals": osigs}
        _opt_prefix = ("P/C Ratio", "IV Rank", "IV Skew", "GEX 负值", "Oracle 期权信号看空")
        _ins_prefix = ("内幕大额抛售", "内幕卖多买少", "内幕净卖出", "内幕人净卖出", "Scout 内幕信号看空")
        rest = [x for x in sigs_all if not x.startswith(_opt_prefix)]
        n_ins = sum(1 for x in rest if x.startswith(_ins_prefix))
        sigs_all = rest[:n_ins] + osigs + rest[n_ins:]
    dims = {"insider": arch_bear["insider_bear"], "valuation": bear["details"]["overval_bear"],
            "options": options_bear, "momentum": bear["details"]["momentum_bear"],
            "news": bear["details"]["news_bear"], "chronos": chronos_bear, "ml": ml_bear,
            "guard": bear["details"].get("guard_bear", 0.0), "short_int": arch_bear.get("short_int_bear", 0.0)}
    if "guard_bear" not in bear["details"]:   # 271 的结果 dict 不带 guard_bear：按它同样的规则从本次 Guard 结果取
        _gdet = guard.get("details", {})
        _c = _gdet.get("consistency")
        _g = 5.5 if (ds_arch.get("guard") == "real" and _c is not None and _c < 0.4) else 0.0
        if ds_arch.get("guard") == "real" and _gdet.get("llm_conflict_type", "") in ("major_conflict", "direction_conflict"):
            _g = max(_g, 6.0)
        dims["guard"] = _g if ds_arch.get("guard") == "real" else 0.0
    mom_eff = stock["momentum_5d"] if stock["momentum_5d"] is not None else 0
    rule = BearBeeContrarian(PheromoneBoard())._compute_bear_score(dims, sigs_all, price, mom_eff)
    try:
        from config import BEAR_SCORING_CONFIG as _B
    except ImportError:
        _B = {}
    bear["score"] = round(clamp_score(10.0 - rule), 2)
    bear["direction"] = ("bearish" if rule >= _B.get("direction_bearish_min", 5.5) else
                         "neutral" if rule >= _B.get("direction_neutral_min", 3.5) else "bullish")
    bear["discovery"] = " | ".join(sigs_all[:6]) if sigs_all else "未发现显著看空信号"
    conf = min(1.0, 0.3 + len(sigs_all) * 0.1)
    bear["confidence"] = round(min(1.0, conf + sum(1 for v in ds_arch.values() if v == "real") * 0.1), 2)
    bear["details"].update(bear_score=round(rule, 2), rule_bear_score=round(rule, 2),
                           bearish_signals=sigs_all, options_bear=options_bear,
                           chronos_bear=chronos_bear, ml_bear=ml_bear, guard_bear=dims["guard"])
    R.publish(board, "BearBeeContrarian", tk, bear)

    results = [others[0], oracle_res, buzz, others[1], others[2], guard, bear] + ([code] if code else [])
    q = QueenDistiller(board, enable_llm=False)
    q.ml_adjustments = dict(rec["ml_weight_adjustments"])
    q.ml_feedback_enabled = True
    try:
        from config import ML_FEEDBACK_CONFIG as _MFC
    except ImportError:
        _MFC = {}
    if _MFC.get("enable_dimension_weighting", True):
        for _d, _f in q.ml_adjustments.items():
            if _d in q.DIMENSION_WEIGHTS:
                q.DIMENSION_WEIGHTS[_d] *= _f
        _t = sum(q.DIMENSION_WEIGHTS.values())
        if _t > 0:
            q.DIMENSION_WEIGHTS = {k: round(v / _t, 4) for k, v in q.DIMENSION_WEIGHTS.items()}
    with patch.object(advanced_analyzer.DealerGEXAnalyzer, "analyze", lambda self, t, px: {}):
        dist = q.distill(tk, results, dealer_gex={})
    return {"guard": guard, "bear": bear, "bear_options_recalc": opt_recalc, "distilled": dist}


# ─────────────────────────────────────────────────────────────── 主流程

out = {}
with csl.snapshot_mode(AS_OF, repo=DEV_REPO):
    for tk in sorted(SWARM):
        if ONLY and tk not in ONLY:
            continue
        rec = SWARM[tk]
        o = rec["agent_details"]["OracleBeeEcho"]
        det = o["details"]
        snap = csl.load_ticker(AS_OF, tk, repo=DEV_REPO)
        real_px = snap["price_at_fetch"]
        ctx = o["discovery"].split(" | $0.0 | ", 1)[1] if " | $0.0 | " in o["discovery"] else ""
        _sr = det.get("iv_skew_ratio")
        if _sr is not None and (_sr > 1.3 or _sr < 0.8):   # Oracle 在 append_context 之后才拼「| Skew x(sig)」
            _tail = f" | Skew {_sr:.2f}({det.get('iv_skew_signal', '')})"
            assert ctx.endswith(_tail), f"{tk}: ctx 尾部不是预期的 Skew 段：{ctx[-80:]!r}"
            ctx = ctx[:-len(_tail)]
        uf = unusual_flow_from_archive(o)
        res_arch = {k: v for k, v in det.items() if k not in _DEEP_KEYS}
        res_new = corrected_options(tk, det, real_px)

        ora_ctrl, b_ctrl = run_oracle(tk, res_arch, uf, ctx)
        ora_new, b_new = run_oracle(tk, res_new, uf, ctx)
        pub_ctrl, pub_new = oracle_board_details(b_ctrl, tk), oracle_board_details(b_new, tk)

        chain_ctrl = run_chain(tk, static(tk, "OracleBeeEcho"), pub_ctrl)
        chain_new = run_chain(tk, {**ora_new, "source": "OracleBeeEcho",
                                   "data_quality": (rec.get("data_quality") or {}).get("OracleBeeEcho", {})},
                              pub_new)
        out[tk] = {"real_price": real_px, "fake_price": det.get("_snapshot_stock_price"),
                   "unusual_flow": uf, "options_corrected": res_new,
                   "oracle_control": ora_ctrl, "oracle_corrected": ora_new,
                   "oracle_pub_control": pub_ctrl, "oracle_pub_corrected": pub_new,
                   "chain_control": chain_ctrl, "chain_corrected": chain_new}
        print(tk, "ok", flush=True)

json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1, default=str)
print("done", len(out))
