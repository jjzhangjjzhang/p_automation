import hashlib
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

CACHE_DIR = Path(__file__).parent.parent / ".cache"


def _key_to_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(key.encode()).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def cached_get(key: str, ttl_hours: float, fetch_fn):
    """Return fetch_fn() result, reusing a cached copy if it's younger than ttl_hours.

    Keeps repeat runs from re-hitting Airbnb for data that hasn't gone stale yet.
    """
    path = _key_to_path(key)
    if path.exists():
        cached = json.loads(path.read_text())
        fetched_at = datetime.fromisoformat(cached["fetched_at"])
        if datetime.now() - fetched_at < timedelta(hours=ttl_hours):
            return cached["value"]

    value = fetch_fn()
    path.write_text(json.dumps({"fetched_at": datetime.now().isoformat(), "value": value}))
    return value


def polite_delay(delay_range: list[float]):
    """Sleep a randomized amount between requests so we don't hammer Airbnb."""
    time.sleep(random.uniform(delay_range[0], delay_range[1]))
