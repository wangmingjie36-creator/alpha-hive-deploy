"""`step_contract` 外壳本身的单元测试（各工具「真实产出满足契约」的测试在各自的测试文件与契约守卫里）。

另含两组跨五个工具的守卫：`step_contract` 本身导入失败时入口退出码 3（`TestStepContractImportFailure`），
以及「缺省业务日 ≠ 编排器 DATE_STR」这条关系的文档与事实（`TestBusinessTodayVsDateStr`）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import step_contract as sc

REPO_ROOT = Path(__file__).resolve().parent.parent   # 指向**代码**，故用 __file__

#: 编排器 Step 10–15 调用、入口经 `step_contract.run_tool` 的五个工具
FIVE_TOOLS = ("scan_continuity", "ic_rerun_readiness", "scan_coverage_gate", "economic_calendar_watch",
              "backup_continuity")


class TestEnvelope:
    def test_keeps_payload_keys_and_adds_envelope(self):
        env = sc.envelope("t", "2026-09-28", "ok", payload={"weeks_accrued": 3, "cohort": {"date": "x"}},
                          generated_at="2026-09-28T14:00:00-07:00")
        assert env["weeks_accrued"] == 3 and env["cohort"] == {"date": "x"}   # 原有键原样在顶层（编排器现行解析照常）
        assert {k: env[k] for k in sc.ENVELOPE_KEYS} == {
            "schema_version": sc.SCHEMA_VERSION, "tool": "t", "date": "2026-09-28",
            "generated_at": "2026-09-28T14:00:00-07:00", "status": "ok", "attention": []}
        assert sc.validate(env) == []

    def test_payload_may_not_shadow_envelope_keys(self):
        """变红的变异：删掉撞名检查 ⇒ 工具原有的 `status`（例如 "ready"）被外壳静默改写。"""
        with pytest.raises(ValueError, match="保留键"):
            sc.envelope("t", "2026-09-28", "ok", payload={"status": "ready"})

    @pytest.mark.parametrize("bad", ["2026-9-28", "", None, "tomorrow"])
    def test_date_must_be_iso(self, bad):
        with pytest.raises(ValueError):
            sc.envelope("t", bad, "ok")

    def test_unknown_status_rejected(self):
        with pytest.raises(ValueError):
            sc.envelope("t", "2026-09-28", "ready")

    def test_attention_items_are_validated_and_sourced(self):
        a = sc.attention_item("x.deadline", "warn", "须早于 2026-10-12 登记", deadline="2026-10-12")
        env = sc.envelope("tool_a", "2026-09-28", "attention", attention=[a])
        assert env["attention"] == [{"id": "x.deadline", "level": "warn", "message": "须早于 2026-10-12 登记",
                                     "deadline": "2026-10-12", "source": "tool_a"}]
        with pytest.raises(ValueError):
            sc.attention_item("x", "urgent", "m")
        with pytest.raises(ValueError):
            sc.attention_item("x", "warn", "m", deadline="10/12")
        with pytest.raises(ValueError, match="缺键"):
            sc.envelope("t", "2026-09-28", "attention", attention=[{"id": "x", "level": "warn"}])

    def test_validate_flags_ok_with_warn(self):
        """status=ok 却带 warn/alarm ⇒ 自相矛盾（消费方按 status 走会漏掉它）。"""
        env = sc.envelope("t", "2026-09-28", "attention",
                          attention=[sc.attention_item("a", "warn", "m")])
        env["status"] = "ok"
        assert any("自相矛盾" in p for p in sc.validate(env))

    def test_validate_reports_missing_and_wrong_schema(self):
        assert sc.validate([]) and sc.validate({})
        env = sc.envelope("t", "2026-09-28", "ok")
        env["schema_version"] = 99
        assert any("schema_version" in p for p in sc.validate(env))

    def test_validate_rejects_non_string_date(self):
        """`_check_date` 会先 `str()`——消费方那一侧的 JSON 日期必须本身就是字符串。"""
        env = sc.envelope("t", "2026-09-28", "ok")
        env["date"] = 20260928
        assert any("date" in p for p in sc.validate(env))


#: 一条合规条目；各坏例只改一个字段（`attention_item` 构造时会拒的，validate 也必须拒）
_GOOD_ITEM = {"id": "t.x", "level": "warn", "message": "m", "deadline": "2026-10-12", "source": "t"}
_BAD_FIELDS = [
    ("int_id", {"id": 7}),
    ("empty_id", {"id": ""}),
    ("none_id", {"id": None}),
    ("bad_level", {"level": "urgent"}),
    ("unhashable_level", {"level": ["warn"]}),
    ("none_message", {"message": None}),
    ("empty_message", {"message": ""}),
    ("int_message", {"message": 3}),
    ("dict_deadline", {"deadline": {"y": 2026}}),
    ("unpadded_deadline", {"deadline": "2026-9-5"}),
    ("slash_deadline", {"deadline": "10/12/2026"}),
    ("int_deadline", {"deadline": 20261012}),
    ("int_source", {"source": 5}),
]


class TestAttentionRulesShared:
    """#9：validate 此前只看「键在不在」与 level——int id、None message、dict / "2026-9-5" 的 deadline 全放行，
    生产方构造时会被 `attention_item` 拒的东西，消费方照单全收。现在三处（attention_item / envelope / validate）
    走同一套规则。变红的变异：把 `validate` 里的 `_attention_problems` 换回只查键 + level 的旧循环。"""

    @pytest.mark.parametrize("label,patch", _BAD_FIELDS, ids=[b[0] for b in _BAD_FIELDS])
    def test_validate_rejects(self, label, patch):
        env = sc.envelope("t", "2026-09-28", "attention", attention=[sc.attention_item("t.ok", "info", "m")])
        env["attention"].append({**_GOOD_ITEM, **patch})
        problems = sc.validate(env)
        field = next(iter(patch))
        assert any(re.match(rf"attention\[1\]\.{field}\b", p) for p in problems), (label, problems)

    @pytest.mark.parametrize("label,patch", _BAD_FIELDS, ids=[b[0] for b in _BAD_FIELDS])
    def test_constructors_reject_the_same(self, label, patch):
        """同一批坏例：`attention_item` 构造即抛；绕过它手搓的 dict 交给 `envelope` 也抛——生产方写不出消费方会拒收的条目。"""
        a = {**_GOOD_ITEM, **patch}
        # int_deadline：构造时 deadline 先经 `_check_date` 归一化（接受 date 对象），20261012 归一化后仍不合规 ⇒ 同样抛
        with pytest.raises(ValueError):
            sc.attention_item(a["id"], a["level"], a["message"], deadline=a["deadline"], source=a["source"])
        with pytest.raises(ValueError, match="不合契约"):
            sc.envelope("t", "2026-09-28", "attention", attention=[a])

    def test_good_item_passes_everywhere(self):
        """正对照：坏例全改自这一条，它本身三处都过（否则上面的「拒」可能只是一律拒）。"""
        a = sc.attention_item(**{k: _GOOD_ITEM[k] for k in ("id", "level", "message")},
                              deadline=_GOOD_ITEM["deadline"], source=_GOOD_ITEM["source"])
        assert a == _GOOD_ITEM
        env = sc.envelope("t", "2026-09-28", "attention", attention=[a, dict(_GOOD_ITEM, id="t.y", deadline=None,
                                                                             source=None)])
        assert sc.validate(env) == [] and env["attention"][1]["source"] == "t"

    def test_date_object_deadline_is_normalized(self):
        """构造时接受 `date` 对象并归一化成字符串——归一化后的结果必须过 validate（JSON 里只有字符串）。"""
        from datetime import date
        a = sc.attention_item("t.d", "warn", "m", deadline=date(2026, 10, 12))
        assert a["deadline"] == "2026-10-12"
        assert sc.validate(sc.envelope("t", "2026-09-28", "attention", attention=[a])) == []


class TestAttentionIdsUnique:
    """D3：`id` 是消费方去重 / 路由的键。同一外壳两条同 id ⇒ 按 id 保留首条的消费方无声丢掉后一条。
    变红的变异：删掉 `_attention_problems` 里的重复检查（validate 与 envelope 两处一起失守）。"""

    def _dup_env(self):
        env = sc.envelope("t", "2026-09-28", "attention", attention=[
            sc.attention_item("t.boundary.vA", "alarm", "A"),
            sc.attention_item("t.boundary.vB", "alarm", "B", deadline="2026-10-12")])
        env["attention"][1]["id"] = "t.boundary.vA"      # 模拟生产方把两条写成同一个 id
        return env

    def test_validate_flags_duplicate_ids(self):
        problems = sc.validate(self._dup_env())
        assert any("重复" in p and "t.boundary.vA" in p for p in problems), problems

    def test_envelope_refuses_duplicate_ids(self):
        with pytest.raises(ValueError, match="重复"):
            sc.envelope("t", "2026-09-28", "attention", attention=self._dup_env()["attention"])

    def test_distinct_ids_pass(self):
        env = self._dup_env()
        env["attention"][1]["id"] = "t.boundary.vB"
        assert sc.validate(env) == []


@pytest.fixture
def umask():
    """临时改进程 umask，测完还原（umask 是进程级状态，不还原会串到别的测试）。"""
    saved = os.umask(0o022)
    os.umask(saved)
    try:
        yield lambda m: os.umask(m)
    finally:
        os.umask(saved)


class TestWriteOut:
    def test_atomic_and_roundtrip(self, tmp_path):
        env = sc.envelope("t", "2026-09-28", "ok", payload={"n": 1})
        (tmp_path / "sub").mkdir()
        p = sc.write_out(tmp_path / "sub" / "o.json", env)
        assert json.loads(p.read_text(encoding="utf-8")) == env
        assert [x.name for x in p.parent.iterdir()] == ["o.json"]   # 没留下 .tmp

    def test_does_not_create_missing_parent(self, tmp_path):
        """D6(a)：父目录不存在 ⇒ 抛 FileNotFoundError（OSError，各工具正常路径照旧按「写不出去」处理），
        且**什么都不建**。此前 `mkdir(parents=True)` ⇒ `run_tool` 的崩溃路径会建出 Step 10/15 正常路径刻意
        不建的目录。变红的变异：恢复 `p.parent.mkdir(parents=True, exist_ok=True)`。"""
        root = tmp_path / "root"          # tmp_path 里 conftest 另放了沙箱目录，单独开一个空目录来数
        root.mkdir()
        target = root / "no_such_dir" / "deeper" / "o.json"
        with pytest.raises(FileNotFoundError):
            sc.write_out(target, sc.envelope("t", "2026-09-28", "ok"))
        assert list(root.iterdir()) == [], "父目录不存在时建出了东西（目录或残留的临时文件）"

    @pytest.mark.parametrize("mask,expect", [(0o022, 0o644), (0o027, 0o640), (0o002, 0o664)],
                             ids=["umask022", "umask027", "umask002"])
    def test_mode_follows_umask_like_open(self, tmp_path, umask, mask, expect):
        """D6(b)：权限 = 普通 `open()` 新建文件的 0o666 & ~umask，不是 `mkstemp` 写死的 0600——本仓的环境笔记拿
        0600 认 iCloud 重名副本，正常产物也 0600 就是误导。三个 umask 各测一次：证明跟的是 umask，
        不是另一个写死的常数。变红的变异：换回 `tempfile.mkstemp`（恒 0600）。"""
        umask(mask)
        p = sc.write_out(tmp_path / "o.json", sc.envelope("t", "2026-09-28", "ok"))
        assert stat.S_IMODE(p.stat().st_mode) == expect, oct(stat.S_IMODE(p.stat().st_mode))
        control = tmp_path / "control.txt"               # 正对照：同一 umask 下普通 open() 的产物
        with open(control, "w", encoding="utf-8") as f:
            f.write("x")
        assert stat.S_IMODE(control.stat().st_mode) == stat.S_IMODE(p.stat().st_mode)

    def test_replace_does_not_keep_old_0600(self, tmp_path, umask):
        """覆盖一个既有的 0600 文件（例如旧版留下的）⇒ 新文件按 umask，不沿用旧权限（replace = 新 inode）。"""
        umask(0o022)
        target = tmp_path / "o.json"
        target.write_text("{}", encoding="utf-8")
        target.chmod(0o600)
        sc.write_out(target, sc.envelope("t", "2026-09-28", "ok"))
        assert stat.S_IMODE(target.stat().st_mode) == 0o644


class TestRunTool:
    def test_passthrough_exit_codes(self):
        assert sc.run_tool("t", lambda: 1, argv=[]) == 1
        assert sc.run_tool("t", lambda: None, argv=[]) == 0

    def test_systemexit_passes_through(self):
        def boom():
            raise SystemExit(2)
        with pytest.raises(SystemExit):
            sc.run_tool("t", boom, argv=[])

    def test_crash_is_3_and_writes_error_envelope(self, tmp_path, capsys):
        """变红的变异：把 `return 3` 改成 1（= Python 默认）⇒ 崩溃又被编排器记成「未就绪（正常）」；
        删掉写外壳 ⇒ out.json 不存在。"""
        out = tmp_path / "o.json"

        def boom():
            raise RuntimeError("file is not a database")
        rc = sc.run_tool("ic_rerun_readiness", boom, argv=["--quiet", "--out", str(out), "--today", "2026-09-28"])
        assert rc == 3
        env = json.loads(out.read_text(encoding="utf-8"))
        assert sc.validate(env) == []
        assert env["status"] == "error" and env["date"] == "2026-09-28"
        assert env["attention"][0]["id"] == "ic_rerun_readiness.crashed" and env["attention"][0]["level"] == "alarm"
        assert "file is not a database" in env["error"]
        assert "退出码 3" in capsys.readouterr().err

    def test_crash_without_out_still_3(self):
        def boom():
            raise ValueError("x")
        assert sc.run_tool("t", boom, argv=[]) == 3

    def test_crash_with_out_in_missing_dir_logs_and_still_3(self, tmp_path, capsys):
        """D6(a)：崩溃路径不比正常路径多建任何东西——`--out` 的父目录不存在 ⇒ 只打 stderr、不建目录、仍返回 3。
        变红的变异：`write_out` 恢复 mkdir（目录与错误外壳被建出来）；或 run_tool 不再吞写外壳的异常（抛出来）。"""
        out = tmp_path / "no_such_dir" / "o.json"

        def boom():
            raise RuntimeError("boom")
        assert sc.run_tool("t", boom, argv=["--out", str(out)]) == 3
        assert not (tmp_path / "no_such_dir").exists(), "崩溃路径建出了正常路径不建的目录"
        err = capsys.readouterr().err
        assert "错误外壳写入" in err and "FileNotFoundError" in err, err

    def test_real_subprocess_crash_exit_code(self, tmp_path):
        """端到端：真起一个子进程，入口照各工具的写法 `sys.exit(run_tool(...))`，异常 ⇒ 进程退出码 3。"""
        script = tmp_path / "tool.py"
        script.write_text(
            "import sys\nsys.path.insert(0, %r)\nimport step_contract as sc\n"
            "def main():\n    raise RuntimeError('boom')\n"
            "sys.exit(sc.run_tool('tool', main))\n" % str(REPO_ROOT), encoding="utf-8")
        out = tmp_path / "o.json"
        r = subprocess.run([sys.executable, str(script), "--out", str(out)], capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 3, r.stderr
        assert json.loads(out.read_text(encoding="utf-8"))["status"] == "error"


# ═══════════════════ D4：step_contract 本身导入失败 ⇒ 五个工具入口退出码 3 ═══════════════════

#: 影子 `step_contract.py`：「缺失」照 Python 找不到模块时的原样异常；「坏了」是语法错（import 期 SyntaxError）
_SHADOWS = {
    "missing": "raise ModuleNotFoundError(\"No module named 'step_contract'\", name='step_contract')\n",
    "broken": "def run_tool(:\n",
}


def _sandbox_env(tmp_path: Path) -> dict:
    env = dict(os.environ)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env.update({
        "ALPHA_HIVE_HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        # 被测工具真正的依赖（is_trading_day / scan_continuity / hive_logger …）从仓库解析；
        # 只有 step_contract 被放在脚本目录里的影子挡住（脚本目录恒排在 sys.path 最前）
        "PYTHONPATH": str(REPO_ROOT),
        "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
        "no_proxy": "", "NO_PROXY": "",
    })
    return env


def _run_copied_tool(tmp_path: Path, tool: str, shadow, *args):
    """把**真实**脚本逐字节拷进 tmp 的独立目录，旁边放（或不放）影子 step_contract.py，照编排器的方式
    `python <脚本路径> …` 起子进程。拷贝而不是软链：各工具用 `Path(__file__).resolve().parent` 定位自己，
    软链会被 resolve 回仓库、影子就挡不住了。"""
    d = tmp_path / "tooldir"
    d.mkdir()
    shutil.copy2(REPO_ROOT / f"{tool}.py", d / f"{tool}.py")
    if shadow is not None:
        (d / "step_contract.py").write_text(shadow, encoding="utf-8")
    return subprocess.run([sys.executable, str(d / f"{tool}.py"), *map(str, args)], capture_output=True,
                          text=True, cwd=str(tmp_path), env=_sandbox_env(tmp_path), timeout=120)


class TestStepContractImportFailure:
    """`import step_contract` 在模块顶层，早于 `run_tool` 能兜的任何地方执行。此前它一失败就是
    ModuleNotFoundError / SyntaxError ⇒ Python 默认退出码 1 ⇒ 编排器记成「降级 / 未就绪（正常）」。
    现在五个工具各自在 import 处兜住、置哨兵，入口见哨兵退出码 3 + stderr 说明（写不出外壳，不写 --out）。

    ⚠️ 只覆盖 step_contract 这一个 import：工具模块自己的其他导入期失败仍是 1（见 `run_tool` docstring）。
    变红的变异：任一工具去掉 try/except（裸 `import step_contract`）⇒ 那个工具 rc 1；
    去掉 `__main__` 的哨兵检查 ⇒ `None.run_tool` AttributeError ⇒ rc 1。"""

    @pytest.mark.parametrize("variant", sorted(_SHADOWS))
    @pytest.mark.parametrize("tool", FIVE_TOOLS)
    def test_exit_3_with_clear_message(self, tmp_path, tool, variant):
        out = tmp_path / "out.json"
        r = _run_copied_tool(tmp_path, tool, _SHADOWS[variant], "--quiet", "--out", out)
        assert r.returncode == 3, f"{tool}/{variant}: rc={r.returncode}\nstderr={r.stderr[-2000:]}"
        assert "无法导入 step_contract" in r.stderr and "退出码 3" in r.stderr, r.stderr[-2000:]
        exc = "ModuleNotFoundError" if variant == "missing" else "SyntaxError"
        assert exc in r.stderr, f"stderr 里要带上导入失败的原因：{r.stderr[-2000:]}"
        assert r.stdout.strip() == "", "导入失败时不该印出一行看起来正常的结论"
        assert not out.exists(), "没有 step_contract 就写不出外壳，也不该写出别的东西"

    @pytest.mark.parametrize("tool", FIVE_TOOLS)
    def test_control_copied_tool_imports_fine(self, tmp_path, tool):
        """正对照：同样的拷贝方式、不放影子（step_contract 从 PYTHONPATH 的仓库解析）⇒ 模块完整导入、
        `--help` 走到 argparse、退出码 0——证明上面的 3 来自影子，不是拷贝本身把导入弄坏了。"""
        r = _run_copied_tool(tmp_path, tool, None, "--help")
        assert r.returncode == 0, f"{tool}: rc={r.returncode}\nstderr={r.stderr[-2000:]}"
        assert "usage" in r.stdout.lower()


# ═══════════════════ D2：缺省业务日 ≠ 编排器 DATE_STR（文档与事实）═══════════════════

class TestBusinessTodayVsDateStr:
    """`business_today()` 取洛杉矶日；编排器 `DATE_STR=$(date +%Y-%m-%d)` 取本机时区（America/Vancouver）。
    此前 step_contract 写「与编排器的 DATE_STR 同一口径」、四个工具写「本机时区即 PDT，生产行为不变」——
    本机 tzdata 里温哥华 2026-11-01 起常年 UTC-7，此后每逢洛杉矶冬令时，两句话每天有一小时是错的。"""

    VAN, LA = ZoneInfo("America/Vancouver"), ZoneInfo("America/Los_Angeles")

    def test_fact_cited_in_docstring_holds_on_this_tzdata(self):
        """docstring 引用的事实本身：2026-11-02 07:30 UTC = 温哥华 11-02 00:30、洛杉矶 11-01 23:30 ⇒ 差一天；
        夏令时期间（09-28）同一时刻两地同日。若换到 tzdata 较旧的机器上这条红，说明文档里的关系在那里不成立。"""
        t = datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc)
        assert (t.astimezone(self.VAN).date().isoformat(), t.astimezone(self.LA).date().isoformat()) == (
            "2026-11-02", "2026-11-01")
        t0 = datetime(2026, 9, 28, 7, 30, tzinfo=timezone.utc)
        assert t0.astimezone(self.VAN).date() == t0.astimezone(self.LA).date()

    def test_docstring_states_the_real_relationship(self):
        doc = sc.business_today.__doc__ or ""
        assert "同一口径" not in doc, doc
        for must in ("America/Vancouver", "2026-11-01", "--date", "--end", "--today"):
            assert must in doc, f"business_today 的 docstring 缺「{must}」"

    @pytest.mark.parametrize("name", ("step_contract",) + FIVE_TOOLS)
    def test_false_claims_gone(self, name):
        """变红的变异：把任一处改回旧说法。"""
        src = (REPO_ROOT / f"{name}.py").read_text(encoding="utf-8")
        for claim in ("本机时区即 PDT", "与编排器的 DATE_STR 同一口径", "与编排器 DATE_STR 同口径"):
            assert claim not in src, f"{name}.py 仍写着「{claim}」"
