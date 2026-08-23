"""Тонкий HTTP-клієнт: ретраї, беквоф, простий рейт-ліміт, кеш на диску."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

try:
    import requests
except ImportError:  # каркас має імпортуватись і без requests
    requests = None  # type: ignore

log = logging.getLogger(__name__)


class HttpError(RuntimeError):
    pass


class HttpClient:
    def __init__(
        self,
        base_url: str = "",
        timeout: float = 15.0,
        max_retries: int = 4,
        rate_limit_per_sec: float = 5.0,
        headers: Optional[dict] = None,
        cache_dir: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = 1.0 / rate_limit_per_sec if rate_limit_per_sec > 0 else 0.0
        self._last = 0.0
        self.cache_dir = cache_dir
        if requests is None:
            self.session = None
        else:
            self.session = requests.Session()
            self.session.headers.update({"Accept": "application/json"})
            if headers:
                self.session.headers.update(headers)

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        delta = time.monotonic() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        if self.session is None:
            raise HttpError("requests не встановлено")
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    raise HttpError(f"{r.status_code} {r.text[:200]}")
                r.raise_for_status()
                data = r.json()
                self._dump_cache(url, params, data)
                return data
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                wait = 2.0**attempt
                log.warning("GET %s помилка (%s), повтор через %.0fс", url, exc, wait)
                time.sleep(wait)
        raise HttpError(f"GET {url} не вдався: {last_err}")

    def _dump_cache(self, url: str, params: Optional[dict], data: Any) -> None:
        """Сирі відповіді зберігаються для дебагу і для офлайн-фікстур."""
        if not self.cache_dir:
            return
        os.makedirs(self.cache_dir, exist_ok=True)
        key = str(abs(hash((url, json.dumps(params, sort_keys=True)))))[:16]
        with open(os.path.join(self.cache_dir, f"{key}.json"), "w") as fh:
            json.dump({"url": url, "params": params, "data": data}, fh)
