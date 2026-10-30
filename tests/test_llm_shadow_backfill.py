"""`llm_shadow_backfill.py`：把「扫描中途出错、逐只结果没进状态文件」的付费日按预注册原规则补记（v0.45.687）。

场景就是 2026-10-29：30 只请求、27 只救回、3 只缺；蜂群跑完后 P4b 抛 API 超时，状态文件里 `tickers_scanned` 为空。
"""
import json
import os
from datetime import date, datetime
from pathlib import Path

import pytest

import ledger_io
import llm_shadow_backfill as bf
import llm_shadow_report as rp
import llm_shadow_scan as sh

DAY = date(2026, 10, 29)
REQ = [f"T{i:02d}" for i in range(30)]
MISSING = ["T05", "T11", "T20"]


def _usage(**kw):
    base = {"total_cost_usd": 2.04, "call_count": 260, "input_tokens": 1, "output_tokens": 1, "truncated_calls": 0,
            "json_parse_failures": 0, "budget_refusals": 0, "unpriced_refusals": 0, "failed_calls": 23,
            "model_refusals": 0}
    base.update(kw)
    return base


def _failed_status(**over):
    written = datetime(2026, 10, 29, 15, 23, 0)
    st = {
        "schema": 1, "date": DAY.isoformat(), "mode": "run", "model": sh.MODEL, "generation": sh.GENERATION,
        "daily_cap_usd": 3.0, "tickers_requested": REQ, "tickers_scanned": [], "elapsed_sec": 2258.2,
        "usage": _usage(), "per_ticker": {t: {"present": False} for t in REQ},
        "quality": {"healthy": False, "reasons": ["queen_llm_rate=0% < 80%（蒸馏没走 LLM）"], "llm_on": True},
        "error": "APITimeoutError: Request timed out or interrupted.", "written_at": written.isoformat(timespec="seconds"),
    }
    st.update(over)
    return st


def _swarm(tickers=None):
    tickers = [t for t in REQ if t not in MISSING] if tickers is None else tickers
    return {t: {"final_score": 6.0 + i * 0.01, "direction": "bearish" if i % 2 else "neutral", "rule_score": 6.2,
                "rule_direction": "neutral", "distill_mode": "llm_enhanced", "llm_confidence": 0.6,
                "data_real_pct": 95.0} for i, t in enumerate(tickers)}


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "shadow"
    r.mkdir()
    return r


def _setup(root, *, status=None, swarm=None, mtime_offset=-30.0, write_swarm=True):
    st = status if status is not None else _failed_status()
    (root / sh.STATUS_DIRNAME).mkdir(parents=True, exist_ok=True)
    ledger_io.write_json(_status_file(root), st)     # 直接写 run 路径（测试要能造出 mode 不对的文件）
    if write_swarm:
        f = root / f".swarm_results_{DAY.isoformat()}.json"
        f.write_text(json.dumps(_swarm() if swarm is None else swarm), encoding="utf-8")
        t = datetime.fromisoformat(st["written_at"]).timestamp() + mtime_offset
        os.utime(f, (t, t))
    return st


def _status_file(root):
    return sh.status_path(root, DAY.isoformat(), "run")


class TestPlan:
    def test_good_case_has_no_problems_and_judges_by_the_original_rules(self, root):
        _setup(root)
        plan = bf.plan_backfill(root, DAY)
        assert plan["problems"] == []
        new = plan["new_status"]
        assert len(new["tickers_scanned"]) == 27 and all(new["per_ticker"][t]["present"] for t in new["tickers_scanned"])
        assert [t for t in REQ if not new["per_ticker"][t]["present"]] == MISSING
        assert new["error"] and new["salvaged"] is True                    # 原始 error 照留
        assert new["quality"]["healthy"] is True, new["quality"]            # 预注册原规则：8.1% 失败率、90% 走 LLM
        assert not any("scan_error" in r for r in new["quality"]["reasons"])  # 修订 1 的 scan_error 自 10-30 起，不追溯
        bfsec = new["backfill"]
        assert bfsec["source"] == f".swarm_results_{DAY.isoformat()}.json" and "不含 scan_error" in bfsec["quality_rule"]
        assert bfsec["original_status"] == "2026-10-29.json.orig" and bfsec["original_error"] == _failed_status()["error"]

    def test_plan_is_read_only(self, root):
        _setup(root)
        before = _status_file(root).read_bytes()
        bf.plan_backfill(root, DAY)
        assert _status_file(root).read_bytes() == before
        assert not bf.orig_path(root, DAY.isoformat()).exists()

    def test_unhealthy_by_the_rules_is_written_honestly_not_forced_healthy(self, root):
        _setup(root, status=_failed_status(usage=_usage(failed_calls=200)))       # 失败率 > 10%
        plan = bf.plan_backfill(root, DAY)
        assert plan["problems"] == [] and plan["new_status"]["quality"]["healthy"] is False
        assert any("failed_call_rate" in r for r in plan["new_status"]["quality"]["reasons"])

    @pytest.mark.parametrize("mutate,needle", [
        (lambda s: s.update(error=None), "没有 error"),
        (lambda s: s.update(tickers_scanned=["T00"]), "已有逐只结果"),
        (lambda s: s.update(mode="control"), "只补付费日"),
        (lambda s: s.update(backfill={"at": "x"}), "补记过"),
    ])
    def test_refuses_days_that_are_not_failed_empty_paid_days(self, root, mutate, needle):
        st = _failed_status()
        mutate(st)
        _setup(root, status=st)
        plan = bf.plan_backfill(root, DAY)
        assert any(needle in p for p in plan["problems"]), plan["problems"]
        assert plan["new_status"] is None

    def test_refuses_without_status_or_swarm_file_or_with_unreadable_file(self, root):
        assert any("不存在" in p for p in bf.plan_backfill(root, DAY)["problems"])
        _setup(root, write_swarm=False)
        assert any("无从补记" in p for p in bf.plan_backfill(root, DAY)["problems"])
        (root / f".swarm_results_{DAY.isoformat()}.json").write_text("{坏的", encoding="utf-8")
        assert any("失败" in p for p in bf.plan_backfill(root, DAY)["problems"])
        (root / f".swarm_results_{DAY.isoformat()}.json").write_text("[]", encoding="utf-8")
        assert any("为空或不是对象" in p for p in bf.plan_backfill(root, DAY)["problems"])

    def test_refuses_a_swarm_file_written_outside_this_scans_window(self, root):
        _setup(root, mtime_offset=+3600.0)                  # 状态写完一小时后才写的：可能是另一次扫描
        assert any("修改时间" in p for p in bf.plan_backfill(root, DAY)["problems"])
        _setup(root, mtime_offset=-5000.0)                  # 比这次扫描起点还早
        assert any("修改时间" in p for p in bf.plan_backfill(root, DAY)["problems"])

    def test_refuses_tickers_that_were_never_requested(self, root):
        _setup(root, swarm=_swarm(REQ[:3] + ["ZZZZ"]))
        assert any("不在请求名单" in p for p in bf.plan_backfill(root, DAY)["problems"])

    def test_refuses_when_the_swarm_copy_already_exists(self, root):
        _setup(root)
        ledger_io.write_json(sh.swarm_results_path(root, DAY.isoformat(), "run"), {"x": 1})
        assert any("已存在" in p for p in bf.plan_backfill(root, DAY)["problems"])


class TestApply:
    def test_apply_writes_status_orig_and_swarm_copy_and_the_report_sees_27_rows(self, root):
        original = _setup(root)
        plan = bf.plan_backfill(root, DAY)
        written = bf.apply_backfill(root, DAY, plan)
        assert {p.name for p in written} == {"2026-10-29.json", "2026-10-29.json.orig", "swarm_results-2026-10-29.json"}
        assert json.loads(bf.orig_path(root, DAY.isoformat()).read_text(encoding="utf-8")) == original   # 原状态原样留着
        days = rp.load_days(root)
        assert [d["date"] for d in days] == ["2026-10-29"]                    # .orig 不会被当成另一天
        rows = rp.llm_rows(days)
        assert len(rows) == 27 and {r["ticker"] for r in rows}.isdisjoint(MISSING)
        assert json.loads(sh.swarm_results_path(root, DAY.isoformat(), "run").read_text(encoding="utf-8")).keys() == _swarm().keys()

    def test_second_apply_is_refused(self, root):
        _setup(root)
        bf.apply_backfill(root, DAY, bf.plan_backfill(root, DAY))
        again = bf.plan_backfill(root, DAY)
        assert again["problems"] and any("补记过" in p for p in again["problems"])
        with pytest.raises(ValueError):
            bf.apply_backfill(root, DAY, again)

    def test_apply_with_problems_writes_nothing(self, root):
        _setup(root, status=_failed_status(error=None))
        plan = bf.plan_backfill(root, DAY)
        before = _status_file(root).read_bytes()
        with pytest.raises(ValueError):
            bf.apply_backfill(root, DAY, plan)
        assert _status_file(root).read_bytes() == before and not bf.orig_path(root, DAY.isoformat()).exists()

    @pytest.mark.parametrize("fail_on", ["swarm_copy", "orig"])
    def test_a_failure_before_the_status_write_leaves_the_status_untouched(self, root, monkeypatch, fail_on):
        """落盘顺序：先 swarm 副本、再 .orig、**最后**才改状态文件。前两步任何一步失败，状态文件必须还是原样——
        否则会留下「状态已改、数据没到位」的一天（顺序反过来，这条就红）。"""
        _setup(root)
        plan = bf.plan_backfill(root, DAY)
        victim = {"swarm_copy": sh.swarm_results_path(root, DAY.isoformat(), "run"),
                  "orig": bf.orig_path(root, DAY.isoformat())}[fail_on]
        real = ledger_io.write_json

        def flaky(path, doc, *a, **k):
            if Path(path) == victim:
                raise OSError("磁盘满")
            return real(path, doc, *a, **k)
        monkeypatch.setattr(ledger_io, "write_json", flaky)
        before = _status_file(root).read_bytes()
        with pytest.raises(OSError):
            bf.apply_backfill(root, DAY, plan)
        assert _status_file(root).read_bytes() == before

    def test_a_failure_on_the_status_write_itself_leaves_it_untouched(self, root, monkeypatch):
        _setup(root)
        plan = bf.plan_backfill(root, DAY)
        real = ledger_io.write_json
        target = _status_file(root)

        def flaky(path, doc, *a, **k):
            if Path(path) == target:
                raise OSError("磁盘满")
            return real(path, doc, *a, **k)
        monkeypatch.setattr(ledger_io, "write_json", flaky)
        before = target.read_bytes()
        with pytest.raises(OSError):
            bf.apply_backfill(root, DAY, plan)
        assert target.read_bytes() == before

    def test_the_day_stays_locked_against_a_second_paid_run(self, root):
        """补记不能把当天的花费锁解开：花过钱的日子 `day_lock` 照样锁。"""
        _setup(root)
        bf.apply_backfill(root, DAY, bf.plan_backfill(root, DAY))
        locked, _why, _attempts = sh.day_lock(_status_file(root))
        assert locked is True


class TestCli:
    @pytest.fixture(autouse=True)
    def _prod_root(self, tmp_path, monkeypatch):
        """测试会话的数据根缺省就是 tmp_path 本身，`tmp_path/shadow` 嵌在里面会被「影子根不得嵌套在生产根」正确拒绝——
        CLI 用例先把「生产根」指到一个与影子根不相干的目录。"""
        prod = tmp_path / "prod-data"
        prod.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(prod))
        return prod

    def test_dry_run_is_the_default_and_writes_nothing(self, root, capsys):
        _setup(root)
        before = _status_file(root).read_bytes()
        assert bf.main(["--day", DAY.isoformat(), "--shadow-root", str(root)]) == sh.EXIT_OK
        out = capsys.readouterr().out
        assert "救回 27 只" in out and "只看，没写" in out
        assert _status_file(root).read_bytes() == before and not bf.orig_path(root, DAY.isoformat()).exists()

    def test_apply_flag_writes(self, root, capsys):
        _setup(root)
        assert bf.main(["--day", DAY.isoformat(), "--apply", "--shadow-root", str(root)]) == sh.EXIT_OK
        assert json.loads(_status_file(root).read_text(encoding="utf-8"))["backfill"]["tool"] == "llm_shadow_backfill.py"

    def test_problems_exit_2_and_say_why(self, root, capsys):
        _setup(root, status=_failed_status(error=None))
        assert bf.main(["--day", DAY.isoformat(), "--apply", "--shadow-root", str(root)]) == sh.EXIT_USAGE
        assert "不能补记" in capsys.readouterr().out

    def test_bad_day_exits_2(self, root):
        assert bf.main(["--day", "not-a-date", "--shadow-root", str(root)]) == sh.EXIT_USAGE

    def test_refuses_a_shadow_root_that_is_the_production_root(self, _prod_root, capsys):
        """生产根里**真有一个能补记的状态**：没有 check_roots 的话这条会一路补下去（所以用它才分得出守卫在不在）。"""
        _setup(_prod_root)
        before = _status_file(_prod_root).read_bytes()
        assert bf.main(["--day", DAY.isoformat(), "--apply", "--shadow-root", str(_prod_root)]) == sh.EXIT_USAGE
        assert "拒绝运行" in capsys.readouterr().out
        assert _status_file(_prod_root).read_bytes() == before and not bf.orig_path(_prod_root, DAY.isoformat()).exists()

    def test_default_shadow_root_is_the_sibling_not_the_production_root(self, tmp_path, monkeypatch, capsys):
        prod = tmp_path / "alpha-hive-data"
        prod.mkdir()
        monkeypatch.setenv("ALPHA_HIVE_HOME", str(prod))
        rc = bf.main(["--day", DAY.isoformat()])                # 影子根不存在 ⇒ 没有状态文件 ⇒ 说明不存在，不写任何东西
        assert rc == sh.EXIT_USAGE and "不存在" in capsys.readouterr().out
        assert sorted(p.name for p in prod.iterdir()) == []
