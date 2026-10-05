"""Cache implementation with TTL."""

import time
from functools import wraps
from typing import TypeVar, Callable, Any

T = TypeVar("T")


class TTLCache:
    def __init__(self, ttl: int):
        self.ttl = ttl
        self._cache: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        if key in self._cache:
            expires, value = self._cache[key]
            if time.time() < expires:
                return value
            del self._cache[key]
        return None

    def set(self, key: str, value: Any) -> None:
        self._cache[key] = (time.time() + self.ttl, value)

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    def clear(self) -> None:
        self._cache.clear()


def cached(ttl: int, key_prefix: str = ""):
    cache = TTLCache(ttl)

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            key = f"{key_prefix}:{func.__name__}:{hash(str(args) + str(kwargs))}"
            if cached := cache.get(key):
                return cached
            result = await func(*args, **kwargs)
            cache.set(key, result)
            return result

        wrapper.cache = cache
        return wrapper

    return decorator