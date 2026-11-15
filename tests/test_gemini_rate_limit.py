"""Automated test suite for gemini-rate-limit plugin."""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add plugin dir and hermes-agent to sys.path
PLUGIN_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = Path("/home/gemini/.hermes/hermes-agent")
for p in (str(PLUGIN_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import hermes_bootstrap  # noqa: F401
import __init__ as plugin
import rate_limiter
from dashboard.plugin_api import ConfigUpdateRequest, get_status, update_config


class TestGeminiRateLimit(unittest.TestCase):

    def setUp(self):
        self.limiter = rate_limiter.get_limiter()
        # Reset limiter state before each test
        with self.limiter._lock:
            self.limiter._window.clear()
            self.limiter._last_request_time = 0.0
            self.limiter._cooldown_until = 0.0
            self.limiter._last_wait_seconds = 0.0
            self.limiter._total_throttled_requests = 0
            self.limiter._is_throttling = False
            self.limiter._config = {
                "enabled": True,
                "rpm": rate_limiter.DEFAULT_RPM,
                "tpm": rate_limiter.DEFAULT_TPM,
                "rpd": rate_limiter.DEFAULT_RPD,
                "safety_factor": rate_limiter.DEFAULT_SAFETY_FACTOR,
            }

    def test_1_non_gemini_bypass(self):
        """Verify non-Gemini requests bypass the middleware with 0ms delay."""
        called = False

        def mock_next_call(req):
            nonlocal called
            called = True
            return {"result": "ok"}

        non_gemini_cases = [
            ({"provider": "anthropic", "model": "claude-3-5-sonnet-20241022"}, {"messages": [{"role": "user", "content": "hi"}]}),
            ({"provider": "openai", "model": "gpt-4o"}, {"messages": [{"role": "user", "content": "hi"}]}),
            ({"provider": "openrouter", "model": "meta-llama/llama-3-70b"}, {"prompt": "hi"}),
        ]

        for kwargs, req in non_gemini_cases:
            called = False
            start = time.perf_counter()
            resp = plugin.throttle_gemini_execution(req, mock_next_call, **kwargs)
            elapsed = time.perf_counter() - start

            self.assertTrue(called)
            self.assertEqual(resp, {"result": "ok"})
            self.assertLess(elapsed, 0.05, f"Non-gemini call took {elapsed}s, should be instantaneous")
            # Window should be completely untouched
            self.assertEqual(len(self.limiter._window), 0)

    def test_2_gemini_provider_detection(self):
        """Verify all variants of Gemini endpoints/models are detected."""
        gemini_cases = [
            {"provider": "google", "model": "gemini-1.5-flash"},
            {"provider": "gemini", "model": "gemini-2.0-flash"},
            {"base_url": "https://generativelanguage.googleapis.com/v1beta"},
            {"api_base": "https://generativelanguage.googleapis.com"},
            {"model": "google/gemini-2.5-pro"},
        ]

        for kwargs in gemini_cases:
            self.assertTrue(
                rate_limiter.is_gemini_target(kwargs),
                f"Failed to detect Gemini for {kwargs}"
            )

    def test_3_rpm_sliding_window_boundary(self):
        """Verify rapid Gemini requests sleep accurately at the effective_rpm boundary."""
        simulated_now = 1000.0

        # Configure small test limits with safety_factor 1.0
        self.limiter.set_config({"rpm": 3, "tpm": 1_000_000, "safety_factor": 1.0})

        with patch("time.time") as mock_time, patch("time.sleep") as mock_sleep:
            mock_time.return_value = simulated_now

            # Call 1: Window [1000.0]
            self.limiter.acquire(estimated_tokens=10)
            mock_sleep.assert_not_called()

            # Advance time past pacing interval (60/3 * 0.75 = 15s)
            simulated_now += 16.0
            mock_time.return_value = simulated_now

            # Call 2: Window [1000.0, 1016.0]
            self.limiter.acquire(estimated_tokens=10)

            # Advance time past pacing interval
            simulated_now += 16.0
            mock_time.return_value = simulated_now

            # Call 3: Window [1000.0, 1016.0, 1032.0]
            self.limiter.acquire(estimated_tokens=10)

            # Now effective_rpm (3) is fully saturated!
            simulated_now += 16.0  # now = 1048.0
            mock_time.return_value = simulated_now

            # Call 4: Must wait until oldest entry (1000.0) leaves 60s window (at 1060.05s)
            # Expected wait = (1000.0 + 60.0) - 1048.0 + 0.05 = 12.05s
            mock_sleep.reset_mock()

            def advance_clock(duration):
                nonlocal simulated_now
                simulated_now += duration
                mock_time.return_value = simulated_now

            mock_sleep.side_effect = advance_clock

            self.limiter.acquire(estimated_tokens=10)
            mock_sleep.assert_called()
            # The wait should have been ~12.05s
            first_sleep = mock_sleep.call_args_list[0][0][0]
            self.assertAlmostEqual(first_sleep, 12.05, places=2)

    def test_4_tpm_sliding_window_gate(self):
        """Verify large-prompt bursts sleep accurately when cumulative tokens exceed effective_tpm."""
        simulated_now = 2000.0
        self.limiter.set_config({"rpm": 100, "tpm": 5000, "safety_factor": 1.0})

        with patch("time.time") as mock_time, patch("time.sleep") as mock_sleep:
            mock_time.return_value = simulated_now

            # Call 1: uses 3,000 tokens (remaining budget: 2,000)
            self.limiter.acquire(estimated_tokens=3000)
            mock_sleep.assert_not_called()

            simulated_now += 1.0
            mock_time.return_value = simulated_now

            # Call 2: requests 3,000 tokens (3000 + 3000 = 6000 > 5000)
            # Must sleep until call 1 exits 60s window: (2000 + 60) - 2001 + 0.05 = 59.05s
            mock_sleep.reset_mock()

            def advance_clock(duration):
                nonlocal simulated_now
                simulated_now += duration
                mock_time.return_value = simulated_now

            mock_sleep.side_effect = advance_clock

            self.limiter.acquire(estimated_tokens=3000)
            mock_sleep.assert_called()
            first_sleep = mock_sleep.call_args_list[0][0][0]
            self.assertAlmostEqual(first_sleep, 59.05, places=2)

    def test_5_default_rpd_unenforced_constant(self):
        """Verify DEFAULT_RPD exists and does NOT block even with >1000 simulated calls."""
        self.assertEqual(plugin.DEFAULT_RPD, 1000)
        self.assertEqual(rate_limiter.DEFAULT_RPD, 1000)

        # Disable sleep/pacing for this stress test by advancing time
        simulated_now = 3000.0
        self.limiter.set_config({"rpm": 5000, "tpm": 100_000_000, "safety_factor": 1.0})

        with patch("time.time") as mock_time, patch("time.sleep") as mock_sleep:
            for i in range(1050):
                simulated_now += 0.1  # window slides forward
                mock_time.return_value = simulated_now
                self.limiter.acquire(estimated_tokens=5)

            # Confirm 1,050 calls executed without raising any RPD exception or blocking
            mock_sleep.assert_not_called()
            # And status reports RPD purely as a reference field
            status = self.limiter.get_status()
            self.assertEqual(status["rpd"], 1000)

    def test_6_rest_config_live_update(self):
        """Verify POST /config updates rpm and tpm live and persists atomically."""
        import asyncio

        async def run_async():
            # Initial status
            initial = await get_status()
            self.assertTrue(initial["enabled"])

            # Update via REST API
            req = ConfigUpdateRequest(rpm=20, tpm=350000, enabled=True)
            updated = await update_config(req)

            self.assertEqual(updated["rpm"], 20)
            self.assertEqual(updated["tpm"], 350000)
            self.assertEqual(updated["effective_rpm"], int(20 * updated["safety_factor"]))
            self.assertEqual(updated["effective_tpm"], int(350000 * updated["safety_factor"]))

            # Verify in-memory singleton reflects the new values immediately
            status = self.limiter.get_status()
            self.assertEqual(status["rpm"], 20)
            self.assertEqual(status["tpm"], 350000)

            # Verify file on disk
            loaded = rate_limiter.load_config()
            self.assertEqual(loaded["rpm"], 20)
            self.assertEqual(loaded["tpm"], 350000)

        asyncio.run(run_async())

    def test_7_reactive_429_cooldown(self):
        """Verify reactive 429 parsing and backoff."""
        with self.limiter._lock:
            self.limiter._cooldown_until = 0.0

        exc = Exception("Error: 429 Resource Exhausted. {'retryDelay': '4.5s'}")
        self.limiter.record_429(exc)

        status = self.limiter.get_status()
        self.assertTrue(status["is_throttling"])
        self.assertGreater(self.limiter._cooldown_until, time.time())


if __name__ == "__main__":
    unittest.main()
