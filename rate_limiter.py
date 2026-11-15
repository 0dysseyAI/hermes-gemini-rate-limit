"""Core rate-limiting logic for Google Gemini API."""

from __future__ import annotations

import collections
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("hermes.plugins.gemini_rate_limit")

# Reference model profile: Gemini 3.5 Flash Lite (Free Tier)
DEFAULT_RPM = 15       # Peak requests per minute (ENFORCED)
DEFAULT_TPM = 250_000  # Peak input tokens per minute (ENFORCED)
DEFAULT_RPD = 1_000    # Peak requests per day (UNUSED REFERENCE CONSTANT — NOT ENFORCED)
DEFAULT_SAFETY_FACTOR = 0.92
WINDOW_SECONDS = 60.0
MIN_PACING_RATIO = 0.75  # Enforces smooth minimum pacing delay: (60.0 / effective_rpm * 0.75)


def get_hermes_home() -> Path:
    """Return the active Hermes home directory (respecting $HERMES_HOME)."""
    home = os.environ.get("HERMES_HOME")
    if home and home.strip():
        return Path(home.strip()).expanduser().resolve()
    return Path.home() / ".hermes"


CONFIG_PATH = get_hermes_home() / "gemini_rate_limit.json"
STATUS_PATH = get_hermes_home() / "gemini_rate_limit_status.json"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write a dictionary as JSON to disk using a temporary file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=str(path.parent), delete=False, encoding="utf-8") as tf:
            json.dump(data, tf, indent=2)
            tf.flush()
            os.fsync(tf.fileno())
            tmp_path = Path(tf.name)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("Failed to atomically write %s: %s", path, exc)
        if 'tmp_path' in locals() and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def load_config() -> dict[str, Any]:
    """Load configuration from disk or create defaults."""
    cfg_file = CONFIG_PATH
    if cfg_file.is_file():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                res = {
                    "enabled": bool(data.get("enabled", True)),
                    "rpm": max(1, int(data.get("rpm", DEFAULT_RPM))),
                    "tpm": max(1000, int(data.get("tpm", DEFAULT_TPM))),
                    "rpd": int(data.get("rpd", DEFAULT_RPD)),
                    "safety_factor": float(data.get("safety_factor", DEFAULT_SAFETY_FACTOR)),
                }
                if "models" in data and isinstance(data["models"], dict):
                    res["models"] = data["models"]
                return res
        except Exception as exc:
            logger.warning("Error reading %s, using defaults: %s", cfg_file, exc)

    default_cfg = {
        "enabled": True,
        "rpm": DEFAULT_RPM,
        "tpm": DEFAULT_TPM,
        "rpd": DEFAULT_RPD,
        "safety_factor": DEFAULT_SAFETY_FACTOR,
    }
    atomic_write_json(cfg_file, default_cfg)
    return default_cfg


def is_gemini_target(kwargs: dict[str, Any], request: Optional[dict[str, Any]] = None) -> bool:
    """Determine if outbound request targets Google Gemini API."""
    provider = str(kwargs.get("provider") or "").lower()
    base_url = str(kwargs.get("base_url") or "").lower()
    api_base = str(kwargs.get("api_base") or "").lower()
    model = str(kwargs.get("model") or "").lower()
    env_base = str(os.environ.get("OPENAI_BASE_URL") or os.environ.get("GEMINI_BASE_URL") or "").lower()

    if request and isinstance(request, dict):
        if not provider:
            provider = str(request.get("provider") or "").lower()
        if not base_url:
            base_url = str(request.get("base_url") or "").lower()
        if not api_base:
            api_base = str(request.get("api_base") or "").lower()
        if not model:
            model = str(request.get("model") or "").lower()

    targets = (provider, base_url, api_base, model, env_base)
    keywords = ("googleapis.com", "generativelanguage", "gemini", "google")

    for kw in keywords:
        for val in targets:
            if kw in val:
                return True
    return False


def estimate_request_tokens(request: Any) -> int:
    """Pre-flight estimate of outbound input tokens from messages and tools."""
    if not request:
        return 1
    total_chars = 0
    if isinstance(request, dict):
        # Inspect messages
        messages = request.get("messages")
        if isinstance(messages, list):
            for msg in messages:
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, str):
                        total_chars += len(content)
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and "text" in part:
                                total_chars += len(str(part["text"]))
                    # System prompt or name
                    if "name" in msg:
                        total_chars += len(str(msg["name"]))
                elif isinstance(msg, str):
                    total_chars += len(msg)

        # Inspect tools / functions
        tools = request.get("tools")
        if isinstance(tools, (list, dict)):
            total_chars += len(json.dumps(tools))

        # Inspect prompt
        prompt = request.get("prompt")
        if isinstance(prompt, str):
            total_chars += len(prompt)
        elif isinstance(prompt, list):
            total_chars += sum(len(str(p)) for p in prompt)

        system = request.get("system")
        if isinstance(system, str):
            total_chars += len(system)
    elif isinstance(request, str):
        total_chars = len(request)

    # 1 token ~= 4 characters heuristic
    return max(1, total_chars // 4)


def extract_prompt_tokens_from_response(response: Any) -> Optional[int]:
    """Best-effort extraction of prompt token count from response metadata."""
    if not response:
        return None
    # 1. Attributes on response object
    for attr in ("usage", "_usage", "usage_metadata"):
        val = getattr(response, attr, None)
        if val is not None:
            for key in ("prompt_tokens", "prompt_token_count", "input_tokens"):
                tok = getattr(val, key, None)
                if tok is not None:
                    try:
                        return int(tok)
                    except (ValueError, TypeError):
                        pass
                if isinstance(val, dict) and key in val:
                    try:
                        return int(val[key])
                    except (ValueError, TypeError):
                        pass

    # 2. Dict access
    if isinstance(response, dict):
        usage = response.get("usage") or response.get("usage_metadata")
        if isinstance(usage, dict):
            for key in ("prompt_tokens", "prompt_token_count", "input_tokens"):
                if key in usage:
                    try:
                        return int(usage[key])
                    except (ValueError, TypeError):
                        pass
    return None


class GeminiRateLimiter:
    """Sliding-window proactive rate limiter for Gemini API."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._window: collections.deque[Tuple[float, int]] = collections.deque()
        self._last_request_time: float = 0.0
        self._cooldown_until: float = 0.0
        self._last_wait_seconds: float = 0.0
        self._total_throttled_requests: int = 0
        self._is_throttling: bool = False
        self._config: dict[str, Any] = load_config()

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._config)

    def set_config(self, new_cfg: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cfg = dict(self._config)
            if "rpm" in new_cfg:
                cfg["rpm"] = max(1, int(new_cfg["rpm"]))
            if "tpm" in new_cfg:
                cfg["tpm"] = max(1000, int(new_cfg["tpm"]))
            if "rpd" in new_cfg:
                cfg["rpd"] = int(new_cfg["rpd"])
            if "enabled" in new_cfg:
                cfg["enabled"] = bool(new_cfg["enabled"])
            if "safety_factor" in new_cfg:
                cfg["safety_factor"] = max(0.1, min(1.0, float(new_cfg["safety_factor"])))
            if "models" in new_cfg and isinstance(new_cfg["models"], dict):
                cfg["models"] = new_cfg["models"]
            self._config = cfg
            atomic_write_json(CONFIG_PATH, self._config)
            self._write_telemetry_locked(time.time(), wait_seconds=0.0)
            return dict(self._config)

    def get_limits(self, model: Optional[str] = None) -> Tuple[Optional[int], Optional[int], Optional[int]]:
        """Return (rpm, tpm, rpd) for the model or fallback to global config."""
        with self._lock:
            default_rpm: Optional[int] = int(self._config.get("rpm", DEFAULT_RPM))
            default_tpm: Optional[int] = int(self._config.get("tpm", DEFAULT_TPM))
            default_rpd: Optional[int] = int(self._config.get("rpd", DEFAULT_RPD))

            if not model:
                return default_rpm, default_tpm, default_rpd

            models_dict = self._config.get("models")
            if not isinstance(models_dict, dict):
                return default_rpm, default_tpm, default_rpd

            norm = model.lower().strip().replace("google/", "").replace("gemini/", "").replace("models/", "")
            norm_slug = re.sub(r'[^a-z0-9.]+', '-', norm).strip('-')

            matched = models_dict.get(norm_slug)
            if not matched:
                for slug, cfg in models_dict.items():
                    if slug in norm_slug or norm_slug in slug:
                        matched = cfg
                        break

            if isinstance(matched, dict):
                rpm = matched.get("rpm")
                tpm = matched.get("tpm")
                rpd = matched.get("rpd")
                return (
                    int(rpm) if (rpm is not None and rpm != "Unlimited") else None,
                    int(tpm) if (tpm is not None and tpm != "Unlimited") else None,
                    int(rpd) if (rpd is not None and rpd != "Unlimited") else None,
                )

            return default_rpm, default_tpm, default_rpd

    def _evict_expired_locked(self, now: float) -> None:
        boundary = now - WINDOW_SECONDS
        while self._window and self._window[0][0] <= boundary:
            self._window.popleft()

    def _window_tokens_locked(self) -> int:
        return sum(tokens for _, tokens in self._window)

    def _write_telemetry_locked(self, now: float, wait_seconds: float) -> None:
        self._evict_expired_locked(now)
        safety = float(self._config.get("safety_factor", DEFAULT_SAFETY_FACTOR))
        rpm = int(self._config.get("rpm", DEFAULT_RPM))
        tpm = int(self._config.get("tpm", DEFAULT_TPM))
        rpd = int(self._config.get("rpd", DEFAULT_RPD))
        effective_rpm = max(1, int(rpm * safety))
        effective_tpm = max(100, int(tpm * safety))
        rpm_used = len(self._window)
        tpm_used = self._window_tokens_locked()

        telemetry = {
            "enabled": bool(self._config.get("enabled", True)),
            "rpm": rpm,
            "tpm": tpm,
            "rpd": rpd,
            "safety_factor": safety,
            "effective_rpm": effective_rpm,
            "effective_tpm": effective_tpm,
            "rpm_used": rpm_used,
            "tpm_used": tpm_used,
            "last_wait_seconds": round(wait_seconds, 3),
            "total_throttled_requests": self._total_throttled_requests,
            "is_throttling": self._is_throttling,
            "timestamp": now,
        }
        atomic_write_json(STATUS_PATH, telemetry)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            self._evict_expired_locked(now)
            safety = float(self._config.get("safety_factor", DEFAULT_SAFETY_FACTOR))
            rpm = int(self._config.get("rpm", DEFAULT_RPM))
            tpm = int(self._config.get("tpm", DEFAULT_TPM))
            rpd = int(self._config.get("rpd", DEFAULT_RPD))
            effective_rpm = max(1, int(rpm * safety))
            effective_tpm = max(100, int(tpm * safety))
            status = {
                "enabled": bool(self._config.get("enabled", True)),
                "rpm": rpm,
                "tpm": tpm,
                "rpd": rpd,
                "safety_factor": safety,
                "effective_rpm": effective_rpm,
                "effective_tpm": effective_tpm,
                "rpm_used": len(self._window),
                "tpm_used": self._window_tokens_locked(),
                "last_wait_seconds": round(self._last_wait_seconds, 3),
                "total_throttled_requests": self._total_throttled_requests,
                "is_throttling": self._is_throttling,
                "timestamp": now,
            }
            if "models" in self._config:
                status["models"] = self._config["models"]
            return status

    def acquire(self, estimated_tokens: int, model: Optional[str] = None) -> float:
        """Acquire permission to execute a Gemini request.
        
        Sleeps if necessary to stay under effective RPM and TPM quotas.
        Returns the total seconds waited.
        """
        total_slept = 0.0

        while True:
            sleep_duration = 0.0
            with self._lock:
                if not self._config.get("enabled", True):
                    return 0.0

                now = time.time()
                self._evict_expired_locked(now)

                # 1. Reactive 429 Cooldown
                if self._cooldown_until > now:
                    sleep_duration = max(sleep_duration, self._cooldown_until - now)

                safety = float(self._config.get("safety_factor", DEFAULT_SAFETY_FACTOR))
                rpm_limit, tpm_limit, _ = self.get_limits(model)
                effective_rpm = max(1, int(rpm_limit * safety)) if rpm_limit is not None else None
                effective_tpm = max(100, int(tpm_limit * safety)) if tpm_limit is not None else None

                # 2. Smooth Minimum Pacing Delay (if RPM bounded)
                if effective_rpm is not None:
                    min_pacing = (WINDOW_SECONDS / effective_rpm) * MIN_PACING_RATIO
                    time_since_last = now - self._last_request_time
                    if self._last_request_time > 0 and time_since_last < min_pacing:
                        pacing_wait = min_pacing - time_since_last
                        sleep_duration = max(sleep_duration, pacing_wait)

                    # 3. RPM Sliding-Window Gate
                    if len(self._window) >= effective_rpm:
                        oldest_ts = self._window[0][0]
                        rpm_wait = (oldest_ts + WINDOW_SECONDS) - now + 0.05
                        sleep_duration = max(sleep_duration, rpm_wait)

                # 4. TPM Sliding-Window Gate (if TPM bounded)
                if effective_tpm is not None:
                    current_tokens = self._window_tokens_locked()
                    if self._window and (current_tokens + estimated_tokens > effective_tpm):
                        # Find how many entries need to expire to fit this request
                        cum_sub = 0
                        tpm_wait = 0.0
                        for ts, tok in self._window:
                            cum_sub += tok
                            tpm_wait = (ts + WINDOW_SECONDS) - now + 0.05
                            if current_tokens - cum_sub + estimated_tokens <= effective_tpm:
                                break
                        sleep_duration = max(sleep_duration, tpm_wait)

                if sleep_duration <= 0.0:
                    # Slot acquired cleanly
                    now = time.time()
                    self._window.append((now, estimated_tokens))
                    self._last_request_time = now
                    self._is_throttling = False
                    self._last_wait_seconds = total_slept
                    if total_slept > 0:
                        self._total_throttled_requests += 1
                    self._write_telemetry_locked(now, total_slept)
                    return total_slept

                # Throttling needed
                self._is_throttling = True
                self._write_telemetry_locked(now, sleep_duration)

            # Sleep outside the lock so other status readers don't deadlock
            time.sleep(sleep_duration)
            total_slept += sleep_duration

    def refine_tokens(self, actual_tokens: int) -> None:
        """Refine the most recent request's token count with the response's actual usage."""
        if actual_tokens <= 0:
            return
        with self._lock:
            if self._window:
                ts, _ = self._window.pop()
                self._window.append((ts, actual_tokens))
                self._write_telemetry_locked(time.time(), wait_seconds=self._last_wait_seconds)

    def record_429(self, exc: Exception) -> None:
        """Reactive 429 handler: parse retryDelay and back off."""
        text = str(exc)
        cooldown = 5.0  # default fallback

        # Search for retryDelay (e.g., 'retryDelay': '3.2s' or 'Retry-After': 10)
        match = re.search(r'retryDelay["\']?\s*[:=]\s*["\']?([0-9]+(?:\.[0-9]+)?)s?', text, re.IGNORECASE)
        if match:
            try:
                cooldown = max(1.0, float(match.group(1)) + 0.5)
            except ValueError:
                pass
        else:
            match_after = re.search(r'Retry-After["\']?\s*[:=]\s*["\']?([0-9]+(?:\.[0-9]+)?)', text, re.IGNORECASE)
            if match_after:
                try:
                    cooldown = max(1.0, float(match_after.group(1)) + 0.5)
                except ValueError:
                    pass
            else:
                with self._lock:
                    if self._window:
                        cooldown = max(1.0, (self._window[0][0] + WINDOW_SECONDS) - time.time() + 0.5)

        with self._lock:
            now = time.time()
            self._cooldown_until = now + cooldown
            self._total_throttled_requests += 1
            self._is_throttling = True
            logger.warning("Gemini 429 detected; parking outbound calls for %.1fs", cooldown)
            self._write_telemetry_locked(now, wait_seconds=cooldown)


# Global singleton instance
_limiter = GeminiRateLimiter()


def get_limiter() -> GeminiRateLimiter:
    return _limiter
