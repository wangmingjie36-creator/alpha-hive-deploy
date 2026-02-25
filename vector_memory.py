#!/usr/bin/env python3
"""
🧠 Alpha Hive Vector Memory - 基于 Chroma 的长期语义记忆层

短期记忆：PheromoneBoard（最近 20 条，内存中）
长期记忆：Chroma 向量数据库（持久化，语义检索）

功能：
- 将 Agent 发现存入向量数据库（自动嵌入）
- 语义相似度检索历史记忆（替代 TF-IDF）
- 自动归档与清理（按保留期限）
"""

import json
import time
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from pathlib import Path

from hive_logger import PATHS, get_logger

_log = get_logger("vector_memory")

try:
    import chromadb
    from chromadb.config import Settings
    CHROMA_AVAILABLE = True
except ImportError:
    CHROMA_AVAILABLE = False


class VectorMemory:
    """
    Chroma 向量记忆层

    用法：
        vm = VectorMemory()
        vm.store("NVDA", "ScoutBeeNova", "机构持仓增加 15%", "bullish", 7.5)
        results = vm.search("机构买入信号", ticker="NVDA", top_k=5)
    """

    COLLECTION_NAME = "alpha_hive_memories"
    DEFAULT_DB_PATH = PATHS.chroma_db
    MAX_RESULTS = 10
    RETENTION_DAYS = 90

    def __init__(self, db_path: str = None, retention_days: int = None):
        self.db_path = db_path or self.DEFAULT_DB_PATH
        self.retention_days = retention_days or self.RETENTION_DAYS
        self.enabled = False
        self._client = None
        self._collection = None

        if not CHROMA_AVAILABLE:
            return

        try:
            Path(self.db_path).mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=self.db_path)
            self._collection = self._client.get_or_create_collection(
                name=self.COLLECTION_NAME,
                metadata={"description": "Alpha Hive agent discoveries"}
            )
            self.enabled = True
        except (OSError, ValueError, RuntimeError) as e:
            _log.warning("Chroma 初始化失败: %s", e)

    def store(
        self,
        ticker: str,
        agent_id: str,
        discovery: str,
        direction: str,
        score: float,
        source: str = "",
        session_id: str = ""
    ) -> Optional[str]:
        """
        存储 Agent 发现到向量数据库

        Args:
            ticker: 股票代码
            agent_id: Agent 名称
            discovery: 发现摘要（会被自动嵌入）
            direction: "bullish"/"bearish"/"neutral"
            score: 0-10 评分
            source: 数据来源
            session_id: 会话 ID

        Returns:
            文档 ID（成功）或 None（失败）
        """
        if not self.enabled:
            return None

        try:
            doc_id = f"{ticker}_{agent_id}_{int(time.time() * 1000)}"
            now = datetime.now().isoformat()

            # 构建嵌入文本：ticker + discovery + direction
            embed_text = f"{ticker} {discovery} {direction} {source}"

            self._collection.add(
                documents=[embed_text[:500]],  # 截断防止过大
                metadatas=[{
                    "ticker": ticker,
                    "agent_id": agent_id,
                    "direction": direction,
                    "score": score,
                    "source": source,
                    "session_id": session_id,
                    "created_at": now,
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "epoch_day": int(time.time() // 86400),
                }],
                ids=[doc_id]
            )
            return doc_id

        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            _log.warning("VectorMemory.store 失败: %s", e)
            return None

    def search(
        self,
        query: str,
        ticker: str = None,
        top_k: int = 5,
        days: int = 30
    ) -> List[Dict]:
        """
        语义搜索历史记忆

        Args:
            query: 自然语言查询（如 "机构买入信号"）
            ticker: 可选的 ticker 过滤
            top_k: 返回结果数
            days: 回溯天数

        Returns:
            匹配的历史记忆列表
        """
        if not self.enabled:
            return []

        try:
            # 构建过滤条件
            where_filter = {}
            if ticker:
                where_filter["ticker"] = ticker

            results = self._collection.query(
                query_texts=[query[:200]],  # 截断查询防止过大
                n_results=min(top_k, self.MAX_RESULTS),
                where=where_filter if where_filter else None,
            )

            if not results or not results["documents"]:
                return []

            # 转换为标准格式
            memories = []
            for i, doc in enumerate(results["documents"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                distance = results["distances"][0][i] if results.get("distances") else 0
                memories.append({
                    "id": results["ids"][0][i],
                    "document": doc,
                    "ticker": meta.get("ticker", ""),
                    "agent_id": meta.get("agent_id", ""),
                    "direction": meta.get("direction", ""),
                    "score": meta.get("score", 0),
                    "source": meta.get("source", ""),
                    "date": meta.get("date", ""),
                    "similarity": round(1.0 / (1.0 + distance), 3) if distance else 0,
                })

            return memories

        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            _log.warning("VectorMemory.search 失败: %s", e)
            return []

    def get_context_for_agent(
        self,
        ticker: str,
        agent_id: str,
        max_chars: int = 200
    ) -> str:
        """
        为 Agent 生成紧凑的历史上下文（替代 MemoryRetriever.get_context_summary）

        Args:
            ticker: 股票代码
            agent_id: Agent 名称
            max_chars: 最大输出字符数

        Returns:
            紧凑的历史摘要字符串
        """
        if not self.enabled:
            return ""

        try:
            results = self.search(
                query=f"{ticker} analysis",
                ticker=ticker,
                top_k=5,
                days=30
            )

            if not results:
                return ""

            # 统计方向
            bullish = sum(1 for r in results if r["direction"] == "bullish")
            bearish = sum(1 for r in results if r["direction"] == "bearish")
            avg_score = sum(r["score"] for r in results) / len(results)

            ctx = f"历史{len(results)}条:多{bullish}/空{bearish},均分{avg_score:.1f}"
            return ctx[:max_chars]

        except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
            _log.debug("get_context_for_agent 降级为空字符串: %s", exc)
            return ""

    def cleanup(self, days: int = None) -> int:
        """
        清理过期记忆

        Args:
            days: 保留天数（默认使用 self.retention_days）

        Returns:
            删除的记录数
        """
        if not self.enabled:
            return 0

        retention = days or self.retention_days
        cutoff = (datetime.now() - timedelta(days=retention)).strftime("%Y-%m-%d")

        try:
            # 获取所有文档，客户端侧过滤日期（ChromaDB where 不支持字符串 $lt）
            all_data = self._collection.get(include=["metadatas"])

            if not all_data or not all_data["ids"]:
                return 0

            expired_ids = []
            for i, meta in enumerate(all_data["metadatas"]):
                doc_date = meta.get("date", "")
                if doc_date and doc_date < cutoff:
                    expired_ids.append(all_data["ids"][i])

            if expired_ids:
                self._collection.delete(ids=expired_ids)
                return len(expired_ids)

            return 0

        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            _log.warning("VectorMemory.cleanup 失败: %s", e)
            return 0

    def stats(self) -> Dict:
        """获取向量数据库统计信息"""
        if not self.enabled:
            return {"enabled": False, "reason": "Chroma 未安装"}

        try:
            count = self._collection.count()
            return {
                "enabled": True,
                "total_documents": count,
                "db_path": self.db_path,
                "retention_days": self.retention_days,
                "collection": self.COLLECTION_NAME,
            }
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            return {"enabled": False, "error": str(e)}


def test_vector_memory():
    """测试向量记忆层"""
    _log.info("Vector Memory 测试")

    if not CHROMA_AVAILABLE:
        _log.error("chromadb 未安装")
        _log.info("安装命令: pip3 install chromadb")
        return False

    vm = VectorMemory(db_path="/tmp/alpha_hive_test_chroma")

    if not vm.enabled:
        _log.error("Chroma 初始化失败")
        return False

    _log.info("Chroma 初始化成功")

    # 存储测试数据
    vm.store("NVDA", "ScoutBeeNova", "机构持仓增加 15%，拥挤度中等", "bullish", 7.5)
    vm.store("NVDA", "OracleBeeEcho", "IV Rank 偏高，Put/Call 0.54", "bullish", 7.0)
    vm.store("TSLA", "BuzzBeeWhisper", "X 情绪偏空，空头叙事增强", "bearish", 4.5)
    _log.info("存储 3 条记忆")

    # 搜索测试
    results = vm.search("机构买入信号", ticker="NVDA", top_k=3)
    _log.info("搜索 NVDA 机构信号: %d 条匹配", len(results))
    for r in results:
        _log.debug("   %s: %s... (相似度: %s)", r['agent_id'], r['document'][:50], r['similarity'])

    # Agent 上下文
    ctx = vm.get_context_for_agent("NVDA", "ScoutBeeNova")
    _log.info("Agent 上下文: %s", ctx)

    # 统计
    stats = vm.stats()
    _log.info("统计: %s", stats)

    # 清理测试 DB
    import shutil
    shutil.rmtree("/tmp/alpha_hive_test_chroma", ignore_errors=True)

    return True


if __name__ == "__main__":
    success = test_vector_memory()
    _log.info("测试通过" if success else "测试失败")
