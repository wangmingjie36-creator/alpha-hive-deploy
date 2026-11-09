"""v0.45.755：调整后 / 非标准 root 的合约（CVX1、B1VDA、拆股 AAPL1）不再被算进「OCC 符号解析失败（疑格式变更）」。

**背景（2026-11-06 / 11-09 实测）。** CVX 的 64 个合约代码全以 `CVX1` 开头、全是 2027-01-15 到期——原赫斯（HES）期权，
雪佛龙收购后 OCC 改成调整后符号；占 CVX 未平仓 0.40%。告警按**合约个数**比 5%，CVX 总合约数低于约 1280 的日子
（11-09：1188 个、5.39%）天天响「疑格式变更」。NVDA 的 `B1VDA`（180 个）、TSLA 的 `B1SLA`（240 个）11-05 起出现，
疑为 NYSE 二元期权，也在 4% 附近逼近同一条线。都是误报，真有格式回归时反而淹在里面。

**这里守的是**（剔除行为本身一点没变，变的只是告警定性）：
  1. 非标准 root 的合约不再触发「疑格式变更」，每进程每个 (标的, root 集合) 记一行 INFO；
  2. 真畸形的合约仍按旧阈值（5%）、旧措辞报，且只数真畸形的个数；
  3. 非标准 root 合约占全链未平仓 > 5% ⇒ WARNING（剔除开始改变口径）——误报收掉的同时留着一根会红的线；
  4. `_OCC` 没被放宽：CVX1 仍被 `_parse_occ` 拒绝（放宽 ⇒ 按 100 股混进 CVX，B1 二元期权被当正股期权）。

**纪律。** 每条断言的 docstring 写明什么变异会让它变红。夹具全是内存 dict，日期钉死 `_pdt_now` / `now_et`，无 skip。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import cboe_options as C

ET = ZoneInfo("America/New_York")
AFTER_CLOSE = datetime(2026, 9, 23, 17, 5, tzinfo=ET)
PDT_NOW = datetime(2026, 9, 23, 13, 30)
VINTAGE = "2026-09-23"
STD_EXPIRIES = ("2026-09-30", "2026-10-07", "2026-10-14", "2026-10-21")
ODD_EXPIRY = "2027-01-15"


def _occ(root, expiry, cp, strike):
    return f"{root}{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}{cp}{int(round(strike * 1000)):08d}"


def _row(symbol, oi):
    return {"option": symbol, "iv": 0.30, "delta": 0.3, "gamma": 0.02, "vega": 0.1, "theta": -0.05, "theo": 1.0,
            "open_interest": oi, "volume": 10, "bid": 1.0, "ask": 1.1, "last_trade_price": 1.05,
            "last_trade_time": "2026-09-23T15:59:00"}


def _payload(*, root="CVX", n_std=120, oi_std=100.0, odd_root=None, n_odd=0, oi_odd=1.0, n_garbage=0):
    """n_std 个标准合约（4 个到期 × C/P × 若干行权价）+ n_odd 个非标准 root 合约 + n_garbage 个真畸形符号。"""
    rows, k = [], 0
    while len(rows) < n_std:
        for e in STD_EXPIRIES:
            for cp in ("C", "P"):
                if len(rows) < n_std:
                    rows.append(_row(_occ(root, e, cp, 100.0 + k), oi_std))
        k += 1
    j = 0
    while odd_root and len([r for r in rows if r["option"].startswith(odd_root)]) < n_odd:
        for cp in ("C", "P"):
            rows.append(_row(_occ(odd_root, ODD_EXPIRY, cp, 50.0 + j), oi_odd))
        j += 1
    rows += [_row(f"GARBAGE{i}", 5.0) for i in range(n_garbage)]
    return {"symbol": root, "close": 100.0, "current_price": 100.0, "options": rows, "last_trade_time": "2026-09-23T16:00:00"}


class _Log:
    """记录 `cboe_options._log` 的调用：(级别, 已格式化文本)。"""

    def __init__(self):
        self.records = []

    def _add(self, level, msg, *args):
        self.records.append((level, (msg % args) if args else msg))

    def warning(self, msg, *args, **_k):
        self._add("WARNING", msg, *args)

    def info(self, msg, *args, **_k):
        self._add("INFO", msg, *args)

    def debug(self, *_a, **_k):
        pass

    error = warning

    def lines(self, level, needle=""):
        return [m for lv, m in self.records if lv == level and needle in m]


@pytest.fixture
def log(monkeypatch):
    rec = _Log()
    monkeypatch.setattr(C, "_log", rec)
    C._nonstd_root_noted.clear()
    C.reset_raw_contracts_stats()
    yield rec
    C._nonstd_root_noted.clear()
    C.reset_raw_contracts_stats()


def _serve(monkeypatch, payload):
    monkeypatch.setattr(C, "_SNAPSHOT_PROVIDER", None)
    monkeypatch.setattr(C, "_fetch_cboe_payload", lambda t, to, **k: payload)
    monkeypatch.setattr(C, "_pdt_now", lambda: PDT_NOW)


def _chain(monkeypatch, payload, ticker="CVX"):
    _serve(monkeypatch, payload)
    return C.fetch_cboe_chain(ticker, 100.0)


def _raw(monkeypatch, payload, ticker="CVX"):
    _serve(monkeypatch, payload)
    return C.fetch_cboe_raw_contracts(ticker, as_of=VINTAGE, now_et=AFTER_CLOSE)


# ───────────────────────────── 0. 分类函数与「别放宽 _OCC」

class TestClassification:
    @pytest.mark.parametrize("sym, root", [
        ("CVX1270115C00065000", "CVX1"),
        ("B1VDA261113P00160000", "B1VDA"),
        ("AAPL1260918C00200000", "AAPL1"),
    ])
    def test_adjusted_and_binary_roots_are_recognised(self, sym, root):
        """CVX1 / B1VDA / 拆股 AAPL1：形状是 OCC、root 带数字 ⇒ 认出 root，且 `_parse_occ` 仍然拒绝它们。

        变红的变异：`_nonstandard_root` 不再要求 root 含数字；或把 `_OCC` 放宽成认数字（那样第二个断言红）。
        """
        assert C._nonstandard_root(sym) == root
        assert C._parse_occ(sym) is None

    @pytest.mark.parametrize("sym", [
        "GARBAGE", "", None, 12345,
        "NVDA261301C00100000",         # 月份 13：纯字母 root，日期不合法 ⇒ 真畸形
        "CVX1271301C00065000",         # 带数字 root 但日期不合法 ⇒ 真畸形，不能借「非标准 root」之名放过
        "CVX1270115C0006500",          # 行权价 7 位
        "cvx1270115C00065000",         # 小写
        "NVDA260702C00200000",         # 标准符号：解析成功的不归它管
    ])
    def test_malformed_and_standard_symbols_are_not_nonstandard(self, sym):
        """真畸形（以及本来就解析得了的标准符号）返回 None ⇒ 仍算「疑格式变更」的候选。

        变红的变异：分类函数对任何 OCC 形状的串都返回 root（含日期不合法的）⇒ 格式回归被当成「调整后符号」放过。
        """
        assert C._nonstandard_root(sym) is None

    def test_standard_symbol_still_parses(self):
        assert C._parse_occ("NVDA260702C00200000") == ("2026-07-02", "C", 200.0)


# ───────────────────────────── 1. CVX 的形状：不再误报

class TestCvxShape:
    """11-09 的 CVX：1188 个合约里 64 个 CVX1（5.39%），占未平仓 0.40%。"""

    def _payload(self):
        # 1124 个标准合约 × OI 480 ≈ 539,520；64 个 CVX1 × OI ≈ 34 ≈ 2,178 ⇒ 0.40%；合计 1188 个
        return _payload(n_std=1124, oi_std=480.0, odd_root="CVX1", n_odd=64, oi_odd=34.03)

    def test_chain_path_no_format_change_warning_and_one_info(self, monkeypatch, log):
        """`fetch_cboe_chain`：64/1188 = 5.39% > 5%，旧代码报「疑格式变更」；现在不报，改记一行 INFO，且点出 CVX1 与占比。

        变红的变异：不过滤非标准 root（回到旧告警）；或告警与 INFO 都不出（静默）。
        """
        res = _chain(monkeypatch, self._payload())
        assert res is not None
        assert log.lines("WARNING", "疑格式变更") == []
        (info,) = log.lines("INFO", "非标准 root")
        assert "CVX1 64 个" in info and "0.40%" in info and "不是格式变更" in info

    def test_raw_path_same_and_counter_unchanged(self, monkeypatch, log):
        """`fetch_cboe_raw_contracts`：同上；`n_dropped_unparseable` 仍是 64（卖权账本按它记 fetch_counts，语义不许变）。

        变红的变异：把非标准 root 合约从 n_dropped_unparseable 里扣掉；或剔除行为变了（contracts 里混进 CVX1）。
        """
        res, why = _raw(monkeypatch, self._payload())
        assert why is None
        assert res["n_raw"] == 1188 and res["n_dropped_unparseable"] == 64
        assert not any(c["symbol"].startswith("CVX1") for c in res["contracts"])
        assert log.lines("WARNING", "疑格式变更") == []
        assert len(log.lines("INFO", "非标准 root")) == 1

    def test_exclusion_is_unchanged(self, monkeypatch, log):
        """剔除行为一点没变：带 CVX1 与不带 CVX1 的两份 payload，主链结果（到期日 / calls / puts）逐项相同。

        变红的变异：收进 CVX1（放宽 `_OCC` 或绕过剔除）⇒ 到期日里多出 2027-01-15、calls / puts 变多。
        """
        with_odd = _chain(monkeypatch, self._payload())
        without = _chain(monkeypatch, _payload(n_std=1124, oi_std=480.0))
        for key in ("expirations", "calls", "puts"):
            assert with_odd[key] == without[key], key
        assert ODD_EXPIRY not in with_odd["expirations"]

    def test_info_is_logged_once_per_process_per_ticker(self, monkeypatch, log):
        """主链每轮约取 4 次——INFO 按 (标的, root 集合) 每进程只记一次，别把日志刷满。

        变红的变异：去掉去重集合（每次调用都记）。
        """
        for _ in range(3):
            _chain(monkeypatch, self._payload())
        _raw(monkeypatch, self._payload())
        assert len(log.lines("INFO", "非标准 root")) == 1


# ───────────────────────────── 2. 真畸形照旧报

class TestMalformedStillWarns:
    def test_garbage_over_threshold_warns_with_old_wording_and_counts_only_malformed(self, monkeypatch, log):
        """10% 真畸形 + 64 个 CVX1：仍报「疑格式变更」，措辞同旧版，个数只数真畸形（不含 CVX1）。

        变红的变异：阈值 / 分子仍含非标准 root（个数变成 148 而不是 84）；或畸形也被一并放过（不报）。
        """
        payload = _payload(n_std=700, oi_std=480.0, odd_root="CVX1", n_odd=64, n_garbage=84)
        n_total = len(payload["options"])
        _chain(monkeypatch, payload)
        (warn,) = log.lines("WARNING", "疑格式变更")
        assert warn == f"CBOE CVX：84/{n_total} 合约 OCC 符号解析失败（疑格式变更）"
        C._nonstd_root_noted.clear()
        _raw(monkeypatch, payload)
        (warn_raw,) = log.lines("WARNING", "原始合约")
        assert warn_raw == f"CBOE CVX 原始合约：84/{n_total} OCC 符号解析失败（疑格式变更）"

    def test_garbage_under_threshold_is_silent(self, monkeypatch, log):
        """真畸形 < 5% 照旧不报（与旧阈值同一把尺）。变红的变异：阈值被降低。"""
        _chain(monkeypatch, _payload(n_std=1000, oi_std=100.0, n_garbage=40))
        assert log.lines("WARNING") == []

    def test_only_nonstandard_below_threshold_is_just_info(self, monkeypatch, log):
        """只有 3 个非标准 root（< 5%）、占 OI 极小：既不报疑格式变更，也只记 INFO。"""
        _chain(monkeypatch, _payload(n_std=1000, oi_std=100.0, odd_root="CVX1", n_odd=3))
        assert log.lines("WARNING") == []
        assert len(log.lines("INFO", "非标准 root")) == 1


# ───────────────────────────── 3. 误报收掉的同时，留着一根会红的线

class TestMaterialShareStillWarns:
    def test_nonstandard_share_of_oi_over_threshold_warns(self, monkeypatch, log):
        """非标准 root 合约占全链未平仓 > 5% ⇒ WARNING（点出占比与 root）：剔除已经在改变口径，不能只在 INFO 里。

        变红的变异：删掉占比分支（只记 INFO）；或占比线被改得极高。
        """
        # 标准 OI 合计 1000 × 1.0 = 1000；非标准 20 个 × 10 = 200 ⇒ 16.7%
        payload = _payload(n_std=1000, oi_std=1.0, odd_root="AAPL1", n_odd=20, oi_odd=10.0)
        _chain(monkeypatch, payload, ticker="AAPL")
        (warn,) = log.lines("WARNING", "非标准 root")
        assert "AAPL1 20 个" in warn and "16.7%" in warn and "口径受影响" in warn
        assert log.lines("WARNING", "疑格式变更") == []
        assert log.lines("INFO", "非标准 root") == [], "占比重大时走 WARNING，不再另记 INFO"

    def test_share_just_under_threshold_is_info(self, monkeypatch, log):
        """占比刚好低于 5% ⇒ INFO。变红的变异：用 `>=` 之外的比较或把阈值写成合约个数口径。"""
        # 标准 OI 1000；非标准 20 × 2 = 40 ⇒ 3.8%
        _chain(monkeypatch, _payload(n_std=1000, oi_std=1.0, odd_root="AAPL1", n_odd=20, oi_odd=2.0), ticker="AAPL")
        assert log.lines("WARNING") == []
        assert len(log.lines("INFO", "非标准 root")) == 1
