"""09-17 重算结果写回（v0.45.464，用户批准「全部数据 + 重生成 09-17 日报」）。两段式：

  compute（**当天代码** 2bcd81a0 的检出里跑，只读生产）：
      python3 experiments/patch_0917_oracle_price_v0464.py compute <旧代码检出> <replay.json> <prod> <plan.json>
  apply（**现行代码**里跑：ledger_io 原子写、一个事务改库、写前先备份、逐项核对写前值）：
      python3 experiments/patch_0917_oracle_price_v0464.py apply <plan.json> <prod> <backup_dir> [--dry-run]

replay.json 由 `replay_0917_oracle_price_v0464.py` 产出（control 已逐位复现存档，见该文件）。

写回范围（每项写前值都记进 plan，apply 时逐项核对没被别人改过，否则整体中止、什么都不写）：
- `.swarm_results_2026-09-17.json`：30 只整条替换为重算的 distill() 结果（Oracle / Guard / Bear 三只蜂 + 全部顶层字段），
  Oracle details 补回快照元字段（现价改真价并注出处），cs_rank 与 thesis_break 求值明细照 v0.45.271 的办法重算；
- `alpha-hive-daily-2026-09-17.md` / `.json`（markdown_report / opportunities / twitter_threads）：用当天的真实生成函数重出，
  「📌 关注事项」段换回原文（`datetime.now()` 会把距今天数算歪，同 271）；
- `pheromone.db`：`predictions` 09-17 的 final_score / direction / dimension_scores / agent_directions，以及 pheromone_compact
  里 Oracle / Bear 条目的 s / d / x（p / c 是那张板自己的，不动）；`signal_archive` 09-17 **已有的行**里值变了的那些
  （按当天的提取器从重算后的 swarm_results 抽；当天没归档过的信号不新增）；
- `report_snapshots/{T}_2026-09-17.json`：composite_score / direction / low_conviction / agent_votes / cs_rank；
- `cache/options_snapshot_{T}_2026-09-17.json`：重算的期权结果 + 原快照元字段 + 真现价与出处 + 重算出处；
- `cache/iv_history_{T}.jsonl` 的 09-17 行：改成重算的 iv_raw_observed（算不出 ⇒ 删该行）；
- `vrp_state/vrp_signals.jsonl` 的 09-17 行（30 行均未结算）：iv / vrp_ex_ante / underlying_price。
不动：ML 深度报告（叙事体裁，同 271）、今天的 index.html、纸面组合（CRM / RKLB 重算后分数不变，照样会开）。
"""
import copy
import hashlib
import json
import os
import re
import sys

AS_OF = "2026-09-17"
DB_REL = "pheromone.db"


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ═══════════════════════════════════════════════════════════════ compute（当天代码）

def compute(old, replay_path, prod, plan_path):
    sys.path.insert(0, old)
    os.environ["ALPHA_HIVE_HOME"] = os.path.join(os.path.dirname(plan_path), "compute_sandbox")
    os.makedirs(os.environ["ALPHA_HIVE_HOME"], exist_ok=True)
    import logging
    logging.disable(logging.CRITICAL)
    import sqlite3
    import report_formatters as rf
    import signal_archive as sa
    from market_intelligence import check_thesis_breaks
    from thesis_breaks import ThesisBreakConfig
    from alpha_hive_daily_report import AlphaHiveDailyReporter, build_agent_votes
    from datetime import datetime

    R = json.load(open(replay_path))
    swarm_path = os.path.join(prod, f".swarm_results_{AS_OF}.json")
    S = json.load(open(swarm_path))
    assert sorted(R) == sorted(S), "重放结果与存档标的集合不一致"
    plan = {"as_of": AS_OF, "files": {}, "sha_before": {}, "predictions": [], "signal_archive": [],
            "notes": {}}

    def _track(path):
        plan["sha_before"][path] = _sha(path) if os.path.exists(path) else None

    # ── 1. swarm_results ───────────────────────────────────────────────────
    P = {}
    for tk in sorted(S):
        base = copy.deepcopy(S[tk])
        dist = copy.deepcopy(R[tk]["chain_corrected"]["distilled"])
        new = {**base, **dist}
        od_old = S[tk]["agent_details"]["OracleBeeEcho"]["details"]
        od = new["agent_details"]["OracleBeeEcho"]["details"]
        for k in ("_snapshot_timestamp", "_snapshot_session", "_snapshot_session_complete", "_snapshot_ticker"):
            if k in od_old:
                od[k] = od_old[k]
        od["_snapshot_stock_price"] = R[tk]["real_price"]
        od["_snapshot_stock_price_source"] = "cboe_close"
        new["_recomputed_v0_45_464"] = {
            "why": "09-17 OracleBee 现价 0.0 ⇒ analyze() 兜底成行权价中位数（假现价）；用真实收盘价重算",
            "chain": "origin/cloud-snapshots 2026-09-17（CBOE 21:03Z，生产 21:35Z 那份取不回）",
            "code": "2bcd81a0（当天生产代码）", "fake_price": R[tk]["fake_price"], "real_price": R[tk]["real_price"],
            "final_score_before": S[tk]["final_score"], "oracle_score_before": S[tk]["agent_details"]["OracleBeeEcho"]["score"],
            "bear_score_before": S[tk]["agent_details"]["BearBeeContrarian"]["score"],
        }
        P[tk] = new

    def _cs_percentile(values):   # 逐字照 v0.45.271（= alpha_hive_daily_report._post_scan_enrichment）
        items = [(t, v) for t, v in values.items() if v is not None]
        n = len(items)
        if n < 2:
            return {t: 0.5 for t, _ in items}
        order = sorted(items, key=lambda x: x[1])
        return {t: round(i / (n - 1), 3) for i, (t, _) in enumerate(order)}

    _cs_final = _cs_percentile({t: r.get("final_score") for t, r in P.items()})
    _dims = ("signal", "catalyst", "sentiment", "odds", "risk_adj")
    _cs_dims = {d: _cs_percentile({t: (r.get("dimension_scores") or {}).get(d) for t, r in P.items()}) for d in _dims}
    for t, r in P.items():
        r["cs_rank"] = {"final": _cs_final.get(t), **{d: _cs_dims[d].get(t) for d in _dims}}

    for tk, row in P.items():   # thesis_break：只刷新求值明细，不改 l1 / l2 文本（同 271）
        try:
            cfg = ThesisBreakConfig.get_breaks_config(tk)
        except Exception:
            cfg = None
        if not cfg:
            continue
        det = row["agent_details"]["OracleBeeEcho"]["details"]
        bear = row["agent_details"]["BearBeeContrarian"]["details"].get("bearish_signals") or []
        res = check_thesis_breaks(tk, det.get("_snapshot_stock_price"), det.get("iv_current"),
                                  det.get("put_call_ratio"), list(bear), row.get("final_score"))
        row["thesis_break_evaluations"] = res.get("evaluations") or []

    _track(swarm_path)
    plan["files"][swarm_path] = {"kind": "json", "content": P}

    # ── 2. 日报 md / json ─────────────────────────────────────────────────
    md_path = os.path.join(prod, f"alpha-hive-daily-{AS_OF}.md")
    dj_path = os.path.join(prod, f"alpha-hive-daily-{AS_OF}.json")
    daily = json.load(open(dj_path))
    old_md = open(md_path, encoding="utf-8").read()

    class _Stub:
        date_str = AS_OF
        timestamp = datetime.strptime("2026-09-17 14:10:38", "%Y-%m-%d %H:%M:%S")

    new_md = rf.generate_swarm_markdown_report(
        _Stub(), P, concentration=daily.get("concentration_analysis"), macro_context=daily.get("macro_context"),
        backtest_stats=daily.get("backtest_stats"), agent_count=8, cross_ticker=daily.get("cross_ticker_analysis"))
    _sec = r"\n## 📌 关注事项.*?(?=\n## 6\) )"
    o_sec, n_sec = re.search(_sec, old_md, re.S), re.search(_sec, new_md, re.S)
    assert o_sec and n_sec, "📌 关注事项段落定位失败——不猜，停下"
    new_md = new_md[:n_sec.start()] + o_sec.group(0) + new_md[n_sec.end():]
    sorted_results = sorted(P.items(), key=lambda x: x[1]["final_score"], reverse=True)
    opp_items = AlphaHiveDailyReporter._build_opportunity_items(None, sorted_results)
    opps = []
    for i, opp in enumerate(opp_items):
        o = {"rank": i + 1, "ticker": opp.ticker, "direction": opp.direction,
             "opp_score": round(opp.opportunity_score, 1), "confidence": f"{opp.confidence:.0f}%",
             "resonance": P[opp.ticker]["resonance"]["resonance_detected"],
             "supporting_agents": P[opp.ticker]["supporting_agents"], "thesis_break": opp.thesis_break}
        for k in ("thesis_break_l1", "thesis_break_l2"):
            if k in P[opp.ticker]:
                o[k] = P[opp.ticker][k]
        opps.append(o)
    daily_new = dict(daily)
    daily_new.update(markdown_report=new_md, opportunities=opps,
                     twitter_threads=rf.generate_swarm_twitter_threads(_Stub(), P))
    for p_ in (md_path, dj_path):
        _track(p_)
    plan["files"][md_path] = {"kind": "text", "content": new_md}
    plan["files"][dj_path] = {"kind": "json", "content": daily_new}

    # ── 3. predictions / signal_archive（只记写前值与新值，apply 时核对）─────────
    con = sqlite3.connect(f"file:{os.path.join(prod, DB_REL)}?mode=ro", uri=True)
    for tk, row in P.items():
        rid, fs, dr, dims, adirs, pc = con.execute(
            "SELECT id, final_score, direction, dimension_scores, agent_directions, pheromone_compact "
            "FROM predictions WHERE date=? AND ticker=?", (AS_OF, tk)).fetchone()
        assert (fs, dr) == (S[tk]["final_score"], S[tk]["direction"]), f"{tk}: predictions 与存档 swarm_results 不一致，停下"
        new_pc = json.loads(pc) if pc else None
        ncomp = {e["a"]: e for e in (row.get("pheromone_compact") or [])}
        for e in (new_pc or []):
            if e.get("a") in ("OracleBe", "BearBeeC") and e["a"] in ncomp:
                for k in ("s", "d", "x"):
                    if k in ncomp[e["a"]]:
                        e[k] = ncomp[e["a"]][k]
                    elif k in e:
                        del e[k]
        plan["predictions"].append({
            "id": rid, "ticker": tk,
            "old": {"final_score": fs, "direction": dr, "dimension_scores": dims, "agent_directions": adirs,
                    "pheromone_compact": pc},
            "new": {"final_score": row["final_score"], "direction": row["direction"],
                    "dimension_scores": json.dumps(row["dimension_scores"], ensure_ascii=False),
                    "agent_directions": json.dumps(row["agent_directions"], ensure_ascii=False),
                    "pheromone_compact": json.dumps(new_pc, ensure_ascii=False) if new_pc is not None else pc}})
    existing = {(t, s): v for t, s, v in con.execute(
        "SELECT ticker, signal, value FROM signal_archive WHERE date=?", (AS_OF,))}
    new_rows = {(t, s): v for (_d, t, s, v) in sa._rows_for(P, AS_OF)}
    plan["notes"]["signal_archive_new_signals_not_inserted"] = sorted(f"{t}:{s}" for t, s in set(new_rows) - set(existing))
    plan["notes"]["signal_archive_existing_not_produced_kept"] = sorted(f"{t}:{s}" for t, s in set(existing) - set(new_rows))
    for k in sorted(set(existing) & set(new_rows)):
        if existing[k] is None or abs(existing[k] - new_rows[k]) > 1e-9:
            plan["signal_archive"].append({"ticker": k[0], "signal": k[1], "old": existing[k], "new": new_rows[k]})
    con.close()

    # ── 4. report_snapshots ───────────────────────────────────────────────
    try:
        from config import SCORE_HIGH_GUARD as _SHG
    except ImportError:
        _SHG = None
    for tk, row in P.items():
        p_ = os.path.join(prod, "report_snapshots", f"{tk}_{AS_OF}.json")
        if not os.path.exists(p_):
            continue
        snap = json.load(open(p_))
        snap["composite_score"] = row.get("final_score", 0.0)
        snap["direction"] = row.get("direction", "Neutral")
        lc, lcr = False, ""
        _dim = row.get("dimension_scores", {}) or {}
        _sent, _sig, _fs = _dim.get("sentiment"), _dim.get("signal"), row.get("final_score", 0.0)
        if (_SHG and _sent is not None and _sig is not None and str(row.get("direction", "")).lower() != "neutral"
                and _fs >= _SHG["score_min"] and float(_sent) < _SHG["sentiment_max"] and float(_sig) >= _SHG["signal_min"]):
            lc, lcr = True, (f"score_high_guard: 高分 {_fs:.1f} 但情绪 {float(_sent):.1f}<6 未确认"
                             f"（signal {float(_sig):.1f}）→ 仓位减半")
        snap["low_conviction"], snap["low_conviction_reason"] = lc, lcr
        snap["agent_votes"], snap["agent_votes_source"] = build_agent_votes(row)
        snap["cs_rank"] = row.get("cs_rank")
        _track(p_)
        plan["files"][p_] = {"kind": "json", "content": snap}

    # ── 5. 期权快照 / IV 索引 / VRP 行 ─────────────────────────────────────
    cache = os.path.join(prod, "cache")
    import vrp_signal as vrp
    vrp_path = os.path.join(prod, "vrp_state", "vrp_signals.jsonl")
    vrp_rows = [json.loads(ln) for ln in open(vrp_path, encoding="utf-8") if ln.strip()]
    vrp_changed = []
    for tk in sorted(P):
        res = copy.deepcopy(R[tk]["options_corrected"])
        sp_ = os.path.join(cache, f"options_snapshot_{tk}_{AS_OF}.json")
        old_snap = json.load(open(sp_))
        for k in ("_snapshot_timestamp", "_snapshot_ticker", "_snapshot_session", "_snapshot_session_complete"):
            if k in old_snap:
                res[k] = old_snap[k]
        res["_snapshot_stock_price"] = R[tk]["real_price"]
        res["_snapshot_stock_price_source"] = "cboe_close"
        res["_recomputed_v0_45_464"] = P[tk]["_recomputed_v0_45_464"]
        _track(sp_)
        plan["files"][sp_] = {"kind": "json", "content": res}

        iv_new = res.get("iv_raw_observed")
        ip_ = os.path.join(cache, f"iv_history_{tk}.jsonl")
        if os.path.exists(ip_):
            rows = [json.loads(ln) for ln in open(ip_, encoding="utf-8") if ln.strip()]
            out = []
            for r_ in rows:
                if r_.get("date") == AS_OF:
                    if iv_new is None:
                        continue
                    r_ = {**r_, "iv": round(iv_new, 4)}
                out.append(r_)
            if out != rows:
                _track(ip_)
                plan["files"][ip_] = {"kind": "jsonl", "content": out}

        for vr in vrp_rows:
            if vr.get("ticker") == tk and vr.get("date") == AS_OF:
                assert vr.get("settled_on") is None and not vr.get("ready"), f"{tk}: VRP 09-17 行已结算 / 已就绪，停下"
                # 用当天 vrp_signal.record_day 的取数规则（IV 来源顺序 / RV 字段 / 现价字段）在重算后的快照上重取
                iv, iv_source = None, None
                for src in vrp.CONFIG["iv_source_order"]:
                    iv = vrp._num(vrp._extract(res, src))
                    if iv is not None:
                        iv_source = src
                        break
                rv = vrp._num(res.get(vrp.CONFIG["rv_source"]))
                assert vrp._r(rv) == vr.get("rv_30d"), f"{tk}: RV 与 VRP 行不一致（{rv} vs {vr.get('rv_30d')}）"
                before = dict(vr)
                vr.update(iv=vrp._r(iv), iv_source=iv_source,
                          vrp_ex_ante=vrp._r(iv - rv) if (iv is not None and rv is not None) else None,
                          underlying_price=vrp._r(vrp._num(res.get("_snapshot_stock_price"))),
                          reason=(None if (iv is not None and rv is not None) else
                                  "iv_and_rv_missing" if iv is None and rv is None else
                                  "iv_missing" if iv is None else "rv_30d_missing"))
                if vr != before:
                    vrp_changed.append({"ticker": tk, "before": before, "after": dict(vr)})
    _track(vrp_path)
    plan["files"][vrp_path] = {"kind": "jsonl", "content": vrp_rows, "lock_dir": True}
    plan["notes"]["vrp_changed"] = vrp_changed
    json.dump(plan, open(plan_path, "w"), ensure_ascii=False, default=str)
    print(f"plan：文件 {len(plan['files'])} 个，predictions {len(plan['predictions'])} 行，"
          f"signal_archive 改值 {len(plan['signal_archive'])} 行，VRP 改 {len(vrp_changed)} 行")


# ═══════════════════════════════════════════════════════════════ apply（现行代码）

def apply(plan_path, prod, backup_dir, dry_run):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, here)
    import shutil
    import sqlite3
    import ledger_io

    plan = json.load(open(plan_path))
    # 写前核对：每个文件仍是 compute 时那份；库里每个旧值仍在
    stale = [p for p, h in plan["sha_before"].items() if (_sha(p) if os.path.exists(p) else None) != h]
    if stale:
        raise SystemExit(f"这些文件在 compute 之后被改过，整体中止：{stale}")
    db = os.path.join(prod, DB_REL)
    con = sqlite3.connect(db)
    for u in plan["predictions"]:
        cur = con.execute("SELECT final_score, direction, dimension_scores, agent_directions, pheromone_compact "
                          "FROM predictions WHERE id=?", (u["id"],)).fetchone()
        o = u["old"]
        if cur != (o["final_score"], o["direction"], o["dimension_scores"], o["agent_directions"], o["pheromone_compact"]):
            raise SystemExit(f"predictions id={u['id']} 写前值已变，整体中止")
    for u in plan["signal_archive"]:
        cur = con.execute("SELECT value FROM signal_archive WHERE date=? AND ticker=? AND signal=?",
                          (plan["as_of"], u["ticker"], u["signal"])).fetchone()
        if cur is None or cur[0] != u["old"]:
            raise SystemExit(f"signal_archive {u['ticker']}:{u['signal']} 写前值已变，整体中止")
    print(f"核对通过：文件 {len(plan['files'])}、predictions {len(plan['predictions'])}、signal_archive {len(plan['signal_archive'])}")
    if dry_run:
        print("dry-run：未写任何东西")
        return

    os.makedirs(backup_dir, exist_ok=False)
    for p in plan["files"]:
        rel = os.path.relpath(p, prod)
        dst = os.path.join(backup_dir, "files", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(p, dst)
    bk = sqlite3.connect(os.path.join(backup_dir, "pheromone.db"))
    con.backup(bk)
    bk.close()
    shutil.copy2(plan_path, os.path.join(backup_dir, "plan.json"))

    with con:   # 一个事务：任一行没命中就整体回滚
        for u in plan["predictions"]:
            n = u["new"]
            c = con.execute("UPDATE predictions SET final_score=?, direction=?, dimension_scores=?, agent_directions=?, "
                            "pheromone_compact=? WHERE id=?",
                            (n["final_score"], n["direction"], n["dimension_scores"], n["agent_directions"],
                             n["pheromone_compact"], u["id"]))
            assert c.rowcount == 1
        for u in plan["signal_archive"]:
            c = con.execute("UPDATE signal_archive SET value=? WHERE date=? AND ticker=? AND signal=? AND value=?",
                            (u["new"], plan["as_of"], u["ticker"], u["signal"], u["old"]))
            assert c.rowcount == 1, u
    con.close()

    for p, spec in plan["files"].items():
        if spec["kind"] == "json":
            # 照原文件的排版：swarm_results 与日报 json 是单行，快照类是 indent=2
            one_line = p.endswith((f".swarm_results_{AS_OF}.json", f"alpha-hive-daily-{AS_OF}.json"))
            ledger_io.write_json(p, spec["content"], indent=None if one_line else 2)
        elif spec["kind"] == "jsonl":
            if spec.get("lock_dir"):
                with ledger_io.locked(os.path.dirname(p)):
                    ledger_io.write_jsonl(p, spec["content"])
            else:
                ledger_io.write_jsonl(p, spec["content"])
        else:
            ledger_io.atomic_write_text(p, spec["content"])
    print(f"已写回；备份在 {backup_dir}")


if __name__ == "__main__":
    if sys.argv[1] == "compute":
        compute(*sys.argv[2:6])
    elif sys.argv[1] == "apply":
        apply(sys.argv[2], sys.argv[3], sys.argv[4], "--dry-run" in sys.argv[5:])
    else:
        raise SystemExit("用法见 docstring")
