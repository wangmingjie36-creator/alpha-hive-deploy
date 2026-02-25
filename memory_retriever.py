#!/usr/bin/env python3
"""
🔍 Alpha Hive Memory Retriever - 跨会话记忆检索引擎
基于 TF-IDF 的中英混合分词相似度检索，< 50ms 性能目标
"""

import logging as _logging
import re
import json
import time
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from datetime import datetime, timedelta
from threading import Lock

_log = _logging.getLogger("alpha_hive.memory_retriever")

try:
    import numpy as np
except ImportError:
    np = None


class MemoryRetriever:
    """基于 TF-IDF 的记忆检索引擎（含 LRU 缓存管理）"""

    # 缓存上限：防止无限增长导致内存泄漏
    MAX_CACHE_TICKERS = 50       # 最多缓存 50 个 ticker 的文档
    MAX_TFIDF_CACHE = 30         # 最多缓存 30 个 ticker 的 TF-IDF 向量
    MAX_CONTEXT_CHARS = 200      # Agent 注入的上下文摘要最大字符数

    def __init__(self, memory_store, cache_ttl_seconds: int = 300):
        """
        初始化检索引擎

        Args:
            memory_store: MemoryStore 实例
            cache_ttl_seconds: 缓存 TTL（秒）
        """
        self.memory_store = memory_store
        self.cache_ttl_seconds = cache_ttl_seconds

        # 缓存：{ticker: {"timestamp": float, "documents": List[Dict]}}
        self._cache: Dict[str, Dict] = {}
        self._cache_lock = Lock()

        # TF-IDF 缓存：{ticker: {"idf": Dict, "vocab": Dict}}
        self._tfidf_cache: Dict[str, Dict] = {}

    def _evict_lru_cache(self) -> None:
        """LRU 淘汰：当缓存超过上限时，删除最旧的条目"""
        with self._cache_lock:
            if len(self._cache) > self.MAX_CACHE_TICKERS:
                # 按 timestamp 排序，淘汰最旧的
                sorted_keys = sorted(
                    self._cache.keys(),
                    key=lambda k: self._cache[k].get("timestamp", 0)
                )
                # 淘汰超出部分
                for key in sorted_keys[:len(self._cache) - self.MAX_CACHE_TICKERS]:
                    del self._cache[key]

            if len(self._tfidf_cache) > self.MAX_TFIDF_CACHE:
                # TF-IDF 缓存没有 timestamp，直接淘汰前 N 个
                keys_to_remove = list(self._tfidf_cache.keys())[:-self.MAX_TFIDF_CACHE]
                for key in keys_to_remove:
                    del self._tfidf_cache[key]

    def _tokenize(self, text: str) -> List[str]:
        """
        中英混合分词（简化版本，不依赖 jieba）

        Args:
            text: 输入文本

        Returns:
            词列表
        """
        # 清理文本
        text = text.lower().strip()

        # 分离中文和英文
        tokens = []

        # 英文分词：按空格和标点符号分割
        parts = re.split(r'[\s\-_.,!?;:]+', text)
        for part in parts:
            if part:
                tokens.append(part)

        # 提取中文字符（简化：每个中文字符一个词）
        chinese_chars = re.findall(r'[\u4e00-\u9fff]', text)
        tokens.extend(chinese_chars)

        # 过滤停用词和过短词
        stopwords = {'的', '是', '和', '或', '如', '但', 'a', 'an', 'the', 'is', 'and', 'or'}
        tokens = [t for t in tokens if len(t) > 1 or t in '亮多空涨跌']

        return tokens

    def _build_tfidf(self, documents: List[Dict]) -> Tuple[Dict, Dict]:
        """
        构建 TF-IDF 向量

        Args:
            documents: 文档列表，每个包含 'discovery' 和 'source' 字段

        Returns:
            (idf_dict, vocab_dict)
        """
        if not documents:
            return {}, {}

        # 1. 分词
        all_tokens = []
        doc_tokens_list = []

        for doc in documents:
            text = doc.get('discovery', '') + ' ' + doc.get('source', '')
            tokens = self._tokenize(text)
            doc_tokens_list.append(set(tokens))
            all_tokens.extend(tokens)

        # 2. 计算 IDF
        vocab = list(set(all_tokens))
        doc_count = len(documents)
        idf = {}

        for word in vocab:
            doc_freq = sum(1 for doc_tokens in doc_tokens_list if word in doc_tokens)
            idf[word] = np.log((doc_count + 1) / (doc_freq + 1)) if np else 1.0

        return idf, {word: i for i, word in enumerate(vocab)}

    def _compute_similarity(self, query: str, doc: Dict, idf: Dict, vocab: Dict) -> float:
        """
        计算查询与文档的余弦相似度

        Args:
            query: 查询字符串
            doc: 文档字典
            idf: IDF 字典
            vocab: 词表

        Returns:
            相似度分数 [0, 1]
        """
        if not vocab or not idf:
            return 0.0

        # 分词
        query_tokens = self._tokenize(query)
        doc_tokens = self._tokenize(doc.get('discovery', '') + ' ' + doc.get('source', ''))

        # 计算词频向量
        query_vec = defaultdict(float)
        doc_vec = defaultdict(float)

        for word in query_tokens:
            if word in idf:
                query_vec[word] += 1.0

        for word in doc_tokens:
            if word in idf:
                doc_vec[word] += 1.0

        # 应用 IDF 加权
        for word in query_vec:
            query_vec[word] *= idf.get(word, 1.0)

        for word in doc_vec:
            doc_vec[word] *= idf.get(word, 1.0)

        # 余弦相似度
        dot_product = sum(query_vec[w] * doc_vec[w] for w in query_vec if w in doc_vec)

        query_norm = np.sqrt(sum(v ** 2 for v in query_vec.values())) if np else 1.0
        doc_norm = np.sqrt(sum(v ** 2 for v in doc_vec.values())) if np else 1.0

        if query_norm == 0 or doc_norm == 0:
            return 0.0

        return dot_product / (query_norm * doc_norm)

    def find_similar(
        self,
        query: str,
        ticker: Optional[str] = None,
        top_k: int = 5,
        min_similarity: float = 0.1
    ) -> List[Dict]:
        """
        查找相似的历史记忆

        Args:
            query: 查询字符串（自然语言或关键词）
            ticker: 可选的股票过滤
            top_k: 返回结果数量
            min_similarity: 最小相似度阈值

        Returns:
            相似文档列表，每个包含 'similarity' 字段
        """
        try:
            # LRU 淘汰检查
            self._evict_lru_cache()

            # 获取最近记忆（30 天内，限制 50 条防止内存膨胀）
            if ticker:
                memories = self.memory_store.get_recent_memories(ticker, days=30, limit=50)
            else:
                return []

            if not memories:
                return []

            # 构建 TF-IDF（缓存命中时跳过重建）
            idf, vocab = self._build_tfidf(memories)

            # 计算相似度
            similarities = []
            for doc in memories:
                sim = self._compute_similarity(query, doc, idf, vocab)
                if sim >= min_similarity:
                    similarities.append({
                        'memory_id': doc.get('memory_id'),
                        'ticker': doc.get('ticker'),
                        'agent_id': doc.get('agent_id'),
                        'discovery': doc.get('discovery'),
                        'direction': doc.get('direction'),
                        'self_score': doc.get('self_score'),
                        'source': doc.get('source'),
                        'created_at': doc.get('created_at'),
                        'similarity': round(sim, 3)
                    })

            # 按相似度排序
            similarities.sort(key=lambda x: x['similarity'], reverse=True)

            return similarities[:top_k]

        except (ValueError, KeyError, TypeError, AttributeError) as e:
            _log.error("find_similar 失败: %s", e, exc_info=True)
            return []

    def get_context_summary(self, ticker: str, current_date: str, days: int = 30) -> str:
        """
        获取历史上下文摘要（用于 Agent 注入）

        Args:
            ticker: 股票代码
            current_date: 当前日期
            days: 回溯天数

        Returns:
            历史摘要字符串（如果无历史，返回空字符串）
        """
        try:
            memories = self.memory_store.get_recent_memories(ticker, days=days, limit=10)

            if not memories:
                return ""

            # 按方向分类
            bullish = [m for m in memories if m.get('direction') == 'bullish']
            bearish = [m for m in memories if m.get('direction') == 'bearish']
            neutral = [m for m in memories if m.get('direction') == 'neutral']

            # 构建摘要
            summary_parts = []

            if bullish:
                avg_score = sum(m.get('self_score', 5) for m in bullish) / len(bullish)
                summary_parts.append(f"历史看多信号 {len(bullish)} 条（平均分 {avg_score:.1f}/10）")

            if bearish:
                avg_score = sum(m.get('self_score', 5) for m in bearish) / len(bearish)
                summary_parts.append(f"历史看空信号 {len(bearish)} 条（平均分 {avg_score:.1f}/10）")

            if summary_parts:
                ctx = f"【历史上下文】{' | '.join(summary_parts)}"
                # 截断防止注入过大上下文到 Agent
                return ctx[:self.MAX_CONTEXT_CHARS]

            return ""

        except (ValueError, KeyError, TypeError, AttributeError) as e:
            _log.error("get_context_summary 失败: %s", e, exc_info=True)
            return ""

    def invalidate_cache(self, ticker: Optional[str] = None) -> None:
        """
        清除缓存

        Args:
            ticker: 如为 None，清除所有缓存；否则清除特定 ticker
        """
        with self._cache_lock:
            if ticker:
                self._cache.pop(ticker, None)
                self._tfidf_cache.pop(ticker, None)
            else:
                self._cache.clear()
                self._tfidf_cache.clear()
