"""零权重不变式：`config.EVALUATION_WEIGHTS` 里显式归零的维度必须一路保持为零。

v0.45.176 新增。**这组测试存在的理由是「谁会红？」答不上来。**

v0.45.172 把 signal / risk_adj 归零，v0.45.175 才发现生产实际权重里这两维仍占
~20% —— 中间整整一天没有任何东西变红，发现它靠的是人肉比对 `.swarm_results`
的字段。链路上有三层在 config 下游改写它：

    config → adapt_weights(0.2×config + 0.8×学习值) → QueenDistiller 整体替换
           → ML 反馈 ×factor 再归一 → RegimeWeightAdjuster 逐标的政体调整

挑「零维必须仍为零」当不变式的理由：它对下游所有**合法**变换都稳健 ——
乘法保零、政体偏移是相对的（`shift * w[k]`，w=0 时恒为 0）。所以它红了就一定
意味着有人接了一条会**改写** config 的通道，不会被 ML 反馈这类设计内调整误触发。

────────────────────────────────────────────────────────────────────────
⚠️ 夹具必须与真 config **可区分**（v0.45.176 二次检查时修的一个真 bug）
────────────────────────────────────────────────────────────────────────
初版用 `monkeypatch.setattr(config, "EVALUATION_WEIGHTS", ...)` 注入，而
`QueenDistiller.__init__` 与（当时的）`_assert_config_zeros_survive` 都会
`importlib.reload(config)` —— **reload 把 monkeypatch 冲掉**，函数读到的是真
config。5 条测试因此全是假绿：它们只是碰巧真 config 等于夹具值。

两条修法，缺一不可：

1. `_pin_config()` 把 `importlib.reload` 变成 no-op，让注入活过 reload；
2. 夹具值**故意与真 config 不同**（catalyst/sentiment/odds = 0.5/0.3/0.2，
   真值是 .332/.325/.343），并在用到它的地方**正面断言它接上了** ——
   否则「接上了」和「没接上」长得一模一样（同 MEMORY 里 `_isolate_env`
   泄漏进子进程那一族：断言要成对，「没读生产」必须配「确实读到了夹具」）。

⚠️ 本文件**不用任何 skip / 模块级 pytestmark**：全部合成数据、零外部依赖，
任何机器上都必须跑得到（模块级 `pytestmark` 会连坐无关测试，而 skip 把
「这条没验」渲染成「这条没问题」）。

每条断言旁边都注明了**能让它变红的变异**，这是加断言的准入条件
（MEMORY：举不出变异先别加）。
"""

from __future__ import annotations

import ast
import importlib
import logging
from pathlib import Path

import pytest

from gex_regime import RegimeWeightAdjuster
from pheromone_board import PheromoneBoard
from swarm_agents.queen_distiller import QueenDistiller
from tests._repo_files import own_python_files

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__

_ZEROED = ("signal", "risk_adj")

#: 注入用权重：两维归零（复刻 v0.45.172 的形状），
#: 其余三维**故意不等于**真 config 的 .332/.325/.343，好让「夹具没接上」变得可见。
_CFG_INJECTED = {"signal": 0.0, "catalyst": 0.5, "sentiment": 0.3,
                 "odds": 0.2, "risk_adj": 0.0}


def _pin_config(monkeypatch, weights=None):
    """把 config.EVALUATION_WEIGHTS 钉成 *weights*，并让它活过 importlib.reload。

    `QueenDistiller.__init__` 里的 `importlib.reload(_cfg)`（Bug #18 的热加载修复）
    会重新执行 config.py，从而丢弃 monkeypatch 设的属性。把 reload 变成 no-op
    是最小侵入的做法；monkeypatch 会在测试结束时把 `importlib.reload` 恢复。
    """
    import config as _cfg
    monkeypatch.setattr(importlib, "reload", lambda m: m)
    monkeypatch.setattr(_cfg, "EVALUATION_WEIGHTS",
                        dict(weights or _CFG_INJECTED), raising=False)


# 覆盖 RegimeWeightAdjuster 全部分支：宏观 ×3、GEX ×3、IV 三段
_REGIMES = [
    ("neutral", "unknown", None),
    ("risk_off", "unknown", None),
    ("risk_on", "unknown", None),
    ("neutral", "negative_gex", None),
    ("neutral", "positive_gex", None),
    ("risk_off", "negative_gex", 85.0),
    ("risk_on", "positive_gex", 15.0),
    ("neutral", "unknown", 50.0),
]


class TestRegimeAdjusterZeroInvariant:
    """`RegimeWeightAdjuster` 是链路最下游 —— 它不保零，上游怎么改都白搭。"""

    @pytest.mark.parametrize("macro,gex,iv", _REGIMES)
    def test_explicit_zero_stays_zero(self, macro, gex, iv):
        """变红的变异：删掉 `adjust_weights` 里的 `if w[k] <= 0: continue`。

        那正是 v0.45.176 之前的写法 —— `max(0.02, 0)` 把零复活成 2%，
        实测三种政体下 signal/risk_adj 全部落到 1.92%~1.97%。
        """
        w, _desc = RegimeWeightAdjuster().adjust_weights(
            base_weights=dict(_CFG_INJECTED), macro_regime=macro,
            gex_regime=gex, iv_rank=iv)
        for dim in _ZEROED:
            assert w[dim] == 0.0, (
                f"政体({macro}/{gex}/iv={iv})把显式归零的 {dim} 抬回了 {w[dim]:.4f}；"
                "地板 max(0.02,·) 的本意是「别把某维压到没有」，"
                "不是「不许某维为零」—— 0 是意图，不是「太小了」。")

    @pytest.mark.parametrize("macro,gex,iv", _REGIMES)
    def test_weights_still_normalized(self, macro, gex, iv):
        """成对的另一半：保零不能是靠「跳过归一化」实现的。

        变红的变异：把零豁免写成 `return w`（跳过末尾归一化）。
        """
        w, _ = RegimeWeightAdjuster().adjust_weights(
            base_weights=dict(_CFG_INJECTED), macro_regime=macro,
            gex_regime=gex, iv_rank=iv)
        assert abs(sum(w.values()) - 1.0) < 0.01, f"权重和 {sum(w.values()):.4f} != 1.0"

    def test_tiny_nonzero_is_still_floored(self):
        """地板的原有用途必须保留 —— 否则这次修复就是把一个 bug 换成另一个。

        变红的变异：把豁免条件从 `w[k] <= 0` 放宽成 `w[k] < 0.05`
        （那会让「极小但非零」也躲过地板，等于悄悄删掉地板）。
        """
        base = dict(_CFG_INJECTED, signal=0.001)
        w, _ = RegimeWeightAdjuster().adjust_weights(base_weights=base)
        assert w["signal"] > 0.01, (
            f"非零但极小的 0.001 应被地板抬升，实得 {w['signal']:.4f}——地板被误删了")
        assert w["risk_adj"] == 0.0, "同一次调用里，真正的零仍须保持为零"


class _FakeMLModel:
    """特征重要性全压在 crowding(→signal) 上：ML 反馈会试图放大 signal。"""

    def get_feature_importance(self):
        return {"crowding": {"weight": 0.90}, "momentum": {"weight": 0.02},
                "catalyst": {"weight": 0.02}, "iv_rank": {"weight": 0.02},
                "volatility": {"weight": 0.04}}


class TestMLFeedbackZeroInvariant:
    """ML 反馈层是乘法，天然保零 —— 但这件事必须被断言钉住，不能靠「碰巧」。"""

    def test_ml_feedback_cannot_resurrect_zero(self, monkeypatch):
        """变红的变异：把 `self.DIMENSION_WEIGHTS[dim] *= factor` 改成 `+= factor`
        或改成 `= factor`（两者都会让 signal 从 0 变成非零）。
        """
        _pin_config(monkeypatch)
        queen = QueenDistiller(PheromoneBoard(), ml_model=_FakeMLModel(), enable_llm=False)

        # 前提零：夹具真的接上了（值与真 config 不同，故这条能区分两种情况）
        assert queen.DIMENSION_WEIGHTS["catalyst"] != pytest.approx(0.332, abs=1e-3), (
            "读到的是真 config 而不是注入值 —— importlib.reload 又把 monkeypatch "
            "冲掉了，本条测试退化为「碰巧真 config 也有零维」")
        assert queen.ml_feedback_enabled, (
            "前提一：ML 反馈**确实被激活了** —— 否则它是空跑，证明不了乘法保零"
            "（MEMORY：判「接没接上」不能看输出动没动）")
        assert queen.ml_adjustments.get("signal", 1.0) > 1.0, (
            "前提二：ML 确实在试图**放大** signal，这样保零才是被检验过的")

        for dim in _ZEROED:
            assert queen.DIMENSION_WEIGHTS[dim] == 0.0, (
                f"ML 反馈把归零的 {dim} 抬回了 {queen.DIMENSION_WEIGHTS[dim]}")


class TestAdaptedWeightsNotWiredToProduction:
    """v0.45.176 断线守卫：生产代码不许再把 adapted_weights 传给 QueenDistiller。

    ⚠️ 枚举驱动而非名单驱动（MEMORY：按名字匹配只能证明「匹配到的是对的」）——
    扫**本仓被 git 跟踪的**每一个非测试 .py（v0.45.186 起；此前是裸 rglob，
    在生产 checkout 上会越界扫进 `.claude/worktrees/` 下 10 个嵌套 worktree），
    AST 找出所有 `QueenDistiller(...)` 调用，断言没有一个带 `adapted_weights=`。
    """

    #: 跳过的顶层目录。`tests/` 与 `experiments/` 都是**离线**代码：
    #: 前者本就要注入自定义权重，后者（权重实验、反事实重放）传自定义权重是它的正当用途。
    #: 跳过它们是有意的，不是漏扫 —— 判据是「这段代码会不会进每日扫描」。
    _SKIP_TOP = {"tests", ".git", "experiments"}

    @classmethod
    def _production_calls(cls, root=None):
        root = Path(root) if root is not None else REPO_ROOT
        hits = []
        # v0.45.186：**不是** `root.rglob("*.py")`。生产 checkout 的
        # `.claude/worktrees/` 下挂着 10 个嵌套 worktree，裸 rglob 在那里扫到
        # 22037 个 .py（git 跟踪的 338 个），于是本守卫报的是别的 worktree 里
        # 停留在 v0.45.176 之前的陈旧副本。理由全文见 `tests/_repo_files.py`。
        for py in own_python_files(root)[0]:
            rel = py.relative_to(root)
            # ⚠️ `_SKIP_TOP` 判的是 `rel.parts[0]`，只在 rel 确实以仓库根为基准时
            # 才成立。越界扫到 `.claude/worktrees/x/tests/…` 时 parts[0] 是
            # `.claude`，**整张跳过清单对嵌套树失效** —— 实测那轮把 225 个别的
            # worktree 的测试文件也当成了生产代码。
            if rel.parts[0] in cls._SKIP_TOP:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "QueenDistiller"):
                    hits.append((str(rel), node.lineno,
                                 {kw.arg for kw in node.keywords if kw.arg}))
        return hits

    def test_enumeration_actually_found_something(self):
        """先自证探针有效 —— 一个调用点都没扫到时，下一条会恒真地绿。

        变红的变异：把 `node.func.id == "QueenDistiller"` 写成 `"QueenDistillerX"`。

        ⚠️ v0.45.186 更正：这里原本写的是「把 `rglob("*.py")` 写成
        `rglob("*.pyx")`」—— 枚举已经不用 rglob 了，那条变异做不出来。
        **说谎的 docstring 危害不在于它错，在于它让人停止检查**
        （MEMORY `alpha-hive-board-eviction` 同款）。
        """
        calls = self._production_calls()
        assert calls, ("AST 扫描没找到任何生产侧 QueenDistiller 调用点 —— "
                       "探针坏了，不是「没有违规」")

    def test_no_production_call_passes_adapted_weights(self):
        """变红的变异：把 `alpha_hive_daily_report.py` 里的
        `QueenDistiller(board, enable_llm=...)` 改回 `QueenDistiller(board,
        adapted_weights=_adapted_diag, ...)`（即 v0.45.175 及以前的写法）。
        """
        bad = [(f, ln) for f, ln, kws in self._production_calls()
               if "adapted_weights" in kws]
        assert not bad, (
            f"生产代码把 adapted_weights 传回了 QueenDistiller：{bad}。"
            "该通道自 v0.45.176 起是只读诊断 —— 它学的是「谁更爱说中性」而不是准头，"
            "接回去会整体顶掉 config.EVALUATION_WEIGHTS（理由见 "
            "`Backtester.adapt_weights` 的 docstring）。")

    # ────────────────────────────────────────────────────────────────
    # v0.45.186：枚举口径守卫。**必须自带病灶** —— 见下面 docstring。
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def _plant_pathology(root):
        """造一棵带病灶的树：根上一个干净的生产文件 + 嵌套 checkout 里一个脏的。

        病灶形状照抄生产 checkout 的真实情况（2026-09-11 实测）：
        `.claude/worktrees/<name>/` 下是 10 个**嵌套 git worktree**，各自带一份
        完整的仓库副本。停留在 v0.45.176 之前的那几份，`alpha_hive_daily_report.py`
        里仍写着 `QueenDistiller(..., adapted_weights=...)`。
        """
        root.mkdir(parents=True, exist_ok=True)
        (root / "alpha_hive_daily_report.py").write_text(
            "q = QueenDistiller(board, enable_llm=False, ml_model=m)\n", encoding="utf-8")
        stale = root / ".claude" / "worktrees" / "stale-worktree-fixture"
        (stale / "tests").mkdir(parents=True, exist_ok=True)
        (stale / "alpha_hive_daily_report.py").write_text(
            "q = QueenDistiller(board, adapted_weights=_adapted_diag, enable_llm=False)\n",
            encoding="utf-8")
        # 嵌套树里的 `tests/` 也要造一个：`_SKIP_TOP` 判的是 `rel.parts[0]`，
        # 而这里的 parts[0] 是 `.claude` ⇒ 跳过清单对嵌套树**整个失效**。
        (stale / "tests" / "test_queen_distiller.py").write_text(
            "q = QueenDistiller(board, adapted_weights={'signal': 1.0})\n", encoding="utf-8")
        return stale

    def test_scan_excludes_nested_checkouts(self, tmp_path):
        """嵌套 checkout 里的陈旧副本**不得**被当成本仓生产代码报出来。

        ⚠️ 这条必须用 tmp 树，**不能只在真仓库上跑** —— 病灶只存在于生产
        checkout（`~/Desktop/Alpha Hive` 下有 10 个嵌套 worktree），10 个
        worktree 里一个都没有。「在没有病灶的环境里测防御＝没测」：
        v0.45.176 加这条守卫的人在 worktree 里看到的是全绿，而生产上它自
        提交之日起就是红的 —— 可见性是**反的**，唯一会红的那台机器没人看。

        变红的变异：把 `_production_calls` 的枚举换回 `root.rglob("*.py")`
        （即 v0.45.176~185 的写法）—— 实测报出 2 条嵌套树命中。
        """
        self._plant_pathology(tmp_path)
        hits = self._production_calls(tmp_path)
        files = sorted(f for f, _ln, _kw in hits)

        assert files == ["alpha_hive_daily_report.py"], (
            f"枚举越过了仓库边界，扫到嵌套 checkout：{files}。"
            "这些副本停留在别的版本上，它们的违规不是本仓的违规。")

    def test_pathology_fixture_actually_bites(self, tmp_path):
        """反向自证：夹具真的**造出了**会被旧写法命中的东西。

        没有这一条，上一条可能只是因为夹具压根没写出匹配项而恒绿
        （MEMORY：探针要先自证有效；「没扫到」和「没有违规」长得一样）。
        """
        stale = self._plant_pathology(tmp_path)
        naive = [str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.py")
                 if "adapted_weights" in p.read_text(encoding="utf-8")]
        assert len(naive) == 2, f"夹具没造出预期的 2 个病灶文件：{naive}"
        assert stale.is_dir()

    def test_real_scan_stays_inside_the_repo(self):
        """真仓库上的正面断言：扫出来的路径一个都不许落在 `.claude/` 里。

        ⚠️ 这条在 worktree 里是**恒真**的（worktree 没有嵌套 worktree），
        所以它不能单独存在 —— 它的价值只在生产 checkout 上，
        病灶的可测性由上面两条 tmp 树负责。
        """
        outside = sorted({f for f, _ln, _kw in self._production_calls()
                          if f.startswith(".claude") or f.startswith("..")})
        assert not outside, f"扫描越过仓库边界：{outside}"


class TestZeroWeightDownstreamBehaviour:
    """真正的零权重会改变下游行为 —— 这个变化要被钉住，不能只在别处「顺手变绿」。

    旧的 `max(0.02, ·)` 地板一直在暗中保证「权重和 > 0」。修掉它之后，
    **只剩零权重维度可用**的标的，`weight_total == 0` ⇒ `base_score` 退回中性 5.0。
    这是配置意图的正确后果（手上只有 config 说不携带信息的维度，就该没话说），
    不是 bug —— 但它确实让 `tests/test_close_t7_production_wiring.py` 那条
    以 `signal` 为载体的测试变过一次红。那条已改用 `sentiment`，
    **本类是那次改动的正面对价**：行为变化在这里有断言，而不是被夹具吞掉。
    """

    @staticmethod
    def _one_dim(dim):
        return [{"score": 9.0, "direction": "bullish", "confidence": 0.9,
                 "discovery": f"only {dim}", "source": "TestAgent",
                 "dimension": dim, "data_quality": {"test": "real"}}]

    def test_only_zeroed_dims_available_yields_neutral(self, monkeypatch):
        """变红的变异：把 `gex_regime` 的零豁免删掉（零被抬回 2% ⇒ 分数不再是 5.0）。"""
        _pin_config(monkeypatch)
        queen = QueenDistiller(PheromoneBoard(), enable_llm=False)
        assert queen.DIMENSION_WEIGHTS["catalyst"] == pytest.approx(0.5), "夹具没接上"
        out = queen.distill("AAA", self._one_dim("signal"))
        assert out["final_score"] == pytest.approx(5.0, abs=0.05), (
            f"唯一可用维度权重为 0，分数却是 {out['final_score']} —— "
            "说明某处又把零权重抬回了非零，9.0 分因此漏进了 final_score")

    def test_weighted_dim_still_moves_the_score(self, monkeypatch):
        """成对的另一半：别把「全都中性」当成保零成功。

        变红的变异：把 `_compute_weighted_score` 的
        `base_score = weighted_sum / weight_total if weight_total > 0 else 5.0`
        改成无条件 `5.0`（那会让上一条恒绿，本条变红）。
        """
        _pin_config(monkeypatch)
        queen = QueenDistiller(PheromoneBoard(), enable_llm=False)
        assert queen.DIMENSION_WEIGHTS["sentiment"] == pytest.approx(0.3), "夹具没接上"
        out = queen.distill("BBB", self._one_dim("sentiment"))
        assert out["final_score"] != pytest.approx(5.0, abs=0.05), (
            "权重非零的维度给了 9.0 分，final_score 却仍是中性 —— "
            "评分链路是死的，上一条测试因此毫无意义")


class TestRegimeLayerRuntimeGuard:
    """政体层保零守卫 —— **探针必须放在真正喂进评分的那个值上**。

    二次检查时发现的真 bug：初版只有 `_assert_config_zeros_survive`，它检的是
    蜂后的 `DIMENSION_WEIGHTS`（政体层**上游**）。实测把 `gex_regime` 的地板 bug
    放回去，那个观测点**仍返回 True**，而逐标的权重已是 signal=0.0192 ——
    探针放在了它要防的那个 bug 的上游，等于没放。
    """

    def _queen(self, monkeypatch):
        _pin_config(monkeypatch)
        q = QueenDistiller(PheromoneBoard(), enable_llm=False)
        assert q.DIMENSION_WEIGHTS["catalyst"] == pytest.approx(0.5), "夹具没接上"
        return q

    def test_guard_catches_resurrected_zero(self, monkeypatch, caplog):
        """喂「零被抬回 2%」的政体权重，必须判违反并打 error。

        变红的变异：让 `_assert_regime_preserved_zeros` 无条件 `return True`。
        """
        q = self._queen(monkeypatch)
        resurrected = {"signal": 0.0192, "catalyst": 0.4808, "sentiment": 0.2885,
                       "odds": 0.1923, "risk_adj": 0.0192}
        with caplog.at_level(logging.ERROR):
            ok = q._assert_regime_preserved_zeros("AAA", resurrected)
        assert ok is False, "地板复活的零没被判违反 —— 守卫没牙"
        assert any("政体层保零违反" in r.getMessage() for r in caplog.records), \
            "判了违反却没打 error —— 无人值守的扫描里没人会知道"

    def test_guard_passes_when_zeros_preserved(self, monkeypatch):
        """成对的另一半：合规时不许误报。

        变红的变异：让它无条件 `return False`。
        """
        q = self._queen(monkeypatch)
        ok = q._assert_regime_preserved_zeros(
            "AAA", {"signal": 0.0, "catalyst": 0.5, "sentiment": 0.3,
                    "odds": 0.2, "risk_adj": 0.0})
        assert ok is True

    def test_guard_is_actually_called_by_distill(self, monkeypatch):
        """**数读者**：守卫写了没人调用就是死代码（MEMORY「死字段」判据）。

        变红的变异：删掉 `distill()` 里那行
        `self._assert_regime_preserved_zeros(ticker, _regime_weights_used)`。
        """
        q = self._queen(monkeypatch)
        seen = []
        monkeypatch.setattr(
            q, "_assert_regime_preserved_zeros",
            lambda ticker, w: seen.append((ticker, dict(w))) or True)
        q.distill("CCC", TestZeroWeightDownstreamBehaviour._one_dim("sentiment"))
        assert seen, "distill() 没有调用政体层保零守卫 —— 它是死代码"
        assert seen[0][0] == "CCC"
        assert set(seen[0][1]) == set(q.DIMENSION_WEIGHTS), \
            "传给守卫的不是完整的五维权重表"

    def test_upstream_guard_alone_is_insufficient(self, monkeypatch):
        """钉住「为什么需要两层」：上游守卫对政体层的违反是瞎的。

        这条不是重复 —— 它把两个守卫的**分工**变成可执行的断言。
        变红的变异：把 `_assert_config_zeros_survive` 改成也去检政体权重
        （那样两层职责重叠，本条的前半句就不成立了，应当有人来更新这条注释）。
        """
        from alpha_hive_daily_report import _assert_config_zeros_survive
        q = self._queen(monkeypatch)
        resurrected = dict(q.DIMENSION_WEIGHTS, signal=0.0192, risk_adj=0.0192)

        upstream_ok = _assert_config_zeros_survive(
            q.DIMENSION_WEIGHTS, cfg_weights=_CFG_INJECTED)
        downstream_ok = q._assert_regime_preserved_zeros("AAA", resurrected)

        assert upstream_ok is True, "上游守卫检的是蜂后基准权重，这里本就是合规的"
        assert downstream_ok is False, (
            "而实际喂进评分的政体权重已经违反 —— 只有下游守卫看得见它。"
            "少了任一层，2026-09-09 那类事故都还会重演一次")


class TestScanTimeObservationPoint:
    """扫描期观测点自身要有牙 —— 一个永远返回 True 的检查等于没有检查。"""

    def test_detects_the_actual_v0_45_175_incident(self, caplog):
        """喂 09-09 生产实际权重，必须判违反。

        这些数字是真的：`.swarm_results_2026-09-09.json` 里 30 只标的
        `dimension_weights` 的加权均值。**这条问的是「当时它会不会红」。**

        变红的变异：让 `_assert_config_zeros_survive` 无条件 `return True`。
        """
        from alpha_hive_daily_report import _assert_config_zeros_survive
        incident = {"signal": 0.2059, "catalyst": 0.1399, "sentiment": 0.2694,
                    "odds": 0.1866, "risk_adj": 0.1982}
        with caplog.at_level(logging.ERROR):
            ok = _assert_config_zeros_survive(incident, cfg_weights=_CFG_INJECTED)
        assert ok is False, "v0.45.175 的实际事故权重没有被判违反 —— 观测点没牙"
        assert any("权重不变式违反" in r.getMessage() for r in caplog.records), \
            "判了违反却没打 error 日志 —— 无人值守的定时扫描里没人会知道"

    def test_passes_on_conforming_weights(self):
        """成对的另一半：合规权重不许误报。

        变红的变异：让它无条件 `return False`。缺这条的话，
        「无条件 return False」能让上一条全绿。
        """
        from alpha_hive_daily_report import _assert_config_zeros_survive
        assert _assert_config_zeros_survive(
            dict(_CFG_INJECTED), cfg_weights=_CFG_INJECTED) is True

    def test_reads_live_config_when_not_injected(self, monkeypatch):
        """不传 `cfg_weights` 时必须读当下的 config，且**不 reload**。

        变红的变异：把函数里的 `import config as _cfg` 那段改回
        `importlib.reload(_cfg)`（reload 会冲掉下面的注入，函数于是读到真 config，
        判定翻转）。这条正是二次检查时抓到的那个 bug 的守卫。
        """
        import config as _cfg
        no_zero = {"signal": 0.5, "catalyst": 0.2, "sentiment": 0.1,
                   "odds": 0.1, "risk_adj": 0.1}
        monkeypatch.setattr(_cfg, "EVALUATION_WEIGHTS", no_zero, raising=False)

        from alpha_hive_daily_report import _assert_config_zeros_survive
        incident = {"signal": 0.2059, "catalyst": 0.1399, "sentiment": 0.2694,
                    "odds": 0.1866, "risk_adj": 0.1982}
        assert _assert_config_zeros_survive(incident) is True, (
            "注入的 config 没有任何零维，本就无从违反 —— 判成违反说明函数"
            "读的不是注入值（多半是 importlib.reload 又把 monkeypatch 冲掉了）")
