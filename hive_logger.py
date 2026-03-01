#!/usr/bin/env python3
"""
Alpha Hive 统一日志 + 路径管理 + 结构化 JSON 日志

用法:
    from hive_logger import logger, PATHS, set_correlation_id
    set_correlation_id("scan_20260225_abc123")
    logger.info("蜂群启动")
    db_path = PATHS.db

结构化日志输出到 alpha_hive_structured.jsonl（每行一条 JSON）。
"""

import json
import logging
import os
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler


# ==================== Correlation ID (线程本地) ====================

_correlation = threading.local()


def set_correlation_id(cid: str = None):
    """设置当前线程的 correlation_id（用于跨模块追踪同一次扫描）"""
    _correlation.id = cid or uuid.uuid4().hex[:12]


def get_correlation_id() -> str:
    """获取当前线程的 correlation_id"""
    return getattr(_correlation, "id", "no_corr")


# ==================== 路径管理 ====================

class _HivePaths:
    """集中管理所有路径，从环境变量读取，带默认值"""

    @property
    def home(self) -> Path:
        return Path(os.environ.get("ALPHA_HIVE_HOME", os.path.dirname(os.path.abspath(__file__))))

    @property
    def logs_dir(self) -> Path:
        p = Path(os.environ.get("ALPHA_HIVE_LOGS_DIR", str(self.home / "logs")))
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cache_dir(self) -> Path:
        p = Path(os.environ.get("ALPHA_HIVE_CACHE_DIR", str(self.home / "cache")))
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db(self) -> str:
        return os.environ.get("ALPHA_HIVE_DB_PATH", str(self.home / "pheromone.db"))

    @property
    def chroma_db(self) -> str:
        return os.environ.get("ALPHA_HIVE_CHROMA_PATH", str(self.home / "chroma_db"))

    @property
    def sandbox_dir(self) -> Path:
        return Path(os.environ.get("ALPHA_HIVE_SANDBOX_DIR", "/tmp/alpha_hive_sandbox"))

    @property
    def google_credentials(self) -> str:
        return os.environ.get(
            "ALPHA_HIVE_GOOGLE_CREDENTIALS",
            os.path.expanduser("~/.alpha_hive_gmail_credentials.json")
        )

    @property
    def calendar_token(self) -> str:
        return os.environ.get(
            "ALPHA_HIVE_CALENDAR_TOKEN",
            os.path.expanduser("~/.alpha_hive_calendar_token.json")
        )


PATHS = _HivePaths()


# ==================== 结构化 JSON Formatter ====================

class JSONFormatter(logging.Formatter):
    """输出 JSON Lines 格式日志（用于文件持久化，便于机器解析）"""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "corr_id": get_correlation_id(),
        }
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


# ==================== 日志配置 ====================

def _setup_logger() -> logging.Logger:
    """配置全局 logger：控制台（人类可读） + 文件旋转（人类可读） + JSON 文件（机器可读）"""
    log = logging.getLogger("alpha_hive")

    if log.handlers:
        return log

    level_name = os.environ.get("ALPHA_HIVE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log.setLevel(level)

    # 格式：时间 | 级别 | 模块 | correlation_id | 消息
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S"
    )

    # 控制台输出（INFO+）
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(fmt)
    log.addHandler(console)

    # 文件输出（旋转，5MB x 3，人类可读）
    try:
        log_file = PATHS.logs_dir / "alpha_hive.log"
        fh = RotatingFileHandler(
            str(log_file), maxBytes=5 * 1024 * 1024, backupCount=3,
            encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as _fh_err:
        logging.getLogger(__name__).debug("Cannot create rotating file handler: %s", _fh_err)

    # JSON Lines 文件输出（结构化，2MB x 5，机器可读）
    try:
        json_file = PATHS.logs_dir / "alpha_hive_structured.jsonl"
        jh = RotatingFileHandler(
            str(json_file), maxBytes=2 * 1024 * 1024, backupCount=5,
            encoding="utf-8"
        )
        jh.setLevel(logging.DEBUG)
        jh.setFormatter(JSONFormatter())
        log.addHandler(jh)
    except OSError as _jh_err:
        logging.getLogger(__name__).debug("Cannot create JSON file handler: %s", _jh_err)

    return log


logger = _setup_logger()


def get_logger(name: str) -> logging.Logger:
    """获取子 logger（自动继承全局配置）"""
    return logging.getLogger(f"alpha_hive.{name}")


class SafeJSONEncoder(json.JSONEncoder):
    """JSON 编码器：安全处理 NaN / Inf / datetime / set / bytes 等不可序列化类型"""

    def default(self, o):
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, set):
            return sorted(o)
        if isinstance(o, bytes):
            return o.decode("utf-8", errors="replace")
        if isinstance(o, Path):
            return str(o)
        # numpy 标量 → Python 原生类型
        try:
            import numpy as _np
            if isinstance(o, (_np.integer,)):
                return int(o)
            if isinstance(o, (_np.floating,)):
                v = float(o)
                import math as _m
                if _m.isnan(v):
                    return None
                if _m.isinf(v):
                    return "Inf" if v > 0 else "-Inf"
                return v
            if isinstance(o, _np.bool_):
                return bool(o)
            if isinstance(o, _np.ndarray):
                return o.tolist()
        except ImportError:
            pass
        # pandas 类型 → Python 原生类型
        try:
            import pandas as _pd
            if isinstance(o, _pd.Timestamp):
                return o.isoformat()
            if isinstance(o, (_pd.Series, _pd.DataFrame)):
                return o.to_dict()
            if isinstance(o, _pd.Categorical):
                return o.tolist()
        except ImportError:
            pass
        try:
            return super().default(o)
        except TypeError:
            return str(o)

    def encode(self, o):
        return super().encode(self._sanitize(o))

    def _sanitize(self, obj):
        """递归清洗数据：NaN → None, Inf → 'Inf'，numpy/pandas → 原生类型"""
        import math as _m
        if isinstance(obj, float):
            if _m.isnan(obj):
                return None
            if _m.isinf(obj):
                return "Inf" if obj > 0 else "-Inf"
            return obj
        # numpy 标量在 _sanitize 阶段提前转换
        try:
            import numpy as _np
            if isinstance(obj, _np.floating):
                v = float(obj)
                return self._sanitize(v)
            if isinstance(obj, _np.integer):
                return int(obj)
            if isinstance(obj, _np.bool_):
                return bool(obj)
            if isinstance(obj, _np.ndarray):
                return [self._sanitize(v) for v in obj.tolist()]
        except ImportError:
            pass
        if isinstance(obj, dict):
            return {k: self._sanitize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._sanitize(v) for v in obj]
        return obj


# ==================== 可选模块注册表 ====================

class FeatureRegistry:
    """轻量级注册表：跟踪可选模块的加载状态，启动时一次性汇报"""

    _features: dict = {}

    @classmethod
    def register(cls, name: str, available: bool, reason: str = ""):
        """注册一个可选模块的加载状态"""
        cls._features[name] = {"available": available, "reason": reason}

    @classmethod
    def summary(cls) -> dict:
        """返回全部模块状态"""
        return dict(cls._features)

    @classmethod
    def log_status(cls):
        """一次性打印所有降级模块（仅 WARNING 级别）"""
        degraded = {k: v for k, v in cls._features.items() if not v["available"]}
        if degraded:
            log = logging.getLogger("alpha_hive.features")
            names = ", ".join(sorted(degraded.keys()))
            log.warning("[FeatureRegistry] %d 个可选模块未加载: %s", len(degraded), names)
            for name, info in sorted(degraded.items()):
                log.info("  ↳ %s: %s", name, info.get("reason", "ImportError"))
        return degraded


def safe_json_dumps(data, **kwargs) -> str:
    """json.dumps 的安全版本，自动处理 NaN/Inf/datetime 等"""
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("cls", SafeJSONEncoder)
    return json.dumps(data, **kwargs)


def atomic_json_write(path, data, **kwargs):
    """Atomically write JSON to *path* (write-to-tmp + os.replace).
    自动使用 SafeJSONEncoder 防止 NaN/Inf 序列化错误。
    """
    import tempfile
    path = Path(path)
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("cls", SafeJSONEncoder)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(path.parent), suffix=".tmp", delete=False
        ) as tmp:
            json.dump(data, tmp, **kwargs)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, str(path))
    except OSError:
        # Clean up temp file on failure
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def read_json_cache(path, ttl: int = 300):
    """Read JSON cache from *path* if it exists and is younger than *ttl* seconds.

    Returns the parsed data on cache hit, or ``None`` on miss / expired / corrupt.
    Paired with :func:`atomic_json_write` for a complete cache read/write cycle.
    """
    import time as _t
    path = Path(path)
    if not path.exists():
        return None
    try:
        age = _t.time() - path.stat().st_mtime
        if age >= ttl:
            return None
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return None
