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

import importlib
import json
import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from logging.handlers import RotatingFileHandler


# ==================== Correlation ID (线程本地) ====================

_correlation = threading.local()


def set_correlation_id(cid: str = None):
    """设置当前线程的 correlation_id（用于跨模块追踪同一次扫描）"""
    if cid is not None and not isinstance(cid, str):
        cid = str(cid)
    _correlation.id = cid or uuid.uuid4().hex[:12]


def get_correlation_id() -> str:
    """获取当前线程的 correlation_id"""
    return getattr(_correlation, "id", "no_corr")


def reset_correlation_id():
    """扫描结束后清理 correlation_id，防止线程复用时残留"""
    if hasattr(_correlation, "id"):
        del _correlation.id


# ==================== 路径管理 ====================

class _HivePaths:
    """集中管理所有路径，从环境变量读取，带默认值"""

    @property
    def home(self) -> Path:
        return Path(os.environ.get("ALPHA_HIVE_HOME", os.path.dirname(os.path.abspath(__file__))))

    @property
    def git_repo_root(self) -> Path:
        """`alpha-hive-deploy` 代码仓库根——git plumbing（gh-pages / main 提交推送）必须在这里跑。

        数据根迁移阶段 4：与 `home`（数据根）是**两个不同概念**，此前
        `agent_toolbox.GitHubTool.__init__` 把两者叠成同一个变量（`ALPHA_HIVE_HOME`
        优先、`__file__` 兜底）——今天两者恰好同目录，掩盖了分歧。阶段 5 把
        `ALPHA_HIVE_HOME` 改指 `~/alpha-hive-data` 后，代码仓库**并不搬家**，
        仍待在原检出位置；若 git plumbing 继续跟 `ALPHA_HIVE_HOME` 走，
        `git commit`/`push`/gh-pages 的全部 git 命令都会在一个没有 `.git` 的
        数据目录里执行，报告提交、main 推送、gh-pages 部署会一起失效。

        因此这里**故意不读 `ALPHA_HIVE_HOME`**，改读专用的 `ALPHA_HIVE_GIT_REPO`
        （仅测试用于把 git plumbing 指向沙箱假仓库，生产从不设它）。兜底
        `__file__` 派生（本文件所在目录）在阶段 5 前后都是对的：代码仓库
        的位置从未改变，改变的只是数据去哪儿找。
        """
        return Path(os.environ.get("ALPHA_HIVE_GIT_REPO", os.path.dirname(os.path.abspath(__file__))))

    @property
    def logs_dir(self) -> Path:
        p = self.logs_dir_unmade()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def logs_dir_unmade(self) -> Path:
        """同 `logs_dir`，但**不建目录**（v0.45.239）。

        给文件日志 handler 每条记录求值用：它只需要知道「现在该写到哪」，
        目录由它真要打开文件时再建。`logs_dir` 的解析逻辑只在这里写一份。
        """
        return Path(os.environ.get("ALPHA_HIVE_LOGS_DIR", str(self.home / "logs")))

    @property
    def cache_dir(self) -> Path:
        p = Path(os.environ.get("ALPHA_HIVE_CACHE_DIR", str(self.home / "cache")))
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cboe_daily_cache(self) -> Path:
        """`CBOEDailyFetcher()` 无参构造时的缓存目录（v0.45.230）。

        此前默认值是 cwd 相对的 `"cache/cboe_daily"`：不读 `ALPHA_HIVE_CACHE_DIR`，
        在哪起 pytest 就建进哪个 checkout（v0.45.224 空目录普查实测）。
        生产从仓库根跑、无 env 覆盖时与原位置相同。`cboe_daily/` 本身由构造器建
        （父目录照 `cache_dir` 的惯例在取值时建）。
        """
        return self.cache_dir / "cboe_daily"

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
    def production_sync(self) -> Path:
        """扫描前生产 checkout 快进结果（v0.45.214，`production_sync.write_result`）。

        `scan_timing.snapshot` 按日期读它并入 `scan_timing.json` ⇒ 编排器 `write_status`
        并进 `status.json` ⇒ `alert_manager` 据此告警。
        """
        return self.logs_dir / "production_sync.json"

    # ── ML 模型产物（v0.45.149）─────────────────────────────────────────
    # 三个文件名此前散落在 `ml_predictor` 的六个函数签名默认值、
    # `generate_ml_report` 的一个**类属性**、以及三处字面量里。收进这里的
    # 理由不只是整洁：`_HivePaths` 的属性是**调用时求值**的，天然读得到
    # 测试逐条 setenv 的 `ALPHA_HIVE_HOME`；写成模块级常量或类属性就会在
    # import 那一刻冻住（v0.45.149 实测：`MLEnhancedReportGenerator._model_file`
    # 在 pytest 收集期冻成仓库根，隔离对它完全无效）。

    @property
    def ml_model(self) -> Path:
        """`ml_predictor.save_model()` 无参调用时的落盘位置。

        ⚠️ 生产**不读**这个文件（全仓 `load_model` 无参调用点 = 0），它只是
        `MLPredictionService.train_model()` 的副产物。此前默认值是 cwd 相对
        路径 `"ml_model.json"`，于是「在哪跑 pytest 就写到哪」。
        """
        return self.home / "ml_model.json"

    @property
    def ml_model_cache(self) -> Path:
        """生产**真正读**的那份 ML 模型。

        三个消费点都显式指它：`alpha_hive_daily_report`（两处）、
        `generate_ml_report._model_file`、`queen_distiller._ml_oos_trust_factor`。

        ⚠️ 必须与 `ml_model` 保持为**不同文件**。`train_model()` 会无参保存到
        `ml_model`，而 `tests/` 里有 11 处无参调它 —— 把两者合并成同一个路径
        （「统一到单一真相」最容易踩的那种统一法）等于让每次跑测试都直接
        写穿生产模型。
        """
        return self.home / "ml_model_cache.json"

    @property
    def ml_model_extended(self) -> Path:
        """`ml_predictor_extended.SimpleMLModel` 的落盘位置（降级实现专用）。"""
        return self.home / "ml_model_extended.json"

    @property
    def google_credentials(self) -> str:
        return os.environ.get(
            "ALPHA_HIVE_GOOGLE_CREDENTIALS",
            os.path.expanduser("~/.alpha_hive_gmail_credentials.json")
        )


PATHS = _HivePaths()


# ==================== 结构化 JSON Formatter ====================

class JSONFormatter(logging.Formatter):
    """输出 JSON Lines 格式日志（用于文件持久化，便于机器解析）"""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "corr_id": get_correlation_id(),
        }
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


# ==================== 日志配置 ====================

class LogsDirRotatingFileHandler(RotatingFileHandler):
    """文件名固定、**目录每条记录按 `PATHS` 求值**的旋转 handler（v0.45.239）。

    为什么不用裸 `RotatingFileHandler`：它在 `__init__` 里把 `baseFilename`
    存死，而 `logger = _setup_logger()` 在 import 时执行 ⇒ 路径冻在 import 那一刻。
    pytest **收集期**就 import 本模块，早于 `conftest._isolate_env` 的 setenv ⇒
    全套测试日志（含夹具造的 ERROR）写进本 checkout 的 `logs/alpha_hive.log`；
    在主 checkout 起 pytest 就混进生产日志（2026-09-14 实测：32 条测试写入 1.5 KB，
    含合成标的 `[AAA] 政体层保零违反` 与一条假的「权重不变式违反」ERROR）。
    `PATHS.logs_dir` 本身是调用时求值的——冻住它的是**持有路径的对象**，
    所以 `test_paths_not_frozen_at_import` 的 AST 扫描看不见（模块级只有一个函数调用）。

    生产语义不变：扫描进程里 env 不会中途变，目标恒等于 `<ALPHA_HIVE_HOME>/logs/<leaf>`，
    从不重指。唯一可见差异：import 不再建 `logs/` 与空日志文件（第一条记录时才建）。

    打不开文件（目录不可写等）不再在 import 时被 `except OSError` 吞成 debug——
    落到 emit 里，由 `Handler.handleError` 打到 stderr（编排器日志里看得见）。
    """

    def __init__(self, leaf: str, **kwargs):
        self._leaf = leaf
        kwargs["delay"] = True
        super().__init__(str(self.current_target()), **kwargs)

    def current_target(self) -> Path:
        """此刻这条 handler 该写的文件（不建目录、无副作用）。"""
        return PATHS.logs_dir_unmade() / self._leaf

    def emit(self, record: logging.LogRecord) -> None:
        # `Handler.handle` 已持锁调用 emit ⇒ 重指与写入不会与其他线程交错。
        try:
            target = os.path.abspath(self.current_target())
            if target != self.baseFilename:
                if self.stream is not None:
                    self.stream.close()
                    self.stream = None
                self.baseFilename = target
        except Exception:
            self.handleError(record)
            return
        super().emit(record)

    def _open(self):
        os.makedirs(os.path.dirname(self.baseFilename), exist_ok=True)
        return super()._open()


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
    # 路径在 emit 时求值、延迟打开 ⇒ 构造不碰文件系统，原先包着的 `except OSError` 已不可达。
    fh = LogsDirRotatingFileHandler(
        "alpha_hive.log", maxBytes=5 * 1024 * 1024, backupCount=3,
        encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    # JSON Lines 文件输出（结构化，2MB x 5，机器可读）
    jh = LogsDirRotatingFileHandler(
        "alpha_hive_structured.jsonl", maxBytes=2 * 1024 * 1024, backupCount=5,
        encoding="utf-8"
    )
    jh.setLevel(logging.DEBUG)
    jh.setFormatter(JSONFormatter())
    log.addHandler(jh)

    return log


logger = _setup_logger()


def get_logger(name: str) -> logging.Logger:
    """获取子 logger（自动继承全局配置）"""
    return logging.getLogger(f"alpha_hive.{name}")


class SafeJSONEncoder(json.JSONEncoder):
    """JSON 编码器：安全处理 NaN / Inf / datetime / set / bytes / numpy / pandas / Decimal / Enum 等"""

    def default(self, o):
        # ── 时间类 ──
        if isinstance(o, datetime):
            return o.isoformat()
        try:
            from datetime import date as _date, time as _time, timedelta as _td
            if isinstance(o, _date):  # date 不是 datetime 的子类
                return o.isoformat()
            if isinstance(o, _time):
                return o.isoformat()
            if isinstance(o, _td):
                return o.total_seconds()
        except ImportError:
            pass

        # ── 容器/二进制 ──
        if isinstance(o, set):
            return sorted(o)
        if isinstance(o, frozenset):
            return sorted(o)
        if isinstance(o, (bytes, bytearray)):
            return o.decode("utf-8", errors="replace")
        if isinstance(o, Path):
            return str(o)

        # ── Decimal / complex / Enum / UUID ──
        try:
            from decimal import Decimal as _Decimal
            if isinstance(o, _Decimal):
                return float(o)
        except ImportError:
            pass
        if isinstance(o, complex):
            return [o.real, o.imag]
        try:
            from enum import Enum as _Enum
            if isinstance(o, _Enum):
                return o.value
        except ImportError:
            pass
        try:
            from uuid import UUID as _UUID
            if isinstance(o, _UUID):
                return str(o)
        except ImportError:
            pass

        # ── numpy 标量 → Python 原生类型 ──
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
            if hasattr(_np, "datetime64") and isinstance(o, _np.datetime64):
                return str(o)
        except ImportError:
            pass

        # ── pandas 类型 ──
        try:
            import pandas as _pd
            if isinstance(o, _pd.Timestamp):
                return o.isoformat()
            if isinstance(o, _pd.Timedelta):
                return o.total_seconds()
            if isinstance(o, (_pd.Series, _pd.DataFrame)):
                return o.to_dict()
            if isinstance(o, _pd.Categorical):
                return o.tolist()
            if isinstance(o, _pd.Index):
                return o.tolist()
        except ImportError:
            pass

        # ── dataclass ──
        try:
            import dataclasses as _dc
            if _dc.is_dataclass(o) and not isinstance(o, type):
                return _dc.asdict(o)
        except ImportError:
            pass

        # ── 自定义对象（有 to_dict / __dict__）──
        if hasattr(o, "to_dict") and callable(o.to_dict):
            try:
                return o.to_dict()
            except Exception:
                pass

        # ── 兜底：转字符串并记录类型（避免静默失败）──
        try:
            return super().default(o)
        except TypeError:
            _typename = type(o).__name__
            try:
                logging.getLogger("alpha_hive.json_encoder").warning(
                    "SafeJSONEncoder 兜底转字符串：未知类型 %s", _typename
                )
            except Exception:
                pass
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
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(path.parent), suffix=".tmp", delete=False
        ) as tmp:
            tmp_path = tmp.name
            json.dump(data, tmp, **kwargs)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, str(path))
    except OSError:
        # Clean up temp file on failure
        if tmp_path:
            try:
                os.unlink(tmp_path)
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


def optional_import(module: str, attr: str = None, *, default=None):
    """尝试导入 *module*（可选 getattr *attr*），ImportError 时返回 *default*。

    用于可选依赖的优雅降级，减少 try/except ImportError 样板代码。

    Usage::

        MetricsCollector = optional_import("metrics_collector", "MetricsCollector")
        # MetricsCollector 为类或 None
    """
    try:
        mod = importlib.import_module(module)
        if attr is not None:
            return getattr(mod, attr, default)
        return mod
    except ImportError:
        return default


# ============================================================
# v0.28.0 — 全局 PDT 日期工具（美股交易日对齐）
# ============================================================
# 使用场景：所有写入存储 / 查询参数 / 标识符的 date 字段应该用此函数，
# 避免用户电脑时区为 CST/北京时跨午夜（本地次日，PDT 仍当日）date 偏移 1 天
#
# 历史：v0.27.3 / v0.27.4 在 alpha_hive_daily_report / backtester / pheromone_board
# 各自加了本地 _pdt_today helper；v0.28.0 统一抽到 hive_logger 全局共享
try:
    from datetime import datetime as _datetime_pdt
    from zoneinfo import ZoneInfo as _ZI_pdt
    _PDT_TZ = _ZI_pdt("America/Los_Angeles")
    def pdt_today() -> str:
        """返回美股交易日（PDT/PST 时区的日期字符串 YYYY-MM-DD）。

        Example:
            >>> from hive_logger import pdt_today
            >>> entry["date"] = pdt_today()  # 而非 datetime.now().strftime(...)
        """
        return _datetime_pdt.now(_PDT_TZ).strftime("%Y-%m-%d")
except Exception:
    # zoneinfo / tzdata 不可用时回退本地（保留旧行为，向后兼容）
    from datetime import datetime as _datetime_pdt
    def pdt_today() -> str:
        return _datetime_pdt.now().strftime("%Y-%m-%d")
