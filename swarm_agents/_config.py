"""Swarm agent 共享配置：logger、正则、评分配置"""

from hive_logger import get_logger
import math
import re as _re

_log = get_logger("swarm")

# 预编译正则表达式（#32 性能优化）
_RE_TICKER = _re.compile(r'^[A-Z]{1,5}$')
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
