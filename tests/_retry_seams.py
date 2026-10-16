"""生产代码里「失败后退避等待」的测试注入接缝登记表（v0.45.524）。

为什么要有它：整套测试里最慢的一批不是在算东西，是在**真睡**——测试让取数立即失败
（`stub_yfinance` 等），生产的重试循环照样按退避节奏等满 0.7+1.4 秒、2+4+8 秒……
每条测试白等，且不随测试条数而随「走到重试的测试条数」增长。

做法：每个模块一个**窄**接缝 `_retry_sleep(seconds)`（生产 = 调用时查找的 `time.sleep`，
行为不变），测试只替换**点名**的接缝，不碰全局 `time.sleep`——有些测试靠真睡验证超时 /
截止时间 / mtime 时间线（`test_root_data_guard` 的 `_SETTLE`、clockshift、
`test_ic_rerun_fg_budget`、`test_ghpages_data_root_migration`），全局一刀会把它们变成空测。

⚠️ 接缝本身是风险点：哪天有人把某个 `_retry_sleep` 改成什么都不做，生产限流 / 退避就
静默没了，而所有测试照样绿（它们本来就替换掉了它）。所以
`tests/test_retry_backoff_seams.py` 对这张表里**每一项**在生产配置下钉两件事：
① 接缝真的调用 `time.sleep`、秒数原样传过去；② 重试循环按原节奏调用接缝。
"""
from __future__ import annotations

#: `stub_yfinance` 让 yfinance 立即失败后，会走到退避重试的模块。
#: `tests/conftest.py::stub_yfinance` 把这些模块的 `_retry_sleep` 换成记录器。
YF_RETRY_SLEEP_SEAMS = ("options_analyzer", "market_intelligence")

#: 全部接缝（含不随 `stub_yfinance` 自动替换、由各自测试点名替换的那些）。
ALL_RETRY_SLEEP_SEAMS = YF_RETRY_SLEEP_SEAMS + ("report_deployer",)
