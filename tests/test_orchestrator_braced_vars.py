"""守卫：编排器里 `$VAR` 后面紧跟非 ASCII 字符，必须写成 `${VAR}`（v0.45.287）

为什么需要这条
--------------
`~/.claude/scripts/alpha-hive-orchestrator.sh`（仓库外、不受版本控制）在 v0.45.287 之前有
18 处形如 `"…（exit=$STEP10_RC），不影响主流程"` 的写法：裸变量后面紧跟全角标点，中间没有 ASCII 边界。
macOS `/bin/bash`（3.2.57）在 UTF-8 的 LC_CTYPE 下，把 UTF-8 **首字节**当成变量名的一部分
（实测：UTF-8 的 51 个首字节 0xC2–0xF4 里，被吃 ⇔ macOS 单字节 `isalpha()` 为真，0 处不一致，
唯一例外 0xD7=`×`（希伯来文 `א` 因此幸免）；`）` `「` `—` emoji 全中），于是变量名成了
`STEP10_RC\\357`，`set -u` 报 unbound variable。**后果取决于裸变量出现在哪：**

- **命令参数里**（那 18 处，全是 `log "…"`）：**整个 shell 当场退出**（rc=127，后面的命令一条都不跑）
  ——不是「只挂一条 log」；
- **未加引号的 heredoc 正文里**（编排器有 5 处 `cat > …status.json << EOFJ`）：shell **不退出**，
  但目标文件被清成 **0 字节**（`set -u` 开，那条 `cat` 的 rc=127 没人查）；`set -u` 关则写出
  丢了值、带坏字节的内容。这比退出**更隐蔽**——所以扫描时 heredoc 正文里以 `#` 开头的行也不跳过
  （bash 照样展开它们）。

只在 UTF-8 locale 下发作：launchd 的 plist 只给 `PATH` ⇒ C locale ⇒ 定时/开机触发一直不受影响；
中招的是从 UTF-8 终端手工跑，或由 Python 拉起（PEP 538 会往子进程环境塞 `LC_CTYPE=C.UTF-8`）。
这也是它藏了这么久的原因：生产路径碰不到。但潜伏点里有无条件路径（第 1495 行的「状态已保存」
每轮必经），谁给 plist 加一行 `LANG=…UTF-8`，编排器就会在每轮末尾退出。

与另一条守卫的分工
------------------
- `tests/test_scan_catchup.py::TestGateBranchesLive::test_gate_branch_under_utf8_locale`：
  **动态**、端到端，真跑编排器（沙箱）走幂等闸的 `exit 0`；但只覆盖闸前那一条路径。
- 本文件：**静态**扫全文，覆盖沙箱走不到的位置（Step 2 之后、收尾段）。

守卫自己也要有牙（CLAUDE.md：检测器两个方向都要自证）
------------------------------------------------------
`TestDetectorHasTeeth` 全是合成文本、零外部依赖，**在任何机器上都跑**；只有「读真编排器」那一类
在文件不在本机时 skip（编排器只存在于装了定时任务的那台 Mac——恰好也是唯一需要它的地方）。
条件性挂在**类/用例**上，不用模块级 `pytestmark`（那会连坐把 teeth 一起跳掉）。
"""

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ORCH = Path(os.path.expanduser("~/.claude/scripts/alpha-hive-orchestrator.sh"))

# 裸变量名（不含 `${…}`、`$1`/`$?`/`$#` 这类特殊参数——实测它们不会吞后面的字节）
# 紧跟任意非 ASCII 字符。故意宽于「实测会中招的那些首字节」：多抓一个 `א` 无害（加花括号总是对的），
# 少抓才会漏。`(?<!\\)` 跳过转义的 `\$VAR`（那是字面量美元符，不展开）。
_BARE_BEFORE_NON_ASCII = re.compile(r"(?<!\\)\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7f])")


# `<<DELIM` / `<<-DELIM` / `<<'DELIM'` / `<<"DELIM"` / `<<\DELIM`。`(?<!<)…(?!<)` 排除 `<<<` here-string。
_HEREDOC_OPEN = re.compile(r"(?<!<)<<(?!<)(-?)\s*(?:'([^']+)'|\"([^\"]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")


def find_unbraced(text: str) -> list[tuple[int, str, str]]:
    """返回 `(行号, 变量名, 该行)`。

    整行注释 bash 不展开，跳过；**heredoc 正文里除外**——未加引号的 heredoc 里以 `#` 开头的行照样被展开，
    所以正文内不跳过注释行。这条规则出错只会退回「不跳」（多报，安全方向），不会多吞：
    找不到终止行的 `<<` 不当 heredoc（免得一次误判让后面全文的注释规则失灵）。
    加了引号的 heredoc（`<<'X'`，bash 不展开）也一并当正文扫——保守地多报，实际 0 例。行内注释照报。
    """
    lines = text.split("\n")
    hits: list[tuple[int, str, str]] = []
    end = None                                   # 正处于 heredoc 正文时：(终止行, 是否 `<<-` 剥制表符)
    for no, line in enumerate(lines, 1):
        if end is not None:
            delim, strip = end
            if (line.lstrip("\t") if strip else line) == delim:
                end = None
                continue
        elif line.lstrip().startswith("#"):
            continue
        hits += [(no, m.group(1), line) for m in _BARE_BEFORE_NON_ASCII.finditer(line)]
        opener = _HEREDOC_OPEN.search(line) if end is None else None
        if opener:
            delim, strip = opener.group(2) or opener.group(3) or opener.group(4), bool(opener.group(1))
            if any((rest.lstrip("\t") if strip else rest) == delim for rest in lines[no:]):
                end = (delim, strip)
    return hits


class TestDetectorHasTeeth:
    """合成文本，任何机器都跑。该抓的抓到、不该抓的不抓，两个方向各喂一次。"""

    @pytest.mark.parametrize("line, name", [
        ('log "WARN" "⚠️ Step 10 异常（exit=$STEP10_RC），不影响主流程"', "STEP10_RC"),
        ('log "WARN" "set_status 收到未知状态「$_new」，按 partial 计"', "_new"),   # 下划线开头 + 「」
        ('log "INFO" "今日（$DATE_STR）已有扫描产出"', "DATE_STR"),
        ('log "INFO" "x=$FOO，y"', "FOO"),                                       # 全角逗号
        ('log "INFO" "status=$FOO—详见末尾"', "FOO"),                             # 破折号
        ('log "INFO" "done $FOO🎉"', "FOO"),                                      # 4 字节首字节（emoji）
        ('log "INFO" "$A: $B："', "B"),                                          # 前一个后面是 ASCII 冒号，不算
    ])
    def test_flags_bare_var_before_non_ascii(self, line, name):
        assert [h[1] for h in find_unbraced(line)] == [name]

    @pytest.mark.parametrize("line", [
        'log "WARN" "⚠️ Step 10 异常（exit=${STEP10_RC}），不影响主流程"',   # 已加花括号：修好后的样子
        'log "INFO" "x=$FOO ）"',                                          # 空格隔开
        'log "INFO" "x=\\"$FOO\\"）"',                                      # 引号隔开
        'log "INFO" "x=$FOO)"',                                            # ASCII 右括号
        'log "INFO" "$1）$?）$#）"',                                        # 特殊参数不吞字节（实测）
        'log "INFO" "$(basename "$F"）"',                                  # 命令替换里的 $F 后是 ASCII 引号
        'log "INFO" "转义的美元符 \\$FOO）是字面量"',                          # \$ 不展开
        '    # log "INFO" "注释里的 $FOO）不会被 bash 展开"',                # 整行注释
        'log "INFO" "${FOO:-默认值}）"',                                    # 花括号里的非 ASCII 无害
    ])
    def test_ignores_safe_forms(self, line):
        assert find_unbraced(line) == []

    def test_reports_line_numbers_and_multiple_hits_per_line(self):
        text = 'a\nlog "x $A（ $B）"\n# $C）\nlog "ok ${D}）"\nlog "y $E，"\n'
        assert [(no, n) for no, n, _ in find_unbraced(text)] == [(2, "A"), (2, "B"), (5, "E")]

    def test_hash_line_inside_unquoted_heredoc_is_expanded_so_it_is_flagged(self):
        text = 'cat > f << EOF\n# 看着像注释，heredoc 正文里 bash 照样展开 $FOO）\nEOF\n'
        assert [(no, n) for no, n, _ in find_unbraced(text)] == [(2, "FOO")]

    def test_comment_skip_resumes_after_the_heredoc_terminator(self):
        text = 'cat > f <<-EOF\n\tbody\n\tEOF\n# $FOO）\n'            # `<<-` 允许制表符缩进的终止行
        assert find_unbraced(text) == []

    @pytest.mark.parametrize("opener", [
        'read -ra A <<< EOFX',        # here-string；没有 `(?<!<)` 会把它当成以 EOFX 收尾的 heredoc
        'n=$((1<<3))',                # 算术左移
        'cat << NEVER_TERMINATED',    # 没有终止行 ⇒ 不当 heredoc，免得误判把后文的注释规则全改掉
    ])
    def test_lookalikes_do_not_switch_off_the_comment_skip(self, opener):
        assert find_unbraced(f'{opener}\n# $FOO）\nEOFX\n') == []


@pytest.fixture(scope="module")
def orch_text():
    if not ORCH.is_file():
        pytest.skip("编排器不在本机（仓库外文件）")
    return ORCH.read_text(encoding="utf-8")


class TestLiveOrchestrator:
    def test_no_bare_var_before_non_ascii(self, orch_text):
        # 先证明读对了文件：空文件/别的脚本会让下面的「零命中」空转成绿
        assert "set -uo pipefail" in orch_text and orch_text.count("\n") > 500, (
            f"{ORCH} 看起来不是编排器（缺 `set -uo pipefail` 或行数异常）")
        hits = find_unbraced(orch_text)
        assert not hits, (
            "编排器有裸 `$VAR` 紧跟非 ASCII 字符：bash 3.2 在 UTF-8 locale 下会把首字节吃进变量名，"
            "`set -u` 让整个 shell 退出（launchd 是 C locale 碰不到，手工跑/PEP 538 会中招）。\n"
            "修法：`$NAME` → `${NAME}`。\n"
            + "\n".join(f"  第 {no} 行 ${{{name}}}: {line.strip()[:100]}" for no, name, line in hits))


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="编排器只跑在 macOS（launchd），误解析是 macOS /bin/bash 3.2 的行为；Linux 上没有这个前提")
class TestMacBashMisparsesBareVar:
    """把「为什么要有这条静态守卫」钉成可执行的事实，免得它变成没人能验证的教条。

    条件性挂在**平台**上（前提 = 「能跑编排器的那类机器」），不挂在「bash 有没有这个 bug」上：
    后者会让 bash 被修好的那天这组测试静默变成 skip。真被修好了应当**红**，提示前提变了。
    """

    @staticmethod
    def _run(body: str, **env):
        return subprocess.run(["/bin/bash", "-c", "set -u\nFOO=99\n" + body],
                              capture_output=True, env={"PATH": "/usr/bin:/bin", **env})

    def test_bare_var_aborts_whole_shell_under_utf8(self):
        r = self._run('echo "exit=$FOO），x"\necho after-ran', LC_CTYPE="C.UTF-8")
        assert r.returncode != 0 and b"unbound variable" in r.stderr and b"after-ran" not in r.stdout, (
            f"/bin/bash 不再复现误解析（rc={r.returncode}）—— 本类失去前提，可删；静态守卫留着无害")

    def test_braced_var_is_fine_under_utf8(self):
        r = self._run('echo "exit=${FOO}），x"\necho after-ran', LC_CTYPE="C.UTF-8")
        assert r.returncode == 0 and r.stdout == "exit=99），x\nafter-ran\n".encode()

    def test_c_locale_is_unaffected_which_is_why_launchd_never_hit_it(self):
        r = self._run('echo "exit=$FOO），x"\necho after-ran', LC_ALL="C")
        assert r.returncode == 0 and r.stdout == "exit=99），x\nafter-ran\n".encode()

    def test_bare_var_in_heredoc_body_empties_the_file_but_shell_continues(self, tmp_path):
        """heredoc 正文里是另一种、更隐蔽的失败形状：不退出，目标文件被清成 0 字节
        （编排器的 `cat > …status.json << EOFJ` 就是这个形状）。这条钉住「为什么扫描要连 heredoc 正文一起扫」。"""
        out = tmp_path / "status.json"
        script = f'cat > {shlex.quote(str(out))} << EOFJ\n{{"v": "$FOO）"}}\nEOFJ\necho "cat-rc=$? shell-continued"\n'
        r = self._run(script, LC_CTYPE="C.UTF-8")
        assert (r.stdout, out.read_bytes()) == (b"cat-rc=127 shell-continued\n", b""), (r.stdout, r.stderr)
        r = self._run(script.replace("$FOO）", "${FOO}）"), LC_CTYPE="C.UTF-8")
        assert (r.stdout, out.read_bytes()) == (b"cat-rc=0 shell-continued\n", '{"v": "99）"}\n'.encode())
