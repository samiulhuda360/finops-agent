"""A small client for any OpenAI-compatible chat endpoint, with a disk cache and call spacing.

Configuration (environment variables; nothing is stored in files):
    AI_API_KEY        the key; without it the explanations use the template writer or the cache
    AI_BASE_URL       default: Gemini's OpenAI-compatible endpoint
    AI_MODEL          default: gemini-flash-lite-latest
    AI_MIN_INTERVAL   minimum seconds between live calls (default 4.5; free quotas are small)

Every reply is cached by a hash of (model, messages), so the evaluation and CI replay offline. A rate-limit
reply (HTTP 429) stops the run with `RateLimited` instead of retrying into a shared quota.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..paths import CACHE

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_MODEL = "gemini-flash-lite-latest"


class NoModel(RuntimeError):
    """No key is configured and the cache has no reply for this prompt."""


class RateLimited(RuntimeError):
    """The endpoint answered 429; the caller stops and reports how far it got."""


@dataclass
class Reply:
    text: str
    cached: bool
    latency_s: float
    prompt_tokens: int
    completion_tokens: int


@dataclass
class LLM:
    model: str = field(default_factory=lambda: os.getenv("AI_MODEL", DEFAULT_MODEL))
    base_url: str = field(default_factory=lambda: os.getenv("AI_BASE_URL", DEFAULT_BASE_URL))
    cache_dir: Path = field(default_factory=lambda: CACHE / "llm")
    offline: bool = False  # replay from the cache only
    min_interval: float = field(default_factory=lambda: float(os.getenv("AI_MIN_INTERVAL", "4.5")))
    live_calls: int = 0
    cached_calls: int = 0
    _last: float = 0.0

    @property
    def live(self) -> bool:
        return not self.offline and bool(os.getenv("AI_API_KEY"))

    def key(self, messages: list[dict[str, str]]) -> str:
        blob = json.dumps({"model": self.model, "messages": messages}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def cached(self, messages: list[dict[str, str]]) -> bool:
        return (self.cache_dir / f"{self.key(messages)}.json").exists()

    def chat(self, messages: list[dict[str, str]]) -> Reply:
        path = self.cache_dir / f"{self.key(messages)}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.cached_calls += 1
            return Reply(data["text"], True, data["latency_s"], data["prompt_tokens"], data["completion_tokens"])
        if not self.live:
            raise NoModel("no AI_API_KEY and no cached reply")
        reply = self._call(messages)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "model": self.model,
                    "text": reply.text,
                    "latency_s": reply.latency_s,
                    "prompt_tokens": reply.prompt_tokens,
                    "completion_tokens": reply.completion_tokens,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        return reply

    def _call(self, messages: list[dict[str, str]]) -> Reply:
        gap = self.min_interval - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()
        body = json.dumps({"model": self.model, "messages": messages, "temperature": 0.0}).encode()
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ['AI_API_KEY']}"},
        )
        started = time.perf_counter()
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    raise RateLimited("the model endpoint returned 429 (quota or rate limit)") from e
                if e.code >= 500 and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
        latency = round(time.perf_counter() - started, 3)
        self.live_calls += 1
        usage = data.get("usage") or {}
        text = data["choices"][0]["message"].get("content") or ""
        return Reply(
            text.strip(), False, latency, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        )
