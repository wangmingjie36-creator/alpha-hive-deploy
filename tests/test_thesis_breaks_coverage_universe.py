"""失效条件覆盖率的分母调用时从 `config.WATCHLIST` 派生；配置里的块不许脱离标的池。

v0.45.217 新增。

事故
----
`thesis_breaks_config.json:_all_tickers` 手抄 24 只，是 2026-08-25 之前 `config.WATCHLIST`
的旧快照（13 只现役 + AMD/AMGN/BIIB/REGN/PLUG/RUN/ICLN/SQ/COIN/MSTR/UPST 11 只早已降级
候补池）。`ThesisBreakConfig.get_coverage_info()` 拿它当分母 ⇒ 网站 30 只里 17 只
（ABBV/AMC/BRK-B/COST/CRM/CVX/DE/DELL/MU/NFLX/SNOW/T/TMO/TMUS/VZ/WMT/XOM）永远不进统计，
不在网站上的 11 只反倒算「已覆盖」，报 24/24=100%。

**`_all_tickers` 与 `config.WATCHLIST` 之间没有一条断言**，所以没人红。
注意坏的只是分母：那 17 只**都有块**，「WATCHLIST 每只都有块」这个方向一直有守卫
（`test_thesis_break_rendering.py::test_real_config_renders_for_every_watchlist_ticker`，
走真实渲染路径；变异实跑：用改动前的代码与测试删掉 XOM 块，它照样红）。
本文件**不重复**那个方向——块内条件是否为空由
`test_thesis_breaks.py::test_all_configured_tickers_have_valid_structure` 逐块守。

守什么（都是之前没人守的）
--------------------------
1. **分母是调用时派生的**，既不是快照，也不是块的键。块的键不行：块 = WATCHLIST 全部
   + 部分候补池（v0.45.217 时 41 = 30 + 11），拿它当分母 covered 恒等于 total，
   覆盖率成了恒 100% 的恒真式，唯一该报的「谁缺失效条件」永远报不出来。
2. **没有两个池子都不认的孤儿块**（键打错 / 标的彻底移除后留下的死配置）。
   候补池标的的块**不算孤儿**（`--extended-pool` 时在用）；候补池里没写块的标的也不算违规。
3. 手抄名单不许回到配置里 —— 由 `test_thesis_breaks_config_authored_only.py` 的
   `ALLOWED_META_KEYS`（v0.45.217 起不含 `_all_tickers`）守，本文件不重复。

⚠️ 不用 skip：配置被 git 跟踪、`WATCHLIST` 在代码里，任何机器都跑得到。
每条断言旁注明了**实跑过**、能让它变红的变异。
"""

from __future__ import annotations

import config
from thesis_breaks import ThesisBreakConfig


def orphan_blocks(cfg: dict, pools) -> list[str]:
    """配置里不属于任何标的池的块（`_` 元数据键不算块）。"""
    pool = set(pools)
    return sorted(k for k in cfg if not k.startswith("_") and k not in pool)


def _real_blocks() -> list[str]:
    return [k for k in ThesisBreakConfig._load() if not k.startswith("_")]


def test_coverage_denominator_is_the_watchlist():
    # 变异（实跑）：get_coverage_info 退回改动前（读 _all_tickers，配置恢复或不恢复该键）⇒ 红；
    #             分母改从块的键派生 ⇒ 红
    info = ThesisBreakConfig.get_coverage_info()
    wl = list(config.WATCHLIST)
    assert info["total"] == len(wl) == len(set(wl))
    assert set(info["covered_tickers"]).isdisjoint(info["missing_tickers"])
    assert set(info["covered_tickers"]) | set(info["missing_tickers"]) == set(wl)


def test_no_block_outside_both_ticker_pools():
    # 变异（实跑）：加一个 ZZZZ 块 ⇒ 只有这条红；把 BRK-B 块改名 BRKB ⇒ 红
    #             （形状守卫放行 BRKB；渲染测试也会红，但它报的是「BRK-B 缺条件」，不指出错键在哪）
    orphans = orphan_blocks(ThesisBreakConfig._load(), [*config.WATCHLIST, *config.WATCHLIST_EXTENDED])
    assert orphans == [], (
        f"配置里有块 {orphans} 不属于 WATCHLIST 也不属于 WATCHLIST_EXTENDED："
        "要么键打错了（对应标的其实没被覆盖），要么是标的移除后留下的死配置。"
        "移出日常扫描的标的应降级到 WATCHLIST_EXTENDED，而不是删掉。")


class TestGuardsHaveTeeth:
    """没有这一组，上面两条全绿只证明「今天恰好对得上」——手抄一份今天的名单照样全绿。"""

    _BLOCK = {lv: {"conditions": [{"id": "x", "metric": "m", "trigger": "t"}]}
              for lv in ("level_1_warning", "level_2_stop_loss")}

    def test_denominator_follows_watchlist_not_block_keys_nor_snapshot(self, monkeypatch):
        # 变异（实跑）：分母写死成今天的 30 只 ⇒ 只有这条红；
        #             thesis_breaks 模块顶部 `from config import WATCHLIST`（import 期冻结）⇒ 只有这条红；
        #             分母改从块的键派生 / 退回读 _all_tickers ⇒ 红
        monkeypatch.setattr(ThesisBreakConfig, "_data",
                            {"NVDA": self._BLOCK, "AMD": self._BLOCK, "_machine_conditions_note": {}})
        monkeypatch.setattr(config, "WATCHLIST", {"NVDA": {}, "ZZZZ": {}})
        assert ThesisBreakConfig.get_coverage_info() == {
            "total": 2, "covered": 1, "coverage_pct": 50,
            "covered_tickers": ["NVDA"], "missing_tickers": ["ZZZZ"]}

    def test_orphan_is_flagged_but_meta_keys_and_extended_pool_are_not(self):
        # 变异（实跑）：真配置那条只传 WATCHLIST、漏掉候补池 ⇒ 真配置那条红（候补池标的的块被误报）
        cfg = {"NVDA": self._BLOCK, "AMD": self._BLOCK, "BRKB": self._BLOCK, "_machine_conditions_note": {}}
        assert orphan_blocks(cfg, ["NVDA", "BRK-B", "AMD"]) == ["BRKB"]

    def test_real_inputs_are_non_degenerate(self):
        # 正对照：名单与配置真的读到了东西；否则「零孤儿」只证明两边都是空的
        # （刻意不断言「WATCHLIST ⊆ 块」：那个方向归渲染测试守，见模块 docstring；
        #   变异实跑：删 XOM 块 + 屏蔽渲染测试那条 ⇒ 本文件零红，即本文件确实没重复它）
        assert len(config.WATCHLIST) >= 20
        assert len(_real_blocks()) >= 20
