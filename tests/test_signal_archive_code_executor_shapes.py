"""signal_archive 的 `fund.*`：必须读得到 CodeExecutorAgent **真实输出**的两种 details 形状（v0.45.250）

缺陷形状：读者只认识生产者的一条路径
--------------------------------------
`CodeExecutorAgent.analyze()` 取数成功后有两条返回路径，装的是**同一个** `data`
（`CodeGenerator._generate_yfinance` 的输出，`marketCap` / `trailingPE`），只是位置不同：

    技术分析跑通（成功路径）  details = {price, sma_20, rsi_signal, fetch_data: data, analysis_data}
    技术分析失败（兜底路径）  details = data                   ⇒ market_cap 在顶层

两种形状自 2026-02-25（c0392ce0）起就**同时**在代码里，从没改过。
`signal_archive` 的 `fund.pe_ratio` / `fund.market_cap` 是 2026-07-30（a93d0d48）写的，
只读顶层 —— 当时它是对的：技术分析脚本撞 yfinance MultiIndex **每次必崩**，
100% 走兜底，生产里只有顶层这一种形状。

v0.43.10（2026-08-12，1dbbfb34）修好了那个崩溃 ⇒ 成功路径变成主路径 ⇒
`.swarm_results_*.json` 实测 8/13 起每天 26–30 只嵌套、0–4 只顶层 ⇒ `fund.*` 每天只剩
走兜底的那 0–4 只入档（生产库 market_cap / final_score 覆盖率：4–5 月 ≈1.0 → 8 月 0.11 → 9 月 0.01）。
**上游修好一个 bug，下游一个读者因此静默断供。** `_dig` 取不到返回 None，行被跳过，没有任何东西会红。

为什么是「合并读」而不是「改名分段」（v0.45.182 `guard.consistency_census` 先例的判据）
------------------------------------------------------------------------------------
判据是「名字变了，还是量变了」。这里是**同一个量换了位置**：
· 代码上：两条路径装的是同一个 `data` 对象，取数脚本的这两个键自 2026-02-25 未改；
· 数据上：同一只票跨形状连续（NVDA 市值 8/10 顶层 5.27e12 → 8/13 嵌套 5.46e12；
  TSLA PE 323 → 303），且两种形状在 1,214 份非空 details 里**互斥**（0 份同时出现）。
⇒ 同 `_crowding_comp` 的 `_legacy` 映射：合并读，不切世代。

为什么夹具不能手写
------------------
`tests/test_signal_archive.py` 全部用手写的 `_tr()`，**没有一条把真实蜂的输出接到读者上**
（v0.45.247 契约守卫文档里点名的同一个盲区）。手写一个嵌套 dict 只证明「我以为的形状能读」，
生产者再挪一次位置它照样绿。这里驱动真实 `analyze()` 与真实 `QueenDistiller.distill()` 投影，
形状由**当前代码**产出。
"""

import json

import pytest

import signal_archive as sa
from code_executor_agent import CodeExecutorAgent
from pheromone_board import PheromoneBoard
from swarm_agents.queen_distiller import QueenDistiller

FUND = ("fund.market_cap", "fund.pe_ratio")

# `CodeGenerator._generate_yfinance` 真实输出的键全集（值随手取，但键不许少）
_FETCH = {
    "ticker": "TEST", "current_price": 100.0,
    "52_week_high": 120.0, "52_week_low": 80.0,
    "market_cap": 1_234_000_000, "pe_ratio": 21.5,
    "volume": 1_000_000, "avg_volume": 900_000,
    "recent_close": 100.0, "recent_volume": 1_000_000,
}
_ANALYSIS_OK = json.dumps({"sma_20": 90.0, "signal": "中性"})
_ANALYSIS_BAD = "not json at all"


class _ScriptedExecutor:
    """按调用顺序回放 stdout。`fetch_ok=False` ⇒ 每次都失败（含 `Debugger.auto_retry` 的重试）。"""

    def __init__(self, fetch_payload=None, analysis_stdout=None, fetch_ok=True):
        self.fetch_payload = fetch_payload
        self.analysis_stdout = analysis_stdout
        self.fetch_ok = fetch_ok
        self.calls = 0

    def execute_python(self, code, *a, **k):
        self.calls += 1
        if not self.fetch_ok:
            return {"success": False, "stdout": "",
                    "stderr": "requests.exceptions.ConnectionError: boom"}
        if self.calls == 1:
            return {"success": True, "stdout": json.dumps(self.fetch_payload), "stderr": ""}
        return {"success": True, "stdout": self.analysis_stdout, "stderr": ""}


def _analyze(monkeypatch, **executor_kw) -> dict:
    agent = CodeExecutorAgent(PheromoneBoard(), executor=_ScriptedExecutor(**executor_kw))
    monkeypatch.setattr(agent, "_get_stock_data", lambda t: {"price": 100.0})
    return agent.analyze("TEST")


def _ticker_result(agent_result: dict) -> dict:
    """走真实的 `distill()` 投影成 `swarm_results[ticker]`——`agent_details` 在那里逐键白名单拷贝，
    手抄一份投影等于替生产者作证。"""
    return QueenDistiller(PheromoneBoard(), enable_llm=False).distill("TEST", [agent_result])


#: (路径名, analyze 参数, 该路径 details 的形状判别)
PATHS = [
    pytest.param("success", dict(fetch_payload=_FETCH, analysis_stdout=_ANALYSIS_OK),
                 lambda d: isinstance(d.get("fetch_data"), dict) and "market_cap" not in d,
                 id="成功路径-嵌套fetch_data"),
    pytest.param("fallback", dict(fetch_payload=_FETCH, analysis_stdout=_ANALYSIS_BAD),
                 lambda d: "market_cap" in d and "fetch_data" not in d,
                 id="兜底路径-顶层"),
]


class TestBothProducerShapesAreArchived:

    @pytest.mark.parametrize("name,kw,is_shape", PATHS)
    def test_premise_path_really_produces_that_shape(self, monkeypatch, name, kw, is_shape):
        """**前提自证**：两条参数真的走到了两条不同的路径、产出两种不同的形状。

        没有这条，生产者哪天把两条路径统一成一种形状，下面那条参数化测试就会
        退化成「同一种形状测两遍」而照样全绿（「测试绿 ≠ 走到被测分支」）。
        红了先别急着改这里：去看 `code_executor_agent.py` 是不是改了返回形状，
        再决定 `signal_archive._code_exec_fetch` 要不要跟着改。
        """
        details = _ticker_result(_analyze(monkeypatch, **kw))["agent_details"]["CodeExecutorAgent"]["details"]
        assert is_shape(details), f"{name} 路径的 details 形状变了：{sorted(details)}"

    @pytest.mark.parametrize("name,kw,is_shape", PATHS)
    def test_fund_signals_extracted(self, monkeypatch, name, kw, is_shape):
        out = sa.extract(_ticker_result(_analyze(monkeypatch, **kw)))
        missing = [s for s in FUND if s not in out]
        assert not missing, (
            f"{name} 路径下 {missing} 没入档 —— 2026-08-13 起生产上 26–30/30 只走成功路径，"
            "读不到它 ⇒ fund.* 每天只剩走兜底的那 0–4 只")
        assert out["fund.market_cap"] == 1_234_000_000
        assert out["fund.pe_ratio"] == 21.5


class TestHonestMissingStaysMissing:
    """**成对**：防「取不到就填个值」式的修法。缺失必须省略，不能冒充观测。"""

    def test_fetch_failure_archives_no_fund_signal(self, monkeypatch):
        r = _analyze(monkeypatch, fetch_ok=False)
        assert "error" in r and not r.get("details"), "前提：取数失败路径不带 details"
        out = sa.extract(_ticker_result(r))
        assert not [s for s in FUND if s in out]

    @pytest.mark.parametrize("name,kw,is_shape", PATHS)
    def test_na_string_is_omitted(self, monkeypatch, name, kw, is_shape):
        """取数脚本对拿不到的字段写字面量 "N/A"（ETF / 亏损股没有 trailingPE，
        生产 1,214 份里 267 份 PE 是 "N/A"）—— 两种形状下都必须省略，不能变成 0。"""
        payload = dict(_FETCH, market_cap="N/A", pe_ratio="N/A")
        kw = dict(kw, fetch_payload=payload)
        out = sa.extract(_ticker_result(_analyze(monkeypatch, **kw)))
        assert not [s for s in FUND if s in out]

    def test_success_path_does_not_read_its_own_top_level_price_namespace(self, monkeypatch):
        """成功路径的顶层是**技术分析**的命名空间（price / sma_20 / rsi_signal），
        取数的量只在 `fetch_data` 里。判形状要按「有没有 fetch_data」分派，
        不能「嵌套取不到就回头读顶层」—— 否则日后顶层多出一个同名键就会被静默混读。"""
        r = _analyze(monkeypatch, fetch_payload=dict(_FETCH, market_cap="N/A"),
                     analysis_stdout=_ANALYSIS_OK)
        r["details"]["market_cap"] = 999.0     # 模拟日后顶层冒出一个同名但不同源的键
        out = sa.extract(_ticker_result(r))
        assert "fund.market_cap" not in out, "嵌套里是 N/A，却回头读到了顶层那个不同源的键"
