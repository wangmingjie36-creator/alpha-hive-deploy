"""
🐝 Alpha Hive - ML 增强报告生成
将机器学习预测集成到高级分析报告
"""

import atexit
import html as _html
import json
import math
import sqlite3
from typing import Optional
import argparse
from datetime import datetime
from pathlib import Path
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from advanced_analyzer import AdvancedAnalyzer
from ml_predictor import (
    MLPredictionService,
    TrainingData,
    # v0.45.142：类型闸的单一真相搬到 ml_predictor，与训练侧共用一个谓词
    usable_dim as _usable_dim,
)
import config
from config import WATCHLIST
from hive_logger import PATHS, get_logger, pdt_today

_log = get_logger("ml_report")


def _html_escape(v) -> str:
    """转义进入 HTML 的动态文本（多为上游失败原因，可能含 < > &）。"""
    return _html.escape(str(v), quote=True)


# 排版化信号标记：替代 emoji（⚠️/✅/🟢/🔴）。emoji 在报告里既不可着色、
# 也不随明暗主题变化，且是"AI 生成页面"最明显的指纹之一。
DOT_BULL = '<span style="color:var(--bull)">●</span> '
DOT_BEAR = '<span style="color:var(--bear)">●</span> '
DOT_NEUT = '<span style="color:var(--neut)">●</span> '
# 缺数占位：与 IV 小节的 .na 同形，页面上「没取到」与「值是 0」必须可区分
_NA_SPAN = '<span class="na" title="数据不可用">—</span>'


def _pdt_now():
    """美西（PDT/PST）当前时间，返回 aware datetime。

    用户在中国、Mac 系统时钟比美西快约 15h，本机 datetime.now() 会把美股交易日
    整体 +1 漂移（例：周四收盘后跑，本机已是周五 → 报告被错标为次日，甚至撞上
    Juneteenth 这类休市日，生成「幽灵报告」）。报告日期一律以交易所时区为准。
    tzdata 不可用时回退本地，保持向后兼容。
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        return datetime.now()


class MLEnhancedReportGenerator:
    """ML 增强的报告生成器"""

    # ⭐ Task 2: 全局模型缓存（类级别，跨实例共享 + 磁盘持久化）
    _model_cache = {}          # 内存缓存（同一进程内）
    _cache_date = None         # 缓存日期
    _training_lock = Lock()    # 防止并发重复训练
    # ⚠️ v0.45.149：这里**不能**是类属性。它曾是
    #     `_model_file = PATHS.home / "ml_model_cache.json"`
    # —— 类体在 import 那一刻求值一次就冻住，而 `tests/` 里有 7 个模块在**模块级**
    # import 本类，pytest 收集期跑在任何 fixture 之前（`ALPHA_HIVE_HOME` 尚未设），
    # 于是整个 session 冻成**仓库根**，`_isolate_env` 的沙箱对它完全无效
    # ⇒ 跑一次全套测试就把生产真正读的那份模型换成了测试夹具模型。
    # property 是调用时求值，写不出这个 bug。

    # ⭐ Task 3: 异步 HTML 生成（后台文件写入）
    _file_writer_pool = None   # 异步文件写入线程池
    _writer_lock = Lock()      # 文件写入锁（防止并发冲突）

    # 回退预加载的样本量下限。**有意**低于 `ML_TRAINING_CONFIG.min_real_samples`
    # （30）：`MLPredictionService.train_model()` 自己会用 30 作闸直读 DB，闸过了
    # 就用它的结果、这里预载的数据被整包丢弃；只有闸不过（样本稀少）时才轮得到
    # `historical_records`。两处都用 30 的话，这条回退永远够不着。
    MIN_REAL_SAMPLES = 10

    def __init__(self):
        self.analyzer = AdvancedAnalyzer()
        self.ml_service = MLPredictionService()
        self.timestamp = _pdt_now()  # 美西时区，避免本机 +15h 漂移导致报告日期错标
        self._training_data_source = "unknown"  # "real" | "sample" | "unknown"

        # ⭐ Task 3: 初始化异步文件写入线程池（全局单例）
        if MLEnhancedReportGenerator._file_writer_pool is None:
            MLEnhancedReportGenerator._file_writer_pool = ThreadPoolExecutor(max_workers=3)
            atexit.register(MLEnhancedReportGenerator._file_writer_pool.shutdown, wait=True)

        # ⭐ Task 2: 智能缓存策略（内存 + 磁盘）
        today = pdt_today()

        # 策略 1：检查内存缓存（同一进程内的快速复用）
        if today in self._model_cache:
            _log.info("复用内存缓存 ML 模型（无需重新训练）")
            self.ml_service.model = self._model_cache[today]

        # 策略 2：检查磁盘缓存（跨进程的缓存）
        elif self._check_disk_cache(today):
            _log.info("复用磁盘缓存 ML 模型（昨日已训练）")
            self._load_model_from_disk()
            # 同时更新内存缓存
            self._model_cache[today] = self.ml_service.model
            self._cache_date = today

        # 策略 3：需要训练
        else:
            with self._training_lock:
                # 双重检查（防止并发重复训练）
                if today not in self._model_cache and not self._check_disk_cache(today):
                    _log.info("初始化 ML 模型（首次训练）...")
                    real_data = self._build_real_training_data()
                    if len(real_data) >= self.MIN_REAL_SAMPLES:
                        _log.info("✅ [ML-REAL] 使用 %d 条真实验证数据训练 ML 模型", len(real_data))
                        self._training_data_source = "real"
                        self.ml_service.data_builder.historical_records = real_data
                    else:
                        if real_data:
                            _log.warning(
                                "⚠️ [ML-MIXED] 真实数据仅 %d 条（不足 %d），"
                                "回退到硬编码样本训练，预测置信度受限",
                                len(real_data), self.MIN_REAL_SAMPLES,
                            )
                        else:
                            _log.warning(
                                "⚠️ [ML-SAMPLE] 无真实验证数据，使用硬编码样本训练，"
                                "预测结果仅供参考（请积累 %d+ 条 T+7 验证记录后重训）",
                                self.MIN_REAL_SAMPLES,
                            )
                        self._training_data_source = "sample"
                    self.ml_service.train_model()
                    # 缓存到内存
                    self._model_cache[today] = self.ml_service.model
                    self._cache_date = today
                    # 缓存到磁盘（供后续进程使用）
                    self._save_model_to_disk()
                else:
                    # 另一个线程已经训练，从缓存中恢复
                    if today in self._model_cache:
                        self.ml_service.model = self._model_cache[today]
                    else:
                        self._load_model_from_disk()
                        self._model_cache[today] = self.ml_service.model

    def _build_real_training_data(self) -> list:
        """从 pheromone.db 读取真实验证数据构建训练集（T+7 已验证）。

        v0.45.142：**委托** `ml_predictor.build_training_data_from_db`，不再
        自己拼 SQL 与特征映射。此前两条路径并存且口径长期相反——

        | 项 | 本函数（旧） | `build_training_data_from_db` |
        |---|---|---|
        | 维度缺失 | `ds.get("signal", 5.0)` 补 5.0 | v0.45.50 起剔除 |
        | `ambiguous_t7=1` | 收下 | v0.45.9 P0 起排除（标签无意义） |
        | `return_t7 IS NULL` | `or 0` → 0.0 收益 | 排除 |
        | `momentum_5d` | 写死 `0.0` | 由 signal/sentiment 派生 |
        | `iv_rank`/`put_call_ratio` | 只判 None，哨兵值原样收 | 哨兵值改由 odds 派生 |

        真库实测（2026-09-07，旧口径 `checked_t7=1 LIMIT 200` 的 200 行）：
        2 行 `dimension_scores` 为空字典（五维全补 5.0 → 整行自洽的假样本）、
        26 行 `ambiguous_t7=1`；产出的 11 个特征里 **3 个 sd=0**
        （`momentum_5d` / `iv_rank` / `put_call_ratio`），树模型无法在常数列上分裂。

        合并后两条路径唯一的区别是样本量下限（见 `MIN_REAL_SAMPLES`）。
        """
        try:
            from backtester import PredictionStore
            from ml_predictor import build_training_data_from_db
        except ImportError as e:
            # 「拿不到」与「没有」必须可区分：这一支是**失败**，不是「库里没数据」。
            # 调用方那句 "无真实验证数据" 描述的是后者，所以这里必须自己出声。
            _log.warning("_build_real_training_data: 依赖导入失败，"
                         "本次训练将退化为硬编码样本: %s", e)
            return []

        try:
            db_path = str(PredictionStore().db_path)
        except (sqlite3.Error, OSError) as e:
            _log.warning("_build_real_training_data: 无法解析 predictions 库路径，"
                         "本次训练将退化为硬编码样本: %s", e)
            return []

        try:
            from config import ML_TRAINING_CONFIG as _MTC
            max_rows = _MTC.get("max_training_rows", 500)
        except (ImportError, AttributeError):
            max_rows = 500

        # db_path 必须显式传：不传会走 `PATHS.db` 默认值，在 git worktree 里
        # 那是一个空桩库，函数会安静地返回 []（v0.45.140 的踩坑点）。
        return build_training_data_from_db(
            db_path=db_path,
            min_samples=self.MIN_REAL_SAMPLES,
            max_rows=max_rows,
        )

    def _check_disk_cache(self, today: str) -> bool:
        """检查磁盘缓存是否存在且有效"""
        try:
            if not self._model_file.exists():
                return False

            # 检查文件修改时间是否是今天（PDT 口径，与 today=pdt_today() 一致）。
            # 否则本机上海时区渲染的 file_date 与 PDT today 在晚间窗口恒不相等 → 缓存永不命中、每次重训。
            import os
            mtime = os.path.getmtime(str(self._model_file))
            try:
                from zoneinfo import ZoneInfo
                file_date = datetime.fromtimestamp(mtime, ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
            except Exception:
                file_date = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
            return file_date == today
        except (FileNotFoundError, OSError, KeyError, ValueError, json.JSONDecodeError) as e:
            # 缓存检查失败，重新训练
            return False

    @property
    def _model_file(self):
        """磁盘缓存文件（JSON，安全序列化）。见类体顶部为何必须是 property。"""
        return PATHS.ml_model_cache

    def _load_model_from_disk(self):
        """从磁盘加载模型（委托给 model.load_model，兼容 SGD/Simple 格式）"""
        try:
            result = self.ml_service.model.load_model(str(self._model_file))
            if not result:
                _log.warning("磁盘缓存加载返回 False，将重新训练")
                self.ml_service.train_model()
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError) as e:
            _log.warning("磁盘缓存加载失败：%s，将重新训练", e)
            self.ml_service.train_model()

    def _save_model_to_disk(self):
        """保存模型到磁盘（委托给 model.save_model，SGD/Simple 均支持 JSON）"""
        try:
            self.ml_service.model.save_model(str(self._model_file))
        except (TypeError, OSError) as e:
            _log.warning("磁盘缓存保存失败：%s", e)

    # ⭐ Task 3: 异步文件写入方法
    def _write_file_async(self, filepath: Path, content: str, is_json: bool = False) -> None:
        """异步写入文件到磁盘（后台线程）"""
        try:
            with self._writer_lock:
                if is_json:
                    # JSON 内容：先对象再转 JSON（用 SafeJSONEncoder 防序列化崩溃）
                    from hive_logger import SafeJSONEncoder
                    with open(filepath, "w") as f:
                        json.dump(content, f, indent=2, cls=SafeJSONEncoder, ensure_ascii=False)
                else:
                    # 文本内容：直接写入
                    with open(filepath, "w") as f:
                        f.write(content)
        except OSError as e:
            # 磁盘 I/O 错误（权限/磁盘满）
            _log.error("[%s] 磁盘写入失败: %s", filepath.name, e)
        except (TypeError, ValueError) as e:
            # JSON 序列化错误：完整记录类型 + 路径，便于定位
            _log.error(
                "[%s] JSON 序列化失败: %s | 内容类型: %s",
                filepath.name, str(e), type(content).__name__
            )
            # 二次尝试：用 default=str 兜底序列化（牺牲精度但不丢数据）
            try:
                with open(filepath, "w") as f:
                    json.dump(content, f, indent=2, default=str, ensure_ascii=False)
                _log.warning("[%s] 已用 default=str 兜底写入", filepath.name)
            except OSError as e2:
                _log.error("[%s] 兜底写入也失败: %s", filepath.name, e2)

    def save_html_and_json_async(
        self,
        ticker: str,
        html_content: str,
        json_data: dict,
        report_dir: Path,
        timestamp: datetime,
    ) -> None:
        """
        异步保存 HTML 和 JSON 文件（后台线程）
        不阻塞主流程
        """
        # 生成文件名
        html_filename = f"alpha-hive-{ticker}-ml-enhanced-{timestamp.strftime('%Y-%m-%d')}.html"
        json_filename = f"analysis-{ticker}-ml-{timestamp.strftime('%Y-%m-%d')}.json"

        html_path = report_dir / html_filename
        json_path = report_dir / json_filename

        # 提交异步写入任务
        self._file_writer_pool.submit(self._write_file_async, html_path, html_content, False)
        self._file_writer_pool.submit(self._write_file_async, json_path, json_data, True)

    def generate_ml_enhanced_report(
        self, ticker: str, realtime_metrics: dict,
        swarm_direction: Optional[str] = None,
        swarm_dimension_scores: Optional[dict] = None,
        swarm_final_score: Optional[float] = None,
        swarm_agent_directions: Optional[dict] = None,
    ) -> dict:
        """生成 ML 增强的分析报告

        swarm_direction：蜂群当日方向，v0.45.132 起交给 AdvancedAnalyzer 做
        「同标的 + 同方向」的历史回溯（第 5 章情景推演的样本条件）。

        swarm_dimension_scores：蜂群当日五维分，v0.45.135 起用于 `catalyst_quality`，
        v0.45.137 起用于 `volatility` / `market_sentiment`，
        v0.45.140 起用于 `odds_score` / `risk_adj_score`。

        swarm_final_score：蜂群当日综合分，v0.45.141 起是 ML 特征 `final_score`
        的唯一来源（与训练端 `predictions.final_score` 同一个量）。

        swarm_agent_directions：蜂群当日**逐蜂方向**（8 只蜂的 name → direction），
        v0.45.146 起是 ML 特征 `agent_agreement` 的唯一来源——与训练端
        `predictions.agent_directions` 同一个量，共识度公式也照抄训练端那三行。

        ⚠️ 四个参数都取自同一个 `swarm_data[ticker]`，**必须成组传**——传一个漏
        一个是本仓反复出现的半接线故障（守卫见
        `tests/test_ml_catalyst_quality_source.py::TestProductionWiring`）。
        """

        # 获取高级分析
        advanced_analysis = self.analyzer.generate_comprehensive_analysis(
            ticker, realtime_metrics, direction=swarm_direction
        )

        # 构建 ML 输入数据
        ml_input = self._prepare_ml_input(
            ticker, realtime_metrics, advanced_analysis,
            swarm_dimension_scores=swarm_dimension_scores,
            swarm_direction=swarm_direction,
            swarm_final_score=swarm_final_score,
            swarm_agent_directions=swarm_agent_directions,
        )

        # 获取 ML 预测
        ml_prediction = self.ml_service.predict_for_opportunity(ml_input)

        # 提取当前价（优先 dealer_gex → realtime_metrics → 0）
        # v0.27.4: dict.get(key, default) 在 key 存在但 value=None 时返回 None，
        # 链式调用必须每段 `or {}` 兜底（与 v0.27.1 _ch3_oracle 同类修复）
        _current_price = (
            (advanced_analysis.get("dealer_gex") or {}).get("stock_price")
            or ((realtime_metrics.get("sources") or {}).get("yahoo_finance") or {}).get("current_price")
            or 0.0
        )

        # 合并分析
        enhanced_report = {
            "ticker": ticker,
            "timestamp": self.timestamp.isoformat(),
            "current_price": float(_current_price) if _current_price else None,
            "advanced_analysis": advanced_analysis,
            "ml_prediction": {
                **ml_prediction,
                "current_price": float(_current_price) if _current_price else None,
                "training_data_source": self._training_data_source,  # "real"/"sample"/"unknown"
                # v0.45.50：输入特征里有几个是补齐的（空 = 全部为真实观测）
                "input_features_missing": list(getattr(self, "_ml_input_missing", [])),
            },
            "combined_recommendation": {
                **self._combine_recommendations(advanced_analysis, ml_prediction),
                "current_price": float(_current_price) if _current_price else None,
            },
        }

        # ── 前向记分账本（v0.45.134 Step 3）────────────────────────────
        # 记下**这一天真正印出去的**那个数。回溯记分（probability_scorecard
        # --walk-forward）假设估计量是 DB 的纯函数，一旦换了估计量或补跑了历史
        # 这个假设就破了；账本不受影响。
        #
        # ⚠️ 失败不阻断报告，但**必须留下会被看见的痕迹**——静默 except 会把
        # 「账本一行没记」变成「没发生过」，那正是本项目 v0.45.91/119/121/124
        # 反复踩的形状。故 WARNING 且计数。
        try:
            from probability_scorecard import record_published
            _pa = advanced_analysis.get("probability_analysis") or {}
            _mp = (ml_prediction.get("prediction") or {}).get("probability")
            _ml_pct = (float(_mp) * 100.0
                       if isinstance(_mp, (int, float)) and not isinstance(_mp, bool) else None)
            record_published(
                report_date=self.timestamp.date().isoformat(),
                ticker=ticker,
                direction=swarm_direction,
                hit_rate_pct=_pa.get("hit_rate_pct"),
                basis=_pa.get("basis"),
                sample_size=_pa.get("sample_size"),
                forward_estimate_pct=_pa.get("forward_estimate_pct"),
                forward_sample_size=_pa.get("forward_sample_size"),
                ml_probability_pct=_ml_pct,      # v0.45.139：融合权重扫描的第二个输入
            )
        except Exception as _led_err:   # noqa: BLE001 —— 记账失败不得阻断报告
            self._ledger_failures = getattr(self, "_ledger_failures", 0) + 1
            _log.warning("[%s] 概率账本写入失败（累计 %d 次）：%s",
                         ticker, self._ledger_failures, _led_err)

        return enhanced_report

    def _prepare_ml_input(
        self, ticker: str, metrics: dict, analysis: dict,
        swarm_dimension_scores: Optional[dict] = None,
        swarm_direction: Optional[str] = None,
        swarm_final_score: Optional[float] = None,
        swarm_agent_directions: Optional[dict] = None,
    ) -> TrainingData:
        """为 ML 模型准备输入数据"""

        # 从实时数据中提取特征（有则用真实值，无则降级到合理默认）
        _yf = metrics.get("sources", {}).get("yahoo_finance", {})
        momentum_5d = _yf.get("price_change_5d", 0.0) or 0.0

        # ── v0.45.137：volatility / market_sentiment 的唯一来源 = 蜂群维度分 ──
        # 旧实现声称从 BuzzBee details 提取（"BUG-6/BUG-7 修复"），但它读的
        # `self._swarm_cache` **全仓从未被赋值过**（AST 实测：赋值点 0、读取点 1，
        # 类属性里也没有）⇒ `hasattr(self, "_swarm_cache")` 恒 False ⇒ 那段是死码。
        # 叠加第二个独立缺陷：查表键 `metrics.get("_ticker")` 在生产
        # `realtime_metrics` 里也不存在（生产用 `"ticker"`），即便缓存存在也会空串落空。
        #
        # 实测 803 份生产 analysis-*-ml-*.json：volatility 恒 5.0、
        # market_sentiment 恒 0.0（各 802/803，剩 1 份是 v2 之前的旧 schema）。
        # volatility 那条四级 fallback 链**每级都是死的**——`_yf` 里没有
        # volatility_20d/atr_pct（生产只有三个价格字段），
        # `options_analysis["historical_volatility"]` 这个键 803/803 份都不存在
        # （真实字段名是 `rv_30d`）。只有字面量 5.0 会触发。
        #
        # ⚠️ 为什么**不**照注释说的接 BuzzBee 真实波动率：这个特征槽在训练路径
        # （`build_training_data_from_db`，n=497）里装的是 `(10-risk_adj)*2.5`，
        # 中位 11.65；BuzzBee 的 `volatility_20d` 是真年化波动率，中位 39.66，
        # 与训练口径 Spearman ρ=+0.068、scale 差 ~4×。接进来 = 用 A 训练拿 B 服务，
        # 制造一个与 v0.45.135 同 species 的 train/serve skew，比现在的常数更糟。
        # sentiment 侧没有这个矛盾（维分与 BuzzBee sentiment_pct ρ=+0.987，
        # 是同一个量的两种刻度），取维分可与训练路径逐字节同源。
        #
        # 常数 5.0 落在训练 volatility 分布的 **9.1 分位**——它不是"中性"，
        # 是系统性偏低；803 份重放显示 52.4% 的 probability 变动 > 0.02。
        from ml_predictor import market_sentiment_from_score, volatility_from_risk_adj
        _dims = swarm_dimension_scores or {}

        # ── v0.45.146：crowding_score 的唯一来源 = 蜂群 signal 维度分 × 10 ──
        # 旧实现 `metrics.get("crowding_score", _fallback_crowding)`：生产
        # `realtime_metrics` 里**既没有** `crowding_score`、**也没有**
        # `short_interest_ratio`（两个键在 803 份落盘 JSON 的上游结构里都不存在），
        # 于是两级兜底全部落到字面量 50.0 —— 实测 777/803 恒 50.0，
        # 另 25 份 500.0（clamp 之前的越界残留）、1 份 45.0。
        #
        # ⚠️ **不要**去接 ScoutBeeNova 的 `details.crowding_score`（那才是真拥挤度）。
        # 这个特征槽的名字说谎：训练端 `build_training_data_from_db` 往它里面装的是
        # `crowding_score=_sig * 10`，也就是**信号维度分×10**，不是拥挤度。实测：
        #   · signal×10          生产中位 49.60 / sd 12.31，训练分布 mean 52.77 / sd 7.60
        #   · ScoutBee 真拥挤度   生产中位 23.75 / sd 12.28
        #   · 二者 Spearman ρ = **−0.46**
        # 接真拥挤度不只是量纲不对（v0.45.137 volatility 那次是 ρ=+0.068 的无关），
        # 这次是**近似反号**——会主动把模型学到的方向喂反。
        # 「改服务端口径前先去训练端读那个槽实际装的是什么量」——名字一致≠同一个量。
        #
        # 与训练端逐字节同源：`Backtester.save_predictions` 把
        # `swarm_results[ticker]["dimension_scores"]` 原样写进 `predictions.dimension_scores`，
        # 训练端再从中取 `signal`。此处取的是同一个 `swarm_data[ticker]`。
        #
        # 常数 50.0 **不是中性**：它落在训练 crowding 分布的 **36.0 分位**。
        # 而 `crowding` 是当前模型 permutation importance **排名第一**的特征
        # （+0.0826，12 维之首）—— 这是历次接线里杠杆最大的一个槽。
        #
        # 旧的 `min(100, max(0, ·))` clamp 一并移除：它是为 `_sir * 10` 越界准备的，
        # 而训练端 `_sig * 10` **不做** clamp。留着 clamp 会在尾部制造新的口径差；
        # 且维度分本就 0~10（生产实测 signal ∈ [1.88, 9.64] ⇒ ×10 ∈ [18.8, 96.4]），
        # clamp 在真实数据上从未生效过。
        _signal_raw = _dims.get("signal")
        _signal_known = _usable_dim(_signal_raw)
        crowding_score = float(_signal_raw) * 10.0 if _signal_known else None

        _risk_adj_raw = _dims.get("risk_adj")
        _sentiment_raw = _dims.get("sentiment")
        _risk_adj_known = _usable_dim(_risk_adj_raw)
        _sentiment_known = _usable_dim(_sentiment_raw)
        # 取不到就是 None，不挑兜底值——None 会被 ml_predictor 自己的
        # `_missing_features` 数进 `imputed_features` / `feature_completeness`，
        # 两套账目因此一致。喂字面量则会让 `input_features_missing` 说缺、
        # `feature_completeness` 说 12/12（生产现存 118 份这种自相矛盾的记录）。
        volatility = volatility_from_risk_adj(_risk_adj_raw) if _risk_adj_known else None
        market_sentiment = (market_sentiment_from_score(_sentiment_raw)
                            if _sentiment_known else None)
        # ⚠️ 派生值**已经**归到 -100~+100，不得再过一遍旧的「三段式量表自动识别」
        # （`abs(x)<=1 → *100`、`abs(x)<=10 → *10`）——那是给来源不明的原始情绪分
        # 准备的，对已归一的输入会把接近中性的值放大 10~100 倍（维分 5.2 → 4.0 → 40.0），
        # 而 sentiment 维分落在 [4.5, 5.5] 的样本在生产里并不罕见。

        # ── v0.45.135：催化剂等级的唯一来源 = ChronosBee 催化剂维度分 ──
        # 旧实现由 `recommendation.rating` 反推（STRONG BUY→A+ / BUY→A /
        # HOLD→B+ / AVOID→C）。但 rating 不是催化剂的度量，它的上游是
        #   advanced_analyzer._estimate_catalyst_quality(ticker)   ← 硬编码三只票
        #     → calculate_win_probability(crowding, grade)
        #       → _generate_recommendation(prob, rr)               ← 四档阈值
        # 于是这个特征实际编码的是**拥挤度**，不是催化剂。
        #
        # 实测 803 份生产 analysis-*-ml-*.json（745 份两条口径都能取到）：
        #   · 等级一致率 7.1%，低于按边缘分布独立时的期望 8.1%
        #   · Spearman ρ = +0.087（≈ 无关）
        #   · 旧路径**从未**产出 "B"/"C"（0/745），而真实分布里两档共占 70.5%
        # 训练路径 `_build_real_training_data` 与 `swarm_agents/rival_bee.py`
        # 都走 `catalyst_quality_from_score`；此处对齐后三处同源，
        # train/serve skew 消除。阈值本身不动（历史样本可比性由那张表保证）。
        from ml_predictor import catalyst_quality_from_score as _cat_qual
        _catalyst_raw = (swarm_dimension_scores or {}).get("catalyst")
        # bool 是 int 子类，`_cat_qual(True)` 会当成 1.0 判成 "C"（最差档）——
        # 与本仓其余 5 处守卫同写法，显式排除。NaN 同理不能进 float 比较。
        # v0.45.137：三个维度派生特征共用 `_usable_dim`，不再各抄一份 isinstance 行。
        _catalyst_known = _usable_dim(_catalyst_raw)
        # ── v0.45.147：取不到时是 None，不是 "B" ──
        # v0.45.135 选 "B" 的理由是「"B+" 是 magnitude 1.0 的基准档，用它会让
        # 『拿不到数据』与『质量正好中等』不可区分」——方向对，但**选中了众数**：
        # 生产实测 "B" 占真实等级的 **57.4%**（461/803），是五档里最常见的一档，
        # 于是 58 份缺失与 461 份真实 "B" 完全同形，比用 "B+" 更糟。
        # 且它是合法枚举值 ⇒ `ml_predictor._missing_features` 不算它缺 ⇒
        # `input_features_missing` 说缺、`feature_completeness` 说 12/12（58/803 份）。
        catalyst_quality = _cat_qual(_catalyst_raw) if _catalyst_known else None

        # ── v0.45.140：odds / risk_adj / final_score 的唯一来源 = 蜂群 ──
        # 旧实现读 `analysis["dimension_scores"]` 与 `recommendation["score"]`，
        # 而 `analysis` 就是 `advanced_analyzer.generate_comprehensive_analysis()`
        # 的返回值——实测 **803/803 份生产 analysis-*-ml-*.json 的
        # `advanced_analysis` 里没有 `dimension_scores` 键、`recommendation` 里
        # 也没有 `score` 键** ⇒ 三个特征恒为字面量 5.0。
        #
        # 与 v0.45.137 那两个死读者的区别：这三个**已被 `_ml_input_missing`
        # 如实报出**（生产 118 份 JSON 的 `input_features_missing` 逐字就是
        # 这三个名字），没有说谎。但「诚实地缺」不等于无害：
        #   · 喂进模型的仍是常数 5.0，而它在训练分布里**不是中性**——
        #     odds 落在 **7.6 分位**、final_score 落在 **16.9 分位**
        #     （训练端实测 n=497：odds 中位 7.54 / risk_adj 5.34 / final 5.44）
        #   · 账本本身也不准：真值一直躺在**同一份 JSON** 的 `swarm_results` 里
        #     （745/803 三个全齐），「取不到」与「没去取」被记成了同一件事
        #
        # 真值与训练端**逐字节同源**，不是抄第二份公式：
        #   训练 `build_training_data_from_db` 读 `predictions.dimension_scores`
        #   / `predictions.final_score`，而 `Backtester.save_predictions(
        #   swarm_results)` 正是从 `swarm_results[ticker]` 的同名键写进去的。
        #
        # 803 份重放（模型按生产口径训练于 497 条真实样本）：
        # |Δprobability| 中位 0.0063、**27.3% 变动 > 0.02**、max 0.0625；
        # 分布 sd 0.0280 → 0.0333；三特征 permutation importance 合计 0.1357。
        _odds_raw = _dims.get("odds")
        _final_raw = swarm_final_score
        _odds_known = _usable_dim(_odds_raw)
        _final_known = _usable_dim(_final_raw)
        # ⚠️ 三条旧读点还有第二重危害（v0.45.141 就 final_score 一支记过）：
        # 缺失表条目 `("final_score", _rec.get("score"))` 等三条因键从不存在而
        # **恒上榜**——自 v0.45.50 有缺失表起，`input_features_missing` 里这三个
        # 名字一次没落下过。「永远缺失」与「真缺失」在输出里同形，
        # **一条永远亮着的告警等于没有告警**。
        #
        # 取不到就是 None（同 volatility / market_sentiment 的约定）——
        # 字面量会让 `input_features_missing` 说缺、`feature_completeness`
        # 说 12/12，生产现存 118 份这种当面矛盾的记录，根因就在这里。
        odds_score = float(_odds_raw) if _odds_known else None
        risk_adj_score = float(_risk_adj_raw) if _risk_adj_known else None
        final_score = float(_final_raw) if _final_known else None

        # v2 新特征（从 analysis 上下文提取）
        _opts = analysis.get("options_analysis", {})
        # v0.45.139：direction_encoded 改读**蜂群方向**，与训练路径同一张表
        # （ml_predictor.build_training_data_from_db 的 direction_map）。
        # 旧实现读 recommendation.rating 再映射 {STRONG BUY:1, BUY:.5, HOLD:0, AVOID:-1}
        # —— 训练用 bullish/neutral/bearish、服务用评级词，与 v0.45.135 修掉的
        # catalyst_quality 是同种 train/serve skew；评级词撤销后它还会静默恒为 0.0。
        _direction_map = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}
        _dir_known = swarm_direction in _direction_map

        # ── v0.45.146：agent_agreement 的唯一来源 = 蜂群 agent_directions ──
        # 旧实现是字面量 `agent_agreement=0.5,  # 预测时无蜂群上下文`。
        # 那条注释自 v0.45.140 起**已经不成立**——本函数此刻就收着
        # `swarm_dimension_scores` / `swarm_direction` / `swarm_final_score`，
        # 而逐蜂方向躺在**同一个** `swarm_data[ticker]` 里（生产 746/746 份都有，
        # 长度恒为 8：ScoutBeeNova / OracleBeeEcho / BuzzBeeWhisper /
        # ChronosBeeHorizon / RivalBeeVanguard / GuardBeeSentinel /
        # BearBeeContrarian / CodeExecutorAgent）。
        #
        # 公式**照抄训练端**，不自己发明共识度：
        #   ad = json.loads(r["agent_directions"]); _dir = r["direction"] or "neutral"
        #   _agree = sum(1 for d in ad.values() if d == _dir) / len(ad)
        # 取数链同源：`Backtester.save_predictions` 把
        # `swarm_results[ticker]["agent_directions"]` 原样写进
        # `predictions.agent_directions`，训练端再从那里读回来。
        # 两端方向词表实测完全一致（生产 5968 条逐蜂方向只有
        # bullish/bearish/neutral 三个值），不存在 v0.45.139 那种评级词 vs 方向词的 skew。
        #
        # 训练端的两条兜底（`ad` 为空 → 0.5、`direction` 为 NULL → "neutral"）
        # 在库里**都是死分支**（500 条候选中各 0 条），所以这里改用「取不到就是
        # None」不会与训练端产生实际口径差；而共识度是**相对于方向定义**的，
        # 没有已知方向就没有「与之一致」可言，故 `_dir_known` 也是前提。
        #
        # ⚠️ 这个槽与前几次接线的重要区别：0.5 在训练分布里**接近众数**
        # （39.0 分位，且恰好 37.0% 的样本就是 0.5 —— 8 只蜂里 4 只同向）。
        # 所以它的危害不是「系统性偏移」，而是**零区分度**：常数不携带任何
        # 跨标的信息。permutation importance 也确实偏低（+0.0077，12 维第 10），
        # 实测扫遍全值域只能改变 63.6% 的行、极差中位 0.0053 —— 预期影响小，
        # 但「小」是测出来的，不是猜的。
        _ad = swarm_agent_directions if isinstance(swarm_agent_directions, dict) else {}
        _agree_known = bool(_ad) and _dir_known
        agent_agreement = (
            sum(1 for _d in _ad.values() if _d == swarm_direction) / len(_ad)
            if _agree_known else None
        )

        # ── v0.45.50：记录哪些特征是**补齐的**，不是观测到的 ──
        # 下面五个 .get(k, 默认值) 在缺失时产出 iv_rank=50 / pc=1.0 / 三个 5.0，
        # 组成一份内部完全自洽的「典型标的」画像。模型不会拒绝它，
        # 会照常吐出一个概率，而那个概率随后被当成真实预测渲染。
        # 预测本身仍然做（有部分特征也比不做强），但**不能声称输入是干净的**。
        # 与同文件已有的 `training_data_source` 来源标记同一思路。
        # v0.45.135：catalyst_quality 也进这张表——否则「蜂群没跑/板上没条目」
        # 与「催化剂正好中等」在输出里长得一样（同 v0.45.113 的判据）。
        # v0.45.137：volatility / market_sentiment 也进这张表。它们此前
        # **不可能**上榜——旧代码在缺数时喂的是字面量 5.0 / 0.0，一个合法数值，
        # 于是「蜂群没跑」与「波动正好偏低、情绪正好中性」在输出里完全同形。
        # 这两个特征的值现在是 None，ml_predictor 自己的 `_missing_features`
        # 也会数到，`input_features_missing` 与 `feature_completeness` 两套账
        # 因此对得上（此前生产有 118 份记录两者当面矛盾）。
        # v0.45.141：final_score 同样改由蜂群参数判可得。旧条目
        # `("final_score", _rec.get("score"))` 因键从不存在而**恒上榜**——
        # 一条永远亮着的告警等于没有告警。
        # v0.45.146：crowding_score / agent_agreement 补进这张表。此前它们
        # **不可能**上榜——旧代码缺数时喂字面量 50.0 / 0.5，两个合法数值，
        # 于是「蜂群没跑」与「信号正好中等、八蜂正好四比四」在输出里完全同形。
        self._ml_input_missing = (
            ([] if _signal_known else ["crowding_score"])
            + ([] if _agree_known else ["agent_agreement"])
            + ([] if _catalyst_known else ["catalyst_quality"])
            + ([] if _dir_known else ["direction"])
            + ([] if _risk_adj_known else ["volatility"])
            + ([] if _sentiment_known else ["market_sentiment"])
            + ([] if _final_known else ["final_score"])
            + ([] if _odds_known else ["odds_score"])
            + ([] if _risk_adj_known else ["risk_adj_score"])
            + [
                _name for _name, _val in (
                    ("iv_rank", _opts.get("iv_rank")),
                    ("put_call_ratio", _opts.get("put_call_ratio")),
                ) if not _usable_dim(_val)
            ]
        )
        if self._ml_input_missing:
            _log.warning("[%s] ML 输入有 %d 个特征不可得——"
                         "本次预测的输入不是干净观测：%s",
                         ticker, len(self._ml_input_missing),
                         ", ".join(self._ml_input_missing))

        return TrainingData(
            ticker=ticker,
            date=_pdt_now().isoformat(),  # 与本文件其余日期口径统一为 PDT（该字段不参与下游日期逻辑）
            crowding_score=crowding_score,
            catalyst_quality=catalyst_quality,
            momentum_5d=momentum_5d,
            volatility=volatility,
            market_sentiment=market_sentiment,
            actual_return_3d=0,
            actual_return_7d=0,
            actual_return_30d=0,
            win_3d=False,
            win_7d=False,
            win_30d=False,
            # v2
            iv_rank=_opts.get("iv_rank", 50.0),
            put_call_ratio=_opts.get("put_call_ratio", 1.0),
            final_score=final_score,
            odds_score=odds_score,
            risk_adj_score=risk_adj_score,
            agent_agreement=agent_agreement,  # v0.45.146：蜂群逐蜂方向的共识度
            # v0.45.147：方向不可得时是 None，不是 0.0 —— **0.0 在这张表里
            # 正是 "neutral"**，一个真实类别。旧兜底让「方向拿不到」与
            # 「蜂群判中性」在特征与账目上都同形（57/803 份）。
            direction_encoded=(_direction_map[swarm_direction] if _dir_known else None),
        )

    def _generate_options_section_html(self, options: dict) -> str:
        """生成期权分析 HTML 部分"""
        if not options:
            return ""

        # P0-1 (v0.38.0): 期权数据不可用（样本链早退）时不渲染指标卡——
        # 此前样本链的假异动信号（$140/$145/$150）会以真数据形式进报告
        if options.get("data_quality") == "unavailable":
            return (
                '<div class="section"><h2>期权市场分析</h2>'
                '<div style="padding:14px 16px;border:1px solid var(--border);'
                'border-radius:2px;font-size:.9em;">'
                '期权数据不可用（CBOE / yfinance 均获取失败），本节指标跳过，'
                '不参与今日评分。</div></div>'
            )

        # ── v0.45.43：与 _ch3_oracle 同一处理，缺失渲染「—」不冒充读数 ──
        # 上面的 data_quality=="unavailable" 闸只挡**全盘**不可用。
        # 2026-08-26 是**部分**降级：CBOE 正常（data_quality="real"）而
        # yfinance 挂掉 → iv_rank 为 None 却照样过闸，随后：
        #   iv_rank=50  → 渲染成「50.0（中等 IV）」   ← 一个确凿的假读数
        #   iv_current or 25 / iv_percentile or 50 / put_call_ratio or 1.0
        # 注意 `or` 比 `if is None` 更糟：真实的 **0** 也会被替换掉。
        _NA_M = '<span class="na" title="数据不可用">—</span>'

        def _m(v, spec="{:.1f}", suffix=""):
            return _NA_M if not isinstance(v, (int, float)) else (spec.format(v) + suffix)

        iv_rank = options.get("iv_rank")
        iv_percentile = options.get("iv_percentile")
        iv_current = options.get("iv_current")
        put_call_ratio = options.get("put_call_ratio")
        gamma_squeeze_risk = options.get("gamma_squeeze_risk", "medium")
        flow_direction = options.get("flow_direction", "neutral")
        options_score = options.get("options_score", 5.0)
        signal_summary = options.get("signal_summary", "信号平衡")
        unusual_activity = options.get("unusual_activity", [])
        key_levels = options.get("key_levels", {})

        # 判断 IV Rank 颜色（v0.45.43：不再归一为 50，缺失单独走中性色）
        if iv_rank is None:
            iv_color = "var(--tm)"
            iv_label = "数据不可用"
        elif iv_rank < 30:
            iv_color = "var(--bull)"  # 绿色，低 IV
            iv_label = "低 IV"
        elif iv_rank > 70:
            iv_color = "var(--bear)"  # 红色，高 IV
            iv_label = "高 IV"
        else:
            iv_color = "var(--neut)"  # 黄色，中等 IV
            iv_label = "中等 IV"

        # 判断流向颜色
        if flow_direction == "bullish":
            flow_color = "var(--bull)"
        elif flow_direction == "bearish":
            flow_color = "var(--bear)"
        else:
            flow_color = "var(--neut)"

        # 生成异动信号 HTML
        unusual_html = ""
        if unusual_activity:
            unusual_html = "<div style='margin-top: 15px;'><strong>异动信号：</strong><ul style='margin: 10px 0; padding-left: 20px;'>"
            for activity in unusual_activity[:5]:  # 只显示前 5 个
                activity_type = activity.get("type", "unknown")
                strike = activity.get("strike", "N/A")
                volume = activity.get("volume", 0)
                unusual_html += f"<li>{activity_type} @ ${strike} (成交量: {volume:,})</li>"
            unusual_html += "</ul></div>"

        # 生成关键位置 HTML
        support_html = ""
        resistance_html = ""

        if key_levels.get("support"):
            support_html = "<div style='margin-top: 15px;'><strong>支撑位：</strong><ul style='margin: 10px 0; padding-left: 20px;'>"
            for level in key_levels.get("support", []):
                strike = level.get("strike", "N/A")
                oi = level.get("oi", 0)
                support_html += f"<li>${strike} (OI: {oi:,})</li>"
            support_html += "</ul></div>"

        if key_levels.get("resistance"):
            resistance_html = "<div style='margin-top: 15px;'><strong>阻力位：</strong><ul style='margin: 10px 0; padding-left: 20px;'>"
            for level in key_levels.get("resistance", []):
                strike = level.get("strike", "N/A")
                oi = level.get("oi", 0)
                resistance_html += f"<li>${strike} (OI: {oi:,})</li>"
            resistance_html += "</ul></div>"

        return f"""
            <div class="section">
                <h2>期权信号分析</h2>

                <div class="ml-section">
                    <h3>核心指标</h3>

                    <div class="metric">
                        <span class="metric-label">IV Rank</span>
                        <span class="metric-value" style="color: {iv_color};">
                            {_m(iv_rank)} ({iv_label})
                        </span>
                    </div>

                    <div class="metric">
                        <span class="metric-label">当前 IV</span>
                        <span class="metric-value">{_m(iv_current, "{:.2f}", "%")}</span>
                    </div>

                    <div class="metric">
                        <span class="metric-label">IV 百分位数</span>
                        <span class="metric-value">{_m(iv_percentile, "{:.1f}", "%")}</span>
                    </div>

                    <div class="metric">
                        <span class="metric-label">Put/Call Ratio</span>
                        <span class="metric-value">{_m(put_call_ratio, "{:.2f}")}</span>
                    </div>

                    <div class="metric">
                        <span class="metric-label">流向</span>
                        <span class="metric-value" style="color: {flow_color};">
                            {flow_direction.upper()}
                        </span>
                    </div>

                    <div class="metric">
                        <span class="metric-label">Gamma Squeeze 风险</span>
                        <span class="metric-value">{gamma_squeeze_risk.upper()}</span>
                    </div>

                    <h3>期权综合评分</h3>

                    <div style="text-align: center; padding: 20px; background:none; border:1px solid var(--border); border-radius:2px;">
                        <div style="font-size: 3.5em; font-weight: bold; color: var(--tp); margin-bottom: 10px;">
                            {options_score:.1f}
                        </div>
                        <div style="font-size: 1.2em; color: var(--tp); margin-bottom: 10px;">/ 10.0</div>
                        <div style="color: var(--ts); font-size: 0.95em;">{signal_summary}</div>
                    </div>

                    {unusual_html}
                    {support_html}
                    {resistance_html}
                </div>
            </div>
"""

    def _combine_recommendations(
        self, advanced_analysis: dict, ml_prediction: dict
    ) -> dict:
        """合并历史命中率与 ML 预测。

        v0.45.134：第一项从 `win_probability_pct`（常数）换成 `hit_rate_pct`
        （同标的同方向历史 T+7 命中率）。

        旧口径为什么必须换：那一项在生产 803 份报告里 **81% 恒等于 65.0**，
        于是 `combined = 0.7×65 + 0.3×ml` 把 ML 的 0~100 全程压进 **[47.0, 74.0]**
        这 27 个点里——触发 AVOID 需 ML<15.0%、触发 STRONG BUY 需 ML>98.3%
        （803 份里 STRONG BUY 只出现过 3 次）。评级实际退化成 ml_prob 的阈值重标记。

        ⚠️ 0.7 / 0.3 这组权重与下面 75/65/50 三道闸，都是从常数量表继承下来的，
        **没有任何验证**。本次只换来源、不动判据（换来源与改判据不该挤在同一次
        改动里）。命中率不可得时**不拿默认值顶替**——那正是旧实现 `, 50)` 的错，
        50 恰好卡在 HOLD 闸上；改为退化成「只看 ML」，并在 reasoning 里说明。
        """
        _pa = advanced_analysis.get("probability_analysis") or {}
        # v0.45.138：融合读**前瞻量**（全书池化），不读描述量（分票分方向频率）。
        # v0.45.134 用的是后者，记分卡随即判定它作为预测显著更差（配对 t=+2.12）。
        fwd = _pa.get("forward_estimate_pct")
        _fwd_known = (isinstance(fwd, (int, float)) and not isinstance(fwd, bool)
                      and math.isfinite(fwd))
        ml_prob = ml_prediction.get("prediction", {}).get("probability", 0.5) * 100

        if _fwd_known:
            combined_prob = fwd * 0.7 + ml_prob * 0.3
            _reasoning = (f"前瞻命中率 {fwd:.1f}%"
                          f"（全书池化 n={_pa.get('forward_sample_size')}；各标的相同）× 0.7"
                          f" + ML 预测 {ml_prob:.1f}% × 0.3 = 综合 {combined_prob:.1f}%")
        else:
            combined_prob = ml_prob
            _reasoning = (f"前瞻命中率不可得（池化样本 n={_pa.get('forward_sample_size')}），"
                          f"综合分退化为纯 ML 预测 {ml_prob:.1f}%")

        # v0.45.139：**不再产出评级词**。实测（n=438 份能对上 T+7 结果的报告）
        # BUY 命中 54.5% vs HOLD 56.9%，z = −0.55；ML 概率五等分非单调、
        # Spearman +0.026 ± 0.099。评级只是 combined_prob 的阈值重标记，而
        # combined_prob 本身不区分结果。撤掉评级词、保留数字与出处。
        # 三道闸（75 / 65 / 50）连同来历记在 CHANGELOG v0.45.139。
        rating, action = None, None

        return {
            # v0.45.134：`human_probability` 这个名字随字段一起退休——它从来不是
            # 「人工分析」，是一条 base 0.55 加常数的公式。不可得时保持 None。
            # v0.45.138：两个口径分列，别混——前者描述过去、后者预测下一笔。
            "hit_rate_pct": _pa.get("hit_rate_pct"),              # 描述：本标的本方向
            "hit_rate_basis": _pa.get("basis"),
            "hit_rate_sample_size": _pa.get("sample_size"),
            "forward_estimate_pct": round(fwd, 1) if _fwd_known else None,   # 预测：全书池化
            "forward_ci95": _pa.get("forward_ci95"),
            "forward_sample_size": _pa.get("forward_sample_size"),
            "forward_is_ticker_specific": False,
            "ml_probability": round(ml_prob, 1),
            "combined_probability": round(combined_prob, 1),
            "combined_basis": "forward*0.7+ml*0.3" if _fwd_known else "ml_only",
            "rating": rating,                 # v0.45.139 起恒为 None，键保留免下游崩
            "action": action,
            "rating_retired": "v0.45.139",
            "confidence": f"{combined_prob:.1f}%",
            "reasoning": _reasoning,
        }

    # ─────────────────────────────────────────────────────────────
    # 模板 C 7 章辅助方法
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _dir_cn(d):
        return {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(d, d)

    @staticmethod
    def _dir_color(d):
        return {"bullish": "var(--bull)", "bearish": "var(--bear)"}.get(d, "var(--neut)")

    def _ch1_core_conclusion(self, swarm: dict, combined: dict, analysis: dict) -> str:
        """第1章：核心结论"""
        if not swarm and not combined:
            return ""
        final_score = swarm.get("final_score", combined.get("combined_probability", 50) / 10)
        direction = swarm.get("direction", "neutral")
        ab = swarm.get("agent_breakdown", {})
        resonance = swarm.get("resonance", {})
        combined_prob = combined.get("combined_probability", 50)
        # v0.45.139：评级已撤（实测不区分结果），这一格改印本标的本方向的历史命中率——
        # 那是第 1 章里唯一逐标的、且能被记分卡核对的数
        _pa1 = analysis.get("probability_analysis") or {}
        _hr1 = _pa1.get("hit_rate_pct")
        _hr_row = (f"{_hr1:.1f}%（n={_pa1.get('sample_size')}）"
                   if isinstance(_hr1, (int, float)) and not isinstance(_hr1, bool)
                   else "不可得")
        dir_cn = self._dir_cn(direction)
        dir_color = self._dir_color(direction)
        # 3句摘要：从overview + 最高分维度 + 最大风险
        overview = analysis.get("overview", "")
        dim_scores = swarm.get("dimension_scores", {})
        top_dim = max(dim_scores, key=lambda k: dim_scores[k]) if dim_scores else ""
        dim_cn = {"signal": "聪明钱信号", "catalyst": "催化剂", "sentiment": "市场情绪",
                  "odds": "期权赔率", "risk_adj": "风险调整"}.get(top_dim, top_dim)
        res_text = ""
        if resonance.get("resonance_detected"):
            res_dims = "、".join(resonance.get("resonant_dimensions", []))
            res_text = f"（{resonance.get('supporting_agents', 0)} Agent 共振：{res_dims}）"
        summary_parts = []
        if overview:
            summary_parts.append(overview)
        if top_dim and dim_scores:
            summary_parts.append(f"最强维度 {dim_cn} 评分 {dim_scores[top_dim]:.1f}/10{res_text}")
        bear = swarm.get("agent_details", {}).get("BearBeeContrarian", {})
        bear_score = bear.get("details", {}).get("bear_score", 0) if bear else 0
        if bear_score >= 6:
            summary_parts.append(f"看空蜂强度 {bear_score:.1f}/10，需关注下行风险")
        summary_html = "".join(f"<p style='margin:6px 0;color:var(--ts);'>{s}</p>" for s in summary_parts[:3])
        return f"""
        <div class="section">
            <h2>第 1 章：核心结论</h2>
            <div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap;margin-bottom:18px;">
                <div style="text-align:center;">
                    <span style="font-size:3em;font-weight:bold;color:{dir_color};">{final_score:.1f}</span>
                    <span style="font-size:1.2em;color:var(--tm);">/10</span>
                    <div><span style="color:{dir_color};border:1px solid currentColor;border-radius:2px;padding:3px 11px;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:11px;font-weight:600;letter-spacing:.5px;">{dir_cn}</span></div>
                </div>
                <div style="flex:1;min-width:180px;">
                    <div class="metric"><span class="metric-label">综合胜率</span><span class="metric-value" style="color:{dir_color};">{combined_prob:.1f}%</span></div>
                    <div class="metric"><span class="metric-label">投票</span><span class="metric-value">{ab.get('bullish',0)}多 / {ab.get('bearish',0)}空 / {ab.get('neutral',0)}中</span></div>
                    <div class="metric"><span class="metric-label">历史命中率（本标的本方向）</span><span class="metric-value">{_hr_row}</span></div>
                </div>
            </div>
            {summary_html}
        </div>"""

    def _ch2_five_dim_table(self, swarm: dict) -> str:
        """第2章：五维评分明细

        权重优先读 `swarm["dimension_weights"]`——这是 queen_distiller 当时
        实际用于合成 final_score 的权重（`_regime_weights_used`，config 基准
        经政体调整后的结果），只有它缺失（旧记录）时才退回 `config.EVALUATION_
        WEIGHTS`。v0.45.174 曾直接用 config 权重，但 2026-09-10 发现二者可能
        不是一回事：`alpha_hive_daily_report.py` 会把 `Backtester.adapt_weights()`
        算出的 `adapted_weights`（存在 pheromone.db 的 adapted_weights 表，daily
        跑、按 T+7 回测准确率重新学习）直接传给 `QueenDistiller(adapted_weights=)`，
        这会让 `__init__` 跳过 config 热加载分支——实测 09-09 全部 30 只标的
        `dimension_weights` 里 signal/risk_adj 仍有 ~14-23% 权重，config 里
        明明已经归零。读 swarm 自带的字段就不必关心究竟是哪条路径生效，
        永远和第1章的真实 final_score 一致。
        """
        if not swarm:
            return ""
        dim_scores = swarm.get("dimension_scores", {})
        if not dim_scores:
            return ""
        HINTS = [
            ("signal",   "信号强度 (Signal)",   "聪明钱 SEC Form4 / 机构持仓"),
            ("catalyst", "催化剂 (Catalyst)",   "事件日历 / 财报 / 产品发布"),
            ("sentiment","情绪 (Sentiment)",    "X 平台 / Reddit / 新闻情绪"),
            ("odds",     "赔率 (Odds)",          "期权 P/C / IV Rank / Polymarket"),
            ("risk_adj", "风险调整 (RiskAdj)",  "拥挤度 / 波动 / 交叉验证调整"),
        ]
        weights_from_config = False
        weights = swarm.get("dimension_weights")
        if not weights:
            weights = config.EVALUATION_WEIGHTS
            weights_from_config = True
        rows = ""
        formula_terms = []
        total_weighted = 0.0
        for key, label, hint in HINTS:
            weight = weights.get(key, 0.0)
            score = dim_scores.get(key, 0)
            weighted = score * weight
            total_weighted += weighted
            formula_terms.append(f"{weight:.0%}×{label.split(' ')[0]}")
            bar_pct = int(score / 10 * 100)
            bar_color = "var(--bull)" if score >= 7 else ("var(--neut)" if score >= 5 else "var(--bear)")
            zero_w_note = '<br><small style="color:var(--bear)">权重=0（当前不计入合成分）</small>' if weight == 0 else ""
            rows += f"""<tr>
                <td>{label}<br><small style="color:var(--tm)">{hint}</small>{zero_w_note}</td>
                <td style="font-weight:bold;color:{bar_color}">{score:.1f}</td>
                <td>{weight:.0%}</td>
                <td style="font-weight:bold">{weighted:.2f}</td>
                <td><div style="background:var(--border);border-radius:4px;height:8px;width:100px;display:inline-block;">
                    <div style="background:{bar_color};border-radius:4px;height:8px;width:{bar_pct}px;"></div>
                </div></td>
            </tr>"""
        score_lv = "高优先级" if total_weighted >= 7.5 else ("观察名单" if total_weighted >= 6.0 else "不行动")
        weight_src_note = (
            "本表按 config.EVALUATION_WEIGHTS 重算（swarm 数据未带 dimension_weights，旧记录）"
            if weights_from_config else
            "本表权重 = 该标的当日实际合成 final_score 时用的权重（政体调整后），非置信度加权"
        )
        rows += f"""<tr style="background:var(--surface2);font-weight:bold;">
            <td><strong>综合 Opportunity Score</strong>（{weight_src_note}）</td>
            <td style="color:var(--tp);font-size:1.2em;">{total_weighted:.2f}</td>
            <td></td>
            <td style="color:var(--tp);font-size:1.2em;">{total_weighted:.2f}</td>
            <td>{score_lv}</td>
        </tr>"""
        config_mismatch_note = ""
        if not weights_from_config:
            _cfg_w = config.EVALUATION_WEIGHTS
            _drift = {k: (weights.get(k, 0.0) - _cfg_w.get(k, 0.0))
                      for k, _, _ in HINTS if abs(weights.get(k, 0.0) - _cfg_w.get(k, 0.0)) > 0.05}
            if _drift:
                _drift_txt = "、".join(f"{k} 实际{weights.get(k,0):.0%} vs config{_cfg_w.get(k,0):.0%}" for k in _drift)
                config_mismatch_note = f"""<p style="margin-top:4px;font-size:0.85em;color:var(--bear);">
                    ⚠️ 实际权重与 config.EVALUATION_WEIGHTS 偏离 >5pp：{_drift_txt}——
                    说明本次合成未直接采用 config 权重（可能经由 adapted_weights/政体调整覆盖），
                    如实展示，不代表 config 配置有误。</p>"""
        real_final = swarm.get("final_score")
        compare_note = ""
        if real_final is not None:
            compare_note = f"""<p style="margin-top:4px;font-size:0.85em;color:var(--tm);">
                蜂群实际输出的 final_score（含置信度加权，第1章展示的数字）= {float(real_final):.2f}，
                与本表差异属预期——置信度低的维度在真实合成中被打折，本表只演示权重结构。</p>"""
        return f"""
        <div class="section">
            <h2>第 2 章：五维评分明细</h2>
            <table>
                <tr><th>维度</th><th>分数</th><th>权重</th><th>加权</th><th>进度</th></tr>
                {rows}
            </table>
            <p style="margin-top:12px;font-size:0.85em;color:var(--tm);">公式：Score = {' + '.join(formula_terms)}</p>
            {compare_note}
            {config_mismatch_note}
        </div>"""

    def _ch3_scout(self, agent_details: dict) -> str:
        """第3章 ScoutBee — 聪明钱侦察"""
        ad = agent_details.get("ScoutBeeNova", {})
        if not ad:
            return ""
        details = ad.get("details", {})
        insider = details.get("insider", {})
        trades = insider.get("notable_trades", [])
        crowding = details.get("crowding_score", 0)
        # 与 _ch3_buzz 同型：键存在且为 None 时 .get 默认值不生效。ScoutBee 当前
        # 该字段健康（2026-08-14 快照 0/27 为 None），加守卫只为防同源降级把
        # BuzzBee 那个崩溃点平移过来。
        momentum = details.get("momentum_5d")
        score = ad.get("score", 0)
        direction = ad.get("direction", "neutral")
        trade_rows = ""
        for t in trades[:6]:
            shares = t.get("shares", 0)
            price = t.get("price", 0)
            amount = shares * price if price else 0
            trade_rows += f"""<tr>
                <td>{t.get('insider','')}</td>
                <td style="font-size:0.85em;color:var(--ts)">{t.get('title','')}</td>
                <td>{t.get('date','')}</td>
                <td>{shares:,.0f}</td>
                <td>{"$"+f"{price:.2f}" if price else "授予"}</td>
                <td>{"$"+f"{amount:,.0f}" if amount else "—"}</td>
            </tr>"""
        if not trade_rows:
            trade_rows = '<tr><td colspan="6" style="color:var(--tm);text-align:center">暂无近期内部人交易记录</td></tr>'
        insider_sentiment = insider.get("sentiment", "neutral")
        ins_cn = self._dir_cn(insider_sentiment)
        ins_color = self._dir_color(insider_sentiment)
        mom_color = "var(--tm)" if momentum is None else ("var(--bull)" if momentum > 0 else "var(--bear)")
        mom_txt = "—" if momentum is None else f"{momentum:+.2f}%"
        return f"""
        <div class="section">
            <h2>ScoutBee — 聪明钱侦察</h2>
            <div style="display:flex;gap:15px;flex-wrap:wrap;margin-bottom:15px;">
                <div class="stat"><div class="num" style="color:{self._dir_color(direction)}">{score:.1f}</div><div class="lbl">Signal 评分</div></div>
                <div class="stat"><div class="num" style="color:{ins_color}">{ins_cn}</div><div class="lbl">内部人情绪</div></div>
                <div class="stat"><div class="num">{crowding:.0f}</div><div class="lbl">拥挤度 /100</div></div>
                <div class="stat"><div class="num" style="color:{mom_color}">{mom_txt}</div><div class="lbl">5日动量</div></div>
            </div>
            <h3>近期内部人交易（Form 4）</h3>
            <table>
                <tr><th>内部人</th><th>职位</th><th>日期</th><th>股数</th><th>均价</th><th>金额</th></tr>
                {trade_rows}
            </table>
            <p style="margin-top:10px;font-size:0.85em;color:var(--ts);"><strong>关键判断：</strong>{ad.get('discovery','')}</p>
        </div>"""

    def _ch3_oracle(self, agent_details: dict, options: dict, current_price: float = 0) -> str:
        """第3章 OracleBee — 期权市场预期（v0.27.0：扩展为完整全链/近端/IV结构/Gamma日历视图）"""
        ad = agent_details.get("OracleBeeEcho", {})
        det = ad.get("details", {}) if ad else {}
        opts = det if det else options
        if not opts and not ad:
            return ""
        # 防 None：dict 里 key 存在但值为 None 时，.get() 返回 None，格式化会崩
        def _safe(v, default=0):
            return default if v is None else v

        # ── v0.45.42：缺失值不再兜底成 0 ──────────────────────────────
        # 旧实现对期权指标一律 `_safe(..., 0)`，于是 8/26 yfinance 全线返回空时，
        # IV Rank 从 None 变成 **0.0%** 渲染上墙。0.0% 的语义不是"没数据"，
        # 是"IV 处于历史区间最低点"——一个强烈且完全虚假的做多波动率信号。
        # 判据（CLAUDE.md 安全默认值）：这个默认值会不会让下游误以为掌握了信息。
        # 同文件 _rv_30d 已因同样理由单独豁免过 _safe（见 `# 可能是 None —— 不要 _safe`），
        # 这里把该豁免推广到全部期权指标：缺失一律渲染 "—"。
        # _NA 原先只在下方 `if (_iv_term ...)` 块里赋值（12 缩进）。_fmt 在该块外也要用，
        # 条件为假时会 NameError，故提到函数顶层；块内那次赋值是同值重绑定，无副作用。
        _NA = '<span class="na" title="数据不可用">—</span>'

        def _fmt(v, spec="{:.1f}", suffix=""):
            """有值按 spec 渲染，None 渲染成 —（绝不代入 0）"""
            return _NA if v is None else (spec.format(v) + suffix)

        score = _safe(ad.get("score", 0)) if ad else 0
        direction = ad.get("direction", "neutral") if ad else "neutral"
        iv_rank = opts.get("iv_rank")
        iv_curr = opts.get("iv_current", opts.get("iv_curr"))
        pc = opts.get("put_call_ratio")
        oi = opts.get("total_oi")
        gex = opts.get("gamma_squeeze_risk") or "—"
        flow = opts.get("flow_direction") or opts.get("options_score") or "—"
        skew = opts.get("iv_skew_ratio", opts.get("iv_skew"))
        # v0.27.1 bug fix: dict.get() 不会用默认值若 key 存在但 value=None；用 `or` 保护
        unusual = opts.get("unusual_activity") or []
        key_levels = opts.get("key_levels") or {}
        support = key_levels.get("support") or []
        resist = key_levels.get("resistance") or []
        pc_color = ("var(--neut)" if pc is None else
                    "var(--bull)" if pc < 0.8 else ("var(--bear)" if pc > 1.2 else "var(--neut)"))
        unusual_rows = ""
        for u in unusual[:5]:
            bullish = u.get("bullish", True)
            emo = ('<span style="color:var(--bull)">▲</span>' if bullish else '<span style="color:var(--bear)">▼</span>')
            unusual_rows += f"<li>{emo} {u.get('type','').replace('_',' ')} Strike ${u.get('strike',0):.0f} × {u.get('volume',0):,.0f}</li>"
        support_txt = " | ".join(f"${s.get('strike',0):.0f}(OI:{s.get('oi',0):,})" for s in support[:3])
        resist_txt = " | ".join(f"${r.get('strike',0):.0f}(OI:{r.get('oi',0):,})" for r in resist[:3])

        # ── v0.27.0：近端 Max Pain（来自 oracle.max_pain，OracleBee 基于近端 3-4 个到期日聚合）──
        # 数据格式：dict `{"max_pain": 225.0, "distance_pct": -1.2, "summary": "..."}`
        # 或纯数值 float 兜底
        _near_max_pain_html = ""
        _near_mp_raw = (det or {}).get("max_pain") if isinstance(det, dict) else None
        _near_mp_val = None
        _near_mp_dist = None
        _near_mp_window = None
        if isinstance(_near_mp_raw, dict):
            _v = _near_mp_raw.get("max_pain")
            if isinstance(_v, (int, float)) and _v > 0:
                _near_mp_val = float(_v)
                _d = _near_mp_raw.get("distance_pct")
                if isinstance(_d, (int, float)):
                    _near_mp_dist = float(_d)
                _w = _near_mp_raw.get("window_days")
                if isinstance(_w, int) and _w > 0:
                    _near_mp_window = _w
        elif isinstance(_near_mp_raw, (int, float)) and _near_mp_raw > 0:
            _near_mp_val = float(_near_mp_raw)
        if _near_mp_val is not None:
            _dist_txt = f"（距现价 {_near_mp_dist:+.1f}%）" if _near_mp_dist is not None else ""
            # v0.45.188：口径写进标签。「近端」此前在代码里没有定义，实际取的是
            # 「最早的 DTE≥7 那一个到期日」——标签说近端、数字不是，用户就是从
            # 「怎么两天差了 25 美元」问上来的。window_days 缺失＝旧口径记录，
            # 此时不假装知道窗口。
            _win_txt = f"≤{_near_mp_window}天 " if _near_mp_window else ""
            _near_max_pain_html = (
                f'<div class="stat"><div class="num" style="color:var(--tp)">${_near_mp_val:.0f}</div>'
                f'<div class="lbl">{_win_txt}近端磁吸目标价{_dist_txt}</div></div>'
            )

        # ── v0.27.0：全链 OI 结构卡片 ──────────────────────────────────────
        _full_chain = opts.get("full_chain_oi") or {}
        _full_oi_html = ""
        if isinstance(_full_chain, dict) and _full_chain:
            _fc_max_pain = _safe(_full_chain.get("max_pain"), 0)
            _fc_pc = _safe(_full_chain.get("full_pc_ratio"), 0)
            _fc_call_oi = _safe(_full_chain.get("total_call_oi"), 0)
            _fc_put_oi = _safe(_full_chain.get("total_put_oi"), 0)
            _fc_expiry_count = _safe(_full_chain.get("expiry_count"), 0)
            _fc_total = _fc_call_oi + _fc_put_oi

            def _wall_rows(walls: list, is_call: bool, cur_p: float) -> str:
                """渲染 Top OI 墙行：strike / OI / 距现价% / 主导到期日 badge"""
                rows = ""
                for w in (walls or [])[:5]:
                    if not isinstance(w, dict):
                        continue
                    sk = w.get("strike")
                    oi_v = w.get("oi") or 0
                    if sk is None:
                        continue
                    try:
                        sk_f = float(sk)
                    except (ValueError, TypeError):
                        continue
                    if cur_p and cur_p > 0:
                        pct = ((sk_f - cur_p) / cur_p) * 100
                        pct_txt = f"{pct:+.1f}%"
                    else:
                        pct_txt = "—"
                    dom_exp = w.get("dom_exp") or w.get("position") or ""
                    badge = f'<span class="mono" style="border:1px solid var(--border);color:var(--tm);font-size:9.5px;padding:0 5px;border-radius:2px;margin-left:5px;">{dom_exp}</span>' if dom_exp else ""
                    color = "var(--bear)" if is_call else "var(--bull)"
                    rows += (
                        f'<tr><td style="color:{color};font-weight:600;">${sk_f:.0f}</td>'
                        f'<td>{int(oi_v):,}</td>'
                        f'<td style="color:var(--ts);">{pct_txt}{badge}</td></tr>'
                    )
                return rows

            _call_walls_html = _wall_rows(_full_chain.get("top_call_oi", []), True, current_price)
            _put_walls_html = _wall_rows(_full_chain.get("top_put_oi", []), False, current_price)
            _fc_pc_color = "var(--bull)" if _fc_pc < 0.8 else ("var(--bear)" if _fc_pc > 1.2 else "var(--neut)")

            _full_oi_html = f"""
            <h3>全链 OI 结构（{_fc_expiry_count} 个到期日聚合，含 LEAPS 远期参考）</h3>
            <div class="grid-4" style="margin-bottom:12px;">
                <div class="stat"><div class="num" style="color:var(--tm)">${_fc_max_pain:.0f}</div><div class="lbl">全链 Max Pain（远期参考）</div></div>
                <div class="stat"><div class="num" style="color:{_fc_pc_color}">{_fc_pc:.2f}</div><div class="lbl">全链 P/C Ratio</div></div>
                <div class="stat"><div class="num">{_fc_total:,}</div><div class="lbl">全链总 OI</div></div>
                <div class="stat"><div class="num">{_fc_call_oi:,} / {_fc_put_oi:,}</div><div class="lbl">Call / Put OI</div></div>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:12px;">
                <div>
                    <strong style="color:var(--bear);">Top5 Call 阻力墙（全链）</strong>
                    <table style="font-size:0.88em;margin-top:6px;">
                        <tr><th>行权价</th><th>OI</th><th>距现价 / 主导到期</th></tr>
                        {_call_walls_html or '<tr><td colspan="3" style="color:var(--tm);text-align:center;">—</td></tr>'}
                    </table>
                </div>
                <div>
                    <strong style="color:var(--bull);">Top5 Put 支撑墙（全链）</strong>
                    <table style="font-size:0.88em;margin-top:6px;">
                        <tr><th>行权价</th><th>OI</th><th>距现价 / 主导到期</th></tr>
                        {_put_walls_html or '<tr><td colspan="3" style="color:var(--tm);text-align:center;">—</td></tr>'}
                    </table>
                </div>
            </div>
            """

        # ── v0.27.0：近端 30 天 OI 墙现场聚合（call_exp_oi/put_exp_oi 矩阵）──
        _near_walls_html = ""
        _call_exp_oi = (_full_chain or {}).get("call_exp_oi") or {}
        _put_exp_oi = (_full_chain or {}).get("put_exp_oi") or {}
        if isinstance(_call_exp_oi, dict) and _call_exp_oi and current_price and current_price > 0:
            from datetime import datetime as _dt_oi
            _now_oi = _dt_oi.now()
            _NEAR_DAYS = 30

            def _aggregate_near(exp_map_dict):
                out = {}
                for sk, exps in (exp_map_dict or {}).items():
                    if not isinstance(exps, dict):
                        continue
                    try:
                        strike_f = float(sk)
                    except (ValueError, TypeError):
                        continue
                    total_near = 0
                    for exp_str, oi_val in exps.items():
                        try:
                            exp_dt = _dt_oi.strptime(exp_str, "%Y-%m-%d")
                            days_to = (exp_dt - _now_oi).days
                        except (ValueError, TypeError):
                            continue
                        if 0 <= days_to <= _NEAR_DAYS:
                            try:
                                total_near += int(oi_val or 0)
                            except (ValueError, TypeError):
                                continue
                    if total_near > 0:
                        out[strike_f] = total_near
                return out

            _near_calls = _aggregate_near(_call_exp_oi)
            _near_puts = _aggregate_near(_put_exp_oi)

            def _top_walls_near(near_dict, is_call):
                """从 {strike: total_oi} 取 Top3，过滤 OTM 方向（call: strike > price; put: strike < price）"""
                if not near_dict:
                    return []
                items = sorted(near_dict.items(), key=lambda x: -x[1])
                out = []
                for sk, oi_v in items:
                    pct = (sk - current_price) / current_price * 100
                    if is_call and pct < -1:  # call 墙取现价附近 + 上方
                        continue
                    if (not is_call) and pct > 1:  # put 墙取现价附近 + 下方
                        continue
                    out.append({"strike": sk, "oi": oi_v, "pct": pct})
                    if len(out) >= 3:
                        break
                return out

            _near_call_top = _top_walls_near(_near_calls, True)
            _near_put_top = _top_walls_near(_near_puts, False)

            # 近端 P/C
            _near_call_sum = sum(_near_calls.values())
            _near_put_sum = sum(_near_puts.values())
            _near_pc_str = f"{(_near_put_sum / _near_call_sum):.2f}" if _near_call_sum > 0 else "—"

            if _near_call_top or _near_put_top:
                def _row_near(walls, is_call):
                    color = "var(--bear)" if is_call else "var(--bull)"
                    if not walls:
                        return '<tr><td colspan="3" style="color:var(--tm);text-align:center;">—</td></tr>'
                    return "".join(
                        f'<tr><td style="color:{color};font-weight:600;">${w["strike"]:.0f}</td>'
                        f'<td>{int(w["oi"]):,}</td>'
                        f'<td style="color:var(--ts);">{w["pct"]:+.1f}%</td></tr>'
                        for w in walls
                    )
                _near_walls_html = f"""
                <h3>近 30 天到期 OI 墙（现场聚合，磁吸效应最强）</h3>
                <div style="background:var(--surface2);border-left:3px solid var(--neut);padding:8px 12px;margin-bottom:10px;font-size:0.88em;color:var(--neut);">
                    近端 P/C = <strong>{_near_pc_str}</strong> | Call OI {_near_call_sum:,} | Put OI {_near_put_sum:,}
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:12px;">
                    <div>
                        <strong style="color:var(--bear);">近端 Top3 Call 阻力</strong>
                        <table style="font-size:0.88em;margin-top:6px;">
                            <tr><th>行权价</th><th>OI</th><th>距现价</th></tr>
                            {_row_near(_near_call_top, True)}
                        </table>
                    </div>
                    <div>
                        <strong style="color:var(--bull);">近端 Top3 Put 支撑</strong>
                        <table style="font-size:0.88em;margin-top:6px;">
                            <tr><th>行权价</th><th>OI</th><th>距现价</th></tr>
                            {_row_near(_near_put_top, False)}
                        </table>
                    </div>
                </div>
                """

        # ── v0.27.0：IV 期限结构 + IV-RV 价差 ───────────────────────────
        # v0.45.4 诚实渲染：**缺数一律显示「—」，绝不兜成 0**。
        # 旧实现 `_safe(v, 0)` 把上游诚实返回的 None 兜成 0.0，页面显示
        # 「0.0% / 0.0%」「+0.0pp」，与"真实测得为 0"完全同形（MEMORY「静默中性化」）。
        _iv_term = opts.get("iv_term_structure") or {}
        _iv_rv_signal = opts.get("iv_rv_signal", "")
        _iv_rv_spread = opts.get("iv_rv_spread")      # 可能是 None —— 不要 _safe
        _rv_30d = opts.get("rv_30d")                  # 可能是 None —— 不要 _safe
        _iv_struct_html = ""
        if (isinstance(_iv_term, dict) and _iv_term) or _iv_rv_signal:
            _NA = '<span class="na" title="数据不可用">—</span>'

            # 期限结构：data_available 缺省时回退到"front/back 都在"作为判据，
            # 兼容旧快照 JSON（v0.45.4 之前没有这个键）。
            _ts_front = (_iv_term or {}).get("front_iv")
            _ts_back = (_iv_term or {}).get("back_iv")
            _ts_ok = bool((_iv_term or {}).get("data_available",
                                               _ts_front is not None and _ts_back is not None))
            _shape = (_iv_term or {}).get("shape") or (_iv_term or {}).get("term_shape") or "unknown"
            _ts_err = str((_iv_term or {}).get("error") or "")
            _ts_src = str((_iv_term or {}).get("source") or "")

            if _ts_ok:
                _shape_color = {"contango": "var(--bull)", "backwardation": "var(--bear)",
                                "flat": "var(--neut)"}.get(str(_shape).lower(), "var(--ts)")
                _shape_cell = f'<div class="num" style="color:{_shape_color};font-size:1.15em;">{str(_shape).upper()}</div>'
                _iv_pair_cell = f'<div class="num">{_ts_front:.1f}% / {_ts_back:.1f}%</div>'
                _shape_cn = {"contango": "Contango（远月>近月，市场预期平稳）",
                             "backwardation": "Backwardation（近月>远月，短期不确定性高）",
                             "flat": "Flat（期限结构平坦）"}.get(str(_shape).lower(), str(_shape))
                if _ts_src:
                    _shape_cn += f'<span class="src-tag">{_ts_src}</span>'
            else:
                _shape_cell = f'<div class="num" style="font-size:1.15em;">{_NA}</div>'
                _iv_pair_cell = f'<div class="num">{_NA}</div>'
                _shape_cn = ('<span class="na-note">数据不可用'
                             + (f'：{_html_escape(_ts_err[:120])}' if _ts_err else '')
                             + '</span>')

            # IV-RV：None 与 0.0 必须可区分
            if _iv_rv_spread is None:
                _spread_cell = f'<div class="num">{_NA}</div>'
            else:
                _iv_rv_color = ("var(--bear)" if _iv_rv_spread > 10
                                else "var(--bull)" if _iv_rv_spread < -5 else "var(--ts)")
                _spread_cell = f'<div class="num" style="color:{_iv_rv_color}">{_iv_rv_spread:+.1f}pp</div>'
            _rv_cell = (f'<div class="num">{_NA}</div>' if _rv_30d is None
                        else f'<div class="num">{_rv_30d:.1f}%</div>')

            _rv_err = str((opts.get("iv_rv_detail") or {}).get("error") or "")
            if _iv_rv_signal and _iv_rv_signal != "unknown":
                _sig_txt = _html_escape(_iv_rv_signal)
            else:
                _sig_txt = ('<span class="na-note">数据不可用'
                            + (f'：{_html_escape(_rv_err[:120])}' if _rv_err else '')
                            + '</span>')

            _iv_struct_html = f"""
            <h3>IV 期限结构 · IV-RV 价差</h3>
            <div class="grid-4" style="margin-bottom:12px;">
                <div class="stat">{_shape_cell}<div class="lbl">期限结构形态</div></div>
                <div class="stat">{_iv_pair_cell}<div class="lbl">近月 IV / 远月 IV</div></div>
                <div class="stat">{_spread_cell}<div class="lbl">IV-RV 价差</div></div>
                <div class="stat">{_rv_cell}<div class="lbl">30 日实现波动率</div></div>
            </div>
            <p class="note">
                <strong>形态解读：</strong>{_shape_cn}<br/>
                <strong>IV-RV 信号：</strong>{_sig_txt}
            </p>
            """

        # ── v0.27.0：Gamma 到期日历 ─────────────────────────────────────
        _gamma_cal = opts.get("gamma_calendar") or {}
        _gamma_cal_html = ""
        if isinstance(_gamma_cal, dict) and _gamma_cal:
            _pin_risk = _gamma_cal.get("pin_risk") or _gamma_cal.get("pin_strike")
            # v0.41.3: next_major_expiry / oi_concentration_pct 是从未存在过的字段
            # （calculate_gamma_expiry_calendar 产出 expiry_oi 列表，按 OI 降序），
            # 这两格自上线起恒为 — / 0.0%。改为从 expiry_oi 实际推导。
            _exp_rows = _gamma_cal.get("expiry_oi") or []
            _next_exp = (_exp_rows[0].get("expiry") if _exp_rows else None) or "—"
            _charm = _gamma_cal.get("charm_direction") or _gamma_cal.get("charm") or "—"
            _total_all = sum(r.get("total_oi", 0) for r in _exp_rows)
            _oi_concentration = (_exp_rows[0].get("total_oi", 0) / _total_all * 100) if (_exp_rows and _total_all > 0) else 0.0
            _pin_txt = f"${float(_pin_risk):.0f}" if isinstance(_pin_risk, (int, float)) and _pin_risk else "—"
            _gamma_cal_html = f"""
            <h3>Gamma 到期日历</h3>
            <table style="font-size:0.88em;margin-bottom:10px;">
                <tr><th>指标</th><th>数值</th><th>含义</th></tr>
                <tr><td>下一主要到期日</td><td>{_next_exp}</td><td>资金面集中点</td></tr>
                <tr><td>Pin Risk 行权价</td><td>{_pin_txt}</td><td>到期日可能磁吸到此价位</td></tr>
                <tr><td>OI 集中度</td><td>{_oi_concentration:.1f}%</td><td>下一到期占全链 OI 比例</td></tr>
                <tr><td>Charm 方向</td><td>{_charm}</td><td>时间衰减对 Delta 的影响</td></tr>
            </table>
            """

        # ── 头部 stat 卡片：注入近端磁吸目标价（如可用）────────────────────
        _hero_cards = (
            f'<div class="stat"><div class="num" style="color:{self._dir_color(direction)}">{score:.1f}</div><div class="lbl">Odds 评分</div></div>'
            f'<div class="stat"><div class="num" style="color:{pc_color}">{_fmt(pc, "{:.2f}")}</div><div class="lbl">近端 P/C Ratio</div></div>'
            f'<div class="stat"><div class="num">{_fmt(iv_rank, "{:.1f}", "%")}</div><div class="lbl">IV Rank</div></div>'
        )
        if _near_max_pain_html:
            _hero_cards += _near_max_pain_html
        else:
            _hero_cards += f'<div class="stat"><div class="num">{_fmt(iv_curr, "{:.1f}", "%")}</div><div class="lbl">当前 IV</div></div>'

        return f"""
        <div class="section">
            <h2>OracleBee — 期权市场预期</h2>
            <div class="grid-4" style="margin-bottom:15px;">
                {_hero_cards}
            </div>
            <table style="margin-bottom:12px;">
                <tr><th>指标</th><th>数值</th><th>信号</th></tr>
                <tr><td>Gamma 压榨风险</td><td>{gex}</td><td>{DOT_NEUT + "高" if str(gex).lower() in ("high","很高") else DOT_BULL + "可控"}</td></tr>
                <tr><td>期权流方向</td><td>{flow}</td><td>{DOT_BULL + "看多" if str(flow).lower() in ("bullish","看多") else (DOT_BEAR + "看空" if str(flow).lower() in ("bearish","看空") else "—")}</td></tr>
                <tr><td>IV 偏斜比</td><td>{_fmt(skew, "{:.2f}")}</td><td>{"—" if skew is None else (DOT_NEUT + "看跌溢价" if skew > 1.2 else DOT_BULL + "正常")}</td></tr>
                <tr><td>近端总持仓量</td><td>{_fmt(oi, "{:,.0f}")}</td><td>—</td></tr>
            </table>
            {_near_walls_html}
            {_full_oi_html}
            {_iv_struct_html}
            {_gamma_cal_html}
            {f'<h3>异常期权活动 Top5</h3><ul>{unusual_rows}</ul>' if unusual_rows else ''}
            {f'<h3>近端关键价位（OracleBee key_levels）</h3><p>支撑：{support_txt or "—"}</p><p>压力：{resist_txt or "—"}</p>' if (support_txt or resist_txt) else ''}
            <p style="margin-top:10px;font-size:0.85em;color:var(--ts);"><strong>关键判断：</strong>{ad.get('discovery','') if ad else ''}</p>
        </div>"""

    def _ch3_chronos(self, agent_details: dict) -> str:
        """第3章 ChronosBee — 催化剂时间线（含 ASCII 时间轴）"""
        ad = agent_details.get("ChronosBeeHorizon", {})
        if not ad:
            return ""
        det = ad.get("details", {})
        catalysts = det.get("catalysts", det.get("catalysts_found", []))
        analyst = det.get("analyst_targets", {})
        score = ad.get("score", 0)
        direction = ad.get("direction", "neutral")
        # v0.24.3 修复：渲染前对 cached catalysts 做过期过滤
        # 旧 .swarm_results_*.json 里可能有 days_until<0 的过期事件
        # 过滤策略：递归用真实 date 重算 days_until，过滤 < -3（容忍 3 天延迟，如周末跨过）
        from datetime import datetime as _dt_filter
        _now = _dt_filter.now()
        catalysts_clean = []
        for c in catalysts:
            if not isinstance(c, dict):
                continue
            try:
                _ev_dt = _dt_filter.strptime(c.get("date", ""), "%Y-%m-%d")
                _real_days = (_ev_dt - _now).days
            except (ValueError, TypeError):
                continue
            if _real_days < -3:
                continue  # 过期 > 3 天的丢弃
            # 用真实 days_until 覆盖（旧 cached 可能是 0 硬编码或负值）
            c2 = dict(c)
            c2["days_until"] = _real_days
            catalysts_clean.append(c2)
        catalysts = catalysts_clean
        # 按 days_until 排序，取前 5 个（未来事件优先；负值绝对值小的次之）
        cats_sorted = sorted(
            catalysts,
            key=lambda x: (x.get("days_until", 999) < 0, abs(x.get("days_until", 999)))
        )[:5]
        # ASCII 时间轴
        timeline_html = ""
        if cats_sorted:
            TOTAL_WIDTH = 80  # 字符宽度
            max_days = max((abs(c.get("days_until", 0)) for c in cats_sorted), default=30) or 30
            max_days = max(max_days, 1)
            labels_top = "今天".ljust(6)
            labels_bot = "0天".ljust(6)
            line = "●"
            for c in cats_sorted:
                days = abs(c.get("days_until", 0))
                pos = int(days / max_days * (TOTAL_WIDTH - 6))
                name = c.get("event", "")[:8]
                gap = max(1, pos - len(line))
                labels_top += " " * gap + name[:8].ljust(10)
                labels_bot += " " * gap + f"{days}天".ljust(10)
                line += "─" * gap + "●"
            sev_colors = {"critical": "var(--bear)", "high": "var(--acc)", "medium": "var(--neut)", "low": "var(--bull)"}
            cat_rows = ""
            for c in cats_sorted:
                sev = c.get("severity", "medium")
                sev_color = sev_colors.get(sev, "var(--tm)")
                days = c.get("days_until", 0)
                days_txt = f"{days}天后" if days >= 0 else f"{abs(days)}天前"
                cat_rows += f"""<tr>
                    <td style="color:{sev_color};font-weight:bold">{c.get('event','')}</td>
                    <td>{c.get('date','')}</td>
                    <td>{days_txt}</td>
                    <td><span style="color:{sev_color};border:1px solid currentColor;border-radius:2px;padding:1px 7px;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:10px;font-weight:600;letter-spacing:.5px;">{sev}</span></td>
                </tr>"""
            timeline_html = f"""
            <div style="border:1px solid var(--border);border-radius:2px;padding:15px;margin:12px 0;overflow-x:auto;">
                <pre style="font-family:monospace;font-size:0.8em;color:var(--tp);line-height:1.8;margin:0">   {labels_top}
   {labels_bot}
    │{"─" * (len(line)-1)}
    {line}</pre>
            </div>
            <table style="margin-top:10px;">
                <tr><th>事件</th><th>日期</th><th>距今</th><th>重要性</th></tr>
                {cat_rows}
            </table>"""
        # 分析师目标价
        analyst_html = ""
        # v0.45.54：全 0 时不渲染「$0.00 目标均价 / $0~$0 目标区间 / +0.0% 潜在涨幅」。
        # $0.00 单看荒谬，但它出现在四张并排 stat 卡里、和「检测到 N 个催化剂」同屏，
        # 读者更可能理解成「分析师没给目标价」而不是「取数挂了」—— 而这两者
        # 后续动作完全不同。
        _a_nums = {k: analyst.get(k) for k in
                   ("current_price", "target_mean", "target_low", "target_high")}
        _a_ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
                    for v in _a_nums.values())
        if analyst and not _a_ok:
            _log.debug("[_ch3_chronos] 分析师目标价不完整 %s，跳过该区块", _a_nums)
            analyst_html = ('<h3>分析师目标价</h3>'
                            '<p style="font-size:.9em;color:var(--tm);">'
                            '本次未取到分析师目标价。</p>')
        elif analyst:
            curr = _a_nums["current_price"]
            mean = _a_nums["target_mean"]
            low = _a_nums["target_low"]
            high = _a_nums["target_high"]
            _up_raw = analyst.get("upside_pct")
            upside = (_up_raw if isinstance(_up_raw, (int, float))
                      else (mean - curr) / curr * 100)
            upside_color = "var(--bull)" if upside > 0 else "var(--bear)"
            analyst_html = f"""
            <h3>分析师目标价</h3>
            <div class="grid-4">
                <div class="stat"><div class="num">${curr:.2f}</div><div class="lbl">当前价</div></div>
                <div class="stat"><div class="num">${mean:.2f}</div><div class="lbl">目标均价</div></div>
                <div class="stat"><div class="num">${low:.0f}~${high:.0f}</div><div class="lbl">目标区间</div></div>
                <div class="stat"><div class="num" style="color:{upside_color}">{upside:+.1f}%</div><div class="lbl">潜在涨幅</div></div>
            </div>"""
        return f"""
        <div class="section">
            <h2>ChronosBee — 催化剂时间线</h2>
            <div style="margin-bottom:12px;">
                <span class="stat" style="display:inline-block;margin-right:10px;">
                    <span class="num" style="color:{self._dir_color(direction)}">{score:.1f}</span>
                    <span class="lbl"> Catalyst 评分</span>
                </span>
                <span style="color:var(--ts);font-size:0.9em">检测到 {len(catalysts)} 个催化剂</span>
            </div>
            {timeline_html if timeline_html else '<p style="color:var(--tm)">暂无催化剂数据</p>'}
            {analyst_html}
            <p style="margin-top:10px;font-size:0.85em;color:var(--ts);"><strong>关键判断：</strong>{ad.get('discovery','')}</p>
        </div>"""

    def _ch3_buzz(self, agent_details: dict) -> str:
        """第3章 BuzzBee — 情绪与叙事"""
        ad = agent_details.get("BuzzBeeWhisper", {})
        if not ad:
            return ""
        det = ad.get("details", {})
        score = ad.get("score", 0)
        direction = ad.get("direction", "neutral")
        # v0.45.54：兜底 50 读作「舆情样本里正面占 50%，多空完全均衡」。
        # 同一个 grid-4 里紧邻的 5 日动量与成交量比在缺数时都正确渲染「—」
        # （见 v0.45.42 的整段注释），唯独这一项还在冒充读数 —— 同屏不一致。
        _sp = det.get("sentiment_pct", det.get("sentiment_score"))
        sentiment_pct = _sp if isinstance(_sp, (int, float)) and not isinstance(_sp, bool) else None
        # v0.43.23: 上游 BuzzBee 对缺价格/成交量的降级源刻意写入 None（见 buzz_bee.py
        # P0-2：不拿 0 冒充"无动量"、不拿 1 冒充"正常量"）。而 .get(k, 默认) 只在**键
        # 缺失**时用默认值——键存在且为 None 时默认值形同虚设，下面的 `> 0` 与
        # `:+.2f` 都会抛 TypeError。实测 2026-08-14 快照 27/28 只该字段为 None，
        # 这正是 7/15 起每日 ML 报告 0~1/12 成功的唯一原因。
        momentum = det.get("momentum_5d")
        vol_ratio = det.get("volume_ratio")
        reddit = det.get("reddit", {})
        fear_greed = det.get("fear_greed_index", det.get("components", {}).get("fear_greed", None))
        sent_color = ("var(--tm)" if sentiment_pct is None
                      else ("var(--bull)" if sentiment_pct > 60
                            else ("var(--bear)" if sentiment_pct < 40 else "var(--neut)")))
        # 预先算好文案 —— 拼接链里不放条件逻辑
        _sent_txt = f"{sentiment_pct:.0f}%" if sentiment_pct is not None else "—"
        # 缺数显示"—"并用中性灰，绝不用 0 / 1 顶替（项目硬规则：不编数据）
        mom_color = "var(--tm)" if momentum is None else ("var(--bull)" if momentum > 0 else "var(--bear)")
        mom_txt = "—" if momentum is None else f"{momentum:+.2f}%"
        vol_txt = "—" if vol_ratio is None else f"{vol_ratio:.2f}×"
        fg_text = ""
        if fear_greed is not None:
            fg_label = "极度恐惧" if fear_greed < 25 else ("恐惧" if fear_greed < 45 else ("中性" if fear_greed < 55 else ("贪婪" if fear_greed < 75 else "极度贪婪")))
            fg_color = "var(--bull)" if fear_greed > 55 else ("var(--bear)" if fear_greed < 45 else "var(--neut)")
            fg_text = f'<div class="stat"><div class="num" style="color:{fg_color}">{fear_greed}</div><div class="lbl">恐贪指数 ({fg_label})</div></div>'
        reddit_html = ""
        if reddit:
            # ⚠️ 不能写 `.get(k, "—")`：键存在但值为 None 时默认值不生效，
            # 页面会渲染出字面量「第None名」（8/24 存档 12 份里 7 份如此）。
            _rk = reddit.get("rank")
            _mt = reddit.get("mentions")
            _bz = reddit.get("buzz")
            _rk_txt = f"第{_rk}名" if _rk is not None else _NA_SPAN
            reddit_html = f"""<p style="margin-top:10px;">Reddit 热度：<strong>{_rk_txt}</strong> | 提及量：<strong>{_mt if _mt is not None else _NA_SPAN}</strong> | 状态：<strong>{_bz or _NA_SPAN}</strong></p>"""
        # 叙事列表
        disc = ad.get("discovery", "")
        bullets = [b.strip() for b in disc.split("|") if b.strip()] if disc else []
        bullets_html = "".join(f"<li>{b}</li>" for i, b in enumerate(bullets[:5]))
        return f"""
        <div class="section">
            <h2>BuzzBee — 情绪与叙事</h2>
            <div class="grid-4" style="margin-bottom:15px;">
                <div class="stat"><div class="num" style="color:{self._dir_color(direction)}">{score:.1f}</div><div class="lbl">Sentiment 评分</div></div>
                <div class="stat"><div class="num" style="color:{sent_color}">{_sent_txt}</div><div class="lbl">正面情绪占比</div></div>
                <div class="stat"><div class="num" style="color:{mom_color}">{mom_txt}</div><div class="lbl">5日动量</div></div>
                <div class="stat"><div class="num">{vol_txt}</div><div class="lbl">成交量比</div></div>
                {fg_text}
            </div>
            {reddit_html}
            {f'<h3>主流叙事</h3><ul style="margin-top:8px">{bullets_html}</ul>' if bullets_html else ''}
        </div>"""

    def _ch3_rival(self, analysis: dict) -> str:
        """第3章 RivalBee — 竞争格局"""
        ind = analysis.get("industry_comparison", {})
        if not ind:
            return ""
        advantages = ind.get("competitive_advantages", [])
        threats = ind.get("competitive_threats", [])
        competitors = ind.get("competitors", [])
        position = ind.get("position", "—")
        strength = ind.get("comparative_strength", 0)
        industry = ind.get("industry", "—")
        strength_color = "var(--bull)" if strength >= 70 else ("var(--neut)" if strength >= 40 else "var(--bear)")
        adv_li = "".join(f'<li>{DOT_BULL}{a}</li>' for a in advantages[:5])
        thr_li = "".join(f'<li>{DOT_NEUT}{t}</li>' for t in threats[:5])
        comp_tags = " ".join(f'<span class="mono" style="border:1px solid var(--border);border-radius:2px;padding:2px 8px;font-size:11px;color:var(--ts);">{c}</span>' for c in competitors[:5])
        return f"""
        <div class="section">
            <h2>RivalBee — 竞争格局</h2>
            <div class="grid-4" style="margin-bottom:15px;">
                <div class="stat"><div class="num">{industry}</div><div class="lbl">行业</div></div>
                <div class="stat"><div class="num">{position}</div><div class="lbl">市场地位</div></div>
                <div class="stat"><div class="num" style="color:{strength_color}">{strength}</div><div class="lbl">竞争实力 /100</div></div>
                <div class="stat"><div class="num">{len(competitors)}</div><div class="lbl">主要竞争对手</div></div>
            </div>
            {f'<p>竞争对手：{comp_tags}</p>' if comp_tags else ''}
            <div class="grid-2" style="margin-top:15px;">
                <div><h3 style="color:var(--bull);">护城河优势</h3><ul>{adv_li}</ul></div>
                <div><h3 style="color:var(--bear);">竞争威胁</h3><ul>{thr_li}</ul></div>
            </div>
        </div>"""

    def _ch3_guard(self, agent_details: dict, swarm: dict) -> str:
        """第3章 GuardBee — 交叉验证（含信号共振矩阵）"""
        ad = agent_details.get("GuardBeeSentinel", {})
        if not ad and not swarm:
            return ""
        det = ad.get("details", {}) if ad else {}
        resonance = det.get("resonance", swarm.get("resonance", {}))
        consistency = det.get("consistency", 0)
        adj_factor = det.get("adjustment_factor", 1.0)
        conflict = swarm.get("conflict_info", {})
        conflict_level = conflict.get("conflict_level", "—")
        score = ad.get("score", 0) if ad else 0
        # 信号共振矩阵
        DIMS = ["catalyst", "signal", "odds", "sentiment", "risk_adj"]
        DIM_CN = {"catalyst": "催化剂", "signal": "内部人", "odds": "期权", "sentiment": "情绪", "risk_adj": "风控"}
        resonant_dims = set(resonance.get("resonant_dimensions", []))
        direction = swarm.get("direction", "neutral")
        # 行=源维度, 列=目标维度, 若都在resonant_dims → ✅, 否则根据conflict判断
        header = "<tr><th></th>" + "".join(f"<th>{DIM_CN.get(d,d)}</th>" for d in DIMS) + "</tr>"
        matrix_rows = ""
        for row_dim in DIMS:
            row_cells = f"<td><strong>{DIM_CN.get(row_dim,row_dim)}</strong></td>"
            for col_dim in DIMS:
                if row_dim == col_dim:
                    row_cells += "<td style='color:var(--tm);text-align:center'>—</td>"
                elif row_dim in resonant_dims and col_dim in resonant_dims:
                    row_cells += "<td style='text-align:center;color:var(--bull)'>●</td>"
                elif conflict_level in ("high", "severe") and (row_dim not in resonant_dims or col_dim not in resonant_dims):
                    row_cells += "<td style='text-align:center;color:var(--neut)'>◐</td>"
                else:
                    row_cells += "<td style='text-align:center;color:var(--tm)'>—</td>"
            matrix_rows += f"<tr>{row_cells}</tr>"
        conflict_cn = {"low": "低", "moderate": "中", "high": "高", "severe": "严重"}.get(conflict_level, conflict_level)
        return f"""
        <div class="section">
            <h2>GuardBee — 交叉验证</h2>
            <div class="grid-4" style="margin-bottom:15px;">
                <div class="stat"><div class="num" style="color:{self._dir_color(direction)}">{score:.1f}</div><div class="lbl">RiskAdj 评分</div></div>
                <div class="stat"><div class="num">{consistency:.0%}</div><div class="lbl">信号一致性</div></div>
                <div class="stat"><div class="num">{adj_factor:.2f}×</div><div class="lbl">调整系数</div></div>
                <div class="stat"><div class="num">{conflict_cn}</div><div class="lbl">冲突等级</div></div>
            </div>
            <h3>信号共振矩阵</h3>
            <div style="overflow-x:auto;">
                <table style="min-width:400px;">
                    {header}
                    {matrix_rows}
                </table>
            </div>
            <p style="margin-top:8px;font-size:0.82em;color:var(--tm);"><span style="color:var(--bull)">●</span> 同向共振 &nbsp;·&nbsp; <span style="color:var(--neut)">◐</span> 存在冲突 &nbsp;·&nbsp; — 中性/无关</p>
            <p style="margin-top:10px;font-size:0.85em;color:var(--ts);"><strong>共振结论：</strong>
                {resonance.get('supporting_agents', 0)} 个 Agent 同向（{', '.join(resonant_dims)}），
                置信度提升 {resonance.get('confidence_boost', 0)}%
            </p>
            <p style="font-size:0.85em;color:var(--ts);"><strong>关键判断：</strong>{ad.get('discovery','') if ad else ''}</p>
        </div>"""

    def _ch3_bear(self, agent_details: dict) -> str:
        """第3章 BearBee — 看空对冲（至少 3 条）"""
        ad = agent_details.get("BearBeeContrarian", {})
        if not ad:
            return ""
        det = ad.get("details", {})
        signals = det.get("bearish_signals", [])
        bear_score = det.get("bear_score", 0)
        score = ad.get("score", 0)
        # ── v0.45.42：iv_skew 取错了蜂 ────────────────────────────────
        # `det` 是 **BearBeeContrarian** 的 details，而 iv_skew_ratio 产自
        # **OracleBeeEcho**。旧代码在 BearBee 里取这个键，永远取不到，
        # 于是恒定落到 fallback 0 —— 这张卡从上线起就没显示过真实值
        # （实测 8/25 与 8/26 都是 0.00，而 OracleBee 里 29/30 有值）。
        # 真值缺失时给 None，由下方渲染成 —，不再冒充 0.00。
        _oracle_det = (agent_details.get("OracleBeeEcho") or {}).get("details") or {}
        iv_skew = _oracle_det.get("iv_skew_ratio", _oracle_det.get("iv_skew"))
        _skew_cell = ('<span class="na" title="数据不可用">—</span>'
                      if iv_skew is None else f"{iv_skew:.2f}")
        # 确保至少 3 条
        fallback = [
            "期权 IV Skew 偏高（看跌期权溢价）",
            "短期催化剂带来波动性风险",
            "估值已充分反映增长预期，上行空间有限",
        ]
        while len(signals) < 3:
            for fb in fallback:
                if fb not in signals:
                    signals.append(fb)
                if len(signals) >= 3:
                    break
        items = ""
        for i, s in enumerate(signals[:6], 1):
            items += f'<li style="margin:10px 0;padding:10px;background:var(--surface2);border-left:3px solid var(--bear);border-radius:4px;"><strong>{i}.</strong> {s}</li>'
        return f"""
        <div class="section">
            <h2>BearBee — 看空对冲</h2>
            <div class="grid-4" style="margin-bottom:15px;">
                <div class="stat"><div class="num" style="color:var(--bear)">{score:.1f}</div><div class="lbl">看空蜂评分</div></div>
                <div class="stat"><div class="num" style="color:{'var(--bear)' if bear_score>=6 else 'var(--neut)'}">{bear_score:.1f}/10</div><div class="lbl">看空强度</div></div>
                <div class="stat"><div class="num">{_skew_cell}</div><div class="lbl">IV Skew 比</div></div>
                <div class="stat"><div class="num">{'高' if bear_score>=7 else ('中' if bear_score>=5 else '低')}</div><div class="lbl">风险等级</div></div>
            </div>
            <h3>反对观点（至少 3 条 — 硬性要求）</h3>
            <ul style="list-style:none;padding:0;">{items}</ul>
        </div>"""

    def _ch4_thesis(self, analysis: dict, agent_details: dict) -> str:
        """第4章：投资假设与失效条件"""
        rec = analysis.get("recommendation", {})
        reasoning = rec.get("reasoning", "")
        overview = analysis.get("overview", "")
        thesis = reasoning or overview or "基于蜂群综合信号，当前机会由催化剂驱动。"
        bear_ad = agent_details.get("BearBeeContrarian", {})
        bear_signals = bear_ad.get("details", {}).get("bearish_signals", []) if bear_ad else []
        chronos_ad = agent_details.get("ChronosBeeHorizon", {})
        catalysts = chronos_ad.get("details", {}).get("catalysts", []) if chronos_ad else []
        risks = rec.get("risks", [])
        # 失效条件：从 BearBee 信号 + ChronosBee critical 催化剂 + 推荐风险
        break_conditions = []
        for s in bear_signals[:3]:
            trigger = s[:40] + "..." if len(s) > 40 else s
            break_conditions.append((trigger, "信号逆转", "BearBee / Form 4 监控"))
        for c in catalysts:
            if c.get("severity") == "critical":
                break_conditions.append((
                    f"{c.get('event','')} 未达预期",
                    "数据未达共识预期",
                    f"{c.get('date','')} 当日监控"
                ))
                break_conditions.append(("大机构突然撤出持仓", ">5% 机构净卖出", "13F/Form 4 监控"))
                break_conditions.append(("宏观风险升级", "VIX > 35 或 F&G < 20", "恐贪指数日监控"))
                break
        if not break_conditions:
            for r in risks[:3]:
                break_conditions.append((r[:40] + "..." if len(r) > 40 else r, "风险具现化", "新闻 + 监管公告"))
        # 保证至少 3 条
        defaults = [
            ("GTC/财报 keynote 无重大亮点", "新品性能低于市场预期", "3/16 直播监控"),
            ("出口管制扩大化", "新规覆盖非中国市场", "Commerce Dept 政策"),
            ("主要客户削减 GPU 采购", "云厂商资本支出下修", "季度云财报"),
        ]
        for d in defaults:
            if len(break_conditions) >= 3:
                break
            break_conditions.append(d)
        cond_rows = "".join(
            f"<tr><td>{c[0]}</td><td>{c[1]}</td><td style='color:var(--tm);font-size:0.85em'>{c[2]}</td></tr>"
            for c in break_conditions[:5]
        )
        return f"""
        <div class="section">
            <h2>第 4 章：投资假设与失效条件</h2>
            <h3>核心 Thesis</h3>
            <blockquote style="border-left:4px solid var(--tp);padding:12px 18px;background:var(--surface2);border-radius:0;margin:10px 0;color:var(--tp);font-style:italic;">
                {thesis}
            </blockquote>
            <h3 style="margin-top:18px;">失效条件（Thesis Break）</h3>
            <table>
                <tr><th>条件</th><th>触发阈值</th><th>监控方式</th></tr>
                {cond_rows}
            </table>
        </div>"""

    # 第 5 章五个情景 = 同标的历史 T+7 收益分布的五个分位点（v0.45.132）。
    # 「概率」列是累计频率的定义本身（P10 = 10% 的历史样本比它更差），不是拍的。
    _CH5_QUANTILES = (
        ("悲观", "p10", 10, "历史上 10% 的同类预测 T+7 更差"),
        ("偏弱", "p25", 25, "下四分位"),
        ("中位", "median", 50, "一半好于此、一半差于此"),
        ("偏强", "p75", 75, "上四分位"),
        ("乐观", "p90", 90, "历史上 10% 的同类预测 T+7 更好"),
    )

    def _ch5_scenarios(self, analysis: dict, swarm: dict) -> str:
        """第5章：情景推演——同标的历史预测的 T+7 真实收益分布

        v0.45.132 之前：`historical_analysis.expected_returns` 来自 advanced_analyzer
        里 6 条手写记录（NVDA/VKTX/TSLA，2023 年），27/30 只标的结构上永远缺失；
        v0.45.54 起守卫把它渲染成「不可用」。现在数据源换成 pheromone.db 里蜂群
        自己 900+ 条核对过 T+7 收盘的预测，本表五行是该标的（同方向，不足时
        不分方向）真实收益分布的 P10/P25/P50/P75/P90，期望价 = 均值。
        表里每个数都是观测到的频率，不再有 25%/45%/20%/10% 这种写死的概率。
        """
        hist = analysis.get("historical_analysis", {}) or {}
        exp = hist.get("expected_returns", {}) or {}
        e7 = exp.get("expected_7d") or {}
        pos = analysis.get("position_management", {}) or {}
        sl = pos.get("stop_loss", {})
        # 当前价：从 agent_details 或 stop_loss 反推（防 None 污染）
        scout = swarm.get("agent_details", {}).get("ScoutBeeNova", {})
        curr_price = float(scout.get("details", {}).get("price") or 0) if scout else 0
        if not curr_price and isinstance(sl, dict):
            conservative = sl.get("conservative", 0)
            curr_price = conservative / 0.97 if conservative else 0

        def _num(v):
            return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

        _missing = []
        if not curr_price:
            _missing.append("现价")
        if not e7:
            _missing.append("T+7 收益分布")
        else:
            # 部分分位缺失 / 非有限数：逐项点名——「部分真实 + 部分常量」的表比全常量更难识破
            for _label, _key, _, _ in self._CH5_QUANTILES:
                if not _num(e7.get(_key)):
                    _missing.append(f"T+7 {_label}分位")
            if not _num(e7.get("mean")):
                _missing.append("T+7 均值")
        if _missing:
            n_same = exp.get("same_direction_n")
            n_any = exp.get("any_direction_n")
            why = exp.get("note") or (
                f"pheromone.db 状态 {exp.get('db_status')}" if exp.get("db_status") not in (None, "ok")
                else "历史分布缺失"
            )
            _log.warning("[_ch5_scenarios] 情景推演不可得：%s（%s）", "、".join(_missing), why)
            return f"""
        <div class="section">
            <h2>第 5 章：情景推演</h2>
            <div style="padding:14px 16px;border:1px solid var(--border);border-radius:2px;
                        font-size:.9em;color:var(--tm);">
                情景推演不可用：{why}。缺少 {', '.join(_missing)}。<br>
                本表依赖该标的历史蜂群预测的 T+7 真实收益分布
                （同方向样本 {n_same if n_same is not None else '—'}，
                不分方向 {n_any if n_any is not None else '—'}，
                最低 {exp.get('min_sample', '—')}）；样本不足时不做推演 ——
                以常量生成的目标价与期望收益无法与真实测算区分。
            </div>
        </div>"""

        n = exp.get("sample_size")
        basis = exp.get("basis")
        direction = exp.get("direction") or swarm.get("direction") or "—"
        d0, d1 = (exp.get("date_range") or ["—", "—"])[:2]
        basis_txt = (
            f"同标的、同方向（{direction}）" if basis == "same_direction"
            else f"同标的、不分方向（同方向 {direction} 仅 {exp.get('same_direction_n', 0)} 条，"
                 f"不足 {exp.get('min_sample', '—')}）"
        )
        rows = ""
        for label, key, cum, note in self._CH5_QUANTILES:
            r = e7[key]
            price = curr_price * (1 + r / 100.0)
            color = "var(--bull)" if r > 0 else ("var(--bear)" if r < 0 else "var(--tm)")
            rows += f"""<tr>
                <td>{label}</td>
                <td>P{cum}</td>
                <td>${price:.2f}</td>
                <td style="color:{color}">{r:+.1f}%</td>
                <td style="font-size:0.85em;color:var(--ts)">{note}</td>
            </tr>"""
        mean = e7["mean"]
        exp_price = curr_price * (1 + mean / 100.0)
        exp_color = "var(--bull)" if mean > 0 else ("var(--bear)" if mean < 0 else "var(--tm)")
        hit = exp.get("hit_rate_pct")
        # 命中率只在「同方向」口径下有定义；不分方向时即便上游带了也不印
        hit_txt = f"，方向命中率 {hit:.1f}%" if basis == "same_direction" and _num(hit) else ""
        return f"""
        <div class="section">
            <h2>第 5 章：情景推演</h2>
            <table>
                <tr><th>情景</th><th>累计分位</th><th>T+7 价格</th><th>涨跌幅</th><th>含义</th></tr>
                {rows}
                <tr style="background:var(--surface2);font-weight:bold;">
                    <td colspan="2">样本均值期望价</td>
                    <td style="color:{exp_color}">${exp_price:.2f}</td>
                    <td style="color:{exp_color}">{mean:+.1f}%</td>
                    <td>from ${curr_price:.2f}</td>
                </tr>
            </table>
            <p style="margin-top:10px;font-size:0.85em;color:var(--tm);">
                依据：pheromone.db 中 {basis_txt} 的 {n} 次蜂群预测，T+7 真实收盘收益分布
                （{d0} ~ {d1}{hit_txt}；口径 {exp.get('return_basis', '—')}）。
                分位数是历史频率，不是对本次的预测；样本跨度内的市场环境与当下未必相同。
            </p>
        </div>"""

    def _ch6_risk_radar(self, swarm: dict, agent_details: dict, options: dict) -> str:
        """第6章：风险雷达"""
        bear_ad = agent_details.get("BearBeeContrarian", {})
        bear_score = bear_ad.get("details", {}).get("bear_score", 0) if bear_ad else 0
        scout_ad = agent_details.get("ScoutBeeNova", {})
        crowding = scout_ad.get("details", {}).get("crowding_score", 50) if scout_ad else 50
        chronos_ad = agent_details.get("ChronosBeeHorizon", {})
        cats = chronos_ad.get("details", {}).get("catalysts", []) if chronos_ad else []
        imminent = [c for c in cats if isinstance(c, dict) and abs(c.get("days_until", 999)) <= 7]
        gex = options.get("gamma_squeeze_risk", "low")
        iv_rank = options.get("iv_rank", 0)
        conflict_level = swarm.get("conflict_info", {}).get("conflict_level", "low")

        # v0.43.27: 这三个值都是 `.get(k, 数字默认)` —— 而默认值只在**键缺失**时
        # 生效，上游写成 None 时它形同虚设。2026-08-24 实测崩在下面的
        # f"{iv_rank:.1f}"：期权链降级为样本数据后 options_analyzer 诚实返回
        # iv_rank=None（v0.43.19 起），而这里没接住。
        #
        # 关键：**不能把 None 当 0 处理**。risk_level(0, ...) 会输出"🟢 低"，
        # 等于把"没数据"渲染成"低风险"——比崩溃更危险，因为它不会报错。
        def _fmt(val, spec, suffix=""):
            return "—" if val is None else f"{val:{spec}}{suffix}"

        def risk_level(val, high_thr, med_thr, high_lbl="高", med_lbl="中", low_lbl="低"):
            if val is None:
                return '<span style="color:var(--tm)">○</span> 数据缺失'
            if val >= high_thr:
                return f'<span style="color:var(--bear)">●</span> {high_lbl}'
            if val >= med_thr:
                return f'<span style="color:var(--neut)">●</span> {med_lbl}'
            return f'<span style="color:var(--bull)">●</span> {low_lbl}'
        # v0.45.63 二次检查：`gamma_squeeze_risk` 现在可能是 "unknown"
        # （GEX 算不出时 options_analyzer 不再伪造 0.0）。旧写法
        # `1 if ... in ("high","很高","medium") else 0` 会把 "unknown" 判成 0，
        # risk_level(0) 输出「● 低」—— 正是上面那段注释警告的
        # 「把没数据渲染成低风险，比崩溃更危险」。传 None 走它自己的「数据缺失」分支。
        _gex_txt = str(gex).lower()
        if _gex_txt in ("unknown", "none", "", "—", "-", "n/a"):
            _gex_risk = None
            _gex_desc = "数据不可用"
        else:
            _gex_risk = 1 if _gex_txt in ("high", "很高", "medium") else 0
            _gex_desc = str(gex)

        rows = [
            ("监管风险", risk_level(1 if conflict_level in ("high","severe") else 0, 1, 0.5), "AI 芯片出口管制 / 政策变化风险"),
            ("市场情绪风险", risk_level(bear_score, 7, 5), f"看空强度 {_fmt(bear_score, '.1f')}/10，{'临近催化剂' if imminent else '无近期催化剂'}"),
            ("估值压缩风险", risk_level(crowding, 70, 50), f"拥挤度 {_fmt(crowding, '.0f')}/100（{'数据缺失' if crowding is None else ('偏高' if crowding > 70 else ('适中' if crowding > 40 else '偏低'))}）"),
            ("流动性风险", '<span style="color:var(--bull)">●</span> 低', "大盘股，日均成交量充足"),
            ("期权事件风险", risk_level(_gex_risk, 1, 0.5), f"Gamma 压榨风险：{_gex_desc}，IV Rank {_fmt(iv_rank, '.1f', '%')}"),
            ("催化剂风险", risk_level(len(imminent), 2, 1), f"7 天内催化剂 {len(imminent)} 个：{', '.join(c.get('event','') for c in imminent[:2])}"),
        ]
        risk_rows = "".join(
            f"<tr><td>{name}</td><td>{level}</td><td style='font-size:0.85em;color:var(--ts)'>{detail}</td></tr>"
            for name, level, detail in rows
        )
        return f"""
        <div class="section">
            <h2>第 6 章：风险雷达</h2>
            <table>
                <tr><th>风险类型</th><th>等级</th><th>具体内容</th></tr>
                {risk_rows}
            </table>
        </div>"""

    def _ch7_tasks(self, agent_details: dict, options: dict) -> str:
        """第7章：明日追踪任务"""
        chronos_ad = agent_details.get("ChronosBeeHorizon", {})
        cats = chronos_ad.get("details", {}).get("catalysts", []) if chronos_ad else []
        exp_dates = options.get("expiration_dates", [])
        tasks = []
        # 近期催化剂
        for c in sorted([x for x in cats if isinstance(x, dict)], key=lambda x: abs(x.get("days_until", 999)))[:5]:
            days = c.get("days_until", 0)
            if abs(days) <= 30:
                days_txt = f"{days}天后" if days > 0 else ("今日" if days == 0 else f"{abs(days)}天前")
                sev = c.get("severity", "")
                prefix = "⭐ " if sev == "critical" else ""
                tasks.append(f"{prefix}关注 **{c.get('event','')}**（{days_txt} {c.get('date','')}）")
        # 期权到期日
        for d in exp_dates[:2]:
            tasks.append(f"监控期权到期日 **{d}** 前后的 Pin Risk / Gamma Exposure")
        # 通用任务
        tasks += [
            "跟踪 BearBee 看空信号是否兑现",
            "检查 SEC EDGAR 是否有新 Form 4 大额内部人减持",
            "观察蜂群评分是否突破 7.5（高优先级阈值）",
        ]
        items = "".join(f'<li style="margin:8px 0;">☐ {t}</li>' for t in tasks[:8])
        return f"""
        <div class="section">
            <h2>第 7 章：明日追踪任务</h2>
            <ul style="list-style:none;padding:0;">{items}</ul>
        </div>"""

    # ── v0.15.0: 估值快照 + Top-3 核心论点 Pills ───────────────────
    def _build_valuation_pills(self, swarm: dict, analysis: dict,
                                options: dict, ml_pred: dict,
                                ticker: str = "") -> str:
        """构建估值快照卡片 + Top-3 核心论点 pills（移植自 deep 报告）"""
        parts = []

        # ── 估值快照 ──
        # v0.17.0: analysis.recommendation 不含估值字段，直接从 yfinance .info 获取
        _yf = analysis.get("recommendation", {})
        _pe_ttm = float(_yf.get("pe_ttm") or analysis.get("pe_ttm") or 0)
        _pe_fwd = float(_yf.get("forward_pe") or analysis.get("forward_pe") or 0)
        _peg = float(_yf.get("peg") or analysis.get("peg_ratio") or 0)
        _target = float(_yf.get("target_price") or analysis.get("target_price") or 0)
        _n_analysts = int(_yf.get("n_analysts") or analysis.get("n_analysts") or 0)

        # v0.17.0 fallback: 若以上字段全为 0 且有 ticker，从 yfinance 实时获取
        if _pe_ttm == 0 and _pe_fwd == 0 and ticker:
            try:
                import yfinance as _yf_lib
                _info = _yf_lib.Ticker(ticker).info or {}
                _pe_ttm = float(_info.get("trailingPE") or 0)
                _pe_fwd = float(_info.get("forwardPE") or 0)
                _peg = float(_info.get("pegRatio") or 0)
                _target = float(_info.get("targetMeanPrice") or 0)
                _n_analysts = int(_info.get("numberOfAnalystOpinions") or 0)
            except Exception:
                pass  # 网络失败静默降级，不影响报告生成

        if _pe_fwd > 0 or _pe_ttm > 0:
            _peg_label = ""
            if _peg > 0:
                if _peg < 0.5:
                    _peg_label = "极度低估"
                elif _peg < 1.0:
                    _peg_label = "低估"
                elif _peg < 2.0:
                    _peg_label = "合理"
                else:
                    _peg_label = "偏贵"

            _val_items = []
            if _pe_ttm > 0:
                _val_items.append(f"<span style='font-weight:600;'>PE (TTM):</span> {_pe_ttm:.1f}x")
            if _pe_fwd > 0:
                _val_items.append(f"<span style='font-weight:600;'>Forward PE:</span> {_pe_fwd:.1f}x")
            if _peg > 0:
                _val_items.append(f"<span style='font-weight:600;'>PEG:</span> {_peg:.2f} ({_peg_label})")
            if _target > 0:
                _ana_txt = f"({_n_analysts}人共识)" if _n_analysts else ""
                _val_items.append(f"<span style='font-weight:600;'>分析师目标价:</span> ${_target:.0f} {_ana_txt}")

            _val_grid = " &nbsp;·&nbsp; ".join(_val_items)
            parts.append(
                f'<div class="section" style="padding:18px 25px;">'
                f'<h3>估值快照</h3>'
                f'<div style="font-size:0.9em;color:var(--ts);line-height:1.8;">{_val_grid}</div>'
                f'</div>'
            )

        # ── Top-3 核心论点 Pills ──
        _thesis_items = []

        # 期权信号
        oracle_ad = swarm.get("agent_details", {}).get("OracleBeeEcho", {})
        _oracle_sc = float(oracle_ad.get("score", 5) if oracle_ad else 5)
        _iv_rank_v = float(options.get("iv_rank", 50) or 50)
        _pcr_v = float(options.get("put_call_ratio", 1.0) or 1.0)
        if _oracle_sc >= 7:
            _thesis_items.append(("期权", abs(_oracle_sc - 5),
                                  f"期权结构强看涨（OracleBee {_oracle_sc:.1f}/10），IV Rank {_iv_rank_v:.0f}%，P/C {_pcr_v:.1f}"))
        elif _oracle_sc <= 3:
            _thesis_items.append(("期权", abs(_oracle_sc - 5),
                                  f"期权结构看跌（OracleBee {_oracle_sc:.1f}/10），IV Rank {_iv_rank_v:.0f}%"))

        # 估值信号
        if _peg > 0 and _peg < 0.5:
            _eps_g = float(analysis.get("eps_growth", 0) or 0)
            _thesis_items.append(("估值", 3.0,
                                  f"Forward PE {_pe_fwd:.1f}x vs EPS Growth {_eps_g:.0f}% → PEG {_peg:.2f} 极度低估"))
        elif _peg > 2.5:
            _thesis_items.append(("估值", 2.5,
                                  f"PEG {_peg:.2f} 偏贵，增长预期可能已被充分定价"))

        # 逆向信号
        bear_ad = swarm.get("agent_details", {}).get("BearBeeContrarian", {})
        _bear_sc = float(bear_ad.get("score", 5) if bear_ad else 5)
        if _bear_sc <= 3.5:
            _bear_detail = bear_ad.get("details", {}) if bear_ad else {}
            _insider_net = _bear_detail.get("insider_net_value", 0)
            _insider_txt = f"，内幕交易净值 ${abs(_insider_net):,.0f}" if _insider_net else ""
            _thesis_items.append(("逆向", abs(_bear_sc - 5),
                                  f"BearBee {_bear_sc:.1f}/10{_insider_txt}"))

        # ML 信号
        _ml_prob = float(ml_pred.get("prediction", {}).get("probability", 0.5)) * 100
        if _ml_prob >= 70:
            _thesis_items.append(("ML", 2.0, f"ML 7日胜率 {_ml_prob:.0f}%，信号偏强"))
        elif _ml_prob <= 30:
            _thesis_items.append(("ML", 2.0, f"ML 7日胜率 {_ml_prob:.0f}%，信号偏弱"))

        # 情绪
        guard_ad = swarm.get("agent_details", {}).get("GuardBeeShield", {})
        _fg = float((guard_ad.get("details", {}) if guard_ad else {}).get("fear_greed", 50) or 50)
        if _fg <= 25:
            _thesis_items.append(("情绪", 2.5, f"极度恐惧 F&G={_fg:.0f}，系统性抛压风险高"))
        elif _fg >= 80:
            _thesis_items.append(("情绪", 2.0, f"极度贪婪 F&G={_fg:.0f}，回调风险上升"))

        if _thesis_items:
            _thesis_items.sort(key=lambda x: x[1], reverse=True)
            _top3 = _thesis_items[:3]
            _pills_html = "".join(
                f'<div style="display:flex;gap:8px;align-items:baseline;margin:5px 0;">'
                f'<span style="color:var(--acc);border:1px solid var(--acc);'
                f'font-size:9.5px;font-weight:600;letter-spacing:.8px;padding:1px 7px;border-radius:2px;white-space:nowrap;">'
                f'{t[0]}</span>'
                f'<span style="font-size:0.88em;color:var(--tp);">{t[2]}</span></div>'
                for t in _top3
            )
            parts.append(
                f'<div class="section" style="padding:18px 25px;">'
                f'<h3>核心论点 Top-3</h3>'
                f'{_pills_html}</div>'
            )

        return "\n".join(parts)

    def generate_html_report(
        self, ticker: str, enhanced_report: dict
    ) -> str:
        """生成 ML 增强的 HTML 报告（完整版）"""
        combined = enhanced_report['combined_recommendation']
        analysis = enhanced_report.get('advanced_analysis', {})
        ml_pred = enhanced_report.get('ml_prediction', {})
        options = analysis.get('options_analysis') or {}
        recommendation = analysis.get('recommendation', {})
        prob = analysis.get('probability_analysis', {})
        swarm = enhanced_report.get('swarm_results', {})

        # v0.45.139：评级已撤，刊头徽章改印前瞻命中率；颜色随蜂群方向走
        rating_color = self._dir_color(swarm.get('direction', 'neutral'))

        # ML 预测部分（提前计算，用于修正蜂群表中 RivalBee 的旧概率值）
        pred = ml_pred.get('prediction', {})
        # v0.45.54：0.5 → 50.0% 是「模型给出五五开」，与真实的中性预测同形。
        _pv = pred.get('probability')
        ml_prob_val = (_pv * 100 if isinstance(_pv, (int, float))
                       and not isinstance(_pv, bool) else None)

        # 用 fresh ML 概率修正 swarm 里 RivalBeeVanguard 的历史缓存值（防止旧扫描结果显示 100%）
        import re as _re
        if swarm and 'agent_details' in swarm and 'RivalBeeVanguard' in swarm['agent_details']:
            rival = swarm['agent_details']['RivalBeeVanguard']
            rival_details = rival.get('details', {})
            old_prob = rival_details.get('probability', None)
            # 只在概率明显异常（>0.95）时覆盖，避免误改正常值
            if old_prob is not None and old_prob > 0.95:
                fresh_prob = pred.get('probability', 0.5)
                rival_details['probability'] = fresh_prob
                rival['details'] = rival_details
                rival['score'] = round(min(9.5, max(0.5, fresh_prob * 10)), 1)
                old_disc = rival.get('discovery', '')
                rival['discovery'] = _re.sub(
                    r'ML 胜率 \d+%',
                    f'ML 胜率 {fresh_prob*100:.0f}%',
                    old_disc
                )

        # ── v0.15.0: 估值快照 + Top-3 核心论点 Pills ─────────────────
        _valuation_pills_html = self._build_valuation_pills(
            swarm or {}, analysis, options, ml_pred, ticker=ticker
        )

        # ── 7 章 HTML ──────────────────────────────────────────────────
        agent_details = swarm.get("agent_details", {}) if swarm else {}

        ch1          = self._ch1_core_conclusion(swarm, combined, analysis)
        ch2          = self._ch2_five_dim_table(swarm)
        ch3_scout    = self._ch3_scout(agent_details)
        # v0.27.0: 传入 current_price 用于近端/全链 OI 墙距离百分比计算
        _curr_price_for_oracle = (
            analysis.get("current_price")
            or swarm.get("agent_details", {}).get("ScoutBeeNova", {}).get("details", {}).get("current_price")
            or 0
        )
        ch3_oracle   = self._ch3_oracle(agent_details, options, current_price=float(_curr_price_for_oracle or 0))
        ch3_chronos  = self._ch3_chronos(agent_details)
        ch3_buzz     = self._ch3_buzz(agent_details)
        ch3_rival    = self._ch3_rival(analysis)
        ch3_guard    = self._ch3_guard(agent_details, swarm)
        ch3_bear     = self._ch3_bear(agent_details)
        ch4          = self._ch4_thesis(analysis, agent_details)
        ch5          = self._ch5_scenarios(analysis, swarm)
        ch6          = self._ch6_risk_radar(swarm, agent_details, options)
        ch7          = self._ch7_tasks(agent_details, options)

        # ── 折叠详情区（止损 / 止盈 / 期权 / ML 特征）──────────────
        # v0.45.134：换成历史命中率；**不可得时是 None，不是 50**
        # （旧默认 50 与 v0.45.50 修掉的 rr 默认 1.5/2.0 同型：卡在闸上的「不知道」）
        _hr = prob.get('hit_rate_pct')
        win_prob = _hr if isinstance(_hr, (int, float)) and not isinstance(_hr, bool) else None
        # v0.45.54：1.00 的 R:R 是一个明确的（很差的）交易结论，不是「不知道」。
        # 上游 _calculate_risk_reward_ratio 现已在无历史时返回 None（v0.45.50）。
        _rr = prob.get('risk_reward_ratio')
        risk_reward = _rr if isinstance(_rr, (int, float)) and not isinstance(_rr, bool) else None

        # ⚠️ 预先算好 —— 不在下面的 f-string 里放条件逻辑（v0.45.50/53 各犯过一次）
        _rr_txt = f"{risk_reward:.2f}" if risk_reward is not None else "未知"
        _mlp_txt = f"{ml_prob_val:.1f}%" if ml_prob_val is not None else "未知"
        _hr_txt = (f"{win_prob:.1f}%（n={prob.get('sample_size')},"
                   f" basis={prob.get('basis')}）"
                   if win_prob is not None else "不可得（无同方向历史可比样本）")
        # v0.45.138：前瞻量与描述量分两行印。合成一行必然被读成同一件事，
        # 而它们恰恰是本版要区分的两件事。
        _fw = prob.get("forward_estimate_pct")
        _fci = prob.get("forward_ci95")
        _fw_txt = (f"{_fw:.1f}%（全书池化 n={prob.get('forward_sample_size')}"
                   + (f"，95% 区间 [{_fci[0]}, {_fci[1]}]"
                      if isinstance(_fci, (list, tuple)) and len(_fci) == 2 else "")
                   + "；各标的相同）"
                   if isinstance(_fw, (int, float)) and not isinstance(_fw, bool)
                   else "不可得")
        _fw_hdr = ((f"{_fw:.1f}%" + (f" [{_fci[0]}, {_fci[1]}]"
                                    if isinstance(_fci, (list, tuple)) and len(_fci) == 2 else ""))
                   if isinstance(_fw, (int, float)) and not isinstance(_fw, bool) else "不可得")
        position    = analysis.get('position_management', {})
        stop_loss   = position.get('stop_loss', {})
        # v0.45.134：分布不可得时上游给 None（不是 {}）。`or {}` 会把它悄悄
        # 变成「有这一节、只是空的」——那正是 v0.45.114「跳过缺失项＝把缺失
        # 渲染成不存在」的形状。这里保留 None 并在下面显式渲染「不可得」。
        take_profit = position.get('take_profit')
        holding     = position.get('optimal_holding_time', '')

        sl_rows = ""
        if isinstance(stop_loss, dict):
            for k, v in stop_loss.items():
                sl_rows += (f"<tr><td>{k}</td><td>${v:.2f}</td></tr>"
                            if isinstance(v, (int, float))
                            else f"<tr><td>{k}</td><td>{v}</td></tr>")
        elif isinstance(stop_loss, list):
            for item in stop_loss:
                if isinstance(item, dict):
                    sl_rows += f"<tr><td>{item.get('level','')}</td><td>${item.get('price',0):.2f}</td></tr>"

        tp_rows = ""
        if isinstance(take_profit, dict):
            for k, v in take_profit.items():
                if not isinstance(v, dict):
                    continue
                # 两列不同口径，别混：价格变动（可为负）与按方向折算的盈利。
                # 空头的价格变动为负正是它在赚钱 —— 只印一个数必然误导一半的行。
                _g = v.get('gain_pct')
                _pf = v.get('profit_pct')
                _g_txt = f"{_g:+.1f}%" if isinstance(_g, (int, float)) else "—"
                _pf_txt = f"{_pf:+.1f}%" if isinstance(_pf, (int, float)) else "—"
                tp_rows += (f"<tr><td>{k}</td><td>${v.get('price',0):.2f}</td>"
                            f"<td>{_g_txt}</td><td>{_pf_txt}</td>"
                            f"<td>{v.get('sell_ratio',0):.0%} | {v.get('reason','')}</td></tr>")
        elif take_profit is None:
            tp_rows = ('<tr><td colspan="5">不可得：无同标的同方向的历史 T+7 '
                       '收益分布（或方向为中性）</td></tr>')

        holding_txt = ""
        if holding:
            holding_txt = f'<p style="margin-top:15px;">最佳持仓周期：<strong>{holding.get("note", holding) if isinstance(holding, dict) else holding}</strong></p>'

        sl_tp_html = ""
        if sl_rows or tp_rows:
            sl_tp_html = f"""
            <div style="margin-bottom:20px;">
                <p style="margin:0 0 10px;color:var(--ts);font-size:0.92em;">
                    出场阶梯取自同标的同方向历史 T+7 收益分布<br>
                    · <strong>历史描述</strong>（本标的本方向）：命中率 {_hr_txt}<br>
                    · <strong>前瞻估计</strong>（用于评级）：{_fw_txt}
                </p>
                <div class="grid-2">
                    <div><h3 style="color:var(--bear);">止损位</h3><table>{sl_rows}</table></div>
                    <div><h3 style="color:var(--bull);">止盈位</h3>
                        <table><tr><th>档位</th><th>价格</th><th>价格变动</th><th>盈利</th><th>操作</th></tr>{tp_rows}</table>
                    </div>
                </div>
                {holding_txt}
            </div>"""

        options_html = self._generate_options_section_html(options) if options else ""

        ml_features = ml_pred.get('feature_importance', {})
        feat_rows = "".join(
            f"<tr><td>{k}</td><td>{v:.3f}</td></tr>"
            for k, v in sorted(ml_features.items(), key=lambda x: -abs(x[1]))[:8]
        ) if ml_features else ""
        ml_feat_html = f"""
            <div style="margin-top:15px;">
                <h3>ML 特征重要度</h3>
                <table><tr><th>特征</th><th>权重</th></tr>{feat_rows}</table>
            </div>""" if feat_rows else ""

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{ticker} 深度研究报告 - Alpha Hive</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Noto+Sans+SC:wght@300;400;500&display=swap" rel="stylesheet">
    <style>
        /* ══════════════════════════════════════════════════════════════
           Alpha Hive 深度研究报告 — 与 index.html 同一套设计令牌。
           v0.45.4 前这里是 var(--tp)→var(--tp) 紫色渐变 + 白圆角卡 + 大投影 +
           emoji 标题，与站点自身的「奶油纸底 + Playfair + 铁锈红」完全脱节：
           从仪表板点进报告像换了个产品。此处不新造第三套审美，直接复用站点令牌。
           ══════════════════════════════════════════════════════════════ */
        :root {{
            --bg:#FAF7F2; --surface:#FFFFFF; --surface2:#F5F0E8; --border:#C8BAA8;
            --tp:#1A1208; --ts:#6B5F52; --tm:#B0A090;
            --acc:#B7410E; --acc2:#1D6B3A; --acc3:#92601A;
            --bull:#1D6B3A; --bear:#9B2C2C; --neut:#92601A;
        }}
        html.dark {{
            --bg:#0A0F1C; --surface:#141928; --surface2:#1a2035; --border:#2a3050;
            --tp:#e2e8f0; --ts:#94a3b8; --tm:#64748b;
            --acc:#E05A1F; --acc2:#22c55e; --acc3:#f59e0b;
            --bull:#22c55e; --bear:#ef4444; --neut:#f59e0b;
        }}
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        html {{ overflow-x:hidden; }}
        body {{
            font-family:'Noto Sans SC',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
            background:var(--bg); color:var(--tp);
            font-size:14px; line-height:1.75; -webkit-font-smoothing:antialiased;
            overflow-x:hidden; padding:0;
        }}
        .container {{ max-width:860px; margin:0 auto; padding:0 28px 96px; }}
        @media (max-width:600px) {{ .container {{ padding:0 18px 64px; }} }}

        /* ── 刊头：左对齐研究报告信笺，不是居中英雄卡 ── */
        .header {{
            padding:56px 0 28px; margin-bottom:0;
            border-bottom:1px solid var(--tp);
            background:none; border-radius:0; box-shadow:none; text-align:left;
        }}
        .header .eyebrow {{
            font-family:'JetBrains Mono',ui-monospace,monospace;
            font-size:9px; letter-spacing:2.5px; text-transform:uppercase;
            color:var(--acc); display:block; margin-bottom:14px;
        }}
        .header h1 {{
            font-family:'Playfair Display',Georgia,serif;
            font-size:clamp(30px,5vw,44px); font-weight:700; color:var(--tp);
            line-height:1.1; letter-spacing:-.5px; margin-bottom:0;
        }}
        .header h1 .tk {{ font-family:'JetBrains Mono',ui-monospace,monospace; letter-spacing:-1px; }}
        .header .rating {{
            display:inline-block; margin:18px 0 0; padding:5px 12px;
            border:1px solid currentColor; border-radius:2px; background:none;
            font-family:'JetBrains Mono',ui-monospace,monospace;
            font-size:12px; font-weight:600; letter-spacing:.5px;
            color:{rating_color};
        }}
        .header .meta {{
            font-family:'JetBrains Mono',ui-monospace,monospace;
            font-size:10px; color:var(--tm); letter-spacing:.8px; margin-top:16px;
        }}
        .header .meta b {{ color:var(--ts); font-weight:500; }}

        /* ── 章节：细线分隔，不是浮空卡片 ── */
        .section {{
            background:none; border-radius:0; box-shadow:none;
            padding:34px 0; margin-bottom:0;
            border-bottom:1px solid var(--border);
        }}
        .section h2 {{
            font-family:'Playfair Display',Georgia,serif;
            color:var(--tp); font-size:19px; font-weight:600;
            margin-bottom:20px; padding-bottom:0; border-bottom:none;
            letter-spacing:-.2px;
        }}
        /* h3 多为中文：mono + 大字距会把汉字撑散，故只对拉丁部分保留微字距 */
        .section h3 {{
            font-family:'JetBrains Mono',ui-monospace,'Noto Sans SC',monospace;
            color:var(--ts); font-size:11.5px; font-weight:600;
            letter-spacing:.5px;
            margin:26px 0 12px; padding-bottom:7px;
            border-bottom:1px solid var(--border);
        }}

        /* ── 数据格：细线分栏 + 等宽等距数字 ── */
        .grid-4 {{ display:grid; grid-template-columns:repeat(4,1fr); gap:0;
                   border:1px solid var(--border); }}
        .grid-2 {{ display:grid; grid-template-columns:1fr 1fr; gap:28px; }}
        .stat {{
            text-align:left; padding:14px 16px; border-radius:0;
            background:none; border:none; border-right:1px solid var(--border);
        }}
        .grid-4 .stat:last-child {{ border-right:none; }}
        .stat .num {{
            font-family:'JetBrains Mono',ui-monospace,monospace;
            font-size:21px; font-weight:600; color:var(--tp);
            font-variant-numeric:tabular-nums; line-height:1.25;
        }}
        .stat .lbl {{
            font-size:10px; color:var(--tm); margin-top:6px;
            letter-spacing:.6px; line-height:1.4;
        }}

        .metric {{
            display:flex; justify-content:space-between; align-items:baseline;
            padding:9px 0; border-bottom:1px solid var(--border); gap:16px;
        }}
        .metric:last-child {{ border-bottom:none; }}
        .metric-label {{ color:var(--ts); font-weight:400; font-size:13px; }}
        .metric-value {{
            font-family:'JetBrains Mono',ui-monospace,monospace;
            font-weight:600; color:var(--tp); font-variant-numeric:tabular-nums;
        }}

        table {{ width:100%; border-collapse:collapse; margin-top:12px; font-size:13px; }}
        th, td {{ padding:9px 12px; text-align:left; border-bottom:1px solid var(--border); }}
        th {{
            background:none; color:var(--tm); font-weight:600;
            font-family:'JetBrains Mono',ui-monospace,monospace;
            font-size:9.5px; letter-spacing:1.2px; text-transform:uppercase;
            border-bottom:1px solid var(--tp);
        }}
        td {{ color:var(--tp); font-variant-numeric:tabular-nums; }}
        tbody tr:last-child td {{ border-bottom:none; }}

        ul {{ padding-left:18px; margin:10px 0; }}
        li {{ margin:6px 0; color:var(--ts); line-height:1.8; }}
        p {{ color:var(--ts); }}
        strong {{ color:var(--tp); font-weight:600; }}
        blockquote {{ color:var(--ts); }}

        details {{
            background:none; border-radius:0; box-shadow:none;
            padding:24px 0; margin-bottom:0; border-bottom:1px solid var(--border);
        }}
        details summary {{
            cursor:pointer; user-select:none;
            font-family:'JetBrains Mono',ui-monospace,monospace;
            color:var(--acc); font-weight:600; font-size:10.5px;
            letter-spacing:1.4px; text-transform:uppercase;
        }}

        /* ── 缺数标记：让「没取到」与「测得为 0」在视觉上不可混淆 ── */
        .na {{
            font-family:'JetBrains Mono',ui-monospace,monospace;
            color:var(--tm); font-weight:400; cursor:help;
            border-bottom:1px dotted var(--tm);
        }}
        .na-note {{ color:var(--tm); font-style:italic; }}
        .mono {{ font-family:'JetBrains Mono',ui-monospace,monospace; }}
        .src-tag {{
            font-family:'JetBrains Mono',ui-monospace,monospace;
            font-size:9px; letter-spacing:.8px; text-transform:uppercase;
            color:var(--tm); border:1px solid var(--border); border-radius:2px;
            padding:1px 5px; margin-left:7px; vertical-align:middle;
        }}
        .note {{ font-size:12.5px; color:var(--ts); margin:12px 0 4px; line-height:1.8; }}

        .footer {{
            text-align:left; color:var(--tm); margin-top:36px;
            font-family:'JetBrains Mono',ui-monospace,monospace;
            font-size:10px; letter-spacing:1px; line-height:2;
        }}
        .theme-btn {{
            position:fixed; top:16px; right:16px; z-index:99;
            font-family:'JetBrains Mono',ui-monospace,monospace; font-size:9.5px;
            letter-spacing:1.2px; text-transform:uppercase; cursor:pointer;
            background:var(--surface); color:var(--ts);
            border:1px solid var(--border); border-radius:2px; padding:6px 11px;
        }}
        .theme-btn:hover {{ color:var(--acc); border-color:var(--acc); }}
        a {{ color:var(--acc); }}
        :focus-visible {{ outline:2px solid var(--acc); outline-offset:2px; }}

        @media (max-width:600px) {{
            .grid-4 {{ grid-template-columns:repeat(2,1fr); }}
            .grid-4 .stat:nth-child(2) {{ border-right:none; }}
            .grid-4 .stat:nth-child(-n+2) {{ border-bottom:1px solid var(--border); }}
            .grid-2 {{ grid-template-columns:1fr; gap:18px; }}
            .header {{ padding:36px 0 22px; }}
        }}
        @media print {{
            .theme-btn {{ display:none; }}
            body {{ background:var(--surface); }}
        }}
    </style>
    <script>
    /* 与仪表板共享 localStorage 主题（同源），从 index.html 点进来不会闪白 */
    (function(){{
      try {{
        var s = localStorage.getItem('ah-theme') || (localStorage.getItem('ahDark')==='1' ? 'dark' : '');
        if (s === 'dark' || (!s && window.matchMedia('(prefers-color-scheme: dark)').matches))
          document.documentElement.classList.add('dark');
      }} catch(e) {{}}
    }})();
    function ahToggleTheme(){{
      var h = document.documentElement, d = h.classList.toggle('dark');
      try {{ localStorage.setItem('ah-theme', d?'dark':'light');
             localStorage.setItem('ahDark', d?'1':'0'); }} catch(e) {{}}
      var b = document.getElementById('themeBtn');
      if (b) b.textContent = d ? '亮色' : '暗色';
    }}
    document.addEventListener('DOMContentLoaded', function(){{
      var b = document.getElementById('themeBtn');
      if (b && document.documentElement.classList.contains('dark')) b.textContent = '亮色';
    }});
    </script>
</head>
<body>
<button id="themeBtn" class="theme-btn" onclick="ahToggleTheme()" aria-label="切换明暗主题">暗色</button>
<div class="container">
    <!-- 刊头 -->
    <div class="header">
        <span class="eyebrow">Alpha Hive · 蜂群智能深度研究</span>
        <h1><span class="tk">{ticker}</span> 深度研究报告</h1>
        <div class="rating">前瞻命中率 {_fw_hdr}</div>
        <p class="meta">
            {self.timestamp.strftime('%Y-%m-%d %H:%M')} PDT
            &nbsp;·&nbsp; 综合胜率 <b>{combined['combined_probability']:.1f}%</b>
            &nbsp;·&nbsp; 风险回报比 <b>{_rr_txt}</b>
            &nbsp;·&nbsp; ML 预测 <b>{_mlp_txt}</b>
        </p>
    </div>

    <!-- 第 1 章：核心结论 -->
    {ch1}

    <!-- v0.15.0: 估值快照 + Top-3 核心论点 Pills -->
    {_valuation_pills_html}

    <!-- 第 2 章：五维评分明细 -->
    {ch2}

    <!-- 第 3 章：7 Agent 独立分析 -->
    {ch3_scout}
    {ch3_oracle}
    {ch3_chronos}
    {ch3_buzz}
    {ch3_rival}
    {ch3_guard}
    {ch3_bear}

    <!-- 第 4 章：投资假设与失效条件 -->
    {ch4}

    <!-- 第 5 章：情景推演 -->
    {ch5}

    <!-- 第 6 章：风险雷达 -->
    {ch6}

    <!-- 第 7 章：明日追踪任务 -->
    {ch7}

    <!-- 折叠详情：止损止盈 / 期权信号 / ML 特征 -->
    <details>
        <summary>详细数据（止损止盈 / 期权信号 / ML 特征）</summary>
        {sl_tp_html}
        {options_html}
        {ml_feat_html}
    </details>

    <!-- 免责声明 -->
    <div class="section" style="background:var(--surface2); border:1px solid var(--neut);">
        <p style="color:var(--neut); font-size:0.9em;">
            <strong>免责声明</strong>：本报告为 AI 自动生成，不构成投资建议。
            所有交易决策需自行判断和风控。预测存在误差，过往表现不代表未来收益。
        </p>
    </div>

    <div class="footer">
        <p><a href="index.html" style="color:white;">← 返回仪表板</a></p>
    </div>
</div>
</body>
</html>"""
        return html


def main():
    """主程序"""

    # v0.45.56：yfinance 全局限流闸门。必须在任何取数之前装好。
    # 8/27 事故：全天 687 次 429，rv_30d/iv_rank/iv_rv_spread/catalysts 各 0/30。
    try:
        import yf_gate
        yf_gate.install()
    except Exception as _e:  # pragma: no cover - 闸门不可得不阻断主流程
        print(f"⚠️  yfinance 限流闸门未装上：{_e}")

    # 解析命令行参数
    parser = argparse.ArgumentParser(
        description="Alpha Hive ML 增强报告生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法：
  python3 generate_ml_report.py
  python3 generate_ml_report.py --tickers NVDA TSLA VKTX
  python3 generate_ml_report.py --all-watchlist
        """
    )
    parser.add_argument(
        '--tickers',
        nargs='+',
        default=["NVDA", "TSLA", "VKTX"],
        help='要分析的股票代码列表（空格分隔，默认：NVDA TSLA VKTX）'
    )
    parser.add_argument(
        '--all-watchlist',
        action='store_true',
        help='分析配置中的全部监控列表'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='忽略美股交易日护栏，即使周末/假日也强制生成'
    )

    args = parser.parse_args()

    # ── 美股交易日护栏 ──
    # 周末 / 美股假日（Juneteenth、Good Friday、感恩节…）跳过，不对无交易日生成幽灵报告。
    # 用 PDT 日期判断（= 美股交易日），不依赖本机时区（用户在中国，Mac 时钟 +15h）。
    # fail-open：检查本身异常时继续生成，宁可多生成也绝不误跳过有效交易日。
    if not args.force:
        try:
            from datetime import date as _date
            from is_trading_day import is_trading_day as _is_trading_day
            _trading, _reason = _is_trading_day(_date.fromisoformat(pdt_today()))
            if not _trading:
                _log.warning("⏭️  跳过 ML 报告生成：%s（如需强制生成加 --force）", _reason)
                return
        except Exception as _e:
            _log.warning("交易日检查异常（%s），继续生成以防误跳过有效交易日", _e)

    # 确定要分析的标的
    if args.all_watchlist:
        tickers = list(WATCHLIST.keys())[:10]  # 默认最多10个
        _log.info("分析全部监控列表（最多10个）: %s", tickers)
    else:
        tickers = args.tickers
        _log.info("分析指定标的: %s", tickers)

    # 加载实时数据（如果存在）
    report_dir = PATHS.home
    realtime_file = report_dir / "realtime_metrics.json"

    metrics = {}
    if realtime_file.exists():
        try:
            with open(realtime_file) as f:
                metrics = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            _log.warning("加载实时数据失败: %s，继续使用空数据", e)
    else:
        _log.warning("未找到 realtime_metrics.json，将使用样本数据")

    # 创建生成器
    report_gen = MLEnhancedReportGenerator()

    # 加载今日蜂群扫描结果（与 markdown 报告同步）
    swarm_data = {}
    today_str = pdt_today()  # PDT 日期，与 daily_report 写出的 .swarm_results_{date} 对齐
    swarm_json = report_dir / f".swarm_results_{today_str}.json"
    if swarm_json.exists():
        try:
            with open(swarm_json) as f:
                swarm_data = json.load(f)
            _log.info("已加载蜂群扫描数据: %d 标的", len(swarm_data))
        except (json.JSONDecodeError, OSError) as e:
            _log.debug("蜂群 JSON 加载失败: %s", e)
    if not swarm_data:
        # 尝试从 checkpoint 恢复（v0.15.3: 仅接受今日 checkpoint）
        _today = pdt_today()
        for ckpt in report_dir.glob(".checkpoint_*.json"):
            try:
                with open(ckpt) as f:
                    ckpt_data = json.load(f)
                    # 双保险：文件名日期 + 内容 saved_at 均需匹配今日
                    if _today not in ckpt.name:
                        _log.debug("跳过非今日 checkpoint: %s", ckpt.name)
                        continue
                    if ckpt_data.get("saved_at", "") != _today:
                        _log.debug("checkpoint saved_at 不匹配今日: %s", ckpt.name)
                        continue
                    swarm_data = ckpt_data.get("results", {})
                    if swarm_data:
                        _log.info("从 checkpoint 加载蜂群数据: %d 标的", len(swarm_data))
                        break
            except (json.JSONDecodeError, OSError, KeyError) as e:
                _log.debug("checkpoint 加载失败: %s", e)

    _log.info("生成 ML 增强报告...")
    _log.info("=" * 60)

    # 为每个标的生成报告
    successful_count = 0
    for ticker in tickers:
        try:
            _log.info("生成 %s ML 增强报告...", ticker)

            # 获取该标的的数据（优先 realtime_metrics → swarm 缓存 → yfinance 实时）
            ticker_data = metrics.get(ticker)
            if not ticker_data or not ticker_data.get("sources", {}).get("yahoo_finance", {}).get("current_price"):
                # v0.40.1: 与 v36.0 同一反模式的第 3 处漏网之鱼——原初始化 100.0，
                # 深夜 yfinance 限流时假价写进 analysis-*.json。现置 0.0 哨兵
                # （下游注入逻辑跳过 0），并优先走 CBOE 起头的多源链。
                _real_price = 0.0
                _real_change = 0.0
                # 优先复用 swarm 的缓存（避免重复 API 调用）
                try:
                    from swarm_agents import get_cached_stock_data as _get_cached
                    _cached = _get_cached(ticker)
                except ImportError:
                    _cached = None
                if _cached and _cached.get("price", 0) > 0:
                    _real_price = _cached["price"]
                    _real_change = _cached.get("momentum_5d") or 0.0
                else:
                    try:
                        from data_pipeline import fetch_stock_data as _fsd_ml
                        _sd_ml = _fsd_ml(ticker)
                        if _sd_ml.get("price", 0) > 0:
                            _real_price = float(_sd_ml["price"])
                            _real_change = float(_sd_ml.get("momentum_5d") or 0.0)
                        else:
                            raise ConnectionError("多源链全部失败")
                    except Exception as e:
                        # yfinance 取价失败（含 YFRateLimitError 限流）→ 降级读磁盘最近一次价格，
                        # 避免整份报告因一次取价崩溃。优先 {ticker}_raw.json 的 _meta.price。
                        _log.warning(
                            "yfinance 取价失败（%s），改用磁盘缓存价格", type(e).__name__
                        )
                        try:
                            import json as _json
                            import os as _os
                            _raw_path = _os.path.join(
                                _os.path.dirname(_os.path.abspath(__file__)),
                                "%s_raw.json" % ticker,
                            )
                            if _os.path.exists(_raw_path):
                                with open(_raw_path) as _f:
                                    _raw = _json.load(_f)
                                _disk_price = (_raw.get("_meta") or {}).get("price", 0) or 0
                                if _disk_price > 0:
                                    _real_price = float(_disk_price)
                                    _real_change = float(
                                        (_raw.get("fundamentals") or {}).get("momentum_5d", 0.0)
                                        or 0.0
                                    )
                                    _log.info(
                                        "已复用磁盘缓存价格 %s=%.2f（来自 %s_raw.json）",
                                        ticker, _real_price, ticker,
                                    )
                        except Exception as _e2:
                            _log.debug("磁盘价格降级失败: %s", _e2)
                ticker_data = {
                    "ticker": ticker,
                    "sources": {
                        "yahoo_finance": {
                            "current_price": _real_price,
                            "price_change_5d": _real_change,
                            "change_pct": _real_change,
                        }
                    }
                }

            # 生成分析
            _sr = swarm_data.get(ticker) or {}
            enhanced_report = report_gen.generate_ml_enhanced_report(
                ticker, ticker_data,
                swarm_direction=_sr.get("direction"),
                swarm_dimension_scores=_sr.get("dimension_scores"),
                swarm_final_score=_sr.get("final_score"),
                swarm_agent_directions=_sr.get("agent_directions"),
            )

            # 注入蜂群数据到报告
            if ticker in swarm_data:
                sr = swarm_data[ticker]
                enhanced_report["swarm_results"] = sr

                # BUG-10 修复：opportunity_score 从 final_score 注入
                if sr.get("opportunity_score") is None and sr.get("final_score") is not None:
                    enhanced_report["swarm_results"]["opportunity_score"] = sr["final_score"]

                # BUG-11 修复：dimension_scores 中的 None 降级为 0.0，data_quality_grade 保守升级
                dim = sr.get("dimension_scores", {})
                if any(v is None for v in dim.values()):
                    enhanced_report["swarm_results"]["dimension_scores"] = {
                        k: (float(v) if v is not None else 0.0) for k, v in dim.items()
                    }
                    enhanced_report["swarm_results"]["data_quality_grade"] = "degraded"

                # ===== v0.15.0→v0.16.0: Probability Boost 已禁用 =====
                # 原因：probability_analysis 数据源不可靠（rr=9.0 来自 1 个样本，
                # win=65% 是硬编码启发式常数），导致 boost 成为固定偏移量而非市场信号。
                # 保留审计字段供报告卡片展示"未启用"状态，不修改 final_score。
                # TODO: 待 probability_analysis 改用真实贝叶斯模型后重新启用。
                try:
                    _prob = (enhanced_report.get("advanced_analysis") or {}).get(
                        "probability_analysis", {}) or {}
                    # v0.45.54：`or 0` 把「不可得」与「真实的 0」混为一谈；
                    # 该结构只用于记录被禁用的加成，保留 None 更诚实。
                    _wp = _prob.get("hit_rate_pct")   # v0.45.134 改名
                    _win = float(_wp) if isinstance(_wp, (int, float)) else None
                    _rrv = _prob.get("risk_reward_ratio")
                    _rr = float(_rrv) if isinstance(_rrv, (int, float)) else None
                    enhanced_report["swarm_results"]["probability_boost"] = {
                        "applied": False,
                        "disabled": True,
                        "hit_rate_pct": _win,
                        "risk_reward_ratio": _rr,
                        "reason": "v0.16.0 已禁用: probability_analysis 数据源不可靠 (sample_size<5, 启发式 win_prob)",
                    }
                except Exception as _pb_err:
                    _log.debug("[%s] Probability boost audit 跳过: %s", ticker, _pb_err)

            # 生成 HTML
            html = report_gen.generate_html_report(ticker, enhanced_report)

            # ⭐ Task 3: 异步保存文件（不阻塞主流程）
            filename = f"alpha-hive-{ticker}-ml-enhanced-{report_gen.timestamp.strftime('%Y-%m-%d')}.html"
            json_filename = f"analysis-{ticker}-ml-{report_gen.timestamp.strftime('%Y-%m-%d')}.json"

            # 提交异步写入任务（立即返回，不等待完成）
            report_gen.save_html_and_json_async(
                ticker,
                html,
                enhanced_report,
                report_dir,
                report_gen.timestamp
            )

            _log.info("报告已提交异步生成：%s", filename)
            _log.info("数据已提交异步保存：%s", json_filename)
            successful_count += 1

        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            # v0.43.23: 必须带 exc_info。此前只记 str(e)[:100]，
            # 导致 _ch3_buzz 的 None 崩溃从 2026-07-15 起潜伏一个月无人定位。
            _log.warning("%s 分析失败: %s", ticker, str(e)[:100], exc_info=True)

    # ⭐ Task 3: 等待所有异步文件写入完成
    if MLEnhancedReportGenerator._file_writer_pool:
        MLEnhancedReportGenerator._file_writer_pool.shutdown(wait=True)

    _log.info("=" * 60)
    _log.info("ML 增强报告生成完毕！成功: %d/%d", successful_count, len(tickers))
    # v0.43.23: 成功率过低必须刺眼。此前 2026-07-15~08-14 连续一个月 0~1/12，
    # 而编排器只看退出码 → 天天报"所有步骤成功"，网站照常更新、只是少了 11 份报告。
    _total = len(tickers)
    if _total and successful_count < _total * 0.5:
        _log.error(
            "🚨 ML 报告成功率异常：%d/%d（<50%%）。上方每条『分析失败』都带完整调用栈，"
            "请直接查栈顶定位；不要只看这行汇总。",
            successful_count, _total,
        )
    _log.info("所有文件已完成写入")
    _log.info("=" * 60)

    # ── v0.45.145：ML 概率常数退化闸 ─────────────────────────────────
    # 2026-09-04 全部 12 份 probability 逐位相同（0.5899693787928219），
    # 报告照常印「ML 预测 59.0%」、退出码 0、日志正常——没有任何东西会红。
    # 闸放在这里而不是循环内：判据是「当日这一批的唯一值个数」，必须等
    # 全部文件落盘（上面的 shutdown(wait=True)）之后才能求值。
    #
    # 读磁盘而非读内存，因为本进程通常只是 Step 3 补跑（编排器只在 Step 2
    # 漏了标的时才调本 main），单看自己写的那 1~2 份 n 太小、判据形同虚设；
    # 读磁盘会把 Step 2 的 12 份一起算进来。
    #
    # ⚠️ 不发 Slack（CLAUDE.md「Slack 通知精简规则」）。观测点 = 日志 + 退出码。
    _exit_code = 0
    _guard_date = report_gen.timestamp.strftime("%Y-%m-%d")
    try:
        from ml_model_guard import enforce_day as _enforce_day
        _verdict = _enforce_day(
            report_dir, _guard_date, raise_on_degenerate=False, logger=_log
        )
        if _verdict.is_degenerate:
            _exit_code = 1
    except ImportError as _ge:
        # 「闸装不上」与「闸响了」一样严重：两种情况都不该报告健康。
        # 沿用 Step 10/11/12 约定的 3 = 无法判定，绝不静默当成 0。
        _log.error(
            "🚨 ML 常数退化闸未装上（%s）——本次运行没有这道观测点，按「无法判定」处理",
            _ge,
        )
        _exit_code = 3

    # ── 自动同步 gh-pages（GitHub Pages 从此分支部署）──
    if _exit_code == 1:
        _log.error("已跳过 gh-pages 同步：当日 ML 概率退化成常数，先查模型再发布。")
    else:
        _sync_ghpages(tickers, successful_count)

    if _exit_code:
        import sys as _sys_exit
        _sys_exit.exit(_exit_code)


def _sync_ghpages(tickers: list, successful_count: int) -> None:
    """将当日 ML 增强报告同步到 gh-pages 分支并推送。"""
    import subprocess
    import os
    import re as _re
    if successful_count == 0:
        return
    repo = str(Path(__file__).parent)
    date_str = pdt_today()
    _ml_pat = _re.compile(r"^alpha-hive-[\w.-]+-ml-enhanced-\d{4}-\d{2}-\d{2}\.html$")
    _CORE = {"index.html", "dashboard-data.json", "manifest.json", "sw.js", "rss.xml", ".nojekyll", "chart.umd.min.js"}  # v0.41.0
    try:
        from is_trading_day import filename_is_nontrading_day as _fnt_dep
    except Exception:
        def _fnt_dep(_n):
            return False  # fail-safe：导入失败则不过滤，不误删
    files = [f for f in os.listdir(repo)
             if (f in _CORE or _ml_pat.match(f)
                 or (f.startswith("alpha-hive-daily-") and f.endswith((".json", ".md"))))
             and not _fnt_dep(f)]  # 非交易日幽灵报告（周末/假日）不部署
    if not files:
        _log.warning("gh-pages 同步：无静态文件")
        return

    idx = os.path.join(repo, ".git", "gh-pages-index")
    if os.path.exists(idx):
        os.remove(idx)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = idx
    try:
        for f in sorted(files):
            blob = subprocess.check_output(["git", "hash-object", "-w", f],
                                           cwd=repo).decode().strip()
            subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                            "100644", blob, f], env=env, cwd=repo, check=True)
        tree = subprocess.check_output(["git", "write-tree"], env=env, cwd=repo).decode().strip()
        parent_args = []
        parent = None
        try:
            parent = subprocess.check_output(
                ["git", "rev-parse", "gh-pages"], cwd=repo, stderr=subprocess.DEVNULL
            ).decode().strip()
            parent_args = ["-p", parent]
        except subprocess.CalledProcessError:
            pass
        # v0.45.2: 空提交闸。`git commit-tree` 是管道命令，不做 `git commit` 的
        # 「无变更则拒绝」检查——tree 与父提交相同也照样生成 commit。实测
        # 2026-08-15 的 8b16977 / 06d99cc / 0c00454 三条 commit message 都声称
        # "(12 tickers)"，`git show --name-only` 却是 0 个文件。message 里的
        # successful_count 是**声称值**，与 tree 实际变更无关，必须并排写出实测值。
        from report_deployer import ghpages_tree_delta
        _has_change, _n_changed = ghpages_tree_delta(repo, tree, parent)
        if not _has_change:
            _log.error(
                "🚨 gh-pages 无变更：新 tree 与父提交 %s 完全相同，跳过 commit。"
                "本次声称 %d 份 ML 报告成功——若非重复运行，说明报告文件根本没重新生成。",
                (parent or "")[:7], successful_count,
            )
        else:
            _msg = f"Deploy: ML reports {date_str} ({successful_count} tickers"
            _msg += f", {_n_changed} files changed)" if _n_changed >= 0 else ")"
            commit = subprocess.check_output(
                ["git", "commit-tree", tree] + parent_args + ["-m", _msg],
                cwd=repo
            ).decode().strip()
            subprocess.run(["git", "update-ref", "refs/heads/gh-pages", commit],
                           cwd=repo, check=True)
        # 无变更时仍尝试 push：相当于重试上一次可能失败的推送
        r = subprocess.run(["git", "push", "origin", "gh-pages", "--force"],
                           cwd=repo, capture_output=True, text=True)
        if r.returncode == 0:
            _log.info("gh-pages 同步成功 (%d 文件，实测变更 %s)",
                      len(files), _n_changed if _n_changed >= 0 else "未知")
        else:
            _log.warning("gh-pages push 失败: %s", r.stderr.strip()[:200])
    except Exception as e:
        _log.warning("gh-pages 同步异常: %s", e)
    finally:
        if os.path.exists(idx):
            os.remove(idx)


if __name__ == "__main__":
    main()
