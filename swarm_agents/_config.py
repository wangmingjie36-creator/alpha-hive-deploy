"""Swarm agent 共享配置：logger、正则、评分配置"""

from hive_logger import get_logger
import math
import re as _re

_log = get_logger("swarm")

# 预编译正则表达式（#32 性能优化）
# v0.45.2: 原 `^[A-Z]{1,5}$` 拒绝类份额后缀（BRK-B / BRK.B / BF-B）。
# 后果不是报错而是静默中性化：BRK-B 是每日扫描标的（v0.45.6 前在
# WATCHLIST_EXTENDED，现已并入 WATCHLIST），8 只蜂有 7 只
# 走 _validate_ticker 提前返回 score=5.0/confidence=0.0，日报照常列出
# "BRK-B NEUTRAL 5.0"，看不出它从未被分析过。
_RE_TICKER = _re.compile(r'^[A-Z]{1,5}(?:[.-][A-Z])?$')
_RE_INSIDER_SELL = _re.compile(r'内幕卖出\s*\$?([\d,]+)')
_RE_INSIDER_BUY = _re.compile(r'内幕买入\s*\$?([\d,]+)')
_RE_PC_RATIO = _re.compile(r'P/C[:\s]*Ratio[:\s]*([\d.]+)')
_RE_PC_SHORT = _re.compile(r'P/C[:\s]*([\d.]+)')
_RE_IV_RANK = _re.compile(r'IV[:\s]*(?:Rank)?[:\s]*([\d.]+)')
_RE_SENTIMENT = _re.compile(r'情绪\s*(\d+)%')

# Agent 评分配置（从 config.py 读取，消除 magic numbers）
try:
    from config import AGENT_SCORING as _AS
except ImportError:
    _AS = {}
