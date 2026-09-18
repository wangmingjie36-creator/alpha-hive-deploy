"""MultiSourceFetcher 全源失败时，momentum_5d/volume_ratio 的独立回落链（v0.45.270）

事故：2026-09-17 扫描开局约 2 分钟本机网络中断——CBOE 熔断「no_data」、
yfinance 同时报 `'NoneType' object is not subscriptable`、Finnhub/AlphaVantage
均 SSL 握手失败，30/30 标的在 `MultiSourceFetcher.fetch()` 走到最后防线
「所有数据源不可用！返回 FALLBACK 默认值」。价格确实拿不到，可以接受；但
momentum_5d/volume_ratio 各自另有一条**独立**于 CBOE/yfinance 主价源的回落链
（自攒价格索引 + Twelve Data，`_fill_momentum_from_index` /
`_fill_volume_from_twelvedata`）——此前只挂在 `CboeSource.fetch()` 内部，
CBOE 熔断器一跳闸，这条回落链连尝试的机会都没有，尽管它大概率仍然健康
（本地索引不经网络；Twelve Data 是与 CBOE/yfinance 都无关的独立账号配额）。

后果：BuzzBeeWhisper 的 `momentum`/`volume` 通道、（经同一 `stock_data` 字典
间接影响的）派生字段全部诚实置 None——这部分是对的——但连「网络已经恢复、
本可以从独立源补上」的机会都被提前放弃了，落到网站上就是「数据部分降级」
横幅：momentum/volume 通道 30/30 标的降级。

本文件覆盖：全源失败时，最终 FALLBACK 字典是否尝试了这条独立回落链，以及
两条回落各自失败/成功的组合不会互相污染或让整条链失去「诚实缺数据」的语义。
"""

import data_pipeline as dp


class _DeadSource:
    """总是返回 None 的假源，模拟某一环彻底打不通（不区分具体失败原因）。"""

    def fetch(self, ticker):
        return None


def _fresh_fetcher():
    f = dp.MultiSourceFetcher()
    f._sources = [_DeadSource(), _DeadSource(), _DeadSource(), _DeadSource()]
    return f


class TestFallbackMomentumVolumeRecovery:
    def test_independent_fallback_fills_momentum_and_volume(self, monkeypatch):
        """四源全灭时，价格仍是 0/不可用，但 momentum_5d/volume_ratio 应被
        独立回落链补上——这正是本次要修的缺口。"""
        monkeypatch.setattr(
            dp, "_fill_momentum_from_index",
            lambda ticker, out: {"momentum_5d": -3.2, "momentum_source": "price_index"})
        monkeypatch.setattr(
            dp, "_fill_volume_from_twelvedata",
            lambda ticker, out: {**(out or {}), "volume_ratio": 0.87,
                                 "avg_volume": 12_000_000, "volume_source": "twelvedata"})

        result = _fresh_fetcher().fetch("NVDA")

        assert result["price"] == 0.0
        assert result["_data_unavailable"] is True
        assert result["data_source"] == dp.DataQuality.FALLBACK
        assert result["momentum_5d"] == -3.2
        assert result["momentum_source"] == "price_index"
        assert result["volume_ratio"] == 0.87
        assert result["avg_volume"] == 12_000_000
        assert result["volume_source"] == "twelvedata"

    def test_both_independent_sources_also_dead_stays_honest_none(self, monkeypatch):
        """本地索引与 Twelve Data 也都拿不到时，必须保持诚实缺数据——
        不能因为加了这条回落链就意外编出一个假值。"""
        monkeypatch.setattr(dp, "_fill_momentum_from_index", lambda ticker, out: out)
        monkeypatch.setattr(dp, "_fill_volume_from_twelvedata", lambda ticker, out: out)

        result = _fresh_fetcher().fetch("NVDA")

        assert result["momentum_5d"] is None
        assert result["volume_ratio"] is None
        assert result["_data_unavailable"] is True

    def test_only_momentum_recovers(self, monkeypatch):
        """两条回落互相独立：一条有数据、另一条没有时不能互相拖累。"""
        monkeypatch.setattr(
            dp, "_fill_momentum_from_index",
            lambda ticker, out: {"momentum_5d": 1.1, "momentum_source": "price_index"})
        monkeypatch.setattr(dp, "_fill_volume_from_twelvedata", lambda ticker, out: out)

        result = _fresh_fetcher().fetch("NVDA")

        assert result["momentum_5d"] == 1.1
        assert result["volume_ratio"] is None

    def test_only_volume_recovers(self, monkeypatch):
        monkeypatch.setattr(dp, "_fill_momentum_from_index", lambda ticker, out: out)
        monkeypatch.setattr(
            dp, "_fill_volume_from_twelvedata",
            lambda ticker, out: {**(out or {}), "volume_ratio": 0.5,
                                 "avg_volume": 900_000, "volume_source": "twelvedata"})

        result = _fresh_fetcher().fetch("NVDA")

        assert result["momentum_5d"] is None
        assert result["volume_ratio"] == 0.5

    def test_fallback_helper_itself_raising_does_not_crash_fetch(self, monkeypatch):
        """这条回落链本身故障（比如 price_history/twelve_data 模块炸了）不能
        把「四源全灭」升级成整个 fetch() 抛异常——回落是优化，不是前提。"""
        def _boom(ticker, out):
            raise RuntimeError("index module broken")

        monkeypatch.setattr(dp, "_fill_momentum_from_index", _boom)

        result = _fresh_fetcher().fetch("NVDA")

        assert result["_data_unavailable"] is True
        assert result["momentum_5d"] is None
        assert result["volume_ratio"] is None

    def test_normal_source_success_path_unaffected(self, monkeypatch):
        """有源成功时完全走原路径，不触碰新增的回落分支
        （用会抛错的假回落函数守住「不该被调用」）。"""
        def _must_not_be_called(ticker, out):
            raise AssertionError("有源成功时不该走到 FALLBACK 回落分支")

        monkeypatch.setattr(dp, "_fill_momentum_from_index", _must_not_be_called)

        class _WorkingSource:
            def fetch(self, ticker):
                return dp.StockData(
                    price=123.45, data_source=dp.DataQuality.REAL,
                    source_name="fake", momentum_5d=2.0, volume_ratio=1.0,
                    fetch_timestamp=__import__("time").time(),
                )

        f = dp.MultiSourceFetcher()
        f._sources = [_WorkingSource()]
        result = f.fetch("NVDA")
        assert result["price"] == 123.45
        assert result["momentum_5d"] == 2.0
