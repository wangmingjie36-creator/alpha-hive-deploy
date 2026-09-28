"""编排器静态检查——裸 `$VAR` 紧跟非 ASCII 字符（v0.45.356 从测试文件抽出，成为唯一实现）。

为什么：macOS `/bin/bash` 3.2 在 UTF-8 locale 下把非 ASCII 字符的 UTF-8 首字节吃进变量名，
`set -u` 让整个 shell 退出（命令参数里）或把 heredoc 目标文件清成 0 字节。完整取证与
「为什么要连 heredoc 正文一起扫」见 `tests/test_orchestrator_braced_vars.py` 模块 docstring。

使用者：
* `tests/test_orchestrator_braced_vars.py`——对仓库副本与合成文本断言；
* `deploy_orchestrator.py`——部署前关卡，命中即拒绝部署（v0.45.356）。
"""

import re

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
