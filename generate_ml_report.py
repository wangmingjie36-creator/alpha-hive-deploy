"""
🐝 Alpha Hive - ML 增强报告生成
将机器学习预测集成到高级分析报告
"""

import atexit
import json
import argparse
from datetime import datetime
from pathlib import Path
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from advanced_analyzer import AdvancedAnalyzer
from ml_predictor import (
    MLPredictionService,
    TrainingData,
)
from config import WATCHLIST
from hive_logger import PATHS, get_logger, pdt_today

_log = get_logger("ml_report")


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
    _model_file = PATHS.home / "ml_model_cache.json"  # 磁盘缓存文件（JSON，安全序列化）

    # ⭐ Task 3: 异步 HTML 生成（后台文件写入）
    _file_writer_pool = None   # 异步文件写入线程池
    _writer_lock = Lock()      # 文件写入锁（防止并发冲突）

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
                    MIN_REAL_SAMPLES = 10
                    real_data = self._build_real_training_data()
                    if len(real_data) >= MIN_REAL_SAMPLES:
                        _log.info("✅ [ML-REAL] 使用 %d 条真实验证数据训练 ML 模型", len(real_data))
                        self._training_data_source = "real"
                        self.ml_service.data_builder.historical_records = real_data
                    else:
                        if real_data:
                            _log.warning(
                                "⚠️ [ML-MIXED] 真实数据仅 %d 条（不足 %d），"
                                "回退到硬编码样本训练，预测置信度受限",
                                len(real_data), MIN_REAL_SAMPLES,
                            )
                        else:
                            _log.warning(
                                "⚠️ [ML-SAMPLE] 无真实验证数据，使用硬编码样本训练，"
                                "预测结果仅供参考（请积累 %d+ 条 T+7 验证记录后重训）",
                                MIN_REAL_SAMPLES,
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
        """从 pheromone.db 读取真实验证数据构建训练集（T+7 已验证）"""
        try:
            import sqlite3 as _sq3
            import json as _json
            from backtester import PredictionStore
            ps = PredictionStore()
            with _sq3.connect(ps.db_path) as conn:
                conn.row_factory = _sq3.Row
                rows = conn.execute("""
                    SELECT ticker, date, final_score, direction,
                           dimension_scores, iv_rank, put_call_ratio,
                           agent_directions,
                           return_t7, correct_t7
                    FROM predictions
                    WHERE checked_t7 = 1
                    ORDER BY date DESC
                    LIMIT 200
                """).fetchall()

            def _cat_qual(v):
                if v >= 8.5: return "A+"
                if v >= 7.5: return "A"
                if v >= 6.5: return "B+"
                if v >= 5.5: return "B"
                return "C"

            direction_map = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}
            result = []
            for r in rows:
                ds = _json.loads(r["dimension_scores"] or "{}")
                _ad = _json.loads(r["agent_directions"] or "{}") if r["agent_directions"] else {}
                _dir = r["direction"] or "neutral"
                if _ad:
                    _majority = sum(1 for d in _ad.values() if d == _dir)
                    _agree = _majority / len(_ad)
                else:
                    _agree = 0.5
                result.append(TrainingData(
                    ticker=r["ticker"],
                    date=r["date"],
                    crowding_score=ds.get("signal", 5.0) * 10,
                    catalyst_quality=_cat_qual(ds.get("catalyst", 5.0)),
                    momentum_5d=0.0,
                    volatility=5.0,
                    market_sentiment=(ds.get("sentiment", 5.0) - 5) * 20,
                    actual_return_3d=float(r["return_t7"] or 0) * 0.4,
                    actual_return_7d=float(r["return_t7"] or 0),
                    actual_return_30d=float(r["return_t7"] or 0) * 2.5,
                    win_3d=bool(r["correct_t7"]),
                    win_7d=bool(r["correct_t7"]),
                    win_30d=bool(r["correct_t7"]),
                    # v2 新特征
                    iv_rank=float(r["iv_rank"]) if r["iv_rank"] is not None else 50.0,
                    put_call_ratio=float(r["put_call_ratio"]) if r["put_call_ratio"] is not None else 1.0,
                    final_score=float(r["final_score"]) if r["final_score"] is not None else 5.0,
                    odds_score=ds.get("odds", 5.0),
                    risk_adj_score=ds.get("risk_adj", 5.0),
                    agent_agreement=_agree,
                    direction_encoded=direction_map.get(_dir, 0.0),
                ))
            return result
        except (ImportError, KeyError, TypeError, ValueError, OSError) as e:
            _log.debug("_build_real_training_data 失败: %s", e)
            return []

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
        self, ticker: str, realtime_metrics: dict
    ) -> dict:
        """生成 ML 增强的分析报告"""

        # 获取高级分析
        advanced_analysis = self.analyzer.generate_comprehensive_analysis(
            ticker, realtime_metrics
        )

        # 构建 ML 输入数据
        ml_input = self._prepare_ml_input(ticker, realtime_metrics, advanced_analysis)

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
            },
            "combined_recommendation": {
                **self._combine_recommendations(advanced_analysis, ml_prediction),
                "current_price": float(_current_price) if _current_price else None,
            },
        }

        return enhanced_report

    def _prepare_ml_input(
        self, ticker: str, metrics: dict, analysis: dict
    ) -> TrainingData:
        """为 ML 模型准备输入数据"""

        # 从实时数据中提取特征（有则用真实值，无则降级到合理默认）
        _yf = metrics.get("sources", {}).get("yahoo_finance", {})
        # BUG FIX: 原来的 _yf.get("short_interest_ratio", 50.0) * 10 当两个来源均缺失时
        # 返回 50.0 * 10 = 500，严重超出 [0,100] 范围，导致 crowding_penalty = 50，
        # 使 expected_7d = -23.24%（强烈看空），与评级矛盾。
        # 修复：当 short_interest_ratio 缺失时使用中性默认值 5.0（5.0 * 10 = 50），
        # 并对最终结果强制 clamp 到 [0, 100]。
        _sir = _yf.get("short_interest_ratio")
        _fallback_crowding = (_sir * 10) if _sir is not None else 50.0
        crowding_score = float(metrics.get("crowding_score", _fallback_crowding))
        crowding_score = min(100.0, max(0.0, crowding_score))  # 防御性边界保护
        catalyst_quality = analysis.get("recommendation", {}).get("rating", "B")
        momentum_5d = _yf.get("price_change_5d", 0.0) or 0.0

        # BUG-6 修复：volatility 从 swarm BuzzBee details 提取，fallback 才用 5.0
        _buzz_details = (
            self._swarm_cache.get(metrics.get("_ticker", ""), {})
            .get("agent_details", {})
            .get("BuzzBeeWhisper", {})
            .get("details", {})
        ) if hasattr(self, "_swarm_cache") else {}
        volatility = (
            _yf.get("volatility_20d")
            or _yf.get("atr_pct")
            or _buzz_details.get("volatility_20d")
            or analysis.get("options_analysis", {}).get("historical_volatility")
            or 5.0
        )

        # BUG-7 修复：market_sentiment 从 swarm BuzzBee details 提取
        _buzz_sentiment_raw = _buzz_details.get("sentiment_pct")  # 0-100
        _raw_sentiment = (
            (_buzz_sentiment_raw - 50) * 2  # 转为 -100~+100
            if _buzz_sentiment_raw is not None
            else metrics.get("sentiment_score", 0.0)
        )
        # BUG FIX: 原来 abs(_raw_sentiment) <= 10 → *10 的逻辑无法区分 0-1（概率）量表：
        #   0-1 范围  → *10 → 0-10（实际应 *100 → 0-100）
        #   0-10 范围 → *10 → 0-100 ✓  |  0-100 范围 → 不变 ✓
        # 修复：三段式量表自动识别，统一输出 -100~+100
        if abs(_raw_sentiment) <= 1.0 and _raw_sentiment != 0.0:
            market_sentiment = _raw_sentiment * 100   # 概率/归一化量表 (0~1 or -1~1)
        elif abs(_raw_sentiment) <= 10.0:
            market_sentiment = _raw_sentiment * 10    # Agent 评分量表 (0~10)
        else:
            market_sentiment = _raw_sentiment          # 已在 -100~+100 范围，直接使用

        # 映射评级到催化剂质量
        rating_to_quality = {
            "STRONG BUY": "A+",
            "BUY": "A",
            "HOLD": "B+",
            "AVOID": "C",
        }
        catalyst_quality = rating_to_quality.get(
            analysis.get("recommendation", {}).get("rating", "B"), "B"
        )

        # v2 新特征（从 analysis 上下文提取）
        _opts = analysis.get("options_analysis", {})
        _rec = analysis.get("recommendation", {})
        _ds = analysis.get("dimension_scores", {})
        _rating_dir = {"STRONG BUY": 1.0, "BUY": 0.5, "HOLD": 0.0, "AVOID": -1.0}

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
            final_score=_rec.get("score", 5.0),
            odds_score=_ds.get("odds", 5.0),
            risk_adj_score=_ds.get("risk_adj", 5.0),
            agent_agreement=0.5,  # 预测时无蜂群上下文
            direction_encoded=_rating_dir.get(_rec.get("rating", "HOLD"), 0.0),
        )

    def _generate_options_section_html(self, options: dict) -> str:
        """生成期权分析 HTML 部分"""
        if not options:
            return ""

        iv_rank = options.get("iv_rank", 50)
        iv_percentile = options.get("iv_percentile", 50)
        iv_current = options.get("iv_current", 25)
        put_call_ratio = options.get("put_call_ratio", 1.0)
        gamma_squeeze_risk = options.get("gamma_squeeze_risk", "medium")
        flow_direction = options.get("flow_direction", "neutral")
        options_score = options.get("options_score", 5.0)
        signal_summary = options.get("signal_summary", "信号平衡")
        unusual_activity = options.get("unusual_activity", [])
        key_levels = options.get("key_levels", {})

        # 判断 IV Rank 颜色
        if iv_rank < 30:
            iv_color = "#28a745"  # 绿色，低 IV
            iv_label = "低 IV"
        elif iv_rank > 70:
            iv_color = "#dc3545"  # 红色，高 IV
            iv_label = "高 IV"
        else:
            iv_color = "#ffc107"  # 黄色，中等 IV
            iv_label = "中等 IV"

        # 判断流向颜色
        if flow_direction == "bullish":
            flow_color = "#28a745"
        elif flow_direction == "bearish":
            flow_color = "#dc3545"
        else:
            flow_color = "#ffc107"

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
                <h2>📈 期权信号分析</h2>

                <div class="ml-section">
                    <h3 style="color: #667eea; margin-bottom: 15px;">⚡ 核心指标</h3>

                    <div class="metric">
                        <span class="metric-label">IV Rank</span>
                        <span class="metric-value" style="color: {iv_color};">
                            {iv_rank:.1f} ({iv_label})
                        </span>
                    </div>

                    <div class="metric">
                        <span class="metric-label">当前 IV</span>
                        <span class="metric-value">{iv_current:.2f}%</span>
                    </div>

                    <div class="metric">
                        <span class="metric-label">IV 百分位数</span>
                        <span class="metric-value">{iv_percentile:.1f}%</span>
                    </div>

                    <div class="metric">
                        <span class="metric-label">Put/Call Ratio</span>
                        <span class="metric-value">{put_call_ratio:.2f}</span>
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

                    <h3 style="color: #667eea; margin-top: 20px; margin-bottom: 15px;">📊 期权综合评分</h3>

                    <div style="text-align: center; padding: 20px; background: #f8f9fa; border-radius: 10px;">
                        <div style="font-size: 3.5em; font-weight: bold; color: #667eea; margin-bottom: 10px;">
                            {options_score:.1f}
                        </div>
                        <div style="font-size: 1.2em; color: #333; margin-bottom: 10px;">/ 10.0</div>
                        <div style="color: #666; font-size: 0.95em;">{signal_summary}</div>
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
        """合并人工和 ML 推荐"""

        human_prob = advanced_analysis.get("probability_analysis", {}).get(
            "win_probability_pct", 50
        )
        ml_prob = ml_prediction.get("prediction", {}).get("probability", 0.5) * 100

        # 加权平均（70% 高级分析 + 30% ML）
        combined_prob = human_prob * 0.7 + ml_prob * 0.3

        # 生成最终建议
        if combined_prob >= 75:
            rating = "STRONG BUY"
            action = "积极布局"
        elif combined_prob >= 65:
            rating = "BUY"
            action = "分批建仓"
        elif combined_prob >= 50:
            rating = "HOLD"
            action = "观察等待"
        else:
            rating = "AVOID"
            action = "回避或减仓"

        return {
            "human_probability": round(human_prob, 1),
            "ml_probability": round(ml_prob, 1),
            "combined_probability": round(combined_prob, 1),
            "rating": rating,
            "action": action,
            "confidence": f"{combined_prob:.1f}%",
            "reasoning": f"人工分析 {human_prob:.1f}% + ML 预测 {ml_prob:.1f}% = 综合 {combined_prob:.1f}%",
        }

    # ─────────────────────────────────────────────────────────────
    # 模板 C 7 章辅助方法
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def _dir_cn(d):
        return {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(d, d)

    @staticmethod
    def _dir_color(d):
        return {"bullish": "#28a745", "bearish": "#dc3545"}.get(d, "#ffc107")

    def _ch1_core_conclusion(self, swarm: dict, combined: dict, analysis: dict) -> str:
        """第1章：核心结论"""
        if not swarm and not combined:
            return ""
        final_score = swarm.get("final_score", combined.get("combined_probability", 50) / 10)
        direction = swarm.get("direction", "neutral")
        ab = swarm.get("agent_breakdown", {})
        resonance = swarm.get("resonance", {})
        combined_prob = combined.get("combined_probability", 50)
        rating = combined.get("rating", "HOLD")
        action = combined.get("action", "观察等待")
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
        summary_html = "".join(f"<p style='margin:6px 0;color:#555;'>{s}</p>" for s in summary_parts[:3])
        return f"""
        <div class="section">
            <h2>第 1 章：核心结论</h2>
            <div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap;margin-bottom:18px;">
                <div style="text-align:center;">
                    <span style="font-size:3em;font-weight:bold;color:{dir_color};">{final_score:.1f}</span>
                    <span style="font-size:1.2em;color:#888;">/10</span>
                    <div><span style="background:{dir_color};color:white;padding:4px 16px;border-radius:15px;font-weight:bold;">{dir_cn}</span></div>
                </div>
                <div style="flex:1;min-width:180px;">
                    <div class="metric"><span class="metric-label">综合胜率</span><span class="metric-value" style="color:{dir_color};">{combined_prob:.1f}%</span></div>
                    <div class="metric"><span class="metric-label">投票</span><span class="metric-value">{ab.get('bullish',0)}多 / {ab.get('bearish',0)}空 / {ab.get('neutral',0)}中</span></div>
                    <div class="metric"><span class="metric-label">建议</span><span class="metric-value">{rating} — {action}</span></div>
                </div>
            </div>
            {summary_html}
        </div>"""

    def _ch2_five_dim_table(self, swarm: dict) -> str:
        """第2章：五维评分明细"""
        if not swarm:
            return ""
        dim_scores = swarm.get("dimension_scores", {})
        if not dim_scores:
            return ""
        DIMS = [
            ("signal",   "🐝 信号强度 (Signal)",   0.30, "聪明钱 SEC Form4 / 机构持仓"),
            ("catalyst", "⏰ 催化剂 (Catalyst)",   0.20, "事件日历 / 财报 / 产品发布"),
            ("sentiment","📢 情绪 (Sentiment)",    0.20, "X 平台 / Reddit / 新闻情绪"),
            ("odds",     "🔮 赔率 (Odds)",          0.15, "期权 P/C / IV Rank / Polymarket"),
            ("risk_adj", "🛡️ 风险调整 (RiskAdj)",  0.15, "拥挤度 / 波动 / 交叉验证调整"),
        ]
        rows = ""
        total_weighted = 0.0
        for key, label, weight, hint in DIMS:
            score = dim_scores.get(key, 0)
            weighted = score * weight
            total_weighted += weighted
            bar_pct = int(score / 10 * 100)
            bar_color = "#28a745" if score >= 7 else ("#ffc107" if score >= 5 else "#dc3545")
            rows += f"""<tr>
                <td>{label}<br><small style="color:#999">{hint}</small></td>
                <td style="font-weight:bold;color:{bar_color}">{score:.1f}</td>
                <td>{weight:.0%}</td>
                <td style="font-weight:bold">{weighted:.2f}</td>
                <td><div style="background:#f0f0f0;border-radius:4px;height:8px;width:100px;display:inline-block;">
                    <div style="background:{bar_color};border-radius:4px;height:8px;width:{bar_pct}px;"></div>
                </div></td>
            </tr>"""
        score_lv = "高优先级 ✅" if total_weighted >= 7.5 else ("观察名单 👀" if total_weighted >= 6.0 else "不行动 ❌")
        rows += f"""<tr style="background:#f8f9ff;font-weight:bold;">
            <td><strong>综合 Opportunity Score</strong></td>
            <td style="color:#667eea;font-size:1.2em;">{total_weighted:.2f}</td>
            <td></td>
            <td style="color:#667eea;font-size:1.2em;">{total_weighted:.2f}</td>
            <td>{score_lv}</td>
        </tr>"""
        return f"""
        <div class="section">
            <h2>第 2 章：五维评分明细</h2>
            <table>
                <tr><th>维度</th><th>分数</th><th>权重</th><th>加权</th><th>进度</th></tr>
                {rows}
            </table>
            <p style="margin-top:12px;font-size:0.85em;color:#888;">公式：Score = 0.30×Signal + 0.20×Catalyst + 0.20×Sentiment + 0.15×Odds + 0.15×RiskAdj</p>
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
        momentum = details.get("momentum_5d", 0)
        score = ad.get("score", 0)
        direction = ad.get("direction", "neutral")
        trade_rows = ""
        for t in trades[:6]:
            shares = t.get("shares", 0)
            price = t.get("price", 0)
            amount = shares * price if price else 0
            trade_rows += f"""<tr>
                <td>{t.get('insider','')}</td>
                <td style="font-size:0.85em;color:#666">{t.get('title','')}</td>
                <td>{t.get('date','')}</td>
                <td>{shares:,.0f}</td>
                <td>{"$"+f"{price:.2f}" if price else "授予"}</td>
                <td>{"$"+f"{amount:,.0f}" if amount else "—"}</td>
            </tr>"""
        if not trade_rows:
            trade_rows = '<tr><td colspan="6" style="color:#999;text-align:center">暂无近期内部人交易记录</td></tr>'
        insider_sentiment = insider.get("sentiment", "neutral")
        ins_cn = self._dir_cn(insider_sentiment)
        ins_color = self._dir_color(insider_sentiment)
        mom_color = "#28a745" if momentum > 0 else "#dc3545"
        return f"""
        <div class="section">
            <h2>🐝 ScoutBee — 聪明钱侦察</h2>
            <div style="display:flex;gap:15px;flex-wrap:wrap;margin-bottom:15px;">
                <div class="stat"><div class="num" style="color:{self._dir_color(direction)}">{score:.1f}</div><div class="lbl">Signal 评分</div></div>
                <div class="stat"><div class="num" style="color:{ins_color}">{ins_cn}</div><div class="lbl">内部人情绪</div></div>
                <div class="stat"><div class="num">{crowding:.0f}</div><div class="lbl">拥挤度 /100</div></div>
                <div class="stat"><div class="num" style="color:{mom_color}">{momentum:+.2f}%</div><div class="lbl">5日动量</div></div>
            </div>
            <h3>近期内部人交易（Form 4）</h3>
            <table>
                <tr><th>内部人</th><th>职位</th><th>日期</th><th>股数</th><th>均价</th><th>金额</th></tr>
                {trade_rows}
            </table>
            <p style="margin-top:10px;font-size:0.85em;color:#666;"><strong>关键判断：</strong>{ad.get('discovery','')}</p>
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
        score = _safe(ad.get("score", 0)) if ad else 0
        direction = ad.get("direction", "neutral") if ad else "neutral"
        iv_rank = _safe(opts.get("iv_rank", 0))
        iv_curr = _safe(opts.get("iv_current", opts.get("iv_curr", 0)))
        pc = _safe(opts.get("put_call_ratio", 0))
        oi = _safe(opts.get("total_oi", 0))
        gex = opts.get("gamma_squeeze_risk") or "—"
        flow = opts.get("flow_direction") or opts.get("options_score") or "—"
        skew = _safe(opts.get("iv_skew_ratio", opts.get("iv_skew", 0)))
        # v0.27.1 bug fix: dict.get() 不会用默认值若 key 存在但 value=None；用 `or` 保护
        unusual = opts.get("unusual_activity") or []
        key_levels = opts.get("key_levels") or {}
        support = key_levels.get("support") or []
        resist = key_levels.get("resistance") or []
        pc_color = "#28a745" if pc < 0.8 else ("#dc3545" if pc > 1.2 else "#ffc107")
        unusual_rows = ""
        for u in unusual[:5]:
            bullish = u.get("bullish", True)
            emo = "🟢" if bullish else "🔴"
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
        if isinstance(_near_mp_raw, dict):
            _v = _near_mp_raw.get("max_pain")
            if isinstance(_v, (int, float)) and _v > 0:
                _near_mp_val = float(_v)
                _d = _near_mp_raw.get("distance_pct")
                if isinstance(_d, (int, float)):
                    _near_mp_dist = float(_d)
        elif isinstance(_near_mp_raw, (int, float)) and _near_mp_raw > 0:
            _near_mp_val = float(_near_mp_raw)
        if _near_mp_val is not None:
            _dist_txt = f"（距现价 {_near_mp_dist:+.1f}%）" if _near_mp_dist is not None else ""
            _near_max_pain_html = (
                f'<div class="stat"><div class="num" style="color:#667eea">${_near_mp_val:.0f}</div>'
                f'<div class="lbl">近端磁吸目标价{_dist_txt}</div></div>'
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
                    badge = f'<span style="background:#eee;color:#666;font-size:0.75em;padding:1px 5px;border-radius:3px;margin-left:4px;">{dom_exp}</span>' if dom_exp else ""
                    color = "#dc3545" if is_call else "#28a745"
                    rows += (
                        f'<tr><td style="color:{color};font-weight:600;">${sk_f:.0f}</td>'
                        f'<td>{int(oi_v):,}</td>'
                        f'<td style="color:#666;">{pct_txt}{badge}</td></tr>'
                    )
                return rows

            _call_walls_html = _wall_rows(_full_chain.get("top_call_oi", []), True, current_price)
            _put_walls_html = _wall_rows(_full_chain.get("top_put_oi", []), False, current_price)
            _fc_pc_color = "#28a745" if _fc_pc < 0.8 else ("#dc3545" if _fc_pc > 1.2 else "#ffc107")

            _full_oi_html = f"""
            <h3 style="color:#667eea;margin-top:18px;">📊 全链 OI 结构（{_fc_expiry_count} 个到期日聚合，含 LEAPS 远期参考）</h3>
            <div class="grid-4" style="margin-bottom:12px;">
                <div class="stat"><div class="num" style="color:#888">${_fc_max_pain:.0f}</div><div class="lbl">全链 Max Pain（远期参考）</div></div>
                <div class="stat"><div class="num" style="color:{_fc_pc_color}">{_fc_pc:.2f}</div><div class="lbl">全链 P/C Ratio</div></div>
                <div class="stat"><div class="num">{_fc_total:,}</div><div class="lbl">全链总 OI</div></div>
                <div class="stat"><div class="num">{_fc_call_oi:,} / {_fc_put_oi:,}</div><div class="lbl">Call / Put OI</div></div>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:12px;">
                <div>
                    <strong style="color:#dc3545;">Top5 Call 阻力墙（全链）</strong>
                    <table style="font-size:0.88em;margin-top:6px;">
                        <tr><th>行权价</th><th>OI</th><th>距现价 / 主导到期</th></tr>
                        {_call_walls_html or '<tr><td colspan="3" style="color:#999;text-align:center;">—</td></tr>'}
                    </table>
                </div>
                <div>
                    <strong style="color:#28a745;">Top5 Put 支撑墙（全链）</strong>
                    <table style="font-size:0.88em;margin-top:6px;">
                        <tr><th>行权价</th><th>OI</th><th>距现价 / 主导到期</th></tr>
                        {_put_walls_html or '<tr><td colspan="3" style="color:#999;text-align:center;">—</td></tr>'}
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
                    color = "#dc3545" if is_call else "#28a745"
                    if not walls:
                        return '<tr><td colspan="3" style="color:#999;text-align:center;">—</td></tr>'
                    return "".join(
                        f'<tr><td style="color:{color};font-weight:600;">${w["strike"]:.0f}</td>'
                        f'<td>{int(w["oi"]):,}</td>'
                        f'<td style="color:#666;">{w["pct"]:+.1f}%</td></tr>'
                        for w in walls
                    )
                _near_walls_html = f"""
                <h3 style="color:#667eea;margin-top:18px;">🎯 近 30 天到期 OI 墙（现场聚合，磁吸效应最强）</h3>
                <div style="background:#fff3cd;border-left:3px solid #ffc107;padding:8px 12px;margin-bottom:10px;font-size:0.88em;color:#856404;">
                    近端 P/C = <strong>{_near_pc_str}</strong> | Call OI {_near_call_sum:,} | Put OI {_near_put_sum:,}
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:12px;">
                    <div>
                        <strong style="color:#dc3545;">近端 Top3 Call 阻力</strong>
                        <table style="font-size:0.88em;margin-top:6px;">
                            <tr><th>行权价</th><th>OI</th><th>距现价</th></tr>
                            {_row_near(_near_call_top, True)}
                        </table>
                    </div>
                    <div>
                        <strong style="color:#28a745;">近端 Top3 Put 支撑</strong>
                        <table style="font-size:0.88em;margin-top:6px;">
                            <tr><th>行权价</th><th>OI</th><th>距现价</th></tr>
                            {_row_near(_near_put_top, False)}
                        </table>
                    </div>
                </div>
                """

        # ── v0.27.0：IV 期限结构 + IV-RV 价差 ───────────────────────────
        _iv_term = opts.get("iv_term_structure") or {}
        _iv_rv_signal = opts.get("iv_rv_signal", "")
        _iv_rv_spread = _safe(opts.get("iv_rv_spread"), 0)
        _rv_30d = _safe(opts.get("rv_30d"), 0)
        _iv_struct_html = ""
        if (isinstance(_iv_term, dict) and _iv_term) or _iv_rv_signal:
            _shape = (_iv_term or {}).get("shape") or (_iv_term or {}).get("term_shape") or "—"
            _shape_color = {"contango": "#28a745", "backwardation": "#dc3545", "flat": "#ffc107"}.get(str(_shape).lower(), "#666")
            _shape_cn = {"contango": "Contango（远月>近月，市场预期平稳）",
                         "backwardation": "Backwardation（近月>远月，短期不确定性高）",
                         "flat": "Flat（期限结构平坦）"}.get(str(_shape).lower(), str(_shape))
            _front_iv = _safe((_iv_term or {}).get("front_iv"), 0)
            _back_iv = _safe((_iv_term or {}).get("back_iv"), 0)
            _iv_rv_color = "#dc3545" if _iv_rv_spread > 10 else ("#28a745" if _iv_rv_spread < -5 else "#666")
            _iv_struct_html = f"""
            <h3 style="color:#667eea;margin-top:18px;">📐 IV 期限结构 + IV-RV 价差</h3>
            <div class="grid-4" style="margin-bottom:12px;">
                <div class="stat"><div class="num" style="color:{_shape_color};font-size:1.3em;">{str(_shape).upper()}</div><div class="lbl">期限结构形态</div></div>
                <div class="stat"><div class="num">{_front_iv:.1f}% / {_back_iv:.1f}%</div><div class="lbl">近月 IV / 远月 IV</div></div>
                <div class="stat"><div class="num" style="color:{_iv_rv_color}">{_iv_rv_spread:+.1f}pp</div><div class="lbl">IV-RV 价差</div></div>
                <div class="stat"><div class="num">{_rv_30d:.1f}%</div><div class="lbl">30日实现波动率</div></div>
            </div>
            <p style="font-size:0.86em;color:#555;margin-bottom:10px;">
                <strong>形态解读：</strong>{_shape_cn}<br/>
                <strong>IV-RV 信号：</strong>{_iv_rv_signal or '—'}
            </p>
            """

        # ── v0.27.0：Gamma 到期日历 ─────────────────────────────────────
        _gamma_cal = opts.get("gamma_calendar") or {}
        _gamma_cal_html = ""
        if isinstance(_gamma_cal, dict) and _gamma_cal:
            _pin_risk = _gamma_cal.get("pin_risk") or _gamma_cal.get("pin_strike")
            _next_exp = _gamma_cal.get("next_major_expiry") or _gamma_cal.get("nearest_expiry") or "—"
            _charm = _gamma_cal.get("charm_direction") or _gamma_cal.get("charm") or "—"
            _oi_concentration = _safe(_gamma_cal.get("oi_concentration_pct"), 0)
            _pin_txt = f"${float(_pin_risk):.0f}" if isinstance(_pin_risk, (int, float)) and _pin_risk else "—"
            _gamma_cal_html = f"""
            <h3 style="color:#667eea;margin-top:18px;">📅 Gamma 到期日历</h3>
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
            f'<div class="stat"><div class="num" style="color:{pc_color}">{pc:.2f}</div><div class="lbl">近端 P/C Ratio</div></div>'
            f'<div class="stat"><div class="num">{iv_rank:.1f}%</div><div class="lbl">IV Rank</div></div>'
        )
        if _near_max_pain_html:
            _hero_cards += _near_max_pain_html
        else:
            _hero_cards += f'<div class="stat"><div class="num">{iv_curr:.1f}%</div><div class="lbl">当前 IV</div></div>'

        return f"""
        <div class="section">
            <h2>🔮 OracleBee — 期权市场预期</h2>
            <div class="grid-4" style="margin-bottom:15px;">
                {_hero_cards}
            </div>
            <table style="margin-bottom:12px;">
                <tr><th>指标</th><th>数值</th><th>信号</th></tr>
                <tr><td>Gamma 压榨风险</td><td>{gex}</td><td>{"⚠️ 高" if str(gex).lower() in ("high","很高") else "✅ 可控"}</td></tr>
                <tr><td>期权流方向</td><td>{flow}</td><td>{"🟢 看多" if str(flow).lower() in ("bullish","看多") else ("🔴 看空" if str(flow).lower() in ("bearish","看空") else "—")}</td></tr>
                <tr><td>IV 偏斜比</td><td>{skew:.2f}</td><td>{"⚠️ 看跌溢价" if skew > 1.2 else "✅ 正常"}</td></tr>
                <tr><td>近端总持仓量</td><td>{oi:,}</td><td>—</td></tr>
            </table>
            {_near_walls_html}
            {_full_oi_html}
            {_iv_struct_html}
            {_gamma_cal_html}
            {f'<h3>异常期权活动 Top5</h3><ul>{unusual_rows}</ul>' if unusual_rows else ''}
            {f'<h3>近端关键价位（OracleBee key_levels）</h3><p>支撑：{support_txt or "—"}</p><p>压力：{resist_txt or "—"}</p>' if (support_txt or resist_txt) else ''}
            <p style="margin-top:10px;font-size:0.85em;color:#666;"><strong>关键判断：</strong>{ad.get('discovery','') if ad else ''}</p>
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
            sev_colors = {"critical": "#dc3545", "high": "#fd7e14", "medium": "#ffc107", "low": "#28a745"}
            cat_rows = ""
            for c in cats_sorted:
                sev = c.get("severity", "medium")
                sev_color = sev_colors.get(sev, "#888")
                days = c.get("days_until", 0)
                days_txt = f"{days}天后" if days >= 0 else f"{abs(days)}天前"
                cat_rows += f"""<tr>
                    <td style="color:{sev_color};font-weight:bold">{c.get('event','')}</td>
                    <td>{c.get('date','')}</td>
                    <td>{days_txt}</td>
                    <td><span style="background:{sev_color};color:white;padding:2px 8px;border-radius:10px;font-size:0.8em">{sev}</span></td>
                </tr>"""
            timeline_html = f"""
            <div style="background:#f8f9ff;border-radius:8px;padding:15px;margin:12px 0;overflow-x:auto;">
                <pre style="font-family:monospace;font-size:0.8em;color:#333;line-height:1.8;margin:0">   {labels_top}
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
        if analyst:
            curr = analyst.get("current_price", 0)
            mean = analyst.get("target_mean", 0)
            low = analyst.get("target_low", 0)
            high = analyst.get("target_high", 0)
            upside = analyst.get("upside_pct", ((mean - curr) / curr * 100) if curr else 0)
            upside_color = "#28a745" if upside > 0 else "#dc3545"
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
            <h2>⏰ ChronosBee — 催化剂时间线</h2>
            <div style="margin-bottom:12px;">
                <span class="stat" style="display:inline-block;margin-right:10px;">
                    <span class="num" style="color:{self._dir_color(direction)}">{score:.1f}</span>
                    <span class="lbl"> Catalyst 评分</span>
                </span>
                <span style="color:#666;font-size:0.9em">检测到 {len(catalysts)} 个催化剂</span>
            </div>
            {timeline_html if timeline_html else '<p style="color:#999">暂无催化剂数据</p>'}
            {analyst_html}
            <p style="margin-top:10px;font-size:0.85em;color:#666;"><strong>关键判断：</strong>{ad.get('discovery','')}</p>
        </div>"""

    def _ch3_buzz(self, agent_details: dict) -> str:
        """第3章 BuzzBee — 情绪与叙事"""
        ad = agent_details.get("BuzzBeeWhisper", {})
        if not ad:
            return ""
        det = ad.get("details", {})
        score = ad.get("score", 0)
        direction = ad.get("direction", "neutral")
        sentiment_pct = det.get("sentiment_pct", det.get("sentiment_score", 50))
        momentum = det.get("momentum_5d", 0)
        vol_ratio = det.get("volume_ratio", 1)
        reddit = det.get("reddit", {})
        fear_greed = det.get("fear_greed_index", det.get("components", {}).get("fear_greed", None))
        sent_color = "#28a745" if sentiment_pct > 60 else ("#dc3545" if sentiment_pct < 40 else "#ffc107")
        mom_color = "#28a745" if momentum > 0 else "#dc3545"
        fg_text = ""
        if fear_greed is not None:
            fg_label = "极度恐惧" if fear_greed < 25 else ("恐惧" if fear_greed < 45 else ("中性" if fear_greed < 55 else ("贪婪" if fear_greed < 75 else "极度贪婪")))
            fg_color = "#28a745" if fear_greed > 55 else ("#dc3545" if fear_greed < 45 else "#ffc107")
            fg_text = f'<div class="stat"><div class="num" style="color:{fg_color}">{fear_greed}</div><div class="lbl">恐贪指数 ({fg_label})</div></div>'
        reddit_html = ""
        if reddit:
            reddit_html = f"""<p style="margin-top:10px;">Reddit 热度：<strong>第{reddit.get('rank','—')}名</strong> | 提及量：<strong>{reddit.get('mentions','—')}</strong> | 状态：<strong>{reddit.get('buzz','—')}</strong></p>"""
        # 叙事列表
        disc = ad.get("discovery", "")
        bullets = [b.strip() for b in disc.split("|") if b.strip()] if disc else []
        bullets_html = "".join(f"<li>{'✅' if i==0 else '📊'} {b}</li>" for i, b in enumerate(bullets[:5]))
        return f"""
        <div class="section">
            <h2>📢 BuzzBee — 情绪与叙事</h2>
            <div class="grid-4" style="margin-bottom:15px;">
                <div class="stat"><div class="num" style="color:{self._dir_color(direction)}">{score:.1f}</div><div class="lbl">Sentiment 评分</div></div>
                <div class="stat"><div class="num" style="color:{sent_color}">{sentiment_pct:.0f}%</div><div class="lbl">正面情绪占比</div></div>
                <div class="stat"><div class="num" style="color:{mom_color}">{momentum:+.2f}%</div><div class="lbl">5日动量</div></div>
                <div class="stat"><div class="num">{vol_ratio:.2f}×</div><div class="lbl">成交量比</div></div>
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
        strength_color = "#28a745" if strength >= 70 else ("#ffc107" if strength >= 40 else "#dc3545")
        adv_li = "".join(f"<li>✅ {a}</li>" for a in advantages[:5])
        thr_li = "".join(f"<li>⚠️ {t}</li>" for t in threats[:5])
        comp_tags = " ".join(f'<span style="background:#e8e8f0;padding:3px 10px;border-radius:10px;font-size:0.85em">{c}</span>' for c in competitors[:5])
        return f"""
        <div class="section">
            <h2>🤖 RivalBee — 竞争格局</h2>
            <div class="grid-4" style="margin-bottom:15px;">
                <div class="stat"><div class="num">{industry}</div><div class="lbl">行业</div></div>
                <div class="stat"><div class="num">{position}</div><div class="lbl">市场地位</div></div>
                <div class="stat"><div class="num" style="color:{strength_color}">{strength}</div><div class="lbl">竞争实力 /100</div></div>
                <div class="stat"><div class="num">{len(competitors)}</div><div class="lbl">主要竞争对手</div></div>
            </div>
            {f'<p>竞争对手：{comp_tags}</p>' if comp_tags else ''}
            <div class="grid-2" style="margin-top:15px;">
                <div><h3 style="color:#28a745;">护城河优势</h3><ul>{adv_li}</ul></div>
                <div><h3 style="color:#dc3545;">竞争威胁</h3><ul>{thr_li}</ul></div>
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
                    row_cells += "<td style='color:#ccc;text-align:center'>—</td>"
                elif row_dim in resonant_dims and col_dim in resonant_dims:
                    row_cells += "<td style='text-align:center;color:#28a745'>✅</td>"
                elif conflict_level in ("high", "severe") and (row_dim not in resonant_dims or col_dim not in resonant_dims):
                    row_cells += "<td style='text-align:center;color:#ffc107'>⚠️</td>"
                else:
                    row_cells += "<td style='text-align:center;color:#ccc'>—</td>"
            matrix_rows += f"<tr>{row_cells}</tr>"
        conflict_cn = {"low": "低 ✅", "moderate": "中 ⚠️", "high": "高 ❌", "severe": "严重 ⛔"}.get(conflict_level, conflict_level)
        return f"""
        <div class="section">
            <h2>🛡️ GuardBee — 交叉验证</h2>
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
            <p style="margin-top:8px;font-size:0.82em;color:#888;">✅ 同向共振 | ⚠️ 存在冲突 | — 中性/无关</p>
            <p style="margin-top:10px;font-size:0.85em;color:#666;"><strong>共振结论：</strong>
                {resonance.get('supporting_agents', 0)} 个 Agent 同向（{', '.join(resonant_dims)}），
                置信度提升 {resonance.get('confidence_boost', 0)}%
            </p>
            <p style="font-size:0.85em;color:#666;"><strong>关键判断：</strong>{ad.get('discovery','') if ad else ''}</p>
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
        iv_skew = det.get("iv_skew_ratio", det.get("iv_skew", 0))
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
            items += f'<li style="margin:10px 0;padding:10px;background:#fff5f5;border-left:3px solid #dc3545;border-radius:4px;"><strong>{i}.</strong> {s}</li>'
        return f"""
        <div class="section">
            <h2>🐻 BearBee — 看空对冲</h2>
            <div class="grid-4" style="margin-bottom:15px;">
                <div class="stat"><div class="num" style="color:#dc3545">{score:.1f}</div><div class="lbl">看空蜂评分</div></div>
                <div class="stat"><div class="num" style="color:{'#dc3545' if bear_score>=6 else '#ffc107'}">{bear_score:.1f}/10</div><div class="lbl">看空强度</div></div>
                <div class="stat"><div class="num">{iv_skew:.2f}</div><div class="lbl">IV Skew 比</div></div>
                <div class="stat"><div class="num">{'⛔' if bear_score>=7 else ('⚠️' if bear_score>=5 else '✅')}</div><div class="lbl">风险等级</div></div>
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
            f"<tr><td>{c[0]}</td><td>{c[1]}</td><td style='color:#888;font-size:0.85em'>{c[2]}</td></tr>"
            for c in break_conditions[:5]
        )
        return f"""
        <div class="section">
            <h2>第 4 章：投资假设与失效条件</h2>
            <h3>核心 Thesis</h3>
            <blockquote style="border-left:4px solid #667eea;padding:12px 18px;background:#f8f9ff;border-radius:0 8px 8px 0;margin:10px 0;color:#333;font-style:italic;">
                {thesis}
            </blockquote>
            <h3 style="margin-top:18px;">失效条件（Thesis Break）</h3>
            <table>
                <tr><th>条件</th><th>触发阈值</th><th>监控方式</th></tr>
                {cond_rows}
            </table>
        </div>"""

    def _ch5_scenarios(self, analysis: dict, swarm: dict) -> str:
        """第5章：情景推演（4场景 + 概率加权期望收益）"""
        hist = analysis.get("historical_analysis", {})
        exp = hist.get("expected_returns", {})
        pos = analysis.get("position_management", {})
        sl = pos.get("stop_loss", {})
        tp = pos.get("take_profit", {})
        # 当前价：从 agent_details 或 stop_loss 反推（防 None 污染）
        scout = swarm.get("agent_details", {}).get("ScoutBeeNova", {})
        curr_price = float(scout.get("details", {}).get("price") or 0) if scout else 0
        if not curr_price and isinstance(sl, dict):
            conservative = sl.get("conservative", 0)
            curr_price = conservative / 0.97 if conservative else 0
        if not curr_price:
            curr_price = 100  # 防零
        # 期望收益数据
        gain_max = exp.get("max_gain", {}).get("mean", 0) or 20
        gain_7d = exp.get("expected_7d", {}).get("mean", 0) or 5
        drawdown = exp.get("max_drawdown", {}).get("mean", 0) or -10
        drawdown_min = exp.get("max_drawdown", {}).get("min", drawdown * 1.5)
        # 4 场景
        scenarios = [
            ("🟢 强多", 25, curr_price * (1 + gain_max / 100), "催化剂超预期 + 出口管制缓和"),
            ("🟢 温和多", 45, curr_price * (1 + gain_7d / 100), "催化剂符合预期，指引维持"),
            ("🟡 震荡", 20, curr_price * (1 + drawdown / 200), "获利回吐，等待下一催化剂"),
            ("🔴 回调", 10, curr_price * (1 + drawdown_min / 100), "政策恶化 或 竞品重大突破"),
        ]
        exp_price = sum(prob / 100 * price for _, prob, price, _ in scenarios)
        exp_return = (exp_price - curr_price) / curr_price * 100 if curr_price else 0
        exp_color = "#28a745" if exp_return > 0 else "#dc3545"
        rows = "".join(
            f"""<tr>
                <td>{icon}</td>
                <td>{prob}%</td>
                <td>${price:.2f}</td>
                <td style="color:{'#28a745' if price>curr_price else '#dc3545'}">{(price-curr_price)/curr_price*100:+.1f}%</td>
                <td style="font-size:0.85em;color:#666">{trigger}</td>
            </tr>"""
            for icon, prob, price, trigger in scenarios
        )
        return f"""
        <div class="section">
            <h2>第 5 章：情景推演</h2>
            <table>
                <tr><th>情景</th><th>概率</th><th>目标价</th><th>涨跌幅</th><th>触发条件</th></tr>
                {rows}
                <tr style="background:#f8f9ff;font-weight:bold;">
                    <td colspan="2">概率加权期望价格</td>
                    <td style="color:{exp_color}">${exp_price:.2f}</td>
                    <td style="color:{exp_color}">{exp_return:+.1f}%</td>
                    <td>from ${curr_price:.2f}</td>
                </tr>
            </table>
            <p style="margin-top:10px;font-size:0.85em;color:#888;">
                期望价格 = Σ(概率 × 情景价格) = {' + '.join(f'{p}%×${pr:.0f}' for _,p,pr,_ in scenarios)} = <strong style="color:{exp_color}">${exp_price:.2f}</strong>
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
        def risk_level(val, high_thr, med_thr, high_lbl="高", med_lbl="中", low_lbl="低"):
            if val >= high_thr:
                return f"🔴 {high_lbl}"
            if val >= med_thr:
                return f"🟡 {med_lbl}"
            return f"🟢 {low_lbl}"
        rows = [
            ("监管风险", risk_level(1 if conflict_level in ("high","severe") else 0, 1, 0.5), "AI 芯片出口管制 / 政策变化风险"),
            ("市场情绪风险", risk_level(bear_score, 7, 5), f"看空强度 {bear_score:.1f}/10，{'临近催化剂' if imminent else '无近期催化剂'}"),
            ("估值压缩风险", risk_level(crowding, 70, 50), f"拥挤度 {crowding:.0f}/100（{'偏高' if crowding>70 else ('适中' if crowding>40 else '偏低')}）"),
            ("流动性风险", "🟢 低", "大盘股，日均成交量充足"),
            ("期权事件风险", risk_level(1 if str(gex).lower() in ("high","很高","medium") else 0, 1, 0.5), f"Gamma 压榨风险：{gex}，IV Rank {iv_rank:.1f}%"),
            ("催化剂风险", risk_level(len(imminent), 2, 1), f"7 天内催化剂 {len(imminent)} 个：{', '.join(c.get('event','') for c in imminent[:2])}"),
        ]
        risk_rows = "".join(
            f"<tr><td>{name}</td><td>{level}</td><td style='font-size:0.85em;color:#666'>{detail}</td></tr>"
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
                f'<h3 style="color:#667eea;font-size:1em;margin-bottom:10px;">📊 估值快照</h3>'
                f'<div style="font-size:0.9em;color:#555;line-height:1.8;">{_val_grid}</div>'
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
                f'<span style="background:linear-gradient(135deg,#667eea,#764ba2);color:white;'
                f'font-size:0.75em;font-weight:700;padding:2px 8px;border-radius:10px;white-space:nowrap;">'
                f'{t[0]}</span>'
                f'<span style="font-size:0.88em;color:#444;">{t[2]}</span></div>'
                for t in _top3
            )
            parts.append(
                f'<div class="section" style="padding:18px 25px;">'
                f'<h3 style="color:#667eea;font-size:1em;margin-bottom:10px;">🎯 核心论点 Top-3</h3>'
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

        # 评级颜色
        rating = combined.get('rating', 'HOLD')
        if rating == 'STRONG BUY':
            rating_color = '#28a745'
        elif rating == 'BUY':
            rating_color = '#17a2b8'
        elif rating == 'AVOID':
            rating_color = '#dc3545'
        else:
            rating_color = '#ffc107'

        # ML 预测部分（提前计算，用于修正蜂群表中 RivalBee 的旧概率值）
        pred = ml_pred.get('prediction', {})
        ml_prob_val = pred.get('probability', 0.5) * 100

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
        win_prob    = prob.get('win_probability_pct', 50)
        risk_reward = prob.get('risk_reward_ratio', 1.0)
        position    = analysis.get('position_management', {})
        stop_loss   = position.get('stop_loss', {})
        take_profit = position.get('take_profit', {})
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
                if isinstance(v, dict):
                    tp_rows += (f"<tr><td>{k}</td><td>${v.get('price',0):.2f}</td>"
                                f"<td>+{v.get('gain_pct',0):.0f}%</td>"
                                f"<td>{v.get('sell_ratio',0):.0%} | {v.get('reason','')}</td></tr>")
                elif isinstance(v, (int, float)):
                    tp_rows += f"<tr><td>{k}</td><td>${v:.2f}</td><td></td><td></td></tr>"
        elif isinstance(take_profit, list):
            for item in take_profit:
                if isinstance(item, dict):
                    tp_rows += f"<tr><td>{item.get('level','')}</td><td>${item.get('price',0):.2f}</td><td></td><td></td></tr>"

        holding_txt = ""
        if holding:
            holding_txt = f'<p style="margin-top:15px;">最佳持仓周期：<strong>{holding.get("note", holding) if isinstance(holding, dict) else holding}</strong></p>'

        sl_tp_html = ""
        if sl_rows or tp_rows:
            sl_tp_html = f"""
            <div style="margin-bottom:20px;">
                <div class="grid-2">
                    <div><h3 style="color:#dc3545;">止损位</h3><table>{sl_rows}</table></div>
                    <div><h3 style="color:#28a745;">止盈位</h3>
                        <table><tr><th>档位</th><th>价格</th><th>涨幅</th><th>操作</th></tr>{tp_rows}</table>
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
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh; padding: 20px;
        }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        .header {{
            background: white; border-radius: 15px; padding: 35px;
            margin-bottom: 25px; box-shadow: 0 10px 40px rgba(0,0,0,0.1);
            text-align: center;
        }}
        .header h1 {{ font-size: 2.2em; color: #667eea; margin-bottom: 8px; }}
        .header .rating {{
            display: inline-block; padding: 8px 25px; border-radius: 25px;
            color: white; font-size: 1.3em; font-weight: bold;
            background: {rating_color}; margin: 10px 0;
        }}
        .section {{
            background: white; border-radius: 12px; padding: 25px;
            margin-bottom: 20px; box-shadow: 0 5px 20px rgba(0,0,0,0.08);
        }}
        .section h2 {{
            color: #667eea; font-size: 1.4em; margin-bottom: 18px;
            padding-bottom: 10px; border-bottom: 2px solid #f0f0f0;
        }}
        .section h3 {{ color: #555; margin: 15px 0 10px; font-size: 1.1em; }}
        .grid-4 {{
            display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px;
        }}
        .grid-2 {{
            display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
        }}
        .stat {{
            text-align: center; padding: 15px; border-radius: 10px;
            background: linear-gradient(135deg, #f8f9fa, #fff);
            border: 1px solid #e8e8e8;
        }}
        .stat .num {{ font-size: 1.8em; font-weight: bold; color: #667eea; }}
        .stat .lbl {{ font-size: 0.85em; color: #888; margin-top: 5px; }}
        .metric {{
            display: flex; justify-content: space-between; align-items: center;
            padding: 10px 0; border-bottom: 1px solid #f5f5f5;
        }}
        .metric-label {{ color: #666; font-weight: 500; }}
        .metric-value {{ font-weight: bold; color: #333; }}
        table {{
            width: 100%; border-collapse: collapse; margin-top: 10px;
        }}
        th, td {{
            padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee;
        }}
        th {{
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white; font-weight: 600; font-size: 0.9em;
        }}
        ul {{ padding-left: 20px; margin: 10px 0; }}
        li {{ margin: 6px 0; color: #444; line-height: 1.6; }}
        details {{ background:white; border-radius:12px; padding:20px;
                   margin-bottom:20px; box-shadow:0 5px 20px rgba(0,0,0,0.08); }}
        details summary {{ cursor:pointer; color:#667eea; font-weight:bold;
                           font-size:1.1em; user-select:none; }}
        .footer {{
            text-align: center; color: rgba(255,255,255,0.85);
            margin-top: 20px; font-size: 0.9em;
        }}
        @media (max-width: 600px) {{
            .grid-4 {{ grid-template-columns: repeat(2, 1fr); }}
            .grid-2 {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>
<div class="container">
    <!-- 头部 -->
    <div class="header">
        <h1>🐝 {ticker} 深度研究报告</h1>
        <div class="rating">{rating} — {combined['action']}</div>
        <p style="color:#888; margin-top:10px;">
            {self.timestamp.strftime('%Y-%m-%d %H:%M')} | Alpha Hive 蜂群智能
        </p>
        <p style="color:#aaa; font-size:0.85em; margin-top:6px;">
            综合胜率 {combined['combined_probability']:.1f}% &nbsp;|&nbsp;
            风险回报比 {risk_reward:.2f} &nbsp;|&nbsp;
            ML 预测 {ml_prob_val:.1f}%
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
        <summary>📊 详细数据（止损止盈 / 期权信号 / ML 特征）</summary>
        {sl_tp_html}
        {options_html}
        {ml_feat_html}
    </details>

    <!-- 免责声明 -->
    <div class="section" style="background:#fff3cd; border:1px solid #ffc107;">
        <p style="color:#856404; font-size:0.9em;">
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
                _real_price = 100.0
                _real_change = 0.0
                # 优先复用 swarm 的 yfinance 缓存（避免重复 API 调用）
                try:
                    from swarm_agents import get_cached_stock_data as _get_cached
                    _cached = _get_cached(ticker)
                except ImportError:
                    _cached = None
                if _cached and _cached.get("price", 0) > 0:
                    _real_price = _cached["price"]
                    _real_change = _cached.get("momentum_5d", 0.0)
                else:
                    try:
                        import yfinance as _yf
                        _t = _yf.Ticker(ticker)
                        _hist = _t.history(period="5d")
                        if not _hist.empty:
                            _real_price = float(_hist["Close"].iloc[-1])
                            if len(_hist) >= 5:
                                _real_change = (_hist["Close"].iloc[-1] / _hist["Close"].iloc[-5] - 1) * 100
                            elif len(_hist) >= 2:
                                _real_change = (_hist["Close"].iloc[-1] / _hist["Close"].iloc[0] - 1) * 100
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
            enhanced_report = report_gen.generate_ml_enhanced_report(
                ticker, ticker_data
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
                    _win = float(_prob.get("win_probability_pct", 0) or 0)
                    _rr  = float(_prob.get("risk_reward_ratio", 0) or 0)
                    enhanced_report["swarm_results"]["probability_boost"] = {
                        "applied": False,
                        "disabled": True,
                        "win_probability_pct": _win,
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
            _log.warning("%s 分析失败: %s", ticker, str(e)[:100])

    # ⭐ Task 3: 等待所有异步文件写入完成
    if MLEnhancedReportGenerator._file_writer_pool:
        MLEnhancedReportGenerator._file_writer_pool.shutdown(wait=True)

    _log.info("=" * 60)
    _log.info("ML 增强报告生成完毕！成功: %d/%d", successful_count, len(tickers))
    _log.info("所有文件已完成写入")
    _log.info("=" * 60)

    # ── 自动同步 gh-pages（GitHub Pages 从此分支部署）──
    _sync_ghpages(tickers, successful_count)


def _sync_ghpages(tickers: list, successful_count: int) -> None:
    """将当日 ML 增强报告同步到 gh-pages 分支并推送。"""
    import subprocess, os, re as _re
    if successful_count == 0:
        return
    repo = str(Path(__file__).parent)
    date_str = pdt_today()
    _ml_pat = _re.compile(r"^alpha-hive-\w+-ml-enhanced-\d{4}-\d{2}-\d{2}\.html$")
    _CORE = {"index.html", "dashboard-data.json", "manifest.json", "sw.js", "rss.xml", ".nojekyll"}
    try:
        from is_trading_day import filename_is_nontrading_day as _fnt_dep
    except Exception:
        _fnt_dep = lambda _n: False  # fail-safe：导入失败则不过滤，不误删
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
        try:
            parent = subprocess.check_output(
                ["git", "rev-parse", "gh-pages"], cwd=repo, stderr=subprocess.DEVNULL
            ).decode().strip()
            parent_args = ["-p", parent]
        except subprocess.CalledProcessError:
            pass
        commit = subprocess.check_output(
            ["git", "commit-tree", tree] + parent_args +
            ["-m", f"Deploy: ML reports {date_str} ({successful_count} tickers)"],
            cwd=repo
        ).decode().strip()
        subprocess.run(["git", "update-ref", "refs/heads/gh-pages", commit],
                       cwd=repo, check=True)
        r = subprocess.run(["git", "push", "origin", "gh-pages", "--force"],
                           cwd=repo, capture_output=True, text=True)
        if r.returncode == 0:
            _log.info("gh-pages 同步成功 (%d 文件)", len(files))
        else:
            _log.warning("gh-pages push 失败: %s", r.stderr.strip()[:200])
    except Exception as e:
        _log.warning("gh-pages 同步异常: %s", e)
    finally:
        if os.path.exists(idx):
            os.remove(idx)


if __name__ == "__main__":
    main()
