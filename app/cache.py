"""Redis cache manager."""

import hashlib
import json
from typing import Any

from redis.asyncio import ConnectionPool, Redis

from app.logger import get_logger

logger = get_logger(__name__)


class CacheManager:
    """Redis cache manager with async support."""

    def __init__(self, redis_url: str):
        self.pool = ConnectionPool.from_url(redis_url, decode_responses=True)
        self.redis: Redis | None = None

    async def connect(self) -> None:
        """Connect to Redis."""
        try:
            self.redis = Redis(connection_pool=self.pool)
            await self.redis.ping()
            logger.info("✅ Redis connected")
        except Exception as e:
            logger.error("❌ Redis connection failed: %s", e)
            self.redis = None

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        if self.redis:
            await self.redis.aclose()
            await self.pool.aclose()

    async def ping(self) -> bool:
        """Check if Redis is available."""
        if not self.redis:
            return False
        try:
            await self.redis.ping()
            return True
        except Exception:
            return False

    async def get(self, key: str) -> Any | None:
        """Get value from cache."""
        if not self.redis:
            return None
        try:
            value = await self.redis.get(key)
            return json.loads(value) if value else None
        except Exception:
            return None

    async def set(self, key: str, value: Any, ttl: int = 300) -> bool:
        """Set value in cache with TTL (default 5 min)."""
        if not self.redis:
            return False
        try:
            await self.redis.setex(key, ttl, json.dumps(value, default=str))
            return True
        except Exception:
            return False

    async def delete(self, key: str) -> bool:
        """Delete key from cache."""
        if not self.redis:
            return False
        try:
            await self.redis.delete(key)
            return True
        except Exception:
            return False

    async def exists(self, key: str) -> bool:
        """Check if key exists."""
        if not self.redis:
            return False
        try:
            return await self.redis.exists(key) > 0
        except Exception:
            return False

    async def delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching pattern."""
        if not self.redis:
            return 0
        try:
            keys = [k async for k in self.redis.scan_iter(match=pattern)]
            if keys:
                return await self.redis.delete(*keys)
            return 0
        except Exception:
            return 0


def cache_key(*args, prefix: str = "cache") -> str:
    """Generate consistent cache key."""
    key_data = ":".join(str(arg) for arg in args if arg)
    key_hash = hashlib.md5(key_data.encode()).hexdigest()[:8]
    return f"{prefix}:{key_data}:{key_hash}"
