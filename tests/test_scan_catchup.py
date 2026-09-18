"""开机补跑闸的守卫（v0.45.34）。

治的是：plist 原 RunAtLoad=false，关机错过 14:00 就永久漏掉那天
（实测 W29/W32/W34 三周完全无扫描，日志零记录 = 机器没开）。

⚠️ 最危险的不是漏跑，是**修复本身**：RunAtLoad=true 会在每次登录触发，
若无时间闸，早上开机就会在盘中跑，把盘中价当收盘价写进 predictions ——
那正是 MEMORY alpha-hive-accuracy-metrics-suspect 记的头号污染源。
所以本文件的重点是守住时间闸，而不是守住"能补跑"。
"""

import datetime
import json
import os
import re
import signal
import subprocess
import time
import types

import pytest

ORCH = os.path.expanduser("~/.claude/scripts/alpha-hive-orchestrator.sh")
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.alpha.hive.daily.plist")

# 行首的 bash 赋值：可缩进、可带 export / readonly / local
_ASSIGN_PREFIX = r"^[ \t]*(?:export[ \t]+|readonly[ \t]+|local[ \t]+)?"
# 闸之前的非注释行里出现这些前缀 ⇒ 有一处「写到哪」由字面绝对路径决定，改绑管不到。
# `~` / `$HOME` 不在此列：沙箱跑时子进程 HOME 指向沙箱，运行时兜住。
_LITERAL_ABS_PATH = re.compile(r"/(?:Users|tmp|private|Volumes)/")


def _orch_literal(orch_text, name):
    """编排器里 `NAME="..."` 的纯字面值；赋值不止一处、或不是纯字面 ⇒ None。

    这些值**归编排器说了算**，测试不另抄一份（v0.45.219 起 marker 目录如此，
    v0.45.221 推广到闸前全部路径）：抄的那份在项目搬家后会继续指旧目录。
    也**不能**锚到 `__file__`：worktree 里的路径编排器根本不看。

    要数**所有**写入（缩进 / `then NAME=` / export / `NAME+=` / `${NAME:=...}`），
    不只认顶格那一行：闸之前再冒出一句 `LOGDIR=...`，改绑了顶格那处也会被它改回去。
    只跳过整行注释；其余一切像写入的都算 —— 多数到了（比如字符串里的 `NAME=`）
    只会返回 None 让守卫红，是安全的方向。`for NAME in` / `read NAME` 数不到。
    只认纯字面 —— 改成 `$HOME/...` 之类要展开的写法时返回 None 让守卫红，不去猜。
    计数规则由 `TestOrchLiteral` 在任何机器上钉住（真编排器里恰好一处覆盖都没有，
    只拿它测，退回「只数顶格」照样全绿）。
    """
    code = "\n".join(ln for ln in orch_text.splitlines() if not ln.lstrip().startswith("#"))
    if len(re.findall(rf"\b{name}\+?=|\$\{{{name}:?=", code)) != 1:
        return None
    m = re.search(rf'{_ASSIGN_PREFIX}{name}="([^"$`\\]+)"[ \t]*(?:#.*)?$', code, flags=re.M)
    return m.group(1) if m else None


def _sandbox_orchestrator(orch_text, sandbox):
    """把编排器关进沙箱，返回 `(改绑后的脚本, {名字: 被替换掉的生产值})`。

    只改绑闸前决定「写到哪 / 放不放行」的五个赋值，闸逻辑逐字节不动 ——
    测的仍是编排器**此刻**的闸，不是一份会过期的抄本。改绑不上、或闸之前
    冒出新的字面绝对路径 ⇒ AssertionError，**在真跑之前**拒绝。
    """
    binds = {
        "LOCKDIR": sandbox / "lock",         # 不抢 /tmp 那把锁：抢到＝同时触发的真扫描被挤掉
        "PROJECT_DIR": sandbox / "project",  # marker 在这里读
        "LOGDIR": sandbox / "logs",
        "REPORTDIR": sandbox / "reports",    # status.json 在这里写
        # 时间闸恒拦：marker 没被读到时落进「早于收盘」分支 ⇒ 断言红，
        # 而不是过了 13:30 真跑一轮全量扫描。真阈值由 TestCatchupGate 静态守着。
        "CATCHUP_AFTER_HHMM": "2400",
    }
    prod = {}
    for name, new in binds.items():
        prod[name] = _orch_literal(orch_text, name)
        assert prod[name], f'编排器里读不出唯一的字面 {name}="..." —— 改绑不上，拒绝真跑'
        orch_text, n = re.subn(rf'({_ASSIGN_PREFIX}{name}=)"[^"]*"',
                               lambda m, v=new: f'{m.group(1)}"{v}"',
                               orch_text, flags=re.M)
        assert n == 1, f"{name} 改绑命中 {n} 处"
    head = orch_text[:orch_text.index("STEP1_START")].replace(str(sandbox), "")
    stray = [ln for ln in head.splitlines()
             if not ln.lstrip().startswith("#") and _LITERAL_ABS_PATH.search(ln)]
    assert not stray, f"闸之前有改绑管不到的字面绝对路径，拒绝真跑：{stray}"
    return orch_text, prod


def _traces(dirs, tag, today):
    """`status.json` 与当天编排器日志里，内容出现 `tag` 的那几个文件。

    判「沙箱路径出没出现」，不判 `(size, mtime)`「动没动过」：真扫描可能正好
    同时在跑（14:00 起连写一个多小时日志），指纹比对在那段时间恒红。
    沙箱路径是本次 tmp_path、全局唯一，而编排器闸前写的两样东西都带着它
    （status.json 的 `logfile`、日志的「📂 项目目录」行）—— 哪处漏改绑写回了生产，
    就在那里留下它。
    """
    hits = []
    for path in (os.path.join(dirs["REPORTDIR"], "status.json"),
                 os.path.join(dirs["LOGDIR"], f"orchestrator-{today}.log")):
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="ignore") as f:
                if tag in f.read():
                    hits.append(path)
    return hits


def _reap_group(pgid):
    """SIGKILL 整个进程组并等它清空；返回动手前组里是否还有活口。

    ⚠️ macOS 上组员退出途中 killpg 偶发 EPERM 而不是 ESRCH（v0.45.221 变异回归里
    撞到 2 次，推测是待回收的僵尸组员）—— 只认 ESRCH 为「清空」，EPERM 当「还在收」，
    每轮重发 SIGKILL。当异常抛出去会被 xfail(raises=AssertionError) 判成 FAILED，
    也会顶掉用例本该报的断言。
    """
    lingered = None
    deadline = time.monotonic() + 10
    while True:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return bool(lingered)
        except PermissionError:
            pass
        if lingered is None:
            lingered = True
        if time.monotonic() > deadline:
            raise AssertionError(f"进程组 {pgid} 10s 内收不干净 —— 看门狗会活满 25h")
        time.sleep(0.05)


_ONE = 'NAME="/x"'


class TestOrchLiteral:
    """`_orch_literal` 的计数规则 —— 纯字符串，不依赖本机编排器，任何环境都跑。"""

    @pytest.mark.parametrize("text, want", [
        (_ONE, "/x"),
        (_ONE + "   # 注释", "/x"),
        ('# NAME="/old"\n' + _ONE, "/x"),                       # 整行注释不算
        (_ONE + '\nM="$NAME/.swarm_results_x.json"', "/x"),     # 引用不算
        ('ALPHA_HIVE_NAME="/s"\n' + _ONE, "/x"),                # 后缀同名不算
        (_ONE + '\nif [ -n "$S" ]; then\n    NAME="/s"\nfi', None),
        (_ONE + '\nif [ -n "$S" ]; then NAME="/s"; fi', None),
        (_ONE + '\nexport NAME="/s"', None),
        (_ONE + '\nNAME+="/s"', None),
        (_ONE + '\n: "${NAME:=/s}"', None),
        (_ONE + '\n: "${NAME=/s}"', None),
        ('NAME="${SANDBOX:-/x}"', None),                        # 非纯字面
        ('NAME="/x"suffix', None),                              # 值其实是 /xsuffix
        (_ONE + "\n" + _ONE, None),
        ('OTHER="/x"', None),
    ])
    def test_counting(self, text, want):
        assert _orch_literal(text, "NAME") == want

    _MINI = ('LOCKDIR="/tmp/l"\nPROJECT_DIR="/Users/u/p"\nLOGDIR="/Users/u/logs"\n'
             'REPORTDIR="/Users/u/rep"\nCATCHUP_AFTER_HHMM="1330"\n{extra}STEP1_START=1\n')

    def test_sandbox_rebinds_every_value(self, tmp_path):
        script, prod = _sandbox_orchestrator(self._MINI.format(extra=""), tmp_path)
        assert prod["LOCKDIR"] == "/tmp/l"
        assert not _LITERAL_ABS_PATH.search(script.replace(str(tmp_path), ""))
        assert 'CATCHUP_AFTER_HHMM="2400"' in script

    @pytest.mark.parametrize("extra", [
        'echo x > "/Users/u/.claude/state.json"\n',            # 闸前新增的字面路径
        'PROJECT_DIR="/Users/u/other"\n',                       # 第二处赋值
    ])
    def test_sandbox_refuses_before_running(self, tmp_path, extra):
        with pytest.raises(AssertionError, match="拒绝真跑"):
            _sandbox_orchestrator(self._MINI.format(extra=extra), tmp_path)


@pytest.fixture(scope="module")
def orch_text():
    if not os.path.isfile(ORCH):
        pytest.skip("编排器不在本机（仓库外文件）")
    with open(ORCH, encoding="utf-8") as f:
        return f.read()


class TestCatchupGate:
    def test_gate_exists(self, orch_text):
        assert "CATCHUP_AFTER_HHMM" in orch_text, "补跑闸不见了"
        assert "SWARM_MARKER" in orch_text, "幂等标记不见了"

    def test_time_gate_not_before_market_close(self, orch_text):
        """阈值必须在美股收盘（本机 13:00 PT）之后。
        调早到盘中 = 直接制造盘中价污染，比漏跑更糟。"""
        m = re.search(r'CATCHUP_AFTER_HHMM="(\d{4})"', orch_text)
        assert m, "读不到阈值"
        assert int(m.group(1)) >= 1300, (
            f"阈值 {m.group(1)} 早于美股收盘 1300 PT —— 会把盘中价当收盘价")

    def test_gate_runs_before_any_scan_step(self, orch_text):
        """闸必须在 Step 1 之前，否则先抓完数据再退出等于白跑。"""
        gate = orch_text.index("CATCHUP_AFTER_HHMM")
        step1 = orch_text.index("STEP1_START")
        assert gate < step1, "补跑闸在 Step 1 之后，起不到节流作用"

    def test_gate_exits_zero_not_error(self, orch_text):
        """跳过是正常状态，必须 exit 0 —— 非 0 会被编排器/监控当成故障。"""
        seg = orch_text[orch_text.index("CATCHUP_AFTER_HHMM"):
                        orch_text.index("STEP1_START")]
        assert "exit 0" in seg
        assert "exit 1" not in seg

    def test_sandbox_rebind_applies(self, orch_text, tmp_path):
        """`TestGateBranchesLive` 靠改绑把编排器关进沙箱。改绑不上时那组会拒绝真跑
        （安全），但默认套件不跑它 —— 写法变了要在这里先红，否则那组一直
        「跑不起来」而没人发现。"""
        script, prod = _sandbox_orchestrator(orch_text, tmp_path)
        for name in ("LOCKDIR", "PROJECT_DIR", "LOGDIR", "REPORTDIR"):
            assert os.path.isabs(prod[name]), f"{name} 不是绝对路径：{prod[name]!r}"
            assert f'{name}="{tmp_path}' in script, f"{name} 没改绑进脚本"
        assert 'CATCHUP_AFTER_HHMM="2400"' in script

    def test_syntax_valid(self, orch_text):
        # 取 orch_text 只为借它「不在本机就 skip」那一步：同 class 其他用例都有
        # 这层保护，唯独这条漏了，于是在任何没有编排器的环境（CI、云沙箱）
        # 都以 `returncode 127 / No such file` 假报「编排器语法错误」。
        r = subprocess.run(["bash", "-n", ORCH], capture_output=True, text=True)
        assert r.returncode == 0, f"编排器语法错误：{r.stderr}"


class TestPlistRunAtLoad:
    @pytest.fixture(scope="class")
    def plist(self):
        if not os.path.isfile(PLIST):
            pytest.skip("plist 不在本机")
        import plistlib
        with open(PLIST, "rb") as f:
            return plistlib.load(f)

    def test_runatload_enabled(self, plist):
        assert plist.get("RunAtLoad") is True, (
            "RunAtLoad=false —— 关机错过 14:00 的那天将永久漏掉")

    def test_schedule_still_present(self, plist):
        """补跑是**补充**不是替代：定时时刻不能被顺手删掉。"""
        sci = plist.get("StartCalendarInterval") or []
        assert len(sci) >= 5, f"定时时刻少于 5 个工作日：{sci}"
        assert all(e.get("Hour") is not None for e in sci)

    def test_points_at_orchestrator(self, plist):
        args = plist.get("ProgramArguments") or []
        assert any("alpha-hive-orchestrator.sh" in a for a in args)


@pytest.mark.integration
class TestGateBranchesLive:
    """真跑编排器的补跑闸 —— **在沙箱里跑**。标 integration：依赖本机那份仓库外的
    编排器。默认套件跑静态守卫即可，这组用 `pytest -m integration tests/test_scan_catchup.py` 单跑。
    （三条分支都已在 v0.45.34 落地时手工端到端验证过，含真实 RunAtLoad 触发。）

    为什么必须沙箱（v0.45.221）—— 编排器走到闸上 `exit 0` 之前就写了生产，
    此前这条测试原样直接跑 `bash ORCH`，全吃：

    - 测试在**生产目录**造 `.swarm_results_<今天>.json` = `{}`：存在期间触发的真扫描
      会被闸当成「已扫过」跳过；scan_coverage_gate / health_check / dashboard_renderer 读到空结果
    - 闸分支无条件覆盖 `~/.claude/reports/status.json` 为 `skipped_already_scanned`：
      上一轮真实运行的结局被盖掉
    - `log()` 的 tee 往 `~/.claude/logs/orchestrator-<今天>.log` 追加一条「已有扫描产出」的假记录
    - 抢 `/tmp/alpha_hive_orchestrator.lock`：同一刻触发的真扫描以「另一个编排实例正在运行」退出
    - 后台全局看门狗 `( sleep 90000; …; kill -TERM $$; kill -9 $$ ) &` 只在脚本跑到末尾才被
      kill，闸上提前 exit 时**不收**。launchd 下 job 退出会回收整个进程组，所以生产无事；
      测试里没人回收 ⇒ 它活 25h，往那天的生产日志写一条假 ERROR「全局超时」，再对一个
      早已退出、PID 可能已被复用的号 `kill -9`。**实证**：v0.45.34 落地那天
      （08-26 11:35–11:43）验闸的 7 次运行，恰好 90000s 后在 08-27 生产日志里留下
      7 条「全局超时」，逐秒对得上（那些 PID 当时是否已被复用：待验证，已无从查）。
    - marker 放到编排器不读的目录时闸被绕过，过了 13:30 真跑全量扫描（v0.45.219 修过一半）。

    怎么关进去（**不改编排器**）：`_sandbox_orchestrator` 改绑五个赋值写成副本，
    子进程环境照 launchd 现造（见 `_gate_run`）、HOME 指沙箱、`ALPHA_HIVE_PROXY=none`
    （编排器现成开关，免掉探活出网），自成进程组、退出后 `killpg` 收掉看门狗。
    沙箱里没有 `is_trading_day.py`，所以周末/假日也走得到闸（原先那几天会先落进「非交易日」分支）。

    没选的两条路：
    - 给编排器加 env 覆盖 PROJECT_DIR/LOGDIR/REPORTDIR：要改仓库外的生产入口；launchd 下的
      真扫描同样读这些变量，泄漏一个就把真扫描的状态与日志导进临时目录、监控变瞎
      （`_isolate_env` 泄漏进子进程的同一形状，方向反过来）；锁、看门狗、时间闸还得另开旋钮。
    - 备份恢复 status.json：marker、锁、日志、看门狗一个没管；恢复本身就是一次写入，
      真扫描恰好在中间写过的话，被盖掉的是真结果。

    谁会红：
    - 改绑不上 / 闸前冒出新字面路径 ⇒ `_sandbox_orchestrator` 在真跑前拒绝
      （默认套件里 `test_sandbox_rebind_applies` 先红）
    - 闸读的不是沙箱 marker ⇒ 下面两条恒有一条红（放了 marker 却没拦 / 没放却说已扫过）
    - 某处写入漏出沙箱 ⇒ `_traces` 在生产侧找到本次沙箱路径；它先在沙箱自己的产物上自证认得出
    - 看门狗没收掉 ⇒ `_reap_group` 超时；进程组没建起来 ⇒ `lingered` 为假
    """

    def _gate_run(self, orch_text, tmp_path, *, with_marker, lc_ctype=None):
        """沙箱里跑一次编排器。

        ⚠️ 不能用 capture_output=True：看门狗继承 stdout/stderr，管道要等**所有**
        写入端关闭才 EOF（实测直接跑 0 秒，走管道卡满 120s）。改为重定向到文件。

        ⚠️ 环境照 launchd 现造（plist 只给 PATH），**不继承 pytest 的**：
        ① conftest `_isolate_env` 的变量会泄漏进子进程；② Python 按 PEP 538 自己往
        环境里塞 `LC_CTYPE=C.UTF-8`，bash 3.2 在 UTF-8 下把 `$DATE_STR）` 里全角括号的
        首字节吃进变量名，`set -u` 在闸上 exit 1（v0.45.221 实测，v0.45.287 已修编排器，见
        `test_gate_branch_under_utf8_locale`）。⇒ 旧写法 `env=dict(os.environ)` 在没显式设
        C locale 的环境里走不到闸的 `exit 0`（崩在写 status.json 之前，marker/日志/锁/看门狗照吃），
        能绿只可能来自它顺带读的生产日志尾巴里已有当天的「已有扫描产出」。
        （推断：旧版会写生产，未复跑。）
        """
        script, prod = _sandbox_orchestrator(orch_text, tmp_path)
        (tmp_path / "orch.sh").write_text(script, encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir()
        # 编排器闸前只 open 它（存在性 + TCC 预检），从不执行
        (project / "alpha_hive_daily_report.py").write_text(
            'raise SystemExit("沙箱桩：不该被执行")\n', encoding="utf-8")
        (tmp_path / "home").mkdir()
        today = datetime.date.today().isoformat()
        if with_marker:
            (project / f".swarm_results_{today}.json").write_text("{}")

        out_path = tmp_path / "orch.out"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": str(tmp_path / "home"), "ALPHA_HIVE_PROXY": "none"}
        if lc_ctype:
            env["LC_CTYPE"] = lc_ctype
        with open(out_path, "w") as fh:
            proc = subprocess.Popen(["bash", str(tmp_path / "orch.sh")], stdout=fh,
                                    stderr=subprocess.STDOUT, env=env,
                                    start_new_session=True)
            try:
                rc = proc.wait(timeout=120)
            finally:
                lingered = _reap_group(proc.pid)
        status_file = tmp_path / "reports" / "status.json"
        return types.SimpleNamespace(
            rc=rc, out=out_path.read_text(errors="ignore"), prod=prod, today=today,
            lingered=lingered,
            status=(json.loads(status_file.read_text(encoding="utf-8")).get("status")
                    if status_file.is_file() else None))

    @staticmethod
    def _assert_stayed_in_sandbox(r, tmp_path):
        own = {"REPORTDIR": tmp_path / "reports", "LOGDIR": tmp_path / "logs"}
        assert len(_traces(own, str(tmp_path), r.today)) == 2, (
            "痕迹检测器连沙箱自己写的两份产物都认不出 —— 它对生产侧「干净」的判断不作数")
        assert _traces(r.prod, str(tmp_path), r.today) == [], "编排器有写入漏出沙箱，落到了生产"
        assert r.lingered, (
            "编排器退出时进程组里已无活口：要么 start_new_session 没生效（看门狗漏在 pytest 的组里"
            "没人收），要么编排器已改成提前 exit 时自己收看门狗（那就删掉这条与类 docstring 的相应段落）")

    @pytest.mark.timeout(300)
    def test_idempotent_when_today_already_scanned(self, orch_text, tmp_path):
        """今日已有 .swarm_results → 必须跳过。
        否则 RunAtLoad 会让每次登录都重跑一次全量扫描。"""
        r = self._gate_run(orch_text, tmp_path, with_marker=True)
        assert "已有扫描产出" in r.out, f"幂等闸没拦住，会重复全量扫描\n{r.out[-2000:]}"
        assert r.status == "skipped_already_scanned", r.status
        assert r.rc == 0
        self._assert_stayed_in_sandbox(r, tmp_path)

    @pytest.mark.timeout(300)
    def test_without_marker_not_treated_as_scanned(self, orch_text, tmp_path):
        """对照：同一沙箱不放 marker ⇒ 必须**不**走幂等分支，落进（被拉到 2400 的）时间闸。

        没有这条，上一条的绿证明不了闸读的是沙箱里的 marker：编排器将来若改由别的
        变量拼 marker 路径、而生产 marker 今天恰好已在，上一条照样绿。"""
        r = self._gate_run(orch_text, tmp_path, with_marker=False)
        assert "已有扫描产出" not in r.out, "没放 marker 却走了幂等分支 —— 闸读的不是沙箱"
        assert r.status == "skipped_before_close", f"{r.status}\n{r.out[-2000:]}"
        assert r.rc == 0
        self._assert_stayed_in_sandbox(r, tmp_path)

    @pytest.mark.timeout(300)
    def test_gate_branch_under_utf8_locale(self, orch_text, tmp_path):
        """UTF-8 LC_CTYPE 下闸的 `exit 0` 分支必须走得通（v0.45.287 起是回归守卫）。

        此前是 `xfail(strict=True)`：编排器（仓库外）有 18 处 `$VAR` 紧跟全角标点，bash 3.2
        在 UTF-8 LC_CTYPE 下把首字节吃进变量名，`set -u` 当场退出**整个 shell**（不是只挂一条命令）。
        v0.45.287 统一改成 `${VAR}`，本条按 xfail 自己的约定 XPASS 变红后转成普通断言。
        launchd 只给 PATH ⇒ C locale ⇒ 定时/开机触发一直不受影响；中招的是从 UTF-8 终端手工跑、
        或由 Python 拉起（PEP 538）。静态侧的守卫见 `tests/test_orchestrator_braced_vars.py`。
        """
        r = self._gate_run(orch_text, tmp_path, with_marker=True, lc_ctype="C.UTF-8")
        assert "已有扫描产出" in r.out and r.rc == 0, r.out[-500:]
