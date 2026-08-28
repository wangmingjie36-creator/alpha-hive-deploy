"""云端快照消费端守卫（v0.45.38）。

守什么
------
1. **数字键还原**——本模块存在的首要理由。JSON 只有字符串键，
   `call_oi` 以行权价为键，往返后 130.0 → '130.0'。这不会崩：
   `sorted()` 照样排，只是排成字典序（'100.0' < '130.0' < '90.0'），
   Max Pain 与 GEX 于是算在错序的行权价上，**数字照出、全是错的**。
2. **快照模式不回落实时**——补跑的是过去某天，回落会拿今天的链贴上那天的
   日期（与 v0.45.36 拦下的污染同源，方向相反）。缺标的必须诚实返回 None。
3. **vintage 双重把关**——目录日期与数据自述不符、或旧格式证不出新鲜度，一律拒绝。
4. **降级段剔除**——兜底常量与观测值同形，留着比删掉危险。

全部按「喂退化数据看它红」构造，并用真实 git 仓库走真实读取路径（不打桩 git）。
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cloud_snapshot_loader as csl  # noqa: E402
import cboe_options as co  # noqa: E402

DATE = "2026-08-20"


def _ticker_json(vintage=DATE, price=210.0, drop_vintage=False):
    d = {
        "ticker": "NVDA", "schema_version": 1,
        "fetched_at_utc": "2026-08-20T20:05:00+00:00",
        "price_at_fetch": price, "price_source": "cboe_delayed",
        "last_trade_time_et": f"{vintage}T15:59:00",
        "vintage_date": vintage, "vintage_status": "ok",
        "prev_day_close": 208.0,
        "chain": {"ticker": "NVDA", "calls": [], "puts": [], "expirations": [],
                  "near_expiry_set": [], "timestamp": "t", "_source": "cboe"},
        "iv_term_structure": [{"expiry": "2026-09-18", "dte": 29, "atm_iv": 41.0}],
        # 键刻意跨越「字典序 ≠ 数值序」的边界：90 / 100 / 130
        "full_chain_oi": {
            "call_oi": {"90.0": 11, "100.0": 22, "130.0": 33},
            "put_oi": {"90.0": 44, "130.0": 55},
            "call_exp_oi": {"90.0": {"2026-09-18": 11}},
            "put_exp_oi": {"90.0": {"2026-09-18": 44}},
            "expiry_breakdown": [{"expiry": "2026-09-18", "call_oi": 66,
                                  "put_oi": 99, "total": 165}],
            "used_exps": 1,
        },
    }
    if drop_vintage:
        d.pop("vintage_date")
    return d


@pytest.fixture
def snap_repo(tmp_path):
    """真实 git 仓库，内容可定制。返回 (repo_path, ref)。"""
    def _make(tickers=None, market=None, manifest=True, date=DATE):
        repo = tmp_path / "snaps"
        day = repo / csl.SNAPSHOT_SUBDIR / date
        day.mkdir(parents=True, exist_ok=True)
        for name, obj in (tickers or {"NVDA": _ticker_json()}).items():
            (day / f"{name}.json").write_text(json.dumps(obj))
        if market is not None:
            (day / "market.json").write_text(json.dumps(market))
        if manifest:
            (day / "manifest.json").write_text(json.dumps({"date": date, "tickers_ok": 1}))
        if not (repo / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "s"], cwd=repo, check=True, capture_output=True)
        return str(repo), "HEAD"
    return _make


# ══════════════════════════════════════════════════════════════════
# 1. 数字键还原 —— 本模块的首要职责
# ══════════════════════════════════════════════════════════════════

def test_strike_keys_restored_to_float(snap_repo):
    repo, ref = snap_repo()
    d = csl.load_ticker(DATE, "NVDA", ref=ref, repo=repo)
    for sec in ("call_oi", "put_oi", "call_exp_oi", "put_exp_oi"):
        ks = list(d["full_chain_oi"][sec])
        assert ks and all(isinstance(k, float) for k in ks), f"{sec} 键未还原：{ks}"


def test_lexicographic_sort_would_be_wrong(snap_repo):
    """证明这个 bug 是真的：同一批键，字典序与数值序结论不同。

    不还原时 `sorted()` 给出 ['100.0','130.0','90.0'] —— 90 排到了最后。
    Max Pain 穷举正是在 all_strikes 上走的（options_analyzer）。
    """
    repo, ref = snap_repo()
    raw = json.loads(subprocess.run(
        ["git", "show", f"{ref}:{csl.SNAPSHOT_SUBDIR}/{DATE}/NVDA.json"],
        cwd=repo, capture_output=True, text=True).stdout)
    lex = sorted(raw["full_chain_oi"]["call_oi"])
    num = sorted(csl.load_ticker(DATE, "NVDA", ref=ref, repo=repo)["full_chain_oi"]["call_oi"])
    assert lex == ["100.0", "130.0", "90.0"], f"构造失效：{lex}"
    assert num == [90.0, 100.0, 130.0]
    assert [float(x) for x in lex] != num, "两种排序必须真的不同，否则这条测试没有意义"


def test_expiry_inner_keys_stay_str(snap_repo):
    """只还原顶层行权价键；内层到期日（'2026-09-18'）本来就该是 str。"""
    repo, ref = snap_repo()
    d = csl.load_ticker(DATE, "NVDA", ref=ref, repo=repo)
    inner = d["full_chain_oi"]["call_exp_oi"][90.0]
    assert list(inner) == ["2026-09-18"]


# ══════════════════════════════════════════════════════════════════
# 2. vintage 把关
# ══════════════════════════════════════════════════════════════════

def test_vintage_mismatch_refused(snap_repo):
    """目录说 8/20、数据自述 8/19 → 拒绝。"""
    repo, ref = snap_repo(tickers={"NVDA": _ticker_json(vintage="2026-08-19")})
    assert csl.load_ticker(DATE, "NVDA", ref=ref, repo=repo) is None


def test_legacy_snapshot_without_vintage_refused_by_default(snap_repo):
    """v0.45.36 之前的快照证不出新鲜度 —— 现存唯一一份恰好就是被污染的那份。"""
    repo, ref = snap_repo(tickers={"NVDA": _ticker_json(drop_vintage=True)})
    assert csl.load_ticker(DATE, "NVDA", ref=ref, repo=repo) is None
    assert csl.load_ticker(DATE, "NVDA", ref=ref, repo=repo,
                           allow_unverified=True) is not None


def test_missing_ticker_returns_none(snap_repo):
    repo, ref = snap_repo()
    assert csl.load_ticker(DATE, "ZZZZ", ref=ref, repo=repo) is None


# ══════════════════════════════════════════════════════════════════
# 3. 快照模式：接管四个入口，且绝不回落实时
# ══════════════════════════════════════════════════════════════════

def test_all_four_entrypoints_routed(snap_repo):
    repo, ref = snap_repo()
    with csl.snapshot_mode(DATE, ref=ref, repo=repo):
        assert co._fetch_cboe_payload("NVDA", 15)["current_price"] == 210.0
        assert co.fetch_cboe_chain("NVDA", 0.0)["_source"] == "cboe"
        assert co.fetch_cboe_iv_term_structure("NVDA", 0.0)[0]["dte"] == 29
        assert co.fetch_cboe_full_chain_oi("NVDA", 0.0)["used_exps"] == 1


def test_no_live_fallback_for_missing_ticker(snap_repo, monkeypatch):
    """核心不变式：快照里没有的标的必须诚实缺失，且**一个网络请求都不许发**。

    回落实时会拿到**今天**的链贴上补跑日的日期 —— 与 v0.45.36 拦下的污染同源，
    只是方向相反。

    ⚠️ 判据是「禁网」而不是「返回 None」：四个钩子是分层的，拆掉上层钩子后
    下层钩子照样会把结果压成 None，只看返回值的测试**证明不了**上层钩子还在
    （实测：变异 fetch_cboe_chain 的钩子，只看返回值的旧版测试全绿）。
    把 urlopen 换成抛错，任何一层回落都会立刻暴露。
    """
    import urllib.request

    def _forbidden(*a, **k):
        raise AssertionError("快照模式下发起了网络请求 —— 存在实时回落")

    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)
    repo, ref = snap_repo()
    with csl.snapshot_mode(DATE, ref=ref, repo=repo):
        assert co.fetch_cboe_chain("AAPL", 200.0) is None
        assert co.fetch_cboe_full_chain_oi("AAPL", 200.0) is None
        assert co.fetch_cboe_iv_term_structure("AAPL", 200.0) is None
        assert co._fetch_cboe_payload("AAPL", 15) is None


def test_provider_unloaded_even_on_exception(snap_repo):
    repo, ref = snap_repo()
    with pytest.raises(ValueError):
        with csl.snapshot_mode(DATE, ref=ref, repo=repo):
            raise ValueError("boom")
    assert co._SNAPSHOT_PROVIDER is None, "异常路径未卸载 → 后续实时扫描会静默读到快照"


def test_absent_date_raises_not_thirty_failures(snap_repo):
    """没有那天的快照要**一次性**报错，而不是进去之后每个标的各失败一次
    （那样会把「没这天」读成「30 只全挂」）。"""
    repo, ref = snap_repo()
    with pytest.raises(csl.SnapshotUnavailable):
        with csl.snapshot_mode("2026-01-01", ref=ref, repo=repo):
            pass
    assert co._SNAPSHOT_PROVIDER is None


# ══════════════════════════════════════════════════════════════════
# 4. 大盘段：降级段必须剔除而非留着
# ══════════════════════════════════════════════════════════════════

def test_degraded_market_sections_dropped(snap_repo):
    """兜底常量与观测值同形，留着比删掉危险（v0.43.24 同款）。"""
    repo, ref = snap_repo(market={
        "cboe": {"vix_term": {"vix_1m": 15.75}, "skew": {"skew_value": 120.0},
                 "pcce": {"ratio": 1.07}},
        "degraded_sections": {"vix_term": "explicit_default_fallback",
                              "skew": "matches_known_fallback_120.0"},
    })
    m = csl.load_market(DATE, ref=ref, repo=repo)
    assert sorted(m["cboe"]) == ["pcce"], "降级段没被剔除"
    assert m["dropped_degraded"] == ["skew", "vix_term"]


def test_available_dates_and_missing_branch(snap_repo):
    repo, ref = snap_repo()
    assert csl.available_dates(ref, repo) == [DATE]
    assert csl.available_dates("no-such-ref", repo) == []


# ══════════════════════════════════════════════════════════════════
# 5. 补跑入口 _snapshot_ctx：要么用上快照，要么把降级说出来
# ══════════════════════════════════════════════════════════════════

class _Args:
    def __init__(self, date=None, no_snapshot=False):
        self.date, self.no_snapshot = date, no_snapshot


def test_ctx_inactive_without_date(capsys):
    """正常当日扫描不进快照模式，也不该有任何噪音。"""
    import alpha_hive_daily_report as ahdr
    with ahdr._snapshot_ctx(_Args()) as prov:
        assert prov is None
        assert co._SNAPSHOT_PROVIDER is None
    assert capsys.readouterr().out == ""


def test_ctx_missing_snapshot_warns_and_continues(capsys):
    """拿不到快照**不中止** —— 价格/情绪/催化剂维度仍可信，中止会把
    「期权维度缺失」升级成「整天没有」。但必须把降级说清楚。"""
    import alpha_hive_daily_report as ahdr
    with ahdr._snapshot_ctx(_Args(date="1999-01-04")) as prov:
        assert prov is None
    out = capsys.readouterr().out
    assert "无可用云端快照" in out
    assert "今天的数据" in out, "降级说得不够明确：必须点明期权/IV 是今天的"


def test_ctx_no_snapshot_flag_warns(capsys):
    """显式关掉快照 → 保留旧行为，但不许静悄悄。"""
    import alpha_hive_daily_report as ahdr
    with ahdr._snapshot_ctx(_Args(date="2026-08-20", no_snapshot=True)) as prov:
        assert prov is None
    assert "--no-snapshot" in capsys.readouterr().out


def test_ctx_activates_and_unloads(snap_repo, monkeypatch, capsys):
    """有快照时真的接管，且退出后卸载（否则后续实时扫描会静默读到快照）。"""
    import alpha_hive_daily_report as ahdr
    import cloud_snapshot_loader as loader
    repo, ref = snap_repo()
    monkeypatch.setattr(loader, "SNAPSHOT_REF", ref)
    monkeypatch.setattr(loader, "REPO_DIR", repo)
    with ahdr._snapshot_ctx(_Args(date=DATE)) as prov:
        assert prov is not None
        assert co.fetch_cboe_chain("NVDA", 0.0)["_source"] == "cboe"
    assert co._SNAPSHOT_PROVIDER is None
    assert "云端快照模式" in capsys.readouterr().out


def test_scan_error_propagates_not_masked(snap_repo, monkeypatch, capsys):
    """🔴 扫描体的异常必须原样传播，不许被「无可用快照」的诊断吞掉。

    初版 `_snapshot_ctx` 把「进入快照模式」和「跑扫描」包在同一个 try 里：
    扫描体抛异常 → 穿过 `yield prov` 被 `except Exception` 抓住 →
    打出「无可用云端快照」这条**完全错误的诊断**（快照明明拿到了）→
    再 `yield None` 触发 `RuntimeError: generator didn't stop after throw()`。
    真实错误被吞，换成一个假诊断加一个莫名其妙的 RuntimeError（构造检验确认）。
    """
    import alpha_hive_daily_report as ahdr
    import cloud_snapshot_loader as loader
    repo, ref = snap_repo()
    monkeypatch.setattr(loader, "SNAPSHOT_REF", ref)
    monkeypatch.setattr(loader, "REPO_DIR", repo)

    with pytest.raises(ValueError, match="蜂群崩溃"):
        with ahdr._snapshot_ctx(_Args(date=DATE)):
            raise ValueError("模拟蜂群崩溃")

    out = capsys.readouterr().out
    assert "无可用云端快照" not in out, "把扫描失败误报成了快照不可用"
    assert co._SNAPSHOT_PROVIDER is None, "异常路径未卸载供给器"


class TestSnapshotModeProbesLoadability:
    """manifest 存在 ≠ 标的能载入（v0.45.58）。

    实测事故：`vintage_date` 于 v0.45.36 加进主线生产端，但云端 routine 跑的是
    cloud-snapshots 分支上的旧脚本（该字段出现 0 次），于是每份快照都缺它，
    而 `load_ticker` 对缺它的一律 return None。

    旧 `snapshot_mode` 只验 manifest 就 yield，调用方随即打印
    「📦 云端快照模式」，扫描却静默退回实时抓取 —— 8-26 与 8-27 两天
    各 30/30 完好的快照一份都没用上，且看起来一直是活的。

    这就是 `check_label_honesty` 要抓的形态：标签宣称成功，
    它所管辖的值却是空的。
    """

    def _seed(self, tmp_path, *, vintage):
        import json as _j
        import os as _os
        d = tmp_path / "cloud_snapshots" / "2026-08-27"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(_j.dumps(
            {"date": "2026-08-27", "tickers_requested": 2, "tickers_ok": 2,
             "ok": ["NVDA", "ABBV"]}))
        for t in ("NVDA", "ABBV"):
            doc = {"ticker": t, "chain": {"calls": [1], "puts": [1]}}
            if vintage:
                doc["vintage_date"] = "2026-08-27"
            (d / f"{t}.json").write_text(_j.dumps(doc))
        return str(tmp_path)

    def _patch_git_show(self, monkeypatch, root):
        import os as _os
        import cloud_snapshot_loader as loader

        def _fake(path, ref=None, repo=None):
            fp = _os.path.join(root, path)
            return open(fp).read() if _os.path.exists(fp) else None

        monkeypatch.setattr(loader, "_git_show", _fake)

    def test_raises_when_manifest_ok_but_nothing_loadable(self, tmp_path, monkeypatch):
        import cloud_snapshot_loader as loader
        self._patch_git_show(monkeypatch, self._seed(tmp_path, vintage=False))
        with pytest.raises(loader.SnapshotUnavailable) as ei:
            with loader.snapshot_mode("2026-08-27"):
                pass
        msg = str(ei.value)
        assert "一只都载不进来" in msg
        assert "vintage_date" in msg, "诊断必须点名根因，否则等于换个说法的沉默"

    def test_allow_unverified_lets_them_through(self, tmp_path, monkeypatch):
        import cloud_snapshot_loader as loader
        self._patch_git_show(monkeypatch, self._seed(tmp_path, vintage=False))
        with loader.snapshot_mode("2026-08-27", allow_unverified=True) as prov:
            assert prov("NVDA") is not None

    def test_healthy_snapshot_still_works(self, tmp_path, monkeypatch):
        """回归：带 vintage_date 的正常快照不得被抽验误伤。"""
        import cloud_snapshot_loader as loader
        self._patch_git_show(monkeypatch, self._seed(tmp_path, vintage=True))
        with loader.snapshot_mode("2026-08-27") as prov:
            assert prov("NVDA") is not None
            assert prov("ABBV") is not None
