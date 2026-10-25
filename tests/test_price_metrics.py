"""v0.45.639：行情三字段公式收成唯一实现 `price_metrics`（此前 data_pipeline 三份 + cache.py 一份 + Twelve Data 回落一份写法）。

守四件事：
1. **主路径逐位不变**：`history_price_metrics` 与 v0.45.639 之前 data_pipeline 的公式（下面冻结一份，不调用生产代码）在随机日线上
   逐位相同——含 NaN 收盘 / NaN 成交量 / 零成交量 / 1~30 根，NaN 也要「同为 NaN」。Twelve Data 列表版同理。
2. 均量算不出 ⇒ avg_volume 与 volume_ratio **都**是 None（这就是 cache.py 旧回落那个分母 1.0 的 bug）。
3. **不许再抄**：data_pipeline.py 与 swarm_agents/cache.py 里不得再出现公式碎片（`pct_change` / `["Volume"]` / √252）。
   冻结通道层 `buzz_v1_channels.py` 是刻意的独立副本，不在此列。
4. 公式在 H1 输入层指纹里：`price_metrics.py` 登记在 `UNITS["price.fields"]`，改它指纹就变。
"""
from __future__ import annotations

import ast
import math
import random
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")   # 生产与 CI 都装 pandas；没有它整个 data_pipeline 也跑不了

import price_metrics as pm

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向代码，故用 __file__


# ── v0.45.639 之前 data_pipeline 的公式（三份逐字相同，这里冻结一份；只用于对拍，不是生产代码） ──────────────
def _frozen_main(hist):
    out = {"momentum_5d": None, "avg_volume": None, "volume_ratio": None, "volatility_20d": None}
    if len(hist) >= 5:
        out["momentum_5d"] = float((hist["Close"].iloc[-1] / hist["Close"].iloc[-5] - 1) * 100)
    recent_vol = float(hist["Volume"].iloc[-1])
    avg_vol = (float(hist["Volume"].iloc[-20:].mean()) if len(hist) >= 20
               else float(hist["Volume"].mean()))
    if not (math.isnan(avg_vol) or avg_vol <= 0):
        out["avg_volume"] = int(avg_vol)
        out["volume_ratio"] = recent_vol / avg_vol
    if len(hist) >= 19:
        returns = hist["Close"].pct_change(fill_method=None).dropna()
        if len(returns) >= 2:
            vol = float(returns.std() * (252 ** 0.5) * 100)
            if not (math.isnan(vol) or math.isinf(vol)):
                out["volatility_20d"] = vol
    return out


def _frozen_td(closes):
    rets = [b / a - 1 for a, b in zip(closes[:-1], closes[1:])]
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252) * 100


def _same(a, b):
    if a is None or b is None:
        return a is b
    if isinstance(a, float) and math.isnan(a):
        return isinstance(b, float) and math.isnan(b)
    return a == b and type(a) is type(b)


def _random_hist(rng, n):
    closes, vols = [], []
    px = rng.uniform(5, 500)
    for _ in range(n):
        px *= math.exp(rng.gauss(0, 0.03))
        closes.append(px)
        vols.append(float(rng.randint(0, 5_000_000)))
    for _ in range(rng.choice([0, 0, 0, 1, 2])):          # 偶尔塞 NaN 收盘
        closes[rng.randrange(n)] = float("nan")
    for _ in range(rng.choice([0, 0, 0, 1])):             # 偶尔塞 NaN 成交量
        vols[rng.randrange(n)] = float("nan")
    if rng.random() < 0.05:                               # 偶尔整段零成交
        vols = [0.0] * n
    idx = pd.date_range("2026-09-01", periods=n, freq="B")
    return pd.DataFrame({"Close": closes, "Volume": vols}, index=idx)


class TestBitIdenticalToFrozenMain:

    def test_random_histories(self):
        rng = random.Random(639)
        cases = 0
        for _ in range(3000):
            hist = _random_hist(rng, rng.randint(1, 30))
            got, want = pm.history_price_metrics(hist), _frozen_main(hist)
            for k in want:
                assert _same(got[k], want[k]), (k, got[k], want[k], len(hist))
            cases += 1
        assert cases == 3000

    def test_fixture_exercises_every_branch(self):
        """夹具自证：随机样本里每个分支都出现过——否则上一条的「相同」可能只在一条路径上成立。"""
        rng = random.Random(639)
        seen = set()
        for _ in range(3000):
            m = _frozen_main(_random_hist(rng, rng.randint(1, 30)))
            seen.add(("mom", m["momentum_5d"] is None))
            seen.add(("mom_nan", isinstance(m["momentum_5d"], float) and math.isnan(m["momentum_5d"])))
            seen.add(("avg", m["avg_volume"] is None))
            seen.add(("vol", m["volatility_20d"] is None))
        assert seen >= {("mom", True), ("mom", False), ("mom_nan", True), ("avg", True), ("avg", False),
                        ("vol", True), ("vol", False)}, seen

    def test_td_list_formula(self):
        rng = random.Random(557)
        for _ in range(500):
            closes = [rng.uniform(5, 500) for _ in range(rng.randint(3, 30))]   # ≥ 3：2 根只有 1 个收益，样本标准差除以 0（新旧同样抛；生产要 ≥ 19 根）
            assert pm.volatility_20d_from_closes(closes) == _frozen_td(closes)


class TestMissingAverageIsHonest:

    @pytest.mark.parametrize("vols", [[0.0] * 25, [float("nan")] * 25], ids=["zero", "nan"])
    def test_both_none_never_denominator_one(self, vols):
        """cache.py 旧回落的 bug：均量算不出时把分母换成 1.0 ⇒ volume_ratio = 当天原始成交量。"""
        idx = pd.date_range("2026-09-01", periods=25, freq="B")
        hist = pd.DataFrame({"Close": [100.0 + i for i in range(25)], "Volume": vols}, index=idx)
        m = pm.history_price_metrics(hist)
        assert m["avg_volume"] is None and m["volume_ratio"] is None
        assert m["momentum_5d"] is not None and m["volatility_20d"] is not None   # 只降级缺的那一项

    def test_threshold_is_19_bars(self):
        idx = pd.date_range("2026-09-01", periods=19, freq="B")
        hist = pd.DataFrame({"Close": [100.0 * (1.01 ** i) * (1 + 0.003 * (-1) ** i) for i in range(19)],
                             "Volume": [1e6] * 19}, index=idx)
        assert pm.VOLATILITY_MIN_BARS == 19
        assert pm.history_price_metrics(hist)["volatility_20d"] is not None
        assert pm.history_price_metrics(hist.iloc[1:])["volatility_20d"] is None


def _formula_fragments(path: Path):
    """一个文件里的公式碎片：`.pct_change(` 调用、`["Volume"]` 下标、`252 ** 0.5` / `sqrt(252)`。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "pct_change":
            hits.append((n.lineno, "pct_change"))
        elif isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and n.slice.value == "Volume":
            hits.append((n.lineno, '["Volume"]'))
        elif (isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow) and isinstance(n.left, ast.Constant)
              and n.left.value == 252):
            hits.append((n.lineno, "252 ** x"))
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "sqrt"
              and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == 252):
            hits.append((n.lineno, "sqrt(252)"))
    return hits


class TestNoMoreCopies:

    @pytest.mark.parametrize("rel", ["data_pipeline.py", "swarm_agents/cache.py"])
    def test_callers_do_not_inline_the_formula(self, rel):
        """变红的变异：在这两个文件里把公式抄回去（四份副本的老路——同一个 bug 修了三次）。"""
        hits = _formula_fragments(REPO_ROOT / rel)
        assert not hits, f"{rel} 又出现公式碎片 {hits}——改用 price_metrics.history_price_metrics / volatility_20d_from_closes"

    def test_detector_sees_the_real_formula(self):
        """正对照：同一个探测器在 price_metrics.py 里找得到全部三类碎片——否则上一条恒绿。"""
        kinds = {k for _ln, k in _formula_fragments(REPO_ROOT / "price_metrics.py")}
        assert kinds >= {"pct_change", '["Volume"]', "252 ** x", "sqrt(252)"}, kinds

    def test_callers_actually_call_it(self):
        src = (REPO_ROOT / "data_pipeline.py").read_text(encoding="utf-8")
        assert src.count("price_metrics.history_price_metrics(") == 3
        assert src.count("price_metrics.volatility_20d_from_closes(") == 1
        assert "price_metrics.history_price_metrics(" in (REPO_ROOT / "swarm_agents/cache.py").read_text(encoding="utf-8")


class TestWatchedByH1Fingerprint:

    def test_registered_in_price_fields_unit(self):
        import signal_code_fingerprints as scf
        # v0.45.645：整份（公司行动空行的判据不提字段名，按名字认不出）
        assert ("price_metrics.py", "module") in scf.UNITS["price.fields"]["code"]

    def test_changing_the_event_row_rule_changes_the_fingerprint(self):
        """v0.45.645：判据决定公式吃哪几根日线——改它必须让 price.fields 指纹变（按 mentions_price_fields 定位时不会变）。"""
        import signal_code_fingerprints as scf

        def read(rel, patch=False):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            if patch and rel == "price_metrics.py":
                assert 'EVENT_ROW_EVENT_COLUMNS = ("Dividends", "Stock Splits")' in text
                text = text.replace('EVENT_ROW_EVENT_COLUMNS = ("Dividends", "Stock Splits")',
                                    'EVENT_ROW_EVENT_COLUMNS = ("Dividends",)')
            return text

        assert scf.unit_fingerprint("price.fields", read=read) != scf.unit_fingerprint(
            "price.fields", read=lambda rel: read(rel, patch=True))

    def test_changing_the_formula_changes_the_fingerprint(self):
        """改公式必须让 price.fields 指纹变——否则搬出来的公式就逃出了 H1 守卫。"""
        import signal_code_fingerprints as scf

        def read(rel, patch=False):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            if patch and rel == "price_metrics.py":
                assert "(252 ** 0.5)" in text
                text = text.replace("(252 ** 0.5)", "(250 ** 0.5)")
            return text

        base = scf.unit_fingerprint("price.fields", read=read)
        changed = scf.unit_fingerprint("price.fields", read=lambda rel: read(rel, patch=True))
        assert base != changed
