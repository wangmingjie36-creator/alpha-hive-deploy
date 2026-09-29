"""
IC 重跑就绪度判定（v0.44.4）

为什么需要这个工具、以及为什么需要测它
--------------------------------------
v0.44.1~0.44.3 修了 ML 预期收益的看多偏斜并把 RivalBee 三个硬编码特征接上真实数据，
但**方向是否变准没有验证** —— 那要等新样本（实测约 **25 个不重叠周**，
见 `experiments/ic_power_report.md`）。

"等攒够"这件事本来没有承载物：不在测试里（测试跑当下）、不在告警里（没有异常），
全靠人记着。半年后没人记得。`ic_rerun_readiness.py` 就是那个承载物。

而它自己也必须被测：**一个永远说"未就绪"的就绪度判定器，和没有它是一样的。**
下面 `TestVerdictActuallyFlips` 就是为此 —— 与
`test_distribution_invariants.py::TestGuardsHaveTeeth` 同一思路。
"""

import sqlite3

import pytest

import ic_rerun_readiness as rr


@pytest.fixture
def db(tmp_path):
    """建一个最小 predictions 表的工厂。

    rows: [(date, ticker, ripe)]  ripe=True 表示 T+7 已回填
    """
    counter = {"n": 0}

    def _make(rows):
        # 每次调用建独立文件 —— 有测试在同一个用例里建两个库做对照
        counter["n"] += 1
        p = tmp_path / f"t{counter['n']}.db"
        con = sqlite3.connect(p)
        con.execute(
            "CREATE TABLE predictions ("
            " date TEXT, ticker TEXT, checked_t7 INTEGER,"
            " close_t7 REAL, price_at_predict REAL)"
        )
        con.executemany(
            "INSERT INTO predictions VALUES (?,?,?,?,?)",
            [(d, t, 1 if ripe else 0, 110.0 if ripe else None, 100.0)
             for d, t, ripe in rows],
        )
        con.commit()
        con.close()
        return p
    return _make


# v0.45.31: 起始日不再硬编码。此前默认写死 "2026-08-17"（当时的最新世代），
# v0.45.30 追加新世代边界后，这些样本全部落到边界之前被过滤掉，7 个测试变红。
# 世代边界会持续追加，硬编码必然反复失效 —— 一律从 _COHORT_HISTORY 末条推导。
_COHORT_START = rr._COHORT_HISTORY[-1][0]


def _after_cohort(weeks=0):
    """世代起始日之后 N 周的日期（供测试构造世代内样本）。"""
    import datetime as dt
    return (dt.date.fromisoformat(_COHORT_START) + dt.timedelta(weeks=weeks)).isoformat()


def _weekly_rows(n_weeks, start=None, tickers=("AAA", "BBB"),
                 ripe=True):
    """每周一条（不重叠取样单位就是周），共 n_weeks 周。默认从当前世代起始日开始。"""
    import datetime as dt
    d0 = dt.date.fromisoformat(start or _COHORT_START)
    out = []
    for w in range(n_weeks):
        d = (d0 + dt.timedelta(weeks=w)).isoformat()
        for t in tickers:
            out.append((d, t, ripe))
    return out


# ════════════════════════════════════════════════════════════════════════════
# 世代边界
# ════════════════════════════════════════════════════════════════════════════

class TestCohortBoundary:

    def test_uses_the_latest_generation(self):
        c = rr.cohort_start()
        assert c["date"] == rr._COHORT_HISTORY[-1][0]
        assert c["n_generations"] == len(rr._COHORT_HISTORY)

    #: 已知世代的**最小集合**：这些 (日期, 版本) 必须在表里。
    #: ⚠️ **刻意写死，不许从 `_COHORT_HISTORY` 派生** —— 派生即恒真。
    #: 追加新世代不需要动这里（子集断言）；只有**删掉**一条历史世代才会红。
    #: 表头写着「只追加，不改写（审计轨迹）」，此前没有任何东西执行这句话。
    MUST_BE_ENUMERATED = frozenset({
        ("2026-08-17", "v0.44.1~0.44.3"),
        ("2026-08-26", "v0.45.30"),
        ("2026-08-27", "v0.45.50"),
        ("2026-09-05", "v0.45.128"),
        ("2026-09-07", "v0.45.151"),
        ("2026-09-07", "v0.45.156"),
        ("2026-09-07", "v0.45.163"),
        # v0.45.334：GexRegimeModifier 断开（作废 120 条的那条；原定 09-24 未赶上扫描，改 09-28）。
        # 钉住它也钉住了 `test_cohort_start_never_moves_backwards` 的下限 —— 删掉它，边界会退回 09-18。
        ("2026-09-28", "v0.45.334"),
        # v0.45.340：Buzz 情绪动量按扫描日回看归档（维度 IC 协议 H1 的 buzz_v1 锚点——删了它锚点回退）。
        ("2026-09-28", "v0.45.340"),
        # v0.45.349：中性化 OracleBee gex_signal + BearBee gex<0 下限。signal_archive 里挂在它上面的信号数分口径：
        # 闭包口径 `_scope_closure` 23 个 + composite.final_score = 24 个（7 个只换标签）；对生产归档实有的
        # 70 个信号名跑 `generation_boundaries` 为 26 个（9 个只换标签——多出退役名 guard.consistency /
        # guard.top_signals_count，不认识的名字受每一条边界约束）。真正后移的 17 个两种口径相同。
        ("2026-09-28", "v0.45.349"),
        # v0.45.357：日报 VIX 当日收盘 + 陈旧 VIX 不计 Guard 票。唯一真正后移的归档信号是 guard.macro_adj
        # （08-15→09-28，661 行 / 已成熟 420 条），其余 9 个挂在它上面的只换标签。
        ("2026-09-28", "v0.45.357"),
        # v0.45.366：补跑的 Guard 宏观票对齐目标日（VIX 取 CSV 的 D 行并计票 / 期限结构读快照 / FOMC 与板块按 D）。
        # 同日同 signal_archive 集合，0 个信号后移。
        ("2026-09-28", "v0.45.366"),
    })

    def test_no_known_cohort_has_vanished(self):
        """审计轨迹只能变长。

        v0.45.166 实测（变异）：删掉**最新**一条 → 22 项全绿；
        删掉**最早**一条 → 同样 22 项全绿。**两头都没人看守** ——
        比姊妹表 `probability_scorecard._ML_ESTIMATOR_GENERATIONS` 还松
        （那张至少钉住了最早那条）。
        """
        assert self.MUST_BE_ENUMERATED, "最小集合被清空了——本条已退化为恒真"
        live = {(d, v) for d, v, _ in rr._COHORT_HISTORY}
        missing = self.MUST_BE_ENUMERATED - live
        assert not missing, (
            f"世代边界表少了已知条目 {sorted(missing)}；表头写明「只追加，不改写」。"
            "若确实要改写审计轨迹，请连同本最小集合一起显式修改。")

    def test_cohort_start_never_moves_backwards(self):
        """成对的另一半：钉的是**消费方看到的值**，不是表的内容。

        这两条抓的东西不一样，缺一不可：
          · 删掉**中间**一条 → 只有 `test_no_known_cohort_has_vanished` 红。
          · 删掉**末尾**一条 → 两条都红，而这条说出的是**后果**：
            `cohort_start()` 取 `[-1]`，末条一没，边界就**往回退**，
            于是上一代的样本被静默并进当前世代 —— 正是这张表存在要防的那件事
            （`assess()` 按 `date >= 边界` 过滤，边界退了没有任何告警）。

        对追加安全：新世代只会把边界往后推，`>=` 恒成立。
        """
        pinned_max = max(d for d, _ in self.MUST_BE_ENUMERATED)
        got = rr.cohort_start()["date"]
        assert got >= pinned_max, (
            f"世代边界回退了：cohort_start()={got} 早于已知的 {pinned_max}。"
            "边界前的样本会被当作本代样本混算，且混算是静默的。")

    def test_history_is_append_only_and_ordered(self):
        """世代历史是审计轨迹：只追加、按时间递增，(日期, 版本) 不重复。

        v0.45.156：原断言是 `len(set(dates)) == len(dates)`（**日期**唯一）。
        它与同一份表 docstring 里「再次改动 … **必须追加一条**」直接冲突——
        同一天部署两个都改 final_score 的版本时，照规矩追加就会让它变红，
        于是它保护的不是不变式，而是「别在同一天改两次」。
        （与 v0.45.151 改 `TestCohortBoundaryAppended` 是同一物种。）

        真正的不变式：日期**非降**（`sorted` 本就允许并列）+ (日期, 版本) 唯一。
        同日多条不影响语义——`cohort_start()` 取 `[-1]`，`assess` 按 `date >= 边界`
        过滤，两者都只看日期值本身。
        """
        entries = [(d, v) for d, v, _ in rr._COHORT_HISTORY]
        dates = [d for d, _ in entries]
        assert dates == sorted(dates), f"世代边界未按时间排序: {dates}"
        assert len(set(entries)) == len(entries), f"(日期, 版本) 重复: {entries}"

    def test_every_generation_records_why(self):
        """只有日期没有原因的边界，半年后无法判断它是否仍然适用。"""
        for date, version, reason in rr._COHORT_HISTORY:
            assert len(date) == 10 and date[4] == "-"
            assert version and reason, f"{date} 缺版本或原因"

    def test_samples_before_boundary_are_excluded(self, db):
        """世代之前的样本必须一条都不算 —— 混算是静默的，数字照出但没意义。

        注意早期序列取的是当前世代边界之前的 20 周，全部应被过滤。
        跨过边界就测不到过滤逻辑了，故起止日均由 _COHORT_START 推导。
        """
        import datetime as dt
        _early = (dt.date.fromisoformat(_COHORT_START) - dt.timedelta(weeks=32)).isoformat()
        rows = (_weekly_rows(20, start=_early)            # 全部早于边界
                + _weekly_rows(2))                        # 世代内 2 周
        res = rr.assess(db_path=db(rows), today=_after_cohort(3))
        assert res["weeks_accrued"] == 2, "边界之前的样本被算进来了"
        assert res["n_all_samples"] == 4, "世代内总样本数也应只数世代内的"


class TestCohortReasonsAreNotRewrittenInPlace:
    """表头「只追加，不改写（审计轨迹）」的**另一半**：已进 main 的条目，原因文本也不许就地改。

    `TestCohortBoundary.MUST_BE_ENUMERATED` 只钉 (日期, 版本) 还在，改 reason 它看不见：v0.45.349 评审变异
    R15 / R16（就地改写一条旧条目的原因文本）全绿存活。而这张表的价值正在于「当时写了什么」——
    本仓的更正惯例（v0.45.176 更正 v0.45.172、v0.45.349 更正 v0.45.334）都是**追加**一条新条目、
    原文不动，读者才能看到「原来说错了什么、何时改口」。就地改写 ⇒ 这段历史消失，且没有任何东西会红。

    `FROZEN` 的摘要 = `sha256(reason.encode("utf-8"))`，**取自 `origin/main` 4c5c0e62 的原文**
    （`git show origin/main:ic_rerun_readiness.py`，不是工作区；当时工作区前 30 条与之逐字节相同）。
    ⚠️ 刻意写死、不从 `_COHORT_HISTORY` 派生——派生即恒真。
    **只钉已进 main 的**：截至 v0.45.357（v0.45.366 补进 349 / 357，摘要取 origin/main 574e5505）。
    本版自己那条（v0.45.366）进 main 之前还可能改，不钉；它进 main 之后，
    下一个动本文件的人把它（及其后已进 main 的条目）补进来，摘要照样取 `origin/main` 的原文，并把 `LAST_FROZEN` 后移。
    """

    LAST_FROZEN = "v0.45.357"

    #: version → (日期, sha256(reason))。
    FROZEN = {
        "v0.43.24": ("2026-08-15", "70d4460cb9de121956fddee2464a22eec06710f83da9b5ac6938edc3193103aa"),
        "v0.43.25": ("2026-08-15", "7c1be5d0a8ca600963acc9d599444e621cdbeda64b3e3d9982a72a2d0d1811a1"),
        "v0.44.1~0.44.3": ("2026-08-17", "5d9ee95680b587493bc3e83b77cac777ae66c5ec2be72360d27e04f178009fdc"),
        "v0.45.2~0.45.15": ("2026-08-26", "f36099eea9b1c2969ae8c861627c1e06c5a018f221da8e7b7627a26c6e91ffa7"),
        "v0.45.30": ("2026-08-26", "d4c887b11c74fe4df86b3ddc40209f23b3721f87f4b87b0049c54fd5e8bdbf46"),
        "v0.45.31": ("2026-08-26", "9d1645b0b42bf6ee2bb581634bcb7a5f0a8c34ad777279903907b8951b2606ad"),
        "v0.45.32": ("2026-08-26", "59d783121d221784c3ac3e063c7e6bb34758454956616cccf14650175e33d4c5"),
        "v0.45.50": ("2026-08-27", "a220a07a2791b5d6576e546f809b27e00e71d75a8817f65a465076c77a8d213d"),
        "v0.45.128": ("2026-09-05", "18577a94ab6176910029339110d46fbb163fb2bb462d93fd5ead14c0ed0a41f8"),
        "v0.45.151": ("2026-09-07", "7c6d629810ad9af6a8fb1a851b02d215afe421fec6aed5e2c600cf3c1e65b112"),
        "v0.45.156": ("2026-09-07", "61378de5a9e9613f58f07c98e4fb94cc05156678dff86de4873c569265e8a330"),
        "v0.45.163": ("2026-09-07", "8abbed287a120862a1a024a1cd538d50eb516686bf56a875a59fca8fb1641103"),
        "v0.45.172": ("2026-09-09", "17a4f77982391bdf6e7244d3e70463554775e9f111eae39a803efe7744c8a92a"),
        "v0.45.176": ("2026-09-10", "6f027bd6ba14d7f47cdb53e8549b10f26a683694da43ce6bed0fa31226553ab4"),
        "v0.45.191": ("2026-09-10", "69852e674b6d12b1f48ae3353e3a97fee3945ae072a713d87195b6b2ff944383"),
        "v0.45.197": ("2026-09-11", "242f7f89ca1c79fabfe0bd46cd1bd0a5684bbdc027120e23210874200decda8a"),
        "v0.45.201": ("2026-09-11", "ddff89368dc52016acde67a4e75f2221f4f74a7f189b96df2a36b762fc22e8ed"),
        "v0.45.209": ("2026-09-11", "b093fb2ecbd1f2256502e9aa3cc0c7ac25e03f6a16fa0cfb00b095e0f0d05959"),
        "v0.45.212": ("2026-09-13", "4f75e48b2047f51d0593ca1881aa9861da9f1417ea90c061e484e041bcf1d606"),
        "v0.45.228": ("2026-09-13", "6b923236b54a23bbd31900ece18580c1fd4c317a2b27cd7cf11332f5a7c50b97"),
        "v0.45.235": ("2026-09-13", "70e3eb3bd8e79191535313c62e1f234e81dd833bc3175ab40d4abcd130a28d45"),
        "v0.45.234": ("2026-09-14", "9db5987d5cdeaec9c598ef0bb9d4ad654a08052a12a03daf28eef6bbf28de340"),
        "v0.45.238": ("2026-09-14", "248bc6745644c0c1f93bc723132317b1da98b37e18fc6228d9c11d56c4a9e5a8"),
        "v0.45.243": ("2026-09-14", "2f0a130cabdf302908683300c800a9789fda9e9c9b33b6e1b539e6e277bf0657"),
        "v0.45.279": ("2026-09-18", "1558e72b193e94a861c24d60310225ab79970ebf4d17b62e6a16c32fcbd5e90e"),
        "v0.45.288": ("2026-09-18", "d3be1a3ee3d66af952278e69a1f531ae54f1527f45eb57f873b08c4ecf1da319"),
        "v0.45.314": ("2026-09-18", "e5ce328a617690df63a6566698f9bb9b7b5e1cd7fa7c6c30e835f5a93d354fec"),
        "v0.45.315": ("2026-09-18", "95cfb80a2e1fe104c523a76b2775ff46941b9cb9405811b90836f98fdeb41aed"),
        "v0.45.334": ("2026-09-28", "8a5454cb5b47b25a8b1f17d09c5620eb2dca3b361069d98af4e5611dca5bca6d"),
        "v0.45.340": ("2026-09-28", "78c82cbce84dd80dc04d13083903a594636f56559c247bd099207cf449e0f2b7"),
        # v0.45.366 补进（本类 docstring 的交接：「下一个动本文件的人」），摘要取 origin/main 574e5505 的原文；
        # 同法算出的 v0.45.340 摘要与上一行逐位相同，作为方法的正对照。
        "v0.45.349": ("2026-09-28", "c10d24b0e05d4b07db989c44a0ef757dfe578c4c49995dc6d572cfbbfc38f329"),
        "v0.45.357": ("2026-09-28", "ebd6459aa28d72802f9a5f05537651bdb0012037e477fc5a7bbb107a93b86630"),
    }

    _HOW_TO_CORRECT = (
        "已进 main 的世代条目不许就地改（审计轨迹）。要更正它：**追加**一条新条目——新 version 标签、"
        "reason 以「⚠️ 更正 vX 条目」写明原文哪里不对、以哪条为准（先例：v0.45.176 更正 v0.45.172、"
        "v0.45.349 更正 v0.45.334）；若只是把边界日期顺延，还要照表头在 `_CORRECTS` 登记「新标签 → 被更正标签」、"
        "在 `signal_archive.COHORT_SIGNAL_SCOPE` 照抄被更正条目的范围。**不要**改本类的摘要让它变绿——"
        "那等于把审计轨迹改写了。")

    def test_frozen_entries_are_byte_identical(self):
        """变红的变异：R15 / R16（就地改写任一已冻结条目的原因文本，哪怕一个字）；改它的日期；删掉它。"""
        import hashlib
        by_v = {}
        for d, v, r in rr._COHORT_HISTORY:
            by_v.setdefault(v, []).append((d, r))
        bad = []
        for v, (d, h) in self.FROZEN.items():
            got = by_v.get(v, [])
            if len(got) != 1:
                bad.append(f"{v}：表里有 {len(got)} 条（应恰 1 条）")
                continue
            d2, r = got[0]
            if d2 != d:
                bad.append(f"{v}：日期 {d} → {d2}")
            if hashlib.sha256(r.encode("utf-8")).hexdigest() != h:
                bad.append(f"{v}：原因文本被改写")
        assert not bad, f"{bad}。{self._HOW_TO_CORRECT}"

    def test_every_entry_through_the_last_frozen_one_is_pinned(self):
        """钉表自己不许悄悄缩水：表里位于 `LAST_FROZEN` 及之前的每一条都必须在 `FROZEN` 里——
        否则删掉一行摘要就能改那条原文，上一条照绿。
        （往中间**补登**历史边界时会红：照 v0.45.275 的补登先例，新条目进 main 后把它的摘要补进来。）

        变红的变异：从 `FROZEN` 删掉任一行；`LAST_FROZEN` 写成表里不存在的标签。
        """
        versions = [v for _d, v, _r in rr._COHORT_HISTORY]
        assert self.LAST_FROZEN in versions, f"LAST_FROZEN={self.LAST_FROZEN} 不在表里。{self._HOW_TO_CORRECT}"
        head = versions[:versions.index(self.LAST_FROZEN) + 1]
        unpinned = [v for v in head if v not in self.FROZEN]
        assert not unpinned, (
            f"表中 {self.LAST_FROZEN} 及之前的条目没钉摘要：{unpinned}。摘要取自 "
            "`git show origin/main:ic_rerun_readiness.py` 的原文（不是工作区），只增不删。")


# ════════════════════════════════════════════════════════════════════════════
# 判据真的会翻转
# ════════════════════════════════════════════════════════════════════════════

class TestVerdictActuallyFlips:
    """一个永远说"未就绪"的判定器 = 没有判定器。

    与 `test_distribution_invariants.py::TestGuardsHaveTeeth` 同一思路：
    喂足量数据必须翻成"已就绪"。
    """

    def test_not_ready_when_short(self, db):
        res = rr.assess(db_path=db(_weekly_rows(5)), today="2026-09-21")
        assert res["ready"] is False
        assert res["weeks_accrued"] == 5
        assert "未就绪" in rr.summary_line(res)

    def test_ready_when_enough_weeks_accrued(self, db):
        need = rr._WEEKS_REQUIRED[rr.DEFAULT_TARGET_IC]
        res = rr.assess(db_path=db(_weekly_rows(need)), today="2027-03-01")
        assert res["ready"] is True, f"攒到 {need} 周仍判未就绪"
        assert res["weeks_remaining"] == 0
        assert "已就绪" in rr.summary_line(res)

    def test_exactly_one_week_short_is_not_ready(self, db):
        """边界条件：差一周就是差一周，不许四舍五入。"""
        need = rr._WEEKS_REQUIRED[rr.DEFAULT_TARGET_IC]
        res = rr.assess(db_path=db(_weekly_rows(need - 1)), today="2027-03-01")
        assert res["ready"] is False
        assert res["weeks_remaining"] == 1

    def test_lower_bar_becomes_ready_sooner(self, db):
        """只想检出更强的信号（|IC|=0.135）时门槛更低 —— 11 周而非 25 周。"""
        rows = _weekly_rows(12)
        assert rr.assess(db_path=db(rows), today="2026-11-09",
                         target_ic=0.135)["ready"] is True
        assert rr.assess(db_path=db(rows), today="2026-11-09",
                         target_ic=0.090)["ready"] is False


# ════════════════════════════════════════════════════════════════════════════
# 只数"已回填 T+7"的样本
# ════════════════════════════════════════════════════════════════════════════

class TestOnlyRipeSamplesCount:

    def test_unripe_samples_do_not_count_toward_weeks(self, db):
        """未到期样本不能算进不重叠周 —— 它们进不了 IC 计算。

        这条很容易写错：`predictions` 里未到期行的 `checked_t7=0`，
        只按日期过滤会把它们算进来，于是就绪度提前变绿。
        """
        need = rr._WEEKS_REQUIRED[rr.DEFAULT_TARGET_IC]
        rows = _weekly_rows(need, ripe=False)
        res = rr.assess(db_path=db(rows), today="2027-03-01")
        assert res["weeks_accrued"] == 0
        assert res["ready"] is False
        assert res["n_all_samples"] > 0, "总样本数应仍然可见（供看进度）"
        assert res["n_ripe_samples"] == 0

    def test_progress_visible_before_anything_ripens(self, db):
        """T+7 有 7 天滞后，刚开始时"已扫描的周"应该已经在动。"""
        rows = _weekly_rows(3, ripe=False)
        res = rr.assess(db_path=db(rows), today="2026-09-07")
        assert res["scan_weeks_in_cohort"] == 3
        assert res["weeks_accrued"] == 0


# ════════════════════════════════════════════════════════════════════════════
# 标的池漂移会打断世代
# ════════════════════════════════════════════════════════════════════════════

class TestPoolDriftBreaksCohort:
    """世代内换了标的池，样本同样不可比 —— 与
    `weekly_optimizer.check_ticker_pool_consistency` 同一思路。
    """

    def test_stable_pool_has_no_note(self, db):
        res = rr.assess(db_path=db(_weekly_rows(6)), today="2026-09-28")
        assert res["pool_note"] is None
        assert res["pool_drift"] == pytest.approx(0.0)

    def test_pool_swap_is_flagged_and_blocks_ready(self, db):
        need = rr._WEEKS_REQUIRED[rr.DEFAULT_TARGET_IC]
        rows = (_weekly_rows(need - 3, tickers=("AAA", "BBB"))
                + _weekly_rows(3, start="2027-02-01",
                               tickers=("XXX", "YYY", "ZZZ")))
        res = rr.assess(db_path=db(rows), today="2027-03-01")
        assert res["pool_note"] is not None
        assert res["ready"] is False, "池被换掉仍判就绪 —— 样本已不可比"
        assert "世代" in rr.summary_line(res)

    def test_small_addition_does_not_break_cohort(self, db):
        """加 1 只到 10 只池（<20% 门槛）不该打断 —— 闸不能过敏。"""
        ten = tuple(f"T{i}" for i in range(10))
        rows = (_weekly_rows(6, tickers=ten)
                + _weekly_rows(2, start="2026-09-28", tickers=ten + ("NEW",)))
        res = rr.assess(db_path=db(rows), today="2026-10-12")
        assert res["pool_note"] is None


# ════════════════════════════════════════════════════════════════════════════
# ETA 与退出码
# ════════════════════════════════════════════════════════════════════════════

class TestEtaAndExitCodes:

    def test_eta_uses_observed_rate_not_calendar(self, db):
        """ETA 必须按**实际周产出率**外推。

        扫描覆盖率实测只有 36.7%，按日历周外推会给出过于乐观的日期。
        """
        # 8 个日历周里只产出 4 个扫描周 ⇒ 产出率 0.5
        # v0.45.128：today 由世代起点推导。此前写死 "2026-10-12"，与从 _COHORT_START
        # 推出来的夹具日期是两个钟（v0.45.96 那类定时炸弹）——边界一动就把 8 周变成 4 周。
        rows = _weekly_rows(4)
        res = rr.assess(db_path=db(rows), today=_after_cohort(8))
        assert res["weeks_per_calendar_week"] < 1.0
        assert res["eta_calendar_weeks"] > res["weeks_remaining"], (
            "ETA 没有把产出率折算进去"
        )

    def test_no_output_yet_gives_no_false_eta(self, db):
        res = rr.assess(db_path=db([]), today="2026-08-24")
        assert res["eta_date"] is None
        assert "扫描连续性" in rr.summary_line(res)

    def _run(self, monkeypatch, argv):
        import sys as _s
        monkeypatch.setattr(_s, "argv", ["ic_rerun_readiness.py", *argv])
        return rr.main()

    def test_missing_db_returns_3_not_2(self, monkeypatch, tmp_path):
        """2 是编排器 `run_step()` 保留给"脚本不存在"的码，不可占用。"""
        rc = self._run(monkeypatch, ["--db", str(tmp_path / "absent.db")])
        assert rc == 3

    def test_not_ready_returns_1(self, monkeypatch, db):
        rc = self._run(monkeypatch, ["--db", str(db(_weekly_rows(2))),
                                     "--today", _after_cohort(3), "--quiet"])
        assert rc == 1

    def test_ready_returns_0(self, monkeypatch, db):
        need = rr._WEEKS_REQUIRED[rr.DEFAULT_TARGET_IC]
        rc = self._run(monkeypatch, ["--db", str(db(_weekly_rows(need))),
                                     "--today", "2027-03-01", "--quiet"])
        assert rc == 0

    def test_json_mode_keeps_keys_the_task_relies_on(self, monkeypatch, db,
                                                     capsys):
        import json
        rc = self._run(monkeypatch, ["--db", str(db(_weekly_rows(2))),
                                     "--today", _after_cohort(3), "--json"])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        for key in ("cohort", "weeks_required", "weeks_accrued",
                    "weeks_remaining", "eta_date", "pool_note", "ready",
                    "next_step"):
            assert key in payload, f"周度任务依赖的键 {key} 消失了"


class TestRequirementsTraceToPowerReport:
    """`_WEEKS_REQUIRED` 的数字必须与功效报告一致 —— 它是本工具的唯一判据来源。"""

    def test_default_target_matches_measured_composite_ic(self):
        assert rr.DEFAULT_TARGET_IC == 0.090, (
            "默认判据应是系统综合分的实测 |IC|=0.090"
        )

    def test_weeks_required_is_monotonic_in_ic(self):
        """越强的信号越容易检出 —— 所需周数必须随 |IC| 单调递减。"""
        ics = sorted(rr._WEEKS_REQUIRED)
        weeks = [rr._WEEKS_REQUIRED[i] for i in ics]
        assert weeks == sorted(weeks, reverse=True), f"非单调: {dict(zip(ics, weeks))}"

    def test_headline_numbers_match_the_report(self):
        """钉住报告里的三个关键行，防止两处悄悄分叉。"""
        assert rr._WEEKS_REQUIRED[0.090] == 25    # 系统综合分实测
        assert rr._WEEKS_REQUIRED[0.077] == 35    # 噪音地板
        assert rr._WEEKS_REQUIRED[0.135] == 11    # 20 日动量基准


class TestBoundaryDateHasDataEvidence:
    """世代边界的日期，能不能从**数据**上判对错（v0.45.197）。

    背景：本仓记过同一处栽跟头 —— 此前几条边界「核过了」其实**零判别力**，
    因为世代内 0 条样本时，日期写对和写错的输出一模一样
    （auto-memory `alpha-hive-failure-propagation`：「安全性论证与可观测性是
    同一个事实的两面」）。v0.45.197 给口径切换留了个归档印记
    （`advanced_analysis.dealer_gex.chain_view == "cboe_full_expiries"`），
    `cohort_boundary_evidence()` 就是拿它对账的。

    与本文件开篇那句同源：**一个永远说「还没证据」的判别器，和没有判别器是一回事。**
    """

    MARKER = "cboe_full_expiries"

    def _write(self, root, date, view=MARKER, ticker="NVDA"):
        import json as _json
        body = {"advanced_analysis": {"dealer_gex": {}}}
        if view is not None:
            body["advanced_analysis"]["dealer_gex"]["chain_view"] = view
        (root / f"analysis-{ticker}-ml-{date}.json").write_text(
            _json.dumps(body), encoding="utf-8")

    @pytest.fixture
    def boundary(self, monkeypatch):
        # v0.45.334 起印记按版本查表（`_BOUNDARY_MARKERS`）：测试用边界要显式登记它的印记，
        # 否则判别器会如实回 `no_marker`，而不是借 v0.45.197 的印记充数（那正是本版修的 bug）。
        monkeypatch.setattr(
            rr, "_COHORT_HISTORY",
            list(rr._COHORT_HISTORY[:-1]) + [("2026-09-11", "vTEST", "测试用边界")])
        monkeypatch.setitem(rr._BOUNDARY_MARKERS, "vTEST",
                            ("chain_view（测试）", rr._marker_gex_full_chain_view))
        return "2026-09-11"

    def test_marker_on_boundary_date_matches(self, tmp_path, boundary):
        """印记首见日 == 边界日 → matches。

        变红的变异：把 `root = Path(home)` 改成 `root = ALPHAHIVE_DIR`
        —— 那样它去扫代码目录（没有归档）⇒ 恒返回 no_evidence_yet。
        这正是本函数初版的 bug。
        """
        self._write(tmp_path, "2026-09-11")
        assert rr.cohort_boundary_evidence(tmp_path)["verdict"] == "matches"

    def test_marker_later_than_boundary_is_flagged_too_early(self, tmp_path, boundary):
        """印记晚于边界 → boundary_too_early（危险方向：中间那几天是旧口径，会被混算）。

        变红的变异：把 `first > boundary` 与 `first < boundary` 两个分支对调。
        """
        self._write(tmp_path, "2026-09-15")
        ev = rr.cohort_boundary_evidence(tmp_path)
        assert ev["verdict"] == "boundary_too_early"
        assert ev["marker_first_seen"] == "2026-09-15"

    def test_marker_earlier_than_boundary_is_flagged_too_late(self, tmp_path, boundary):
        """印记早于边界 → boundary_too_late（保守方向：白丢了一些新口径样本）。

        变红的变异：同上，两个分支对调。
        """
        self._write(tmp_path, "2026-09-09")
        assert rr.cohort_boundary_evidence(tmp_path)["verdict"] == "boundary_too_late"

    def test_no_marker_is_not_reported_as_matching(self, tmp_path, boundary):
        """没有印记时必须说「还没有证据」，**不能**说「一致」。

        「还没验」和「验过了没问题」必须可区分 —— 本仓六次「失败没传导到下游」
        全是这两者被混成一个。

        变红的变异：把 `verdict = "no_evidence_yet"` 改成 `"matches"`。
        """
        ev = rr.cohort_boundary_evidence(tmp_path)
        assert ev["verdict"] == "no_evidence_yet"
        assert ev["marker_first_seen"] is None

    def test_archives_without_the_marker_are_not_evidence(self, tmp_path, boundary):
        """没有该口径字段的旧归档不算证据 —— 否则判别器会把旧口径认成新口径。

        变红的变异：把 `_marker_gex_full_chain_view` 的 `return view == "cboe_full_expiries"`
        改成 `return True`（v0.45.334 前是 `cohort_boundary_evidence` 里的 `if view == ...`）。
        """
        self._write(tmp_path, "2026-09-01", view=None)
        self._write(tmp_path, "2026-09-02", view="something_else")
        assert rr.cohort_boundary_evidence(tmp_path)["verdict"] == "no_evidence_yet"

    def test_first_seen_is_the_earliest_not_the_last(self, tmp_path, boundary):
        """多天都有印记时取**最早**那天 —— 那才是「首个受影响的业务日」。

        变红的变异：把 `min(first, date)` 改成 `max(first, date)`。

        ⚠️ 夹具的 ticker 名是**刻意**挑的，不是随手起的：归档按文件名排序遍历，
        而文件名是 `analysis-<TICKER>-ml-<DATE>.json` ⇒ **先按 ticker 排，不按日期**。
        若让日期最早的 ticker 恰好排在最前，扫描过程中 `first` 单调下降、
        `min` 与 `max` 走不到分歧点 ⇒ 这条断言对该变异**没有牙**（初版就是这样，
        变异校验当场抓到）。所以要让**日期最晚的 ticker 排在最前**。
        """
        self._write(tmp_path, "2026-09-30", ticker="AAA")   # 排最前、日期最晚
        self._write(tmp_path, "2026-09-20", ticker="MMM")
        self._write(tmp_path, "2026-09-12", ticker="ZZZ")   # 排最后、日期最早
        assert rr.cohort_boundary_evidence(tmp_path)["marker_first_seen"] == "2026-09-12"


class TestBoundaryEvidenceIsPerVersion:
    """v0.45.334：印记按版本查表（`_BOUNDARY_MARKERS`），只和**同一版本**那条边界的日期比。

    修的 bug：旧实现写死认 v0.45.197 的 chain_view 印记，却拿 `_COHORT_HISTORY[-1]` 的日期比。
    197 之后每追加一条边界，它就在拿一件事的印记去核另一件事的日期 —— 表头是 09-18
    （v0.45.315）时实测报 `boundary_too_late`（印记 09-11 早于 09-18），而那两件事毫不相干。
    **一个会误报的判别器和一个永远不报的一样糟**：误报几次之后，真报也没人看了。
    """

    @staticmethod
    def _write(root, date, body, ticker="NVDA"):
        import json as _json
        (root / f"analysis-{ticker}-ml-{date}.json").write_text(_json.dumps(body), encoding="utf-8")

    @staticmethod
    def _chain_view_body():
        return {"advanced_analysis": {"dealer_gex": {"chain_view": "cboe_full_expiries"}}}

    @staticmethod
    def _gex_mod_body(**extra):
        mod = {"gex_adjustment": -0.15, "gex_regime": "positive_gex", **extra}
        return {"swarm_results": {"gex_regime_mod": mod}}

    @staticmethod
    def _date_of(version):
        return next(d for d, v, _r in rr._COHORT_HISTORY if v == version)

    def test_197_marker_is_not_used_to_judge_the_head_boundary(self, tmp_path):
        """只有 v0.45.197 的印记时，表头那条边界不得被判成「写晚了 / 一致」。

        变红的变异：把 `cohort_boundary_evidence` 改回 v0.45.334 之前的写法 —— 固定用
        chain_view 印记、固定比 `_COHORT_HISTORY[-1]`（本条会拿到 boundary_too_late）。
        """
        self._write(tmp_path, self._date_of("v0.45.197"), self._chain_view_body())
        head = rr.cohort_boundary_evidence(tmp_path)
        assert head["version"] == rr._COHORT_HISTORY[-1][1]
        assert head["marker_first_seen"] is None, head
        assert head["verdict"] in ("no_evidence_yet", "no_marker"), head

    def test_197_marker_still_judges_its_own_boundary(self, tmp_path):
        """成对的另一半：修 bug 不能把 197 的判据修没了。

        变红的变异：把 `_BOUNDARY_MARKERS` 里 "v0.45.197" 那条删掉（本条会拿到 no_marker）。
        """
        self._write(tmp_path, self._date_of("v0.45.197"), self._chain_view_body())
        own = rr.cohort_boundary_evidence(tmp_path, version="v0.45.197")
        assert own["verdict"] == "matches", own
        assert own["boundary"] == self._date_of("v0.45.197")

    def test_334_marker_ignores_records_without_the_key(self, tmp_path):
        """缺 `applied` 键的是 v0.45.334 之前的记录（当时施加过）⇒ **不是**印记。

        变红的变异：把 `_marker_gex_modifier_not_applied` 的 `m.get("applied") is False`
        改成 `not m.get("applied")` —— 缺键被当成印记 ⇒ 首见日落到边界前 ⇒ boundary_too_late，
        生产上就是把全部旧归档认成新口径、恒报「写晚了」。
        """
        import datetime as dt
        b = self._date_of("v0.45.334")
        before = (dt.date.fromisoformat(b) - dt.timedelta(days=2)).isoformat()
        self._write(tmp_path, before, self._gex_mod_body(), ticker="ZZZ")                 # 旧记录：无键
        self._write(tmp_path, before, self._gex_mod_body(applied=True), ticker="YYY")     # 非字面量 False
        self._write(tmp_path, b, self._gex_mod_body(applied=False), ticker="AAA")
        ev = rr.cohort_boundary_evidence(tmp_path, version="v0.45.334")
        assert ev["marker_first_seen"] == b and ev["verdict"] == "matches", ev

    def test_334_marker_later_than_boundary_is_too_early(self, tmp_path):
        """推送晚于边界日（09-28）扫描的情形：印记首见晚于边界 ⇒ boundary_too_early（危险方向，会混算）。

        这是本条边界日期的**前提**失败时唯一会红的观测点（见 `_COHORT_HISTORY` v0.45.334 条）。
        变红的变异：把 `_marker_gex_modifier_not_applied` 改成恒 False（判别器失明 ⇒ `marker_first_seen`
        为 None；v0.45.334 起 verdict 仍是 too_early —— 边界后有无印记的归档 —— 红在首见日那一半）。
        """
        import datetime as dt
        b = self._date_of("v0.45.334")
        late = (dt.date.fromisoformat(b) + dt.timedelta(days=1)).isoformat()
        self._write(tmp_path, b, self._gex_mod_body(), ticker="AAA")                # 边界当天仍是旧链
        self._write(tmp_path, late, self._gex_mod_body(applied=False), ticker="BBB")
        ev = rr.cohort_boundary_evidence(tmp_path, version="v0.45.334")
        assert ev["verdict"] == "boundary_too_early" and ev["marker_first_seen"] == late, ev

    def test_unregistered_boundary_reports_no_marker(self, tmp_path, monkeypatch):
        """没登记印记的边界 ⇒ `no_marker`，不借别人的印记、也不说「还没证据」。

        变红的变异：取不到 `_BOUNDARY_MARKERS[version]` 时退回 chain_view 印记
        （本条会拿到 boundary_too_late），或退回 `no_evidence_yet`（读者会以为有判据、只是还没到）。
        """
        monkeypatch.setattr(rr, "_COHORT_HISTORY",
                            list(rr._COHORT_HISTORY) + [("2099-01-01", "vNOMARK", "测试用")])
        self._write(tmp_path, self._date_of("v0.45.197"), self._chain_view_body())
        self._write(tmp_path, "2099-01-01", self._gex_mod_body(applied=False), ticker="AAA")
        ev = rr.cohort_boundary_evidence(tmp_path)
        assert ev["version"] == "vNOMARK"
        assert ev["verdict"] == "no_marker" and ev["marker_first_seen"] is None, ev

    def test_unknown_version_raises(self, tmp_path):
        """版本号写错是调用方的错 —— 抛，而不是安静地回一个看起来正常的 verdict。

        变红的变异：把 `raise ValueError(...)` 改成按表头边界继续算。
        """
        with pytest.raises(ValueError):
            rr.cohort_boundary_evidence(tmp_path, version="v9.9.9")

    def test_every_marker_names_a_real_boundary(self):
        """印记表的键必须是真实边界 —— 版本号拼错，那条印记就永远用不上，且没人会红。

        变红的变异：把 `_BOUNDARY_MARKERS` 的 "v0.45.334" 写成 "v0.45.34"。
        """
        versions = {v for _d, v, _r in rr._COHORT_HISTORY}
        assert set(rr._BOUNDARY_MARKERS) <= versions, sorted(set(rr._BOUNDARY_MARKERS) - versions)
        assert {"v0.45.197", "v0.45.334", "v0.45.340", "v0.45.349", "v0.45.357", "v0.45.366"} <= set(rr._BOUNDARY_MARKERS)

    def test_cli_renders_no_marker_for_the_head(self, monkeypatch, db, capsys):
        """CLI 人读模式要能把 `no_marker` 印出来（带版本），不能 KeyError。

        变红的变异：删掉 `_BOUNDARY_VERDICT_TEXT["no_marker"]`（main() 会 KeyError）。
        """
        import sys as _s
        monkeypatch.setattr(rr, "_COHORT_HISTORY",
                            list(rr._COHORT_HISTORY) + [("2099-01-01", "vNOMARK", "测试用")])
        monkeypatch.setattr(_s, "argv", ["ic_rerun_readiness.py", "--db", str(db([])),
                                         "--today", "2099-01-08"])
        rc = rr.main()
        out = capsys.readouterr().out
        assert rc == 1
        assert "没有登记可判别的归档印记" in out and "vNOMARK" in out, out


# ════════════════════════════════════════════════════════════════════════════
# 边界证据要到得了自动调用方（v0.45.334）
# ════════════════════════════════════════════════════════════════════════════

_TQ_BOUNDARY = "2099-01-05"
_TQ_TODAY = "2099-01-20"


def _write_gex_archive(root, date, applied="__missing__", ticker="NVDA"):
    """归档形状同生产：`swarm_results.gex_regime_mod`；`applied` 缺省 = 旧记录（没有这个键）。"""
    import json as _json
    mod = {"gex_adjustment": -0.15, "gex_regime": "positive_gex"}
    if applied != "__missing__":
        mod["applied"] = applied
    (root / f"analysis-{ticker}-ml-{date}.json").write_text(
        _json.dumps({"swarm_results": {"gex_regime_mod": mod}}), encoding="utf-8")


@pytest.fixture
def tq_boundary(monkeypatch):
    """表尾追加一条测试边界并登记 v0.45.334 同款印记 —— 不依赖「表头恰好是 v0.45.334」，
    下一条真边界追加后本组测试不跟着失效。"""
    monkeypatch.setattr(rr, "_COHORT_HISTORY",
                        list(rr._COHORT_HISTORY) + [(_TQ_BOUNDARY, "vTQ", "测试用边界")])
    monkeypatch.setitem(rr._BOUNDARY_MARKERS, "vTQ",
                        ("applied is False（测试）", rr._marker_gex_modifier_not_applied))
    return _TQ_BOUNDARY


class TestBoundaryEvidenceReachesAutomatedCallers:
    """`cohort_boundary_evidence()` 是 v0.45.334 那条 09-28 边界「推送晚了」时唯一会红的观测点，
    但此前只有人读模式调用它：`--json` / `--quiet` 在它之前 return，`--out` 的 JSON 里也没有。
    而**所有**自动调用方都走那几条路 —— 编排器 Step 11 是 `--quiet --out`，周度任务是 `--quiet`。
    等于这个观测点在自动流程里从没被执行过。

    契约（不许打破）：`--quiet` 仍是**一行**，前四段顺序不变（周度任务按「第三段 = F&G」解析），
    证据**只在要人看时**（日期与印记不符 / 核不了）追加为第五段、以 🚨 开头，正常时仍是四段
    （`tests/test_dim_ic_forward_test.py` 也钉着四段）；退出码 0/1/3 不因它改变；
    `--json` / `--out` 恒带完整结果，编排器读的 JSON 键照旧都在。
    """

    @staticmethod
    def _run(monkeypatch, argv):
        import sys as _s
        monkeypatch.setattr(_s, "argv", ["ic_rerun_readiness.py", *argv])
        return rr.main()

    def _quiet_segments(self, monkeypatch, capsys, db_path):
        rc = self._run(monkeypatch, ["--db", str(db_path), "--today", _TQ_TODAY, "--quiet"])
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 1, f"--quiet 必须只打一行（周度任务原样抄那一行）：{lines}"
        return rc, lines[0].split("｜")

    def test_quiet_flags_marker_later_than_boundary(self, monkeypatch, capsys, db, tq_boundary):
        """推送晚了一天：边界日是旧口径、次日才见印记 ⇒ 第五段以 🚨 开头。

        变红的变异：把 `--quiet` 那行末尾的 `+ "｜" + bev["line"]` 删掉（回到 v0.45.334 之前）。
        """
        p = db([])
        _write_gex_archive(p.parent, _TQ_BOUNDARY, ticker="AAA")                     # 旧链
        _write_gex_archive(p.parent, "2099-01-06", applied=False, ticker="BBB")     # 新链
        rc, seg = self._quiet_segments(monkeypatch, capsys, p)
        assert rc == 1, "退出码契约不因边界证据改变（未就绪仍是 1）"
        assert len(seg) == 5, seg
        assert seg[0] == rr.summary_line(rr.assess(db_path=p, today=_TQ_TODAY)), "第一段必须仍是 IC 摘要"
        assert seg[4].startswith("🚨"), seg[4]
        assert "vTQ" in seg[4] and _TQ_BOUNDARY in seg[4] and "2099-01-06" in seg[4], seg[4]

    def test_quiet_flags_old_archives_after_boundary_before_any_marker(
            self, monkeypatch, capsys, db, tq_boundary):
        """推送还没到：边界日已有归档、却一份印记都没有 ⇒ 也是「写早了」，**现在就红**，
        不等新代码真跑起来（此前这种情形一直报 ⏳ no_evidence_yet「生产还没跑到」）。

        变红的变异：把 `cohort_boundary_evidence` 里 `first is None` 分支改回恒 `no_evidence_yet`。
        """
        p = db([])
        _write_gex_archive(p.parent, "2099-01-01", ticker="AAA")       # 边界前的旧记录：不算
        _write_gex_archive(p.parent, _TQ_BOUNDARY, ticker="BBB")       # 边界当天仍是旧链
        rc, seg = self._quiet_segments(monkeypatch, capsys, p)
        assert rc == 1
        assert seg[4].startswith("🚨") and "1 个归档日无印记" in seg[4], seg[4]
        ev = rr.cohort_boundary_evidence(p.parent)
        assert ev["verdict"] == "boundary_too_early" and ev["marker_first_seen"] is None, ev
        assert ev["unmarked_after_boundary"] == [_TQ_BOUNDARY], ev

    def test_before_boundary_archives_only_is_still_no_evidence(self, monkeypatch, capsys, db, tq_boundary):
        """成对的另一半：边界日之后还没有任何归档 ⇒ 仍是 ⏳ no_evidence_yet，不许误报成 🚨。

        变红的变异：把 `elif date >= boundary:` 改成 `else:`（边界前的旧记录也被数进去）。
        """
        p = db([])
        _write_gex_archive(p.parent, "2099-01-01", ticker="AAA")
        rc, seg = self._quiet_segments(monkeypatch, capsys, p)
        assert rc == 1
        assert len(seg) == 4, f"不该追加 🚨 段：{seg[4:]}"
        ev = rr.boundary_evidence_status(p.parent)
        assert ev["verdict"] == "no_evidence_yet" and ev["alarm"] is False, ev
        assert ev["line"].startswith("⏳") and "vTQ" in ev["line"], ev["line"]

    def test_quiet_healthy_boundary_is_not_flagged(self, monkeypatch, capsys, db, tq_boundary):
        """边界日即见印记 ⇒ matches，不加段（一个恒在的 🚨 段，和没有这一段一样没人看）。

        变红的变异：把 `if bev["alarm"]:` 改成恒真（正常时也追加第五段）。
        """
        p = db([])
        _write_gex_archive(p.parent, _TQ_BOUNDARY, applied=False, ticker="AAA")
        _rc, seg = self._quiet_segments(monkeypatch, capsys, p)
        assert len(seg) == 4, f"正常时不该追加段：{seg[4:]}"
        ev = rr.boundary_evidence_status(p.parent)
        assert ev["verdict"] == "matches" and ev["alarm"] is False, "前提：夹具确实是健康的那一种"
        assert ev["line"].startswith("✅"), ev["line"]

    def test_json_and_out_carry_the_evidence(self, monkeypatch, capsys, db, tq_boundary, tmp_path):
        """`--json` 与 `--out` 都带 `cohort_boundary_evidence`，且编排器读的键照旧都在。

        变红的变异：删掉 `res["cohort_boundary_evidence"] = bev`；或把 `bev = …` 挪到 `--out`
        写盘之后（`--json` 仍绿，`--out` 红 —— 编排器读的正是 `--out`）。
        """
        import json as _json
        p = db([])
        _write_gex_archive(p.parent, _TQ_BOUNDARY, ticker="AAA")
        _write_gex_archive(p.parent, "2099-01-06", applied=False, ticker="BBB")
        out_file = tmp_path / "readiness_out.json"
        rc = self._run(monkeypatch, ["--db", str(p), "--today", _TQ_TODAY, "--json",
                                     "--out", str(out_file)])
        assert rc == 1
        printed = _json.loads(capsys.readouterr().out)
        written = _json.loads(out_file.read_text(encoding="utf-8"))
        for payload in (printed, written):
            ev = payload.get("cohort_boundary_evidence")
            assert ev is not None, sorted(payload)
            assert ev["verdict"] == "boundary_too_early" and ev["alarm"] is True, ev
            assert ev["version"] == "vTQ" and ev["marker_first_seen"] == "2099-01-06", ev
            # 编排器 Step 11 读这几个键（B 起经 orchestrator_steps.py 的 _readiness_line；B 之前是 READINESS_LINE 内联）
            for key in ("cohort", "weeks_accrued", "weeks_required", "n_ripe_samples",
                        "eta_date", "pool_note"):
                assert key in payload, key

    def test_evidence_exception_is_rendered_not_raised(self, monkeypatch, capsys, db, tq_boundary):
        """判别器抛异常 ⇒ `cannot_judge`（同样 🚨），退出码照旧。

        抛出去的代价：Python 未捕获异常退出码是 **1**，编排器把 1 读成「未就绪（正常，继续攒）」
        —— 失败被改写成了正常状态，且没有任何一行说它坏了。
        变红的变异：删掉 `boundary_evidence_status` 里的 try/except。
        """
        def boom(*_a, **_k):
            raise RuntimeError("归档读炸了（测试注入）")
        monkeypatch.setattr(rr, "cohort_boundary_evidence", boom)
        rc, seg = self._quiet_segments(monkeypatch, capsys, db([]))
        assert rc == 1
        assert seg[4].startswith("🚨") and "RuntimeError" in seg[4], seg[4]
        st = rr.boundary_evidence_status(db([]).parent)
        assert st["verdict"] == "cannot_judge" and st["alarm"] is True, st

    def test_every_verdict_renders_a_line(self):
        """`_boundary_line` 对每个判定都要出得了一行（缺文案 ⇒ KeyError ⇒ 整个 main 崩）。

        变红的变异：删掉 `_BOUNDARY_VERDICT_TEXT["cannot_judge"]`。
        """
        base = {"version": "vX", "boundary": "2099-01-01", "marker_first_seen": None,
                "unmarked_after_boundary": []}
        for verdict in ("matches", "boundary_too_early", "boundary_too_late", "no_evidence_yet",
                        "no_marker", "cannot_judge"):
            line = rr._boundary_line({**base, "verdict": verdict})
            assert ("vX" in line) and (line.startswith("🚨") == (verdict in rr.BOUNDARY_ALARM_VERDICTS)), line


def _write_two_marker_archive(root, date, gex_new, buzz_new, ticker="NVDA", oracle_new=None):
    """一份归档同时带 v0.45.334（GEX）与 v0.45.340（Buzz）两种印记的新 / 旧形态；
    `oracle_new` 给定时再带 v0.45.349（OracleBee `gex_signal_in_score`）印记的新 / 旧形态。"""
    import json as _json
    mod = {"gex_adjustment": -0.15, "gex_regime": "positive_gex"}
    if gex_new:
        mod["applied"] = False
    sm = {"delta_3d": None, "momentum_regime": "unknown", "momentum_score_adj": 0.0}
    if buzz_new:
        sm.update(as_of=date, as_of_source="scan", history_source="signal_archive")
    agents = {"BuzzBeeWhisper": {"details": {"sentiment_momentum": sm}}}
    if oracle_new is not None:
        od = {"options_score": 6.1, "gamma_exposure": -0.2}
        if oracle_new:
            od["gex_signal_in_score"] = False
        agents["OracleBeeEcho"] = {"score": 6.0, "direction": "neutral", "details": od}
    body = {"swarm_results": {"gex_regime_mod": mod, "agent_details": agents}}
    (root / f"analysis-{ticker}-ml-{date}.json").write_text(_json.dumps(body), encoding="utf-8")


class TestSameDayBoundariesAreAllChecked:
    """v0.45.340：v0.45.334 与 v0.45.340 两条边界同在 09-28，都挂着「推送晚于边界日」的同一种风险。
    判别器缺省只核末条 ⇒ 后登记的那条一追加，先登记那条的核对就从 `--quiet` 第五段无声消失。
    v0.45.349 起 09-28 有三条；逐条结果在 `per_version`（替换 `same_day`），顶层取最差那条。
    """

    @pytest.fixture
    def two_boundaries(self, monkeypatch):
        monkeypatch.setattr(rr, "_COHORT_HISTORY", list(rr._COHORT_HISTORY) + [
            (_TQ_BOUNDARY, "vGEX", "测试用边界（GEX 印记）"),
            (_TQ_BOUNDARY, "vBUZZ", "测试用边界（Buzz 印记）")])
        monkeypatch.setitem(rr._BOUNDARY_MARKERS, "vGEX",
                            ("applied is False（测试）", rr._marker_gex_modifier_not_applied))
        monkeypatch.setitem(rr._BOUNDARY_MARKERS, "vBUZZ",
                            ("as_of_source（测试）", rr._marker_buzz_momentum_as_of))

    def test_earlier_same_day_boundary_still_alarms(self, db, two_boundaries):
        """末条健康、同日前一条写早了 ⇒ 仍报警，顶层字段是报警的那条。

        变红的变异：把 `boundary_evidence_status` 改回只核末条（`cohort_boundary_evidence(home)`）。
        """
        p = db([])
        _write_two_marker_archive(p.parent, _TQ_BOUNDARY, gex_new=False, buzz_new=True)
        ev = rr.boundary_evidence_status(p.parent)
        assert ev["alarm"] is True and ev["version"] == "vGEX", ev
        assert ev["verdict"] == "boundary_too_early", ev
        assert [e["version"] for e in ev["per_version"]] == ["vGEX", "vBUZZ"], ev["per_version"]
        assert ev["per_version"][1]["verdict"] == "matches", "前提：末条确实是健康的"

    def test_both_alarm_lines_are_joined(self, db, two_boundaries):
        """两条都写早了 ⇒ 一行里两条都点名（周度任务只抄这一行）。

        变红的变异：删掉 `if len(alarmed) > 1:` 那段拼接。
        """
        p = db([])
        _write_two_marker_archive(p.parent, _TQ_BOUNDARY, gex_new=False, buzz_new=False)
        ev = rr.boundary_evidence_status(p.parent)
        assert ev["alarm"] is True
        assert "vGEX" in ev["line"] and "vBUZZ" in ev["line"] and "；" in ev["line"], ev["line"]

    def test_all_healthy_is_quiet(self, db, two_boundaries):
        p = db([])
        _write_two_marker_archive(p.parent, _TQ_BOUNDARY, gex_new=True, buzz_new=True)
        ev = rr.boundary_evidence_status(p.parent)
        assert ev["alarm"] is False and ev["verdict"] == "matches" and ev["version"] == "vBUZZ", ev


class TestSameDayWorstVerdictDrivesTopLevel:
    """v0.45.349：09-28 有三条边界（v0.45.334 / 340 / 349）。编排器 Step 11 只抽顶层键
    （version / boundary / verdict / marker_first_seen / unmarked_after_boundary / alarm / line / error），
    所以「同日各条」的状况必须**折进顶层**：顶层其余键取最差那条、`alarm` = 任一条、`line` 概括全部，
    逐条完整结果在 `per_version`。`--quiet` 的四段 + 可选 🚨 第五段格式不变。
    """

    @pytest.fixture
    def three_boundaries(self, monkeypatch):
        monkeypatch.setattr(rr, "_COHORT_HISTORY", list(rr._COHORT_HISTORY) + [
            (_TQ_BOUNDARY, "vGEX", "测试用边界（GEX 印记）"),
            (_TQ_BOUNDARY, "vBUZZ", "测试用边界（Buzz 印记）"),
            (_TQ_BOUNDARY, "vORA", "测试用边界（Oracle 印记）")])
        for v, desc, fn in (("vGEX", "applied is False（测试）", rr._marker_gex_modifier_not_applied),
                            ("vBUZZ", "as_of_source（测试）", rr._marker_buzz_momentum_as_of),
                            ("vORA", "gex_signal_in_score（测试）", rr._marker_oracle_gex_signal_neutralized)):
            monkeypatch.setitem(rr._BOUNDARY_MARKERS, v, (desc, fn))

    @staticmethod
    def _main(monkeypatch, argv):
        import sys as _s
        monkeypatch.setattr(_s, "argv", ["ic_rerun_readiness.py", *argv])
        return rr.main()

    def test_only_a_non_last_entry_alarms_top_level_alarms(self, monkeypatch, capsys, db, three_boundaries,
                                                           tmp_path):
        """三条里只有**中间**那条写早了（末条健康）⇒ 顶层 alarm 为真、顶层字段是它，`--quiet` 出 🚨 第五段，
        `--out` 的顶层键（编排器读的）照旧齐全，`per_version` 三条都在。

        变红的变异：把 `boundary_evidence_status` 改回只核末条；或把 `res["alarm"] = any(...)` 改成取末条的 alarm。
        """
        import json as _json
        p = db([])
        _write_two_marker_archive(p.parent, _TQ_BOUNDARY, gex_new=True, buzz_new=False, oracle_new=True)
        ev = rr.boundary_evidence_status(p.parent)
        assert [e["version"] for e in ev["per_version"]] == ["vGEX", "vBUZZ", "vORA"], ev["per_version"]
        assert [e["verdict"] for e in ev["per_version"]] == ["matches", "boundary_too_early", "matches"], \
            "前提：只有中间那条报警"
        assert ev["alarm"] is True and ev["version"] == "vBUZZ" and ev["verdict"] == "boundary_too_early", ev
        assert ev["line"].startswith("🚨") and "vBUZZ" in ev["line"], ev["line"]
        assert "vGEX=matches" in ev["line"] and "vORA=matches" in ev["line"], "line 要概括同日全部，不只报警那条"

        rc = self._main(monkeypatch, ["--db", str(p), "--today", _TQ_TODAY, "--quiet"])
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert rc == 1 and len(lines) == 1, (rc, lines)
        seg = lines[0].split("｜")
        assert len(seg) == 5 and seg[4].startswith("🚨") and "vBUZZ" in seg[4], seg

        out_file = tmp_path / "readiness_out.json"
        rc = self._main(monkeypatch, ["--db", str(p), "--today", _TQ_TODAY, "--quiet", "--out", str(out_file)])
        capsys.readouterr()
        bev = _json.loads(out_file.read_text(encoding="utf-8"))["cohort_boundary_evidence"]
        # 编排器 Step 11 抽的键（B 起经 orchestrator_steps.py 的 _BOUNDARY_KEEP；B 之前是内联 `keep=(...)`；error 只在核不了时有）
        for key in ("version", "boundary", "verdict", "marker_first_seen", "unmarked_after_boundary",
                    "alarm", "line"):
            assert key in bev, key
        assert bev["alarm"] is True and len(bev["per_version"]) == 3, bev

    def test_worst_alarm_wins_over_table_order(self, db, three_boundaries):
        """两条报警：表序在前的只是「写晚了」（白丢样本），在后的是「写早了」（会混算）⇒ 顶层取写早了那条。

        变红的变异：顶层改回「第一条报警的」（v0.45.340 的写法 ⇒ 拿到 vGEX / boundary_too_late）。
        """
        import datetime as dt
        p = db([])
        b = dt.date.fromisoformat(_TQ_BOUNDARY)
        d_before, d_after = (b - dt.timedelta(days=1)).isoformat(), (b + dt.timedelta(days=1)).isoformat()
        _write_two_marker_archive(p.parent, d_before, gex_new=True, buzz_new=False, oracle_new=False)
        _write_two_marker_archive(p.parent, _TQ_BOUNDARY, gex_new=True, buzz_new=False, oracle_new=True)
        _write_two_marker_archive(p.parent, d_after, gex_new=True, buzz_new=True, oracle_new=True)
        ev = rr.boundary_evidence_status(p.parent)
        by_v = {e["version"]: e["verdict"] for e in ev["per_version"]}
        assert by_v == {"vGEX": "boundary_too_late", "vBUZZ": "boundary_too_early", "vORA": "matches"}, by_v
        assert ev["version"] == "vBUZZ" and ev["verdict"] == "boundary_too_early", ev
        assert ev["marker_first_seen"] == d_after, "顶层其余键须取自同一条（最差那条）"
        assert ev["line"].startswith("🚨") and "vGEX" in ev["line"] and "vORA=matches" in ev["line"], ev["line"]

    def test_non_alarm_problem_is_not_hidden_by_a_healthy_last_entry(self, monkeypatch, capsys, db,
                                                                     three_boundaries):
        """同日一条核不了（没登记印记 ⇒ no_marker）、其余一致 ⇒ 不报警（`--quiet` 仍四段），
        但顶层 verdict 是 no_marker、line 点名全部三条 —— 编排器非报警分支照抄 `line`，末条健康不许盖住它。

        变红的变异：无报警时顶层改回取末条（v0.45.340 的 `evs[-1]` ⇒ 拿到 vORA / matches）。
        """
        monkeypatch.delitem(rr._BOUNDARY_MARKERS, "vGEX")
        p = db([])
        _write_two_marker_archive(p.parent, _TQ_BOUNDARY, gex_new=True, buzz_new=True, oracle_new=True)
        ev = rr.boundary_evidence_status(p.parent)
        assert ev["alarm"] is False and ev["verdict"] == "no_marker" and ev["version"] == "vGEX", ev
        for token in ("vGEX=no_marker", "vBUZZ=matches", "vORA=matches"):
            assert token in ev["line"], ev["line"]
        rc = self._main(monkeypatch, ["--db", str(p), "--today", _TQ_TODAY, "--quiet"])
        seg = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()][0].split("｜")
        assert rc == 1 and len(seg) == 4, f"不报警时不许追加第五段：{seg[4:]}"

    def test_single_entry_keeps_its_own_line(self, db, tq_boundary):
        """只有一条时 `line` 与那条自己的摘要逐字相同（与 v0.45.349 之前一致），`per_version` 也恒在。

        变红的变异：把 `if len(evs) > 1:` 去掉（单条也拼「同日共核 1 条」）；或只在多于一条时写 `per_version`。
        """
        p = db([])
        _write_gex_archive(p.parent, _TQ_BOUNDARY, applied=False, ticker="AAA")
        ev = rr.boundary_evidence_status(p.parent)
        assert [e["version"] for e in ev["per_version"]] == ["vTQ"], ev
        assert ev["line"] == ev["per_version"][0]["line"] and ev["line"].startswith("✅"), ev["line"]

    def test_severity_covers_every_verdict_and_alarms_rank_first(self):
        """排名表必须覆盖每个判定（漏一个 ⇒ 运行时 KeyError），且报警判定一律排在非报警之前
        （否则顶层 alarm 与顶层 verdict 会自相矛盾）。

        变红的变异：删掉 `_VERDICT_SEVERITY["no_marker"]`；或把 `boundary_too_late` 排到 `matches` 之后。
        """
        assert set(rr._VERDICT_SEVERITY) == set(rr._BOUNDARY_VERDICT_TEXT), (
            sorted(set(rr._VERDICT_SEVERITY) ^ set(rr._BOUNDARY_VERDICT_TEXT)))
        worst_quiet = min(s for v, s in rr._VERDICT_SEVERITY.items() if v not in rr.BOUNDARY_ALARM_VERDICTS)
        assert all(rr._VERDICT_SEVERITY[v] < worst_quiet for v in rr.BOUNDARY_ALARM_VERDICTS)

    #: 排名表的**完整**全序，逐对写明理由（从最差到最好）。⚠️ 刻意写死、不从 `_VERDICT_SEVERITY` 派生——派生即恒真。
    #: 上一条只钉「覆盖全部判定」与「报警在前」，报警组内部、非报警组内部的先后它都不管：
    #: v0.45.349 评审变异 R3（对调 no_marker / no_evidence_yet）、R4（把 boundary_too_late 排到 cannot_judge
    #: 之前）都曾全绿存活。顶层取哪条会原样进编排器 Step 11 与周度任务抄的那一行，排错 ⇒ 更要紧的那条被盖住。
    SEVERITY_CHAIN = (
        ("boundary_too_early", "cannot_judge",
         "写早了是**确定的**混算：边界之后、印记之前的旧口径样本被当成本代算进 IC，`assess()` 不报任何警；"
         "核不了只是**可能**有问题"),
        ("cannot_judge", "boundary_too_late",
         "核不了（判别器抛异常）可能正掩盖着一次写早了，而写晚了的代价有上界——只是白丢边界前几天的新样本、"
         "不混算；两条同日时顶层要先让人去修判别器"),
        ("boundary_too_late", "no_marker",
         "报警判定一律排在非报警之前，否则顶层 alarm（任一条报警）与顶层 verdict 自相矛盾"),
        ("no_marker", "no_evidence_yet",
         "no_marker 是**永久**核不了（没登记印记，等多久都不会变，只能人去补印记）；no_evidence_yet 在边界日"
         "首次扫描后会自己变成 matches 或 boundary_too_early——把 no_marker 排后 ⇒ 同日有它时顶层只显示"
         "「还在等」，这条永远没人补"),
        ("no_evidence_yet", "matches", "还没见到证据不等于一致"),
    )

    def test_severity_full_order_is_pinned_with_reasons(self):
        """`_VERDICT_SEVERITY` 的全序按 `SEVERITY_CHAIN` 逐对核对，失败信息就是那一对的理由；
        并列也不许（并列时顶层取哪条退化成表序，排名表不再表达任何判断）。

        变红的变异：R3 对调 `no_marker` / `no_evidence_yet` 的数值；R4 把 `boundary_too_late` 排到
        `cannot_judge` 之前；任意两项改成同一个数；新增判定只进 `_VERDICT_SEVERITY` 不进本链。
        """
        sev = rr._VERDICT_SEVERITY
        assert len(set(sev.values())) == len(sev), f"排名有并列：{sev}"
        chain = [self.SEVERITY_CHAIN[0][0]] + [b for _w, b, _why in self.SEVERITY_CHAIN]
        assert all(w == chain[i] for i, (w, _b, _why) in enumerate(self.SEVERITY_CHAIN)), "SEVERITY_CHAIN 首尾不相接"
        assert set(chain) == set(sev) and len(chain) == len(sev), (
            f"排名表与本链的判定集合不同：{sorted(set(chain) ^ set(sev))} —— 新判定要在这里写明它排在哪、为什么")
        for worse, better, why in self.SEVERITY_CHAIN:
            assert sev[worse] < sev[better], f"{worse} 必须比 {better} 更差（数值更小）：{why}"

    def test_no_marker_outranks_no_evidence_yet_at_top_level(self, monkeypatch, tmp_path, three_boundaries):
        """行为侧钉 R3 那一对（顶层是编排器与周度任务唯一看得见的东西）：同日一条 no_marker、
        两条 no_evidence_yet ⇒ 顶层是 no_marker —— 「还在等」会自己消失，「永远核不了」不会。

        变红的变异：R3（对调两者数值 ⇒ 顶层变成表序靠后的 vORA / no_evidence_yet）。
        """
        import datetime as dt
        monkeypatch.delitem(rr._BOUNDARY_MARKERS, "vGEX")                  # ⇒ no_marker
        before = (dt.date.fromisoformat(_TQ_BOUNDARY) - dt.timedelta(days=1)).isoformat()
        # 只有边界前的旧口径归档、边界日还没跑到 ⇒ 另两条都是 no_evidence_yet
        _write_two_marker_archive(tmp_path, before, gex_new=False, buzz_new=False, oracle_new=False)
        ev = rr.boundary_evidence_status(tmp_path)
        assert [e["verdict"] for e in ev["per_version"]] == ["no_marker", "no_evidence_yet", "no_evidence_yet"], \
            "前提：同日恰是一条 no_marker + 两条 no_evidence_yet"
        assert ev["verdict"] == "no_marker" and ev["version"] == "vGEX" and ev["alarm"] is False, ev

    def test_cannot_judge_outranks_boundary_too_late_at_top_level(self, monkeypatch, tmp_path, three_boundaries):
        """行为侧钉 R4 那一对：同日一条判别器抛异常（cannot_judge）、一条写晚了（boundary_too_late）⇒
        顶层是 cannot_judge —— 核不了的那条可能正藏着一次写早了，写晚了只是白丢样本。

        变红的变异：R4（把 boundary_too_late 排到 cannot_judge 之前 ⇒ 顶层变成 vGEX / boundary_too_late）。
        """
        import datetime as dt

        def _boom(_d):
            raise RuntimeError("判别器坏了（测试）")
        monkeypatch.setitem(rr._BOUNDARY_MARKERS, "vBUZZ", ("会抛（测试）", _boom))   # ⇒ cannot_judge
        before = (dt.date.fromisoformat(_TQ_BOUNDARY) - dt.timedelta(days=1)).isoformat()
        # vGEX 的印记早于边界 ⇒ boundary_too_late；vORA 边界日首见 ⇒ matches
        _write_two_marker_archive(tmp_path, before, gex_new=True, buzz_new=False, oracle_new=False)
        _write_two_marker_archive(tmp_path, _TQ_BOUNDARY, gex_new=True, buzz_new=True, oracle_new=True)
        ev = rr.boundary_evidence_status(tmp_path)
        by_v = {e["version"]: e["verdict"] for e in ev["per_version"]}
        assert by_v == {"vGEX": "boundary_too_late", "vBUZZ": "cannot_judge", "vORA": "matches"}, \
            f"前提：同日恰是 cannot_judge + boundary_too_late + matches：{by_v}"
        assert ev["verdict"] == "cannot_judge" and ev["version"] == "vBUZZ" and ev["alarm"] is True, ev
        assert "判别器坏了" in ev.get("error", ""), "顶层须取自抛异常那条（带 error 键，编排器会抽它）"


class TestBuzzMomentumMarker:
    """v0.45.340 的归档印记：`sentiment_momentum` 带 `as_of_source` 键（旧代码从不写这个键）。"""

    def test_new_shape_is_recognized(self):
        d = {"swarm_results": {"agent_details": {"BuzzBeeWhisper": {"details": {
            "sentiment_momentum": {"as_of_source": "wall_clock"}}}}}}
        assert rr._marker_buzz_momentum_as_of(d) is True

    @pytest.mark.parametrize("d", [
        {},
        {"swarm_results": {"agent_details": {"BuzzBeeWhisper": None}}},
        {"swarm_results": {"agent_details": {"BuzzBeeWhisper": {"details": None}}}},
        {"swarm_results": {"agent_details": {"BuzzBeeWhisper": {"details": {
            "sentiment_momentum": {"delta_3d": 2, "momentum_regime": "stable"}}}}}},
    ])
    def test_old_or_missing_shapes_are_not(self, d):
        """变红的变异：判定改成「有 sentiment_momentum 就算」（旧归档 899 条全有它）。"""
        assert rr._marker_buzz_momentum_as_of(d) is False

    def test_real_buzz_output_carries_the_marker(self, tmp_path):
        """正对照拿**生产函数的真输出**：印记键名与 `_get_sentiment_momentum` 实际写的键一致。

        变红的变异：把 sentiment.py 里的 `"as_of_source"` 键改名。
        """
        from swarm_agents.sentiment import _get_sentiment_momentum
        sm = _get_sentiment_momentum("NVDA", 50, as_of="2099-01-05", db_path=tmp_path / "missing.db")
        d = {"swarm_results": {"agent_details": {"BuzzBeeWhisper": {"details": {"sentiment_momentum": sm}}}}}
        assert rr._marker_buzz_momentum_as_of(d) is True, sm


def _oracle_body(details):
    """归档形状同生产：`swarm_results.agent_details.OracleBeeEcho.details`。"""
    return {"swarm_results": {"agent_details": {"OracleBeeEcho": {
        "score": 6.0, "direction": "neutral", "details": details}}}}


class TestOracleGexSignalMarker:
    """v0.45.349 的归档印记：OracleBee `details.gex_signal_in_score` 为字面量 `False`（新代码每条路径都写，
    旧代码从不写这个键 —— 推送前生产归档 902/902 实测）。只认字面量 False：缺键 / True / 其它假值都不算。
    """

    @staticmethod
    def _write(root, date, details, ticker="NVDA"):
        import json as _json
        (root / f"analysis-{ticker}-ml-{date}.json").write_text(
            _json.dumps(_oracle_body(details)), encoding="utf-8")

    @staticmethod
    def _b():
        return next(d for d, v, _r in rr._COHORT_HISTORY if v == "v0.45.349")

    @staticmethod
    def _shift(date, days):
        import datetime as dt
        return (dt.date.fromisoformat(date) + dt.timedelta(days=days)).isoformat()

    def test_new_shape_is_recognized(self):
        assert rr._marker_oracle_gex_signal_neutralized(_oracle_body({"gex_signal_in_score": False})) is True

    @pytest.mark.parametrize("d", [
        {},
        {"swarm_results": {"agent_details": {"OracleBeeEcho": None}}},
        {"swarm_results": {"agent_details": {"OracleBeeEcho": {"details": None}}}},
        _oracle_body({"options_score": 6.1, "gamma_exposure": -0.2}),     # 旧代码：没有这个键
        _oracle_body({"gex_signal_in_score": True}),                     # 有人把通道接回去了
        _oracle_body({"gex_signal_in_score": 0}),                        # 假值但不是字面量 False
        _oracle_body({"gex_signal_in_score": None}),
    ])
    def test_old_missing_or_non_literal_shapes_are_not(self, d):
        """变红的变异：把判定写成 `not det.get("gex_signal_in_score")`（缺键 / 0 / None 都被认成新口径）。"""
        assert rr._marker_oracle_gex_signal_neutralized(d) is False

    def test_marker_on_boundary_day_matches(self, tmp_path):
        """边界日当天即见印记 ⇒ matches，首见日 == 表里 v0.45.349 那条的日期。

        变红的变异：把 `_BOUNDARY_MARKERS["v0.45.349"]` 删掉（⇒ no_marker）。
        """
        b = self._b()
        self._write(tmp_path, self._shift(b, -3), {"gamma_exposure": -0.2}, ticker="ZZZ")   # 旧记录
        self._write(tmp_path, b, {"gex_signal_in_score": False}, ticker="AAA")
        ev = rr.cohort_boundary_evidence(tmp_path, version="v0.45.349")
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == b, ev

    def test_first_seen_later_than_boundary_is_too_early(self, tmp_path):
        """推送 / 生产快进晚于 09-28 首次编排器运行的情形：边界日仍是旧口径、次日才见印记 ⇒ boundary_too_early。

        变红的变异：把 `_marker_oracle_gex_signal_neutralized` 改成恒 False（首见日变 None），
        或把 `first > boundary` 与 `first < boundary` 两个分支对调。
        """
        b = self._b()
        late = self._shift(b, 1)
        self._write(tmp_path, b, {"gamma_exposure": -0.2}, ticker="AAA")                   # 边界当天仍旧
        self._write(tmp_path, late, {"gex_signal_in_score": False}, ticker="BBB")
        ev = rr.cohort_boundary_evidence(tmp_path, version="v0.45.349")
        assert ev["verdict"] == "boundary_too_early" and ev["marker_first_seen"] == late, ev

    def test_missing_key_does_not_count(self, tmp_path):
        """边界前只有缺键的旧记录 ⇒ 仍是「还没证据」，**不是**「写晚了」。

        变红的变异：缺键当印记（`det.get("gex_signal_in_score", False) is False`）⇒ 首见日落到边界前 ⇒
        boundary_too_late —— 生产上就是把 902 份旧归档全认成新口径、恒报「写晚了」。
        """
        b = self._b()
        self._write(tmp_path, self._shift(b, -2), {"gamma_exposure": -0.2}, ticker="AAA")
        self._write(tmp_path, self._shift(b, -1), {"options_score": 6.1}, ticker="BBB")
        ev = rr.cohort_boundary_evidence(tmp_path, version="v0.45.349")
        assert ev["verdict"] == "no_evidence_yet" and ev["marker_first_seen"] is None, ev

    def test_true_does_not_count(self, tmp_path):
        """边界前的 `True` 不是印记：首见日必须是边界日那份字面量 False。

        变红的变异：判定改成「键存在就算」（`"gex_signal_in_score" in det`）⇒ 首见日落到边界前、boundary_too_late。
        """
        b = self._b()
        self._write(tmp_path, self._shift(b, -1), {"gex_signal_in_score": True}, ticker="AAA")
        self._write(tmp_path, b, {"gex_signal_in_score": False}, ticker="BBB")
        ev = rr.cohort_boundary_evidence(tmp_path, version="v0.45.349")
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == b, ev

    def test_real_oracle_output_carries_the_marker(self):
        """正对照拿**生产类的真输出**（无效 ticker 那条离线路径）：印记键名与 OracleBee 实际写的一致。

        变红的变异：把 swarm_agents/oracle_bee.py 里的 `"gex_signal_in_score"` 键改名，或删掉无效 ticker
        分支上的打标。
        """
        from pheromone_board import PheromoneBoard
        from swarm_agents.oracle_bee import OracleBeeEcho
        out = OracleBeeEcho(PheromoneBoard()).analyze("not a ticker")
        assert out.get("error") == "invalid_ticker", "前提：走的是离线的无效 ticker 分支"
        d = {"swarm_results": {"agent_details": {"OracleBeeEcho": out}}}
        assert rr._marker_oracle_gex_signal_neutralized(d) is True, out


class TestCorrectionEntriesKeepTheBoundaryCheckable:
    """v0.45.334 那条边界自带预案：推送晚了就**追加**一条更正、把边界顺延。预案一执行，
    判别器必须跟着核新日期 —— 否则它要么永远拿旧日期报 🚨（「狼来了」之后真报也没人看），
    要么回 `no_marker`（这条边界从此核不了）。两种都是在预案**被正确执行之后**才坏。

    修前：缺省版本取表尾，日期却用 `next(...)` 取**第一条**同名条目；更正若新开标签则查不到印记。
    """

    @pytest.fixture
    def with_boundary(self, monkeypatch):
        def _apply(extra):
            monkeypatch.setattr(rr, "_COHORT_HISTORY", list(rr._COHORT_HISTORY) + extra)
            monkeypatch.setitem(rr._BOUNDARY_MARKERS, "vTQ",
                                ("applied is False（测试）", rr._marker_gex_modifier_not_applied))
        return _apply

    @staticmethod
    def _archives(root):
        _write_gex_archive(root, "2099-01-05", ticker="AAA")                  # 原边界日：旧链
        _write_gex_archive(root, "2099-01-06", applied=False, ticker="BBB")   # 顺延后的边界日：新链

    def test_same_label_correction_uses_the_last_entry(self, tmp_path, with_boundary):
        """更正沿用同一标签：缺省与显式都核**最后一条**，且与 `assess()` 的边界一致。

        变红的变异：把显式分支改回 `next(... for ... in _COHORT_HISTORY ...)`（第一条），
        或把缺省改回「先取表尾的 version、再按 version 回查日期」。
        """
        with_boundary([("2099-01-05", "vTQ", "测试用边界"), ("2099-01-06", "vTQ", "更正：顺延一天")])
        self._archives(tmp_path)
        for ev in (rr.cohort_boundary_evidence(tmp_path),
                   rr.cohort_boundary_evidence(tmp_path, version="vTQ")):
            assert ev["boundary"] == "2099-01-06" == rr.cohort_start()["date"], ev
            assert ev["verdict"] == "matches", ev

    def test_new_label_correction_inherits_the_marker(self, tmp_path, with_boundary, monkeypatch):
        """更正新开标签（表头约定的写法）+ 登记 `_CORRECTS` ⇒ 沿用被更正条目的印记。

        变红的变异：删掉 `cohort_boundary_evidence` 里 `or _BOUNDARY_MARKERS.get(_CORRECTS.get(version))`
        （本条拿到 no_marker）。
        """
        with_boundary([("2099-01-05", "vTQ", "测试用边界"), ("2099-01-06", "vTQ_fix", "更正：顺延一天")])
        monkeypatch.setitem(rr._CORRECTS, "vTQ_fix", "vTQ")
        self._archives(tmp_path)
        ev = rr.cohort_boundary_evidence(tmp_path)
        assert ev["version"] == "vTQ_fix" and ev["boundary"] == "2099-01-06", ev
        assert ev["verdict"] == "matches" and ev["marker_first_seen"] == "2099-01-06", ev
        # 显式核被更正的那条：如实报它当初写早了 —— 那正是它被更正的原因
        assert rr.cohort_boundary_evidence(tmp_path, version="vTQ")["verdict"] == "boundary_too_early"

    def test_unregistered_new_label_correction_is_no_marker(self, tmp_path, with_boundary):
        """对照：新开标签却没登记 `_CORRECTS` ⇒ `no_marker`（表头约定 ② 为什么不能省）。"""
        with_boundary([("2099-01-05", "vTQ", "测试用边界"), ("2099-01-06", "vTQ_fix", "更正：顺延一天")])
        self._archives(tmp_path)
        assert rr.cohort_boundary_evidence(tmp_path)["verdict"] == "no_marker"

    @staticmethod
    def _correction_problems(history, corrects):
        """表头约定的静态核对：`_CORRECTS` 两端都是真实版本、更正在被更正者之后、reason 以「更正」开头；
        反过来，reason 以「更正」开头的条目必须登记在 `_CORRECTS`（漏登 ⇒ 判别器回 no_marker）。"""
        order = {}
        for i, (_d, v, _r) in enumerate(history):
            order.setdefault(v, i)
        problems = []
        for fix, target in corrects.items():
            if fix not in order or target not in order:
                problems.append(f"{fix}→{target}：不在 _COHORT_HISTORY 里")
            elif order[fix] <= order[target]:
                problems.append(f"{fix} 排在它更正的 {target} 之前")
        for _d, v, r in history:
            if v in corrects and not r.startswith("更正"):
                problems.append(f"{v}：登记为更正，reason 却不以「更正」开头")
            if r.startswith("更正") and v not in corrects:
                problems.append(f"{v}：reason 以「更正」开头却没登记 _CORRECTS")
        return problems

    def test_real_correction_table_is_consistent(self):
        assert self._correction_problems(rr._COHORT_HISTORY, rr._CORRECTS) == []

    def test_correction_checker_has_teeth(self):
        """反向自证：上一条对真表恒空时不能是因为核对器是瞎的。"""
        h = [("2099-01-05", "vA", "改了什么"), ("2099-01-06", "vB", "更正：顺延一天")]
        assert self._correction_problems(h, {}) != []                   # 漏登
        assert self._correction_problems(h, {"vB": "vA"}) == []         # 正确登记
        assert self._correction_problems(h, {"vA": "vB"}) != []         # 方向反了
        assert self._correction_problems(h, {"vB": "vZ"}) != []         # 指向不存在的版本
