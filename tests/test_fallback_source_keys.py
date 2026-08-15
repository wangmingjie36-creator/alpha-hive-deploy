"""
降级源 key 加载与链序（v0.43.26 回归）

事故：`data_pipeline` 的 AlphaVantageSource / FinnhubSource 都只读环境变量
（`ALPHA_VANTAGE_API_KEY` / `FINNHUB_API_KEY`），而 key 实际存在
`~/.alpha_hive_av_key` / `~/.alpha_hive_finnhub_key`，环境变量从未设过。
两个源每次都 `if not self.api_key: return None`——
**"CBOE→yfinance→AV→Finnhub" 降级链的后两环从未生效过。**

这是"防御看着在、其实是死的"的又一例：链子写得完整，源类也实现了，
只是永远拿不到 key，静默返回 None，日志里连一行都没有。

项目本就有集中的 `config.get_secret`（环境变量优先 → 降级文件 + 权限校验），
只是这两个源没用它。
"""

import pytest

import data_pipeline as dp


class TestKeysLoadFromFileNotOnlyEnv:
    """核心回归：环境变量未设时，key 必须仍能从集中管理处取到"""

    @pytest.mark.parametrize("cls,secret_name", [
        (dp.FinnhubSource, "FINNHUB_API_KEY"),
        (dp.AlphaVantageSource, "AV_API_KEY"),
    ])
    def test_key_comes_from_get_secret(self, cls, secret_name, monkeypatch):
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
        monkeypatch.setattr(dp, "_get_secret",
                            lambda name: "KEY-FROM-FILE" if name == secret_name else "")
        assert cls().api_key == "KEY-FROM-FILE"

    @pytest.mark.parametrize("cls", [dp.FinnhubSource, dp.AlphaVantageSource])
    def test_no_key_still_disables_source(self, cls, monkeypatch):
        """拿不到 key 时照旧禁用，不该为了"能跑"去裸调无 token 接口"""
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
        monkeypatch.setattr(dp, "_get_secret", lambda name: "")
        src = cls()
        assert src.api_key == ""
        assert src.fetch("NVDA") is None

    def test_env_var_still_wins(self, monkeypatch):
        """环境变量优先级不变——临时覆盖仍然有效"""
        monkeypatch.setattr(dp, "_get_secret", lambda name: "FROM-ENV"
                            if name == "FINNHUB_API_KEY" else "")
        assert dp.FinnhubSource().api_key == "FROM-ENV"


class TestSecretRegistry:
    def test_finnhub_is_registered(self):
        """漏登记正是本次事故的根因之一"""
        from config import _SECRET_REGISTRY
        assert _SECRET_REGISTRY.get("FINNHUB_API_KEY") == "~/.alpha_hive_finnhub_key"


class TestChainOrder:
    def test_finnhub_before_alpha_vantage(self):
        """额度差一个量级：Finnhub 60 次/分钟 vs AV 25 次/天。
        需要降级的场景就是"30 只批量失败"，把 25/天 的源排前面等于第 26 只
        起必然再降一级。"""
        import inspect
        src = inspect.getsource(dp)
        i_fh = src.index("FinnhubSource(),")
        i_av = src.index("AlphaVantageSource(),")
        assert i_fh < i_av, "Finnhub 必须排在 AlphaVantage 之前"
