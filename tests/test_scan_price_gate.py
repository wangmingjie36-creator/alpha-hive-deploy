"""v0.45.504：扫描输入闸 + 编排器代理重探 / Step 2 重试。

2026-10-12：编排器 14:00:08 代理探活**一次**失败就整轮「直连」（实际仍走系统代理，v0.45.496），全部 TLS EOF；
30/30 只现价 0 照样写 predictions / 归档 / 快照 / 账本、发布网站、`status=success`。本文件钉：

1. `price_coverage`（纯函数）的「可用现价」判据与「超过阈值才拒」；
2. `main()` 收到 `ScanInputsUnavailable` ⇒ 不 save_report、不部署，scan_timing 写 `early_exit=price_gate`，退出码 4；
3. 编排器：探活失败按次数 / 间隔重探，恢复了就走代理；Step 2 退出码 4 ⇒ 等待、重定出站、重跑，至多重试 N 次，
   其余退出码一律不重试。

端到端（10-12 故障复现 harness + 本版代码）：预取后 30/30 不可用 ⇒ 在第一次持久写入之前抛出，沙箱里 10-12 的
.swarm_results / predictions / signal_archive / agent_memory / 快照 / 净值行全部为 0（见 CHANGELOG v0.45.504）。
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import alpha_hive_daily_report as adr
from tests._orchestrator import extract_function, repo_orchestrator_text


# ═══════════════════════════════ 1. 判据 ═══════════════════════════════

_OK = {"price": 100.0, "data_source": "real"}


class TestPriceCoverage:
    def _cov(self, data, targets, thr=0.5):
        return adr.price_coverage(data, targets, thr)

    def test_all_fallback_refused(self):
        data = {t: {"price": 0.0, "data_source": "fallback", "_data_unavailable": True} for t in "ABCD"}
        c = self._cov(data, list("ABCD"))
        assert c["refused"] and c["unavailable"] == 4 and c["unavailable_ratio"] == 1.0

    def test_single_missing_not_refused(self):
        data = {t: dict(_OK) for t in "ABC"}
        c = self._cov(data, list("ABCD"))          # D 根本没预取到
        assert not c["refused"] and c["unavailable_tickers"] == ["D"]

    def test_threshold_is_strictly_greater(self):
        data = {"A": dict(_OK), "B": {"price": 0.0}}
        assert not self._cov(data, ["A", "B"])["refused"]            # 1/2 = 0.5，不 > 0.5
        assert self._cov(data, ["A", "B", "C"])["refused"]           # 2/3

    @pytest.mark.parametrize("bad", [
        {"price": 0.0, "data_source": "real"},
        {"price": -1.0, "data_source": "real"},
        {"price": math.nan, "data_source": "real"},
        {"price": math.inf, "data_source": "real"},
        {"price": True, "data_source": "real"},
        {"price": "100", "data_source": "real"},
        {"price": 100.0, "data_source": "fallback"},
        {"price": 100.0, "data_source": "FALLBACK"},
        {"price": 100.0, "data_source": "real", "_data_unavailable": True},
        None,
    ])
    def test_unusable_shapes(self, bad):
        assert not adr._price_usable(bad)

    def test_usable(self):
        assert adr._price_usable(dict(_OK))
        assert adr._price_usable({"price": 15, "data_source": "cached"})

    def test_empty_targets_never_refused(self):
        assert not self._cov({}, [])["refused"]


# ═══════════════════════════════ 2. main() / 退出码 ═══════════════════════════════

@pytest.fixture
def cli(monkeypatch):
    calls, writes = [], []
    cov = adr.price_coverage({}, ["NVDA", "TSLA"], 0.5)

    class SpyReporter:
        date_str = "2026-10-12"

        def __init__(self, date_override=None):
            calls.append("__init__")

        def run_swarm_scan(self, focus_tickers=None):
            calls.append("run_swarm_scan")
            raise adr.ScanInputsUnavailable(cov)

        def __getattr__(self, name):
            def _record(*a, **k):
                calls.append(name)
                return {}
            return _record

    import yf_gate
    monkeypatch.setattr(adr, "AlphaHiveDailyReporter", SpyReporter)
    monkeypatch.setattr(yf_gate, "install", lambda: False)
    monkeypatch.setattr(adr._timing, "write", lambda date, extra=None, **k: writes.append((date, extra)))
    monkeypatch.setattr(sys, "argv", ["alpha_hive_daily_report.py", "--swarm", "--force", "--no-llm",
                                      "--tickers", "NVDA", "TSLA"])
    return SimpleNamespace(calls=calls, writes=writes, cov=cov)


class TestMainOnGate:
    def test_no_save_no_deploy_timing_written(self, cli):
        res = adr.main()
        assert res == {"error": "price_gate", "price_gate": cli.cov}
        assert cli.calls == ["__init__", "run_swarm_scan"], cli.calls      # 没有 save_report / deploy_and_notify
        assert cli.writes and cli.writes[-1][1]["early_exit"] == "price_gate"
        assert cli.writes[-1][1]["price_gate"]["unavailable"] == 2

    def test_exit_code(self, cli):
        assert adr._exit_code_for(adr.main()) == adr.PRICE_GATE_EXIT == 4

    @pytest.mark.parametrize("result,code", [
        ({}, 0), (None, 0), ({"ml_model_guard": {"verdict": "constant"}}, 1),
        ({"ml_model_guard": {"verdict": "ok"}}, 0), ({"error": "price_gate"}, 4),
    ])
    def test_exit_code_mapping(self, result, code):
        assert adr._exit_code_for(result) == code


# ═══════════════════════════════ 3. 编排器 ═══════════════════════════════

def _bash(script: str, env_extra=None) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
           "PYTHON3": sys.executable}
    for k in ("PYTHONUSERBASE",):
        if k in os.environ:
            env[k] = os.environ[k]
    env.update(env_extra or {})
    return subprocess.run(["bash", "-c", "set -uo pipefail\n" + script], env=env,
                          capture_output=True, text=True, timeout=60)


_LOG = 'log() { echo "[$1] ${*:2}"; }\n'


class TestProxyReprobe:
    """`_configure_outbound` 按次数 / 间隔重探；`_probe_proxy_once` 换成脚本化桩。"""

    def _run(self, outcomes: str, attempts="4"):
        text = repo_orchestrator_text()
        cfg = extract_function(text, "_configure_outbound")
        script = (_LOG + 'PROXY_URL="http://127.0.0.1:7897"\n'
                  f'PROXY_PROBE_ATTEMPTS={attempts}\nPROXY_PROBE_INTERVAL=0\n'
                  f'_OUT=({outcomes})\n_N=0\n'
                  '_probe_proxy_once() { local r=${_OUT[$_N]:-1}; _N=$((_N+1)); return $r; }\n'
                  + cfg + '_configure_outbound\n'
                  'echo "PROBES=$_N OK=$_proxy_ok HTTPS_PROXY=${HTTPS_PROXY:-} NO_PROXY=${NO_PROXY:-}"\n')
        r = _bash(script)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("PROBES=")]
        assert line, r.stdout + r.stderr
        return line[0], r.stdout

    def test_recovers_on_third_probe(self):
        line, out = self._run("1 1 0")
        assert line.startswith("PROBES=3 OK=1 HTTPS_PROXY=http://127.0.0.1:7897")
        assert out.count("探活失败（第") == 2 and "第 3 次探活通过" in out

    def test_gives_up_after_attempts(self):
        line, out = self._run("1 1 1 1 1 1")
        assert line.startswith("PROBES=4 OK=0 HTTPS_PROXY= NO_PROXY=*")
        assert "4 次全部失败" in out

    def test_first_probe_ok_no_retry(self):
        line, _ = self._run("0")
        assert line.startswith("PROBES=1 OK=1")

    def test_attempts_one_restores_old_behaviour(self):
        line, _ = self._run("1 0", attempts="1")
        assert line.startswith("PROBES=1 OK=0")


class TestStep2GateRetry:
    """Step 2 循环：退出码 4 ⇒ 等待、重定出站、重跑（至多 N 次）；其它退出码不重试。"""

    def _block(self) -> str:
        text = repo_orchestrator_text()
        i = text.index("STEP2_GATE_RC=4\n")
        j = text.index("unset _gate_detail\n", i) + len("unset _gate_detail\n")
        return text[i:j]

    def _run(self, rcs: str, retries="2", tmp_path=None):
        script = (_LOG + f'_RCS=({rcs})\n_K=0\n_CFG=0\n'
                  'run_step() { local r=${_RCS[$_K]:-0}; _K=$((_K+1)); return $r; }\n'
                  '_configure_outbound() { _CFG=$((_CFG+1)); _proxy_ok=1; }\n'
                  f'DATA_DIR="{tmp_path}"\nPROJECT_DIR=/nonexistent\nLOGFILE=/dev/null\n'
                  'STEP2_TIMEOUT=10\nLLM_FLAG=--no-llm\nTICKERS_ARG="NVDA"\nSTEP2_START=0\n'
                  + self._block() +
                  'echo "RUNS=$_K CFG=$_CFG RC=$STEP2_RC ATTEMPT=$STEP2_ATTEMPT"\n')
        r = _bash(script, {"ALPHA_HIVE_STEP2_GATE_RETRIES": retries, "ALPHA_HIVE_STEP2_GATE_WAIT": "0"})
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("RUNS=")]
        assert line, r.stdout + r.stderr
        return line[0], r.stdout

    def test_success_first_time(self, tmp_path):
        assert self._run("0", tmp_path=tmp_path)[0] == "RUNS=1 CFG=0 RC=0 ATTEMPT=1"

    def test_other_failure_not_retried(self, tmp_path):
        assert self._run("1", tmp_path=tmp_path)[0] == "RUNS=1 CFG=0 RC=1 ATTEMPT=1"
        assert self._run("124", tmp_path=tmp_path)[0] == "RUNS=1 CFG=0 RC=124 ATTEMPT=1"

    def test_gate_then_ok(self, tmp_path):
        line, out = self._run("4 0", tmp_path=tmp_path)
        assert line == "RUNS=2 CFG=1 RC=0 ATTEMPT=2"
        assert "输入闸拒绝（第 1 次）" in out

    def test_gate_exhausts_retries(self, tmp_path):
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs" / "scan_timing.json").write_text(
            '{"extra": {"price_gate": {"unavailable": 30, "total": 30, "max_unavailable_ratio": 0.5}}}')
        line, out = self._run("4 4 4 4", tmp_path=tmp_path)
        assert line == "RUNS=3 CFG=2 RC=4 ATTEMPT=3"
        assert "30/30 只取不到现价" in out and "按零产出处理" in out

    def test_zero_retries(self, tmp_path):
        assert self._run("4 0", retries="0", tmp_path=tmp_path)[0] == "RUNS=1 CFG=0 RC=4 ATTEMPT=1"


def test_gate_raises_inside_init_scan_context():
    """闸必须在 `_init_scan_context`（预取之后、蜂群 / 板 / 任何落库之前）里抛——挪到后面就会先写后拒。
    端到端证据：10-12 故障复现 harness 下沙箱 10-12 的六类产物全为 0（CHANGELOG v0.45.504）。"""
    import ast
    import inspect
    src = inspect.getsource(adr.AlphaHiveDailyReporter._init_scan_context)
    tree = ast.parse(src.replace("\n    ", "\n")[4:] if src.startswith("    ") else src)
    raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)
              and isinstance(n.exc, ast.Call) and getattr(n.exc.func, "id", "") == "ScanInputsUnavailable"]
    assert raises, "_init_scan_context 里找不到 raise ScanInputsUnavailable"
    run_src = inspect.getsource(adr.AlphaHiveDailyReporter.run_swarm_scan)
    assert run_src.index("_init_scan_context(") < run_src.index("_load_checkpoint(")
