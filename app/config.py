from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    generation_mode: str = "mock"
    llm_provider: str = "openai"
    llm_model: str = "gpt-4.1-mini"
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_timeout_seconds: int = 30
    llm_max_retries: int = 2
    llm_temperature: float = 0.2
    llm_max_output_tokens: int = 900
    allow_mock_fallback: bool = False

    journey_paused_after_hours: int = 24
    journey_abandoned_after_days: int = 7
    review_suggest_after_days: int = 14
    recent_activity_window_hours: int = 12


def get_settings() -> Settings:
    return Settings(
        generation_mode=os.getenv("GENERATION_MODE", "mock"),
        llm_provider=os.getenv("LLM_PROVIDER", "openai"),
        llm_model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
        llm_api_key=os.getenv("LLM_API_KEY"),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
        llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
        llm_max_output_tokens=int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "900")),
        allow_mock_fallback=os.getenv("ALLOW_MOCK_FALLBACK", "false").lower() == "true",
        journey_paused_after_hours=int(os.getenv("JOURNEY_PAUSED_AFTER_HOURS", "24")),
        journey_abandoned_after_days=int(os.getenv("JOURNEY_ABANDONED_AFTER_DAYS", "7")),
        review_suggest_after_days=int(os.getenv("REVIEW_SUGGEST_AFTER_DAYS", "14")),
        recent_activity_window_hours=int(os.getenv("RECENT_ACTIVITY_WINDOW_HOURS", "12")),
    )
