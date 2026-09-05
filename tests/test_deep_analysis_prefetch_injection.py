"""deep_analysis 的预取包必须真正注入到蜂身上（v0.45.126 回归）

背景
----
`deep_analysis.py` 的 Phase 0 原本写：

    prefetched = prefetch_shared_data([ticker])
    inject_prefetched(prefetched)          # ← 少传一个参数

而签名是 `inject_prefetched(agents, prefetched)`（2026-03-09 定型），
这行是 2026-03-11 写的 —— **从写下那天起就抛 TypeError**。
它被外层 `except Exception` 吞掉，只在终端留一句「⚠ 预取部分失败」，
于是预取包**每次都被整包丢弃**、七只蜂各自去抓 yfinance，持续约 6 个月。

三个让它藏住的条件缺一不可：
  ① 异常被宽 `except Exception` 捕获（不是崩溃，是降级）
  ② 降级消息说的是「预取部分失败」—— 把**调用签名错误**说成数据问题，
     读到的人会去查网络/限流，不会怀疑代码
  ③ 回退路径本身是正确的（各蜂自己抓数据，结果不变），
     所以**只有速度和 yfinance 配额受损，产出完全正常** ⇒ 无人察觉

修法
----
不是补一个参数就完事：Phase 0 那个位置**agent 还不存在**
（每个线程在 `run_agent` 里各自 `cls(board)`）。
故预取包留到 Phase 1，由 `run_agent` 在建好 agent 后注入。
"""

import ast
import sys
import types

import pytest

import deep_analysis


class _StubAgent:
    """冒充一只蜂：只记录自己拿到了什么，不碰网络。"""
    def __init__(self, board):
        self.board = board

    def analyze(self, ticker):
        return {"score": 6.0, "direction": "bullish", "confidence": 0.5,
                "_saw_market": getattr(self, "_prefetched_market", None),
                "_saw_stock": getattr(self, "_prefetched_stock", None)}


@pytest.fixture
def stub_agent(monkeypatch):
    """把 AGENTS 里的一项指向本文件的桩类，避免真建蜂、真打网络。"""
    mod = types.ModuleType("_stub_bee_mod")
    mod.StubBee = _StubAgent
    monkeypatch.setitem(sys.modules, "_stub_bee_mod", mod)
    monkeypatch.setitem(deep_analysis.AGENTS, "scout", ("_stub_bee_mod", "StubBee"))
    return mod


BUNDLE = {"stock_data": {"NVDA": {"price": 100.0}},
          "contexts": {}, "target_date": "2026-09-05",
          "market": {"info": {"NVDA": {"trailingPE": 42.0}}}}


class TestPrefetchReachesTheAgent:
    def test_market_bundle_is_injected(self, stub_agent):
        r = deep_analysis.run_agent("scout", object(), "NVDA", BUNDLE)
        assert r["_saw_market"] == {"info": {"NVDA": {"trailingPE": 42.0}}}, (
            "预取包没到蜂身上——各蜂会自己去抓 yfinance"
        )

    def test_stock_data_also_injected(self, stub_agent):
        """inject_prefetched 同时注入 stock_data / contexts / target_date，
        一并固化，免得将来只接了 market 这一路。"""
        r = deep_analysis.run_agent("scout", object(), "NVDA", BUNDLE)
        assert r["_saw_stock"] == {"NVDA": {"price": 100.0}}

    @pytest.mark.parametrize("empty", [None, {}], ids=["None", "空dict"])
    def test_no_bundle_still_runs(self, stub_agent, empty):
        """护栏：没有预取包时照常跑（单票 CLI / 取数失败的降级路径）。
        少了这条，一个「必须有包才跑」的过度修法也能让上面全绿。"""
        r = deep_analysis.run_agent("scout", object(), "NVDA", empty)
        assert r["score"] == 6.0 and r["_saw_market"] in (None, {})


class TestOldCallWasBroken:
    """反向自证：判据必须能判出旧写法是坏的，否则上面可能只是碰巧成立。"""

    def test_single_arg_call_raises(self):
        from swarm_agents import inject_prefetched
        with pytest.raises(TypeError, match="prefetched"):
            inject_prefetched(BUNDLE)          # 旧代码就是这么调的

    def test_signature_needs_two_args(self):
        import inspect
        from swarm_agents import inject_prefetched
        params = list(inspect.signature(inject_prefetched).parameters)
        assert params[:2] == ["agents", "prefetched"]


class TestPhase1PassesTheBundle:
    """补 M2 缺口：上面几条都直接调 `run_agent`，**从不经过 `pool.submit`**，
    所以「提交任务时忘了把包传下去」不会被它们发现（实测：删掉 submit 里的
    `prefetched` 参数，那 7 条全绿）。这条专盯那一处接线。

    用 AST 而不是文本匹配：`pool.submit(run_agent, name, board, ticker, prefetched)`
    的实参个数是结构信息，正则会被换行/改名/加注释晃到。
    """

    @staticmethod
    def _submit_calls():
        import pathlib
        tree = ast.parse(pathlib.Path(deep_analysis.__file__).read_text(encoding="utf-8"))
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "submit":
                if node.args and isinstance(node.args[0], ast.Name) \
                        and node.args[0].id == "run_agent":
                    out.append(node)
        return out

    def test_submit_forwards_prefetched(self):
        calls = self._submit_calls()
        assert calls, "找不到 pool.submit(run_agent, ...) —— 结构变了，本条需重写"
        for c in calls:
            names = [a.id for a in c.args if isinstance(a, ast.Name)]
            assert "prefetched" in names, (
                f"pool.submit(run_agent, ...) 没有把 prefetched 传下去："
                f"实参 {[getattr(a,'id',type(a).__name__) for a in c.args]}"
            )

    def test_run_agent_accepts_it(self):
        """成对的另一半：参数传了，但形参没了同样是坏的。"""
        import inspect
        assert "prefetched" in inspect.signature(deep_analysis.run_agent).parameters


class TestPhase0NoLongerInjects:
    def test_source_has_no_single_arg_inject(self):
        """源码层护栏：Phase 0 不得再出现少参数的 inject_prefetched 调用。
        （这条盯的是**那一行代码**，与上面的行为断言互补——
          行为断言证明新路径通了，这条证明旧路径没留在原地。）"""
        import pathlib
        src = pathlib.Path(deep_analysis.__file__).read_text(encoding="utf-8")
        import re
        # 允许注释里引用它；只查真实调用（行首缩进后直接跟调用）
        calls = re.findall(r"^\s+inject_prefetched\(([^)]*)\)", src, re.M)
        for c in calls:
            assert "," in c, f"inject_prefetched({c}) 只传了一个参数"
