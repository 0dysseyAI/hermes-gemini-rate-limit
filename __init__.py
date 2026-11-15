"""Gemini Rate Limiter — Proactive sliding-window quota gate for Google Gemini API."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

# Ensure package directory is importable
_PACKAGE_DIR = Path(__file__).resolve().parent
if str(_PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_DIR))

from rate_limiter import (  # noqa: E402
    DEFAULT_RPM,
    DEFAULT_TPM,
    DEFAULT_RPD,
    DEFAULT_SAFETY_FACTOR,
    CONFIG_PATH,
    STATUS_PATH,
    estimate_request_tokens,
    extract_prompt_tokens_from_response,
    get_hermes_home,
    get_limiter,
    is_gemini_target,
    load_config,
)

logger = logging.getLogger("hermes.plugins.gemini_rate_limit")

__all__ = [
    "DEFAULT_RPM",
    "DEFAULT_TPM",
    "DEFAULT_RPD",
    "DEFAULT_SAFETY_FACTOR",
    "CONFIG_PATH",
    "STATUS_PATH",
    "register",
    "throttle_gemini_execution",
    "handle_gemini_limit_cmd",
    "get_limiter",
]


def throttle_gemini_execution(request: Any, next_call: Callable[[Any], Any], **kwargs: Any) -> Any:
    """LLM execution middleware gating outbound Gemini requests by RPM and TPM."""
    if not is_gemini_target(kwargs, request):
        return next_call(request)

    limiter = get_limiter()
    estimated_tokens = estimate_request_tokens(request)
    model = kwargs.get("model") or (request.get("model") if isinstance(request, dict) else None)

    limiter.acquire(estimated_tokens, model=model)

    try:
        response = next_call(request)
    except Exception as exc:
        err_msg = str(exc)
        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "rate limit" in err_msg.lower():
            limiter.record_429(exc)
        raise

    actual_tokens = extract_prompt_tokens_from_response(response)
    if actual_tokens is not None:
        limiter.refine_tokens(actual_tokens)

    return response


def handle_gemini_limit_cmd(raw_args: str = "", **kwargs: Any) -> str:
    """Slash command handler for /gemini-limit [rpm] [tpm]."""
    limiter = get_limiter()
    args = (raw_args or "").strip().split()

    if not args:
        status = limiter.get_status()
        state_str = "Enabled" if status["enabled"] else "Disabled"
        return (
            f"**Gemini Rate Limiter ({state_str})**\n"
            f"• **RPM**: {status['rpm_used']}/{status['effective_rpm']} used "
            f"(Configured: {status['rpm']}, Safety factor: {status['safety_factor']})\n"
            f"• **TPM**: {status['tpm_used']:,}/{status['effective_tpm']:,} tokens used "
            f"(Configured: {status['tpm']:,})\n"
            f"• **RPD Reference**: {status['rpd']:,} peak requests/day (*unenforced constant*)\n"
            f"• **Throttled**: {status['total_throttled_requests']} total "
            f"(Last wait: {status['last_wait_seconds']}s)\n\n"
            f"*Tip: Update limits with `/gemini-limit [rpm] [tpm]` or via the Desktop status bar.*"
        )

    # User provided arguments to update limits
    try:
        new_rpm = int(args[0])
        new_tpm = int(args[1]) if len(args) > 1 else None

        if new_rpm < 1:
            return "Error: RPM must be at least 1."
        if new_tpm is not None and new_tpm < 1000:
            return "Error: TPM must be at least 1,000."

        update_dict: dict[str, Any] = {"rpm": new_rpm}
        if new_tpm is not None:
            update_dict["tpm"] = new_tpm

        updated = limiter.set_config(update_dict)
        eff_rpm = max(1, int(updated["rpm"] * updated["safety_factor"]))
        eff_tpm = max(100, int(updated["tpm"] * updated["safety_factor"]))

        return (
            f"✓ **Updated Gemini Rate Limits**\n"
            f"• **RPM**: {updated['rpm']} (Effective: {eff_rpm})\n"
            f"• **TPM**: {updated['tpm']:,} (Effective: {eff_tpm:,})\n"
            f"• **RPD Reference**: {updated.get('rpd', DEFAULT_RPD):,} (unenforced)\n"
            f"Saved to `~/.hermes/gemini_rate_limit.json` and active live."
        )
    except ValueError:
        return "Usage: `/gemini-limit [rpm] [tpm]` (e.g. `/gemini-limit 15 250000` or `/gemini-limit 10`)"


def register(ctx: Any) -> None:
    """Register middleware and slash commands with Hermes."""
    try:
        ctx.register_middleware("llm_execution", throttle_gemini_execution)
    except Exception as exc:
        logger.warning("Could not register llm_execution middleware: %s", exc)

    try:
        ctx.register_command(
            "gemini-limit",
            handle_gemini_limit_cmd,
            description="View or set Gemini RPM and TPM limits: /gemini-limit [rpm] [tpm]",
            args_hint="[rpm] [tpm]",
        )
    except Exception as exc:
        logger.warning("Could not register /gemini-limit command: %s", exc)
