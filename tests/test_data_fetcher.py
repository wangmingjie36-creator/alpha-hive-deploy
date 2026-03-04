"""
tests/test_data_fetcher.py — CacheManager + DataFetcher 单元测试

覆盖：缓存保存/加载、TTL 过期、损坏 JSON、DataFetcher 初始化
"""

import json
import os
import time
import pytest


# ==================== CacheManager 测试 ====================

class TestCacheManager:
    """测试文件缓存管理器"""

    @pytest.fixture
    def cache(self, tmp_path):
        from data_fetcher import CacheManager
        return CacheManager(cache_dir=str(tmp_path / "cache"))

    def test_save_and_load(self, cache):
        data = {"price": 142.5, "volume": 1000000}
        ok = cache.save("test_key", data)
        assert ok is True
        loaded = cache.load("test_key", ttl=3600)
        assert loaded is not None
        assert loaded["price"] == 142.5

    def test_load_expired_returns_none(self, cache, tmp_path):
        data = {"old": True}
        cache.save("expired_key", data)
        # 手动修改文件时间为 2 小时前
        cache_file = os.path.join(str(tmp_path / "cache"), "expired_key.json")
        old_time = time.time() - 7200
        os.utime(cache_file, (old_time, old_time))
        result = cache.load("expired_key", ttl=3600)
        assert result is None

    def test_load_missing_returns_none(self, cache):
        assert cache.load("nonexistent_key") is None

    def test_corrupt_json_returns_none(self, cache, tmp_path):
        cache_file = os.path.join(str(tmp_path / "cache"), "corrupt.json")
        with open(cache_file, "w") as f:
            f.write("{invalid json!!!")
        result = cache.load("corrupt", ttl=99999)
        assert result is None

    def test_cache_key_generation(self, cache):
        key = cache.get_cache_key("stocktwits", "NVDA")
        assert key == "stocktwits#nvda"

    def test_cache_key_lowercase(self, cache):
        key1 = cache.get_cache_key("Yahoo", "TSLA")
        key2 = cache.get_cache_key("yahoo", "tsla")
        assert key1 == key2

    def test_save_creates_directory(self, tmp_path):
        from data_fetcher import CacheManager
        deep_path = str(tmp_path / "a" / "b" / "c" / "cache")
        cm = CacheManager(cache_dir=deep_path)
        ok = cm.save("nested", {"x": 1})
        assert ok is True
        assert os.path.exists(os.path.join(deep_path, "nested.json"))

    def test_load_respects_ttl_zero(self, cache):
        """TTL=0 时所有缓存都视为过期"""
        cache.save("zero_ttl", {"data": True})
        # TTL=0 意味着立即过期
        result = cache.load("zero_ttl", ttl=0)
        assert result is None


# ==================== DataFetcher 初始化测试 ====================

class TestDataFetcher:
    """测试 DataFetcher 初始化"""

    def test_init_creates_cache(self):
        from data_fetcher import DataFetcher
        df = DataFetcher()
        assert df.cache is not None
        assert df.cache_hits == 0
        assert df.cache_misses == 0

    def test_api_cache_ttl_default(self):
        from data_fetcher import DataFetcher
        df = DataFetcher()
        assert df.api_cache_ttl == 24 * 3600  # 24 小时
