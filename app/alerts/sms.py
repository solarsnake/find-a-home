"""
Twilio SMS alert channel.

Sends a short (≤160 char) message per match, or a brief summary when
called with send_batch().  CRITICAL priority listings get a prefix flag.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.alerts.base import BaseAlert
from app.config import settings
from app.models import AlertPriority, MatchResult

logger = logging.getLogger(__name__)


def _format_sms(result: MatchResult) -> str:
    """
    Build a concise SMS body.  Stays under 160 chars for a single SMS segment.

    Example:
      [ASSUMABLE] 4bd/2ba $3,812/mo | 123 Main St, Escondido $849k
      zillow.com/homedetails/...
    """
    listing = result.listing
    piti = result.piti

    prefix = ""
    if result.alert_priority == AlertPriority.CRITICAL:
        prefix = "[ASSUMABLE] "
    elif result.alert_priority == AlertPriority.HIGH:
        prefix = "[ASSUMABLE-REVIEW] "

    equity_note = ""
    if result.assumable.high_cash_required:
        gap_k = int((result.assumable.equity_gap or 0) / 1_000)
        equity_note = f" ⚠ Equity gap ~${gap_k}k"

    price_k = int(listing.price / 1_000)
    body = (
        f"{prefix}{listing.bedrooms}bd/{listing.bathrooms}ba "
        f"${piti.total_monthly:,.0f}/mo PITI | "
        f"{listing.address}, {listing.city} ${price_k}k"
        f"{equity_note}\n{listing.url}"
    )
    return body


class SMSAlert(BaseAlert):
    def __init__(self) -> None:
        self._client: Optional[object] = None

    def _get_client(self):
        if self._client is None:
            from twilio.rest import Client
            self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        return self._client

    @property
    def is_configured(self) -> bool:
        return settings.sms_enabled

    async def send(self, result: MatchResult) -> None:
        if not self.is_configured:
            return
        try:
            body = _format_sms(result)
            client = self._get_client()
            # Twilio client is synchronous — run in thread pool
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.messages.create(
                    body=body,
                    from_=settings.twilio_from_number,
                    to=settings.twilio_to_number,
                ),
            )
            logger.info("SMS sent for listing %s", result.listing.listing_id)
        except Exception as exc:
            logger.error("SMS send failed for %s: %s", result.listing.listing_id, exc)

    async def send_batch(self, results: list[MatchResult]) -> None:
        if not self.is_configured or not results:
            return
        # Send a brief summary SMS for batch runs
        critical = sum(1 for r in results if r.alert_priority == AlertPriority.CRITICAL)
        high = sum(1 for r in results if r.alert_priority == AlertPriority.HIGH)
        normal = sum(1 for r in results if r.alert_priority == AlertPriority.NORMAL)
        body = (
            f"find-a-home: {len(results)} new match(es) — "
            f"{critical} assumable, {high} high, {normal} normal. "
            "Check your email for details."
        )
        try:
            client = self._get_client()
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.messages.create(
                    body=body,
                    from_=settings.twilio_from_number,
                    to=settings.twilio_to_number,
                ),
            )
        except Exception as exc:
            logger.error("Batch SMS send failed: %s", exc)
