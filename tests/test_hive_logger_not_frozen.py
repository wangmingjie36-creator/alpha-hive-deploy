"""hive_logger 的文件日志落点不许在 import 时冻住（v0.45.239）。

事故：`logger = _setup_logger()` 在 import 时执行，裸 `RotatingFileHandler` 把 `baseFilename`
存死；pytest 收集期 import 早于 `conftest._isolate_env` ⇒ 全套测试日志（含夹具造的
`[AAA] 政体层保零违反` ERROR）写进 checkout 的 `logs/alpha_hive.log`。

conftest 的 ①②③ 是逐条/会话观测；本文件是**行为本身**的正面证据，其中
`TestFreshInterpreter` 不经 conftest 的任何隔离（新解释器、env 现造），
所以 conftest 的会话缺省即使把问题接住了，这里照样会红。
"""

import json
import logging
import os
import pathlib
import subprocess
import sys
import uuid

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _flush_alpha_hive_handlers():
    for h in logging.getLogger("alpha_hive").handlers:
        h.flush()


class TestInProcess:
    def test_log_lands_in_this_tests_sandbox(self, tmp_path):
        """事故原样：handler 在收集期构造，本条的 env 是在那之后才由 fixture 设的。"""
        from hive_logger import get_logger

        marker = f"isolation-probe-{uuid.uuid4().hex}"
        get_logger("test_probe").error(marker)
        _flush_alpha_hive_handlers()

        for leaf in ("alpha_hive.log", "alpha_hive_structured.jsonl"):
            f = tmp_path / "logs" / leaf
            assert f.exists(), f"{leaf} 没写进本条沙箱 {f.parent}——落点多半又冻在 import 那一刻了"
            assert marker in f.read_text(encoding="utf-8"), f"{f} 存在但不含本条的标记"

    def test_rotation_happens_at_the_repointed_path(self, tmp_path, monkeypatch):
        """重指之后旋转必须发生在新目录（`doRollover` 按 `baseFilename` 改名）。"""
        from hive_logger import LogsDirRotatingFileHandler

        h = LogsDirRotatingFileHandler("rot_probe.log", maxBytes=200, backupCount=2,
                                       encoding="utf-8")
        h.setFormatter(logging.Formatter("%(message)s"))
        try:
            for d in ("A", "B"):
                monkeypatch.setenv("ALPHA_HIVE_LOGS_DIR", str(tmp_path / d))
                for _ in range(5):
                    h.handle(logging.makeLogRecord({"msg": "x" * 80, "levelno": logging.INFO}))
        finally:
            h.close()

        for d in ("A", "B"):
            names = sorted(p.name for p in (tmp_path / d).iterdir())
            assert names == ["rot_probe.log", "rot_probe.log.1", "rot_probe.log.2"], (d, names)


# 新解释器里跑：「先 import，后改 env」——与 pytest 收集期 / fixture 的先后完全相同，
# 且不带 conftest 的会话缺省。ALPHA_HIVE_HOME 在 import **前**就指到沙箱：变异（改回冻住）下
# handler 冻在这里，不写 checkout。
_PROBE = r"""
import json, logging, os, sys
sys.path.insert(0, sys.argv[1])
home, a, b = sys.argv[2], sys.argv[3], sys.argv[4]

import hive_logger
out = {
    "home_logs_after_import": os.path.exists(os.path.join(home, "logs")),
    "hive_logger_file": os.path.abspath(hive_logger.__file__),
}

os.environ["ALPHA_HIVE_LOGS_DIR"] = a
hive_logger.get_logger("probe").error("marker-A")
os.environ["ALPHA_HIVE_LOGS_DIR"] = b
hive_logger.get_logger("probe").error("marker-B")
for h in logging.getLogger("alpha_hive").handlers:
    h.flush()

# 生产不设任何 env：只问落点，不写
del os.environ["ALPHA_HIVE_LOGS_DIR"]
del os.environ["ALPHA_HIVE_HOME"]
out["production_targets"] = sorted(
    str(h.current_target()) for h in logging.getLogger("alpha_hive").handlers
    if hasattr(h, "current_target"))
print(json.dumps(out))
"""


class TestFreshInterpreter:
    def _run(self, tmp_path):
        home, a, b = (tmp_path / n for n in ("home", "A", "B"))
        env = {k: v for k, v in os.environ.items() if not k.startswith("ALPHA_HIVE_")}
        env["ALPHA_HIVE_HOME"] = str(home)
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        r = subprocess.run(
            [sys.executable, "-c", _PROBE, str(REPO_ROOT), str(home), str(a), str(b)],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout.strip().splitlines()[-1]), home, a, b

    def test_env_changed_after_import_is_followed(self, tmp_path):
        out, home, a, b = self._run(tmp_path)
        assert out["hive_logger_file"] == str(REPO_ROOT / "hive_logger.py"), out

        for d, mine, other in ((a, "marker-A", "marker-B"), (b, "marker-B", "marker-A")):
            for leaf in ("alpha_hive.log", "alpha_hive_structured.jsonl"):
                text = (d / leaf).read_text(encoding="utf-8") if (d / leaf).exists() else ""
                assert mine in text and other not in text, (
                    f"{d / leaf}：应只含 {mine}。import 之后改 env 没被跟上 ⇒ 落点冻在 import 时。")
        assert not (home / "logs").exists(), "import 时的缺省落点收到了写入 ⇒ handler 冻住了"

    def test_import_alone_touches_no_file(self, tmp_path):
        """import 不建 `logs/`、不建空日志文件——收集期 import 因此无从留下痕迹。"""
        out, *_ = self._run(tmp_path)
        assert out["home_logs_after_import"] is False

    def test_production_default_is_this_checkouts_logs_dir(self, tmp_path):
        """生产不设 env：落点必须仍是 hive_logger 所在 checkout 的 `logs/`（人工排查读这里）。"""
        out, *_ = self._run(tmp_path)
        logs = REPO_ROOT / "logs"
        assert out["production_targets"] == sorted(
            [str(logs / "alpha_hive.log"), str(logs / "alpha_hive_structured.jsonl")]), out


class TestSetupCheckHasTeeth:
    """conftest ① 的判据：冻住的 handler 必须被报出来，跟着 env 走的不许被误报。"""

    def test_frozen_handler_outside_sandbox_is_reported(self, tmp_path, hive_log_handler_escapes):
        from logging.handlers import RotatingFileHandler

        frozen = tmp_path.parent / f"{tmp_path.name}__frozen" / "alpha_hive.log"
        h = RotatingFileHandler(str(frozen), delay=True)
        try:
            got = hive_log_handler_escapes(tmp_path, handlers=[h])
        finally:
            h.close()
        assert got == [("RotatingFileHandler", str(frozen.resolve()))]

    def test_stale_basefilename_is_not_reported_when_resolver_points_inside(
            self, tmp_path, hive_log_handler_escapes):
        """`baseFilename` 在两条测试之间是上一条的（emit 时才重指）——判据必须问 resolver。"""
        from hive_logger import LogsDirRotatingFileHandler

        h = LogsDirRotatingFileHandler("alpha_hive.log")
        h.baseFilename = str(tmp_path.parent / "some_earlier_test" / "alpha_hive.log")
        try:
            assert hive_log_handler_escapes(tmp_path, handlers=[h]) == []
        finally:
            h.close()

    def test_real_handlers_are_inside(self, tmp_path, hive_log_handler_escapes):
        assert hive_log_handler_escapes(tmp_path) == []
