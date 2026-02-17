import asyncio
import time
from collections import deque


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._timestamps: deque[float] = deque()

    def can_proceed(self) -> bool:
        now = time.monotonic()
        while self._timestamps and self._timestamps[0] < now - self.window:
            self._timestamps.popleft()
        return len(self._timestamps) < self.max_requests

    def record(self):
        self._timestamps.append(time.monotonic())

    async def wait_if_needed(self):
        while not self.can_proceed():
            await asyncio.sleep(0.5)
        self.record()
