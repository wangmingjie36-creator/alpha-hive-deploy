"""`experiments/misjudgment_pattern_walkforward.py` 的无前视与判定规则。

v0.45.215 新增。这个脚本是「P2-⑧ 撤掉而不是修」的唯一证据承载物，
将来定期重跑时要靠它的退出码下决定 —— 所以它自己的量具必须有牙：

  - 模式库只能看见 gap 之前的误判（前视会让任何模式看起来都有预测力）
  - 阈值只能看见截止日之前的数据（生产 pheromone_source 用的全样本分位数就是前视）
  - 激活按**不同误判日**计数（撤掉的实现按调用次数计，正是在那里膨胀的）
  - 闸门退出码与 vol_regime_filter 同义：0 有效 / 1 显著反向 / 3 无法区分

全部合成数据（tmp_path 里造 sqlite），不读生产库、不出网、不用 skip。
每条断言旁注明了能让它变红的变异。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "experiments"))
import misjudgment_pattern_walkforward as mw  # noqa: E402


def _make_db(path: Path, rows, signals=None, with_dir_columns=True) -> Path:
    """rows: (ticker, date, direction, final_score, price, close_t7, dir_correct)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    extra = ", dir_correct_t7 INTEGER, dir_ambiguous_t7 INTEGER" if with_dir_columns else ""
    con.execute(f"CREATE TABLE predictions (ticker TEXT, date TEXT, direction TEXT, final_score REAL, "
                f"price_at_predict REAL, close_t7 REAL{extra})")
    con.execute("CREATE TABLE signal_archive (date TEXT, ticker TEXT, signal TEXT, value REAL)")
    for t, d, direction, score, px, close, ok in rows:
        if with_dir_columns:
            con.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,0)", (t, d, direction, score, px, close, ok))
        else:
            con.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?)", (t, d, direction, score, px, close))
        # 每条都给同一个 P/C ⇒ 信号集合恒定：call_dominant / pc_bullish / score_high(/low)
        con.execute("INSERT INTO signal_archive VALUES (?,?,?,?)", (d, t, "options.put_call_ratio", 0.5))
    for d, t, k, v in signals or []:
        con.execute("INSERT INTO signal_archive VALUES (?,?,?,?)", (d, t, k, v))
    con.commit()
    con.close()
    return path


def _miss(d, direction="bullish"):
    close = 95.0 if direction == "bullish" else 105.0          # 反向走 5% ⇒ 误判
    return ("AAA", d, direction, 5.0, 100.0, close, 0)


QUERY = ("AAA", "2026-05-01", "bullish", 5.0, 100.0, 103.0, 1)  # gap=14 ⇒ 库截止 2026-04-17


def _warned_for_query(tmp_path, misses, query=QUERY, gap=14):
    db = _make_db(tmp_path / "p.db", [*misses, query])
    preds, sig_map = mw.load(str(db))
    rows = mw.walk_forward(preds, sig_map, gap_days=gap, start=query[1])
    (row,) = [r for r in rows if r["date"] == query[1]]
    return row["warned"]


class TestNoLookAhead:
    def test_three_distinct_misses_before_gap_activate(self, tmp_path):
        # 正对照：下面几条「不预警」只有在这条会预警时才有意义
        assert _warned_for_query(tmp_path, [_miss("2026-04-01"), _miss("2026-04-08"), _miss("2026-04-15")]) is True

    def test_miss_inside_gap_is_invisible(self, tmp_path):
        # 变异：build_library 里忽略 lib_cutoff / gap_days 取 0 ⇒ 第三条被看见 ⇒ 红
        assert _warned_for_query(tmp_path, [_miss("2026-04-01"), _miss("2026-04-08"), _miss("2026-04-20")]) is False

    def test_boundary_day_is_inclusive(self, tmp_path):
        # date == t − gap 已判定，应计入。变异：`p["date"] > lib_cutoff` 改成 `>=` ⇒ 红
        assert _warned_for_query(tmp_path, [_miss("2026-04-01"), _miss("2026-04-08"), _miss("2026-04-17")]) is True

    @pytest.mark.parametrize("leak", ["same_day", "future"])
    def test_thresholds_see_only_strictly_earlier_dates(self, leak):
        past = [{"date": f"2026-04-0{i}", "final_score": float(i)} for i in range(1, 5)]
        leak_dates = ["2026-04-05", "2026-04-05"] if leak == "same_day" else ["2026-04-06", "2026-04-07"]
        preds = past + [{"date": d, "final_score": 100.0} for d in leak_dates]
        thr = mw.Thresholds(preds, {}).at("2026-04-05")
        # 过去 [1,2,3,4] 的 p75 = s[3] = 4；混进两条 100 后是 s[4] = 100。
        # ⚠️ 只混一条是等价变异（[1,2,3,4,100] 的 p75 仍是 s[3]=4），实测过。
        # 变异：_before 改 bisect_right(…,(cutoff, inf))（same_day 红）/ 不切片（两条都红）
        assert thr["score_hi"] == 4.0 and thr["score_lo"] == 2.0


class TestActivationAndMatching:
    def test_two_distinct_misses_do_not_activate(self, tmp_path):
        # 变异：ACTIVE_MIN_DISTINCT = 2 ⇒ 红
        assert _warned_for_query(tmp_path, [_miss("2026-04-01"), _miss("2026-04-08")]) is False

    def test_direction_must_match(self, tmp_path):
        bearish_query = ("AAA", "2026-05-01", "bearish", 5.0, 100.0, 97.0, 1)
        # 变异：is_warned 不比方向 ⇒ 红
        assert _warned_for_query(tmp_path, [_miss("2026-04-01"), _miss("2026-04-08"), _miss("2026-04-15")],
                                 query=bearish_query) is False

    @pytest.mark.parametrize("pattern_keys, live, expected", [
        ({"a", "b", "c"}, {"a"}, False),        # ⅓ < ½。变异 OVERLAP_MIN=0.30 ⇒ 红（0.34 是等价变异）
        ({"a", "b", "c"}, {"a", "b"}, True),    # ⅔
        ({"a", "b"}, {"a"}, True),              # ½ 恰好过线。变异 `>=` 改 `>` ⇒ 红
        (set(), {"a"}, False),                  # 空信号模式永不命中
    ])
    def test_overlap_rule(self, pattern_keys, live, expected):
        lib = {"AAA": {"k": {"direction": "看多", "signal_keys": pattern_keys,
                              "dates": {"2026-04-01", "2026-04-02", "2026-04-03"}}}}
        assert mw.is_warned(lib, "AAA", "看多", {k: True for k in live}) is expected


class TestGateAndCli:
    @pytest.mark.parametrize("lo, hi, code", [
        (0.01, 0.20, 0), (-0.20, -0.01, 1), (-0.10, 0.10, 3), (0.0, 0.10, 3), (None, None, 3)])
    def test_gate_exit_codes(self, lo, hi, code):
        # 变异：`ci_lo > 0` 改 `>= 0` ⇒ (0.0, 0.10) 变 0 ⇒ 红
        assert mw.gate(lo, hi) == code

    def test_insufficient_sample_is_3_and_writes_nothing_unless_asked(self, tmp_path, capsys):
        # 目录名带 `#`：未转义的 `file:{path}?mode=ro` 会把 # 之后当 URI 片段截掉 ⇒ 打不开。
        # ⚠️ 只带空格是等价变异（sqlite 容忍 URI 里的裸空格），实测过。
        db = _make_db(tmp_path / "dir with space #1" / "p.db",
                      [_miss("2026-04-01"), _miss("2026-04-08"), _miss("2026-04-15"), QUERY])
        assert mw.main(["--db", str(db), "--bootstrap", "10"]) == 3
        assert "样本不足" in capsys.readouterr().out
        assert not list(tmp_path.rglob("*.md"))

    def test_missing_label_columns_is_3(self, tmp_path):
        db = _make_db(tmp_path / "old.db", [_miss("2026-04-01")], with_dir_columns=False)
        assert mw.main(["--db", str(db)]) == 3

    def test_default_db_is_resolved_via_paths_at_call_time(self, tmp_path, monkeypatch, capsys):
        # 变异：把默认库路径冻成模块级常量（import 时求值）⇒ 读不到这里的环境变量 ⇒ 红
        target = tmp_path / "nowhere" / "pheromone.db"
        monkeypatch.setenv("ALPHA_HIVE_DB_PATH", str(target))
        assert mw.main([]) == 3
        assert str(target) in capsys.readouterr().out

    def test_load_is_read_only_and_never_creates_a_database(self, tmp_path):
        # 只读打开的可观测差异：普通模式对不存在的路径会**凭空建一个空库**，mode=ro 会抛。
        # （「读完字节不变」测不出区别——普通模式只读也不改字节，实测是等价变异。）
        # 变异：去掉 ?mode=ro ⇒ 不抛、且 missing 被创建 ⇒ 红
        missing = tmp_path / "absent" / "pheromone.db"
        missing.parent.mkdir()
        with pytest.raises(sqlite3.OperationalError):
            mw.load(str(missing))
        assert not missing.exists()
