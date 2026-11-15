"""Backend REST API for gemini-rate-limit, mounted at /api/plugins/gemini-rate-limit/"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from rate_limiter import DEFAULT_RPD, get_limiter  # noqa: E402

logger = logging.getLogger("hermes.plugins.gemini_rate_limit.api")
router = APIRouter()


class ConfigUpdateRequest(BaseModel):
    rpm: Optional[int] = Field(None, ge=1, description="Peak requests per minute (enforced)")
    tpm: Optional[int] = Field(None, ge=1000, description="Peak input tokens per minute (enforced)")
    rpd: Optional[int] = Field(None, ge=1, description="Peak requests per day (unused reference constant)")
    enabled: Optional[bool] = Field(None, description="Whether rate limiting is enabled")
    safety_factor: Optional[float] = Field(None, ge=0.1, le=1.0, description="Safety factor (0.1 to 1.0)")


@router.get("/status")
async def get_status() -> dict[str, Any]:
    """Get current configuration and live 60-second window quota telemetry."""
    limiter = get_limiter()
    return limiter.get_status()


@router.post("/config")
async def update_config(payload: ConfigUpdateRequest) -> dict[str, Any]:
    """Update configuration live, save to disk atomically, and return updated state."""
    limiter = get_limiter()
    update_data = payload.model_dump(exclude_unset=True)

    if not update_data:
        raise HTTPException(status_code=400, detail="No valid configuration fields provided")

    # Validate specific constraints
    if "rpm" in update_data and update_data["rpm"] < 1:
        raise HTTPException(status_code=400, detail="RPM must be >= 1")
    if "tpm" in update_data and update_data["tpm"] < 1000:
        raise HTTPException(status_code=400, detail="TPM must be >= 1000")

    try:
        limiter.set_config(update_data)
        logger.info("Updated Gemini rate limit configuration: %s", update_data)
        return limiter.get_status()
    except Exception as exc:
        logger.exception("Failed to update config: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to update config: {exc}")
