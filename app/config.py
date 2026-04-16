"""
Application settings loaded from environment variables / .env file.

These are *runtime* settings (credentials, feature flags).
User-visible search criteria live in search_profiles.json, not here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import SearchProfile


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Twilio ────────────────────────────────────────────────────────────────
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    twilio_to_number: str = ""

    # ── SendGrid ──────────────────────────────────────────────────────────────
    sendgrid_api_key: str = ""
    alert_email_from: str = ""
    alert_email_to: str = ""

    # ── Scraping ──────────────────────────────────────────────────────────────
    playwright_headless: bool = True
    scrape_delay_min: float = 2.5
    scrape_delay_max: float = 6.0

    # ── Storage ───────────────────────────────────────────────────────────────
    data_dir: str = "data"
    seen_listings_file: str = "data/seen_listings.json"

    # ── Financial defaults ────────────────────────────────────────────────────
    default_interest_rate: float = 0.065
    default_down_payment: float = 100_000.0
    default_max_piti: float = 4_500.0
    default_insurance: float = 200.0

    # ── Web API (future) ──────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_secret_key: str = "change-me-in-production"

    # ── Derived helpers ───────────────────────────────────────────────────────
    @property
    def sms_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    @property
    def email_enabled(self) -> bool:
        return bool(self.sendgrid_api_key or self.alert_email_from)

    def ensure_data_dir(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)


def load_profiles(path: str = "search_profiles.json") -> list[SearchProfile]:
    """
    Load SearchProfile objects from the user-editable JSON file.

    This function is intentionally simple so that in a future web/iOS app
    profiles are fetched from a database instead — swap this one function.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"search_profiles.json not found at {p.resolve()}\n"
            "Copy search_profiles.example.json → search_profiles.json and edit it."
        )
    data = json.loads(p.read_text())
    return [SearchProfile(**profile) for profile in data.get("profiles", [])]


# Module-level singleton — import `settings` anywhere in the app
settings = Settings()
