"""扫描耗时可见化（v0.45.118）

固化的事实：2026-08-26→09-04 规则模式 Step 2 从 749s 涨到 3342s（4.5×），
被超时杀两次，没有任何东西响。三个计数器（yf_gate / twelve_data / cboe）
早就在数，只是没人打印；CBOE 甚至没有计数器——29 只标的抓 83 次这件事
是事后从日志里 grep 出来的。

这里守三条：
  1. 阶段计时能记、能累加、异常也记；
  2. CBOE 计数器真的随命中/抓取/陈旧/失败变化（不是只定义了没接线）；
  3. 「没测到」写 None，不写 0——0 和缺失在下游必须可区分（v0.45.114）；
  4. 编排器 write_status 的 jq 并入过滤器只认本轮日期的文件。
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scan_timing as st


@pytest.fixture(autouse=True)
def _clean():
    st.reset()
    yield
    st.reset()


# ───────────────────────────────────────────── 计时
class TestPhaseTiming:
    def test_record_accumulates_same_phase(self):
        st.record("parallel", 1.0)
        st.record("parallel", 2.5)
        assert st.phases()["parallel"] == 3.5

    def test_timed_records_even_when_body_raises(self):
        with pytest.raises(RuntimeError):
            with st.timed("boom"):
                raise RuntimeError("x")
        assert "boom" in st.phases(), "失败的阶段不能从耗时表里消失"

    def test_missing_phase_is_absent_not_zero(self):
        st.record("prefetch", 1.0)
        snap = st.snapshot("2026-09-05")
        assert "parallel" not in snap["phases"]
        assert "parallel" not in st.summary_line(snap)


# ───────────────────────────────────────────── 计数器
class TestCounters:
    def test_unavailable_counter_is_none_not_zero(self, monkeypatch):
        """模块导入失败 → None。绝不能是 {} 或 0（下游会读成「零调用」）。"""
        import builtins
        real_import = builtins.__import__

        def _imp(name, *a, **k):
            if name in ("yf_gate", "twelve_data", "cboe_options"):
                raise ImportError(name)
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _imp)
        c = st.counters()
        assert c == {"yfinance": None, "twelve_data": None, "cboe": None}
        line = st.summary_line({"phases": {}, "counters": c})
        assert "—" in line and "0次" not in line

    def test_yfinance_counter_none_when_gate_not_installed(self, monkeypatch):
        import yf_gate
        monkeypatch.setattr(yf_gate, "is_installed", lambda: False)
        assert st.counters()["yfinance"] is None

    def test_counters_pick_up_real_stats_dicts(self):
        c = st.counters()
        assert set(c["twelve_data"]) >= {"hits", "misses", "fetches"}
        assert set(c["cboe"]) == {"hits", "fetches", "stale", "failed"}


class TestCboePayloadStats:
    """计数器必须真的接在取数路径上——只定义 dict 不加一的版本要红。"""

    @pytest.fixture
    def cb(self, monkeypatch):
        import cboe_options
        monkeypatch.setattr(cboe_options, "_SNAPSHOT_PROVIDER", None)
        monkeypatch.setattr(cboe_options, "_payload_stale_vintage", lambda t, d: None)
        cboe_options._payload_cache.clear()
        cboe_options.reset_payload_stats()
        yield cboe_options
        cboe_options._payload_cache.clear()
        cboe_options.reset_payload_stats()

    @staticmethod
    def _fake_urlopen(payload: dict):
        body = json.dumps({"data": payload}).encode()

        def _open(req, timeout=0):
            return io.BytesIO(body)
        return _open

    def test_fetch_then_hit(self, cb, monkeypatch):
        monkeypatch.setattr(cb.urllib.request, "urlopen",
                            self._fake_urlopen({"options": [{"option": "X"}], "current_price": 1.0}))
        assert cb._fetch_cboe_payload("ZZZ", 5) is not None
        assert cb._fetch_cboe_payload("ZZZ", 5) is not None   # TTL 内 → 命中
        assert cb.payload_stats() == {"hits": 1, "fetches": 1, "stale": 0, "failed": 0}

    def test_empty_chain_counts_failed(self, cb, monkeypatch):
        monkeypatch.setattr(cb.urllib.request, "urlopen",
                            self._fake_urlopen({"options": []}))
        assert cb._fetch_cboe_payload("ZZZ", 5) is None
        assert cb.payload_stats()["failed"] == 1
        assert cb.payload_stats()["fetches"] == 1

    def test_stale_counts_stale_and_does_not_cache(self, cb, monkeypatch):
        monkeypatch.setattr(cb.urllib.request, "urlopen",
                            self._fake_urlopen({"options": [{"option": "X"}]}))
        monkeypatch.setattr(cb, "_payload_stale_vintage", lambda t, d: ("2026-09-03", "2026-09-04"))
        assert cb._fetch_cboe_payload("ZZZ", 5) is None
        assert cb._fetch_cboe_payload("ZZZ", 5) is None
        s = cb.payload_stats()
        assert s["stale"] == 2 and s["fetches"] == 2 and s["hits"] == 0, \
            "陈旧文件不入缓存，第二次必须再抓、再判陈旧"

    def test_network_failure_counts_each_attempt_and_one_failed(self, cb, monkeypatch):
        def _boom(req, timeout=0):
            raise OSError("down")
        monkeypatch.setattr(cb.urllib.request, "urlopen", _boom)
        monkeypatch.setattr(cb.time, "sleep", lambda s: None)
        assert cb._fetch_cboe_payload("ZZZ", 5, retries=3) is None
        s = cb.payload_stats()
        assert s["fetches"] == 3, "每次 urlopen 都是一次真实请求，重试也要数"
        assert s["failed"] == 1


# ───────────────────────────────────────────── 落盘
class TestWrite:
    def test_write_is_valid_json_with_date_and_sections(self, tmp_path):
        st.record("prefetch", 12.3)
        p = st.write("2026-09-05", path=tmp_path / "t.json", extra={"note": "x"})
        assert p is not None and p.exists()
        d = json.loads(p.read_text())
        assert d["date"] == "2026-09-05"
        assert d["phases"]["prefetch"] == 12.3
        assert set(d["counters"]) == {"yfinance", "twelve_data", "cboe"}
        assert d["extra"] == {"note": "x"}
        assert not (tmp_path / "t.json.tmp").exists(), "临时文件必须被 os.replace 掉"

    def test_write_failure_returns_none_not_raise(self, tmp_path):
        target = tmp_path / "not_a_dir"
        target.write_text("file, not directory")
        assert st.write("2026-09-05", path=target / "x.json") is None


# ───────────────────────────────────────────── 编排器并入
_ORCH = os.path.expanduser("~/.claude/scripts/alpha-hive-orchestrator.sh")


@pytest.mark.skipif(not shutil.which("jq") or not os.path.exists(_ORCH),
                    reason="需要 jq 与编排器脚本")
class TestOrchestratorMerge:
    """从编排器**原文**里抠出两条 jq 过滤器来跑，不在测试里另抄一份。"""

    @pytest.fixture(scope="class")
    def filters(self):
        src = open(_ORCH, encoding="utf-8").read()
        date_f = re.search(r"jq -e --arg d \"\$DATE_STR\" '([^']+)' \"\$_timing\"", src)
        merge_f = re.search(r"jq --slurpfile t \"\$_timing\" '([^']+)' \"\$_tmp\"", src)
        assert date_f and merge_f, "编排器里找不到 v0.45.118 的并入过滤器"
        return date_f.group(1), merge_f.group(1)

    @staticmethod
    def _jq(args):
        return subprocess.run(["jq", *args], capture_output=True, text=True)

    def test_same_day_file_is_merged(self, filters, tmp_path):
        date_f, merge_f = filters
        timing = tmp_path / "scan_timing.json"
        status = tmp_path / "status.json"
        timing.write_text(json.dumps({"date": "2026-09-05", "phases": {"parallel": 2466.0}}))
        status.write_text(json.dumps({"status": "success"}))
        assert self._jq(["-e", "--arg", "d", "2026-09-05", date_f, str(timing)]).returncode == 0
        r = self._jq(["--slurpfile", "t", str(timing), merge_f, str(status)])
        assert r.returncode == 0, r.stderr
        merged = json.loads(r.stdout)
        assert merged["status"] == "success"
        assert merged["scan_timing"]["phases"]["parallel"] == 2466.0

    def test_yesterdays_file_is_rejected(self, filters, tmp_path):
        date_f, _ = filters
        timing = tmp_path / "scan_timing.json"
        timing.write_text(json.dumps({"date": "2026-09-04"}))
        assert self._jq(["-e", "--arg", "d", "2026-09-05", date_f, str(timing)]).returncode != 0, \
            "昨天的耗时文件不能冒充今天的"

    def test_step2_budget_fields_are_in_status_heredoc(self):
        src = open(_ORCH, encoding="utf-8").read()
        for key in ("timeout_seconds", "duration_seconds", "headroom_seconds", "headroom_pct"):
            assert f'"{key}": ${{STEP2_' in src, f"status.json 缺 step2_budget.{key}"
