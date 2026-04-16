"""
Email alert channel — SendGrid (primary) or SMTP fallback.

send()       → one email per critical/high-priority listing (immediate alert)
send_batch() → one digest email for all results from a run
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from app.alerts.base import BaseAlert
from app.config import settings
from app.models import AlertPriority, MatchResult

logger = logging.getLogger(__name__)


# ── HTML template helpers ─────────────────────────────────────────────────────

_PRIORITY_COLOR = {
    AlertPriority.CRITICAL: "#d32f2f",
    AlertPriority.HIGH: "#f57c00",
    AlertPriority.NORMAL: "#388e3c",
}

_PRIORITY_LABEL = {
    AlertPriority.CRITICAL: "ASSUMABLE — UNDER BUDGET",
    AlertPriority.HIGH: "ASSUMABLE — REVIEW",
    AlertPriority.NORMAL: "NEW MATCH",
}


def _listing_card_html(result: MatchResult) -> str:
    listing = result.listing
    piti = result.piti
    color = _PRIORITY_COLOR[result.alert_priority]
    label = _PRIORITY_LABEL[result.alert_priority]

    assumable_row = ""
    if result.assumable.is_assumable:
        gap_str = (
            f"${result.assumable.equity_gap:,.0f}"
            if result.assumable.equity_gap is not None
            else "Unknown"
        )
        cash_warn = (
            " &nbsp;<strong style='color:#d32f2f'>⚠ HIGH CASH REQUIRED</strong>"
            if result.assumable.high_cash_required
            else ""
        )
        assumable_row = f"""
        <tr>
          <td style="padding:4px 8px;color:#555">Assumable Loan</td>
          <td style="padding:4px 8px">
            Rate: {f"{result.assumable.assumable_rate*100:.2f}%" if result.assumable.assumable_rate else "see listing"}
            &nbsp;|&nbsp; Est. Equity Gap: {gap_str}{cash_warn}
          </td>
        </tr>"""

    assumable_piti_row = ""
    if result.assumable_piti:
        assumable_piti_row = f"""
        <tr>
          <td style="padding:4px 8px;color:#555">PITI (at assumable rate)</td>
          <td style="padding:4px 8px;font-weight:bold">
            ${result.assumable_piti.total_monthly:,.0f}/mo
          </td>
        </tr>"""

    why_items = "".join(f"<li>{w}</li>" for w in result.why_matched)

    return f"""
    <div style="border:2px solid {color};border-radius:6px;margin:16px 0;font-family:sans-serif;">
      <div style="background:{color};color:#fff;padding:8px 12px;font-weight:bold;">
        {label} &mdash; {listing.address}, {listing.city}, {listing.state}
      </div>
      <div style="padding:12px;">
        <table style="border-collapse:collapse;width:100%">
          <tr>
            <td style="padding:4px 8px;color:#555">Price</td>
            <td style="padding:4px 8px;font-weight:bold">${listing.price:,.0f}</td>
            <td style="padding:4px 8px;color:#555">Beds/Baths</td>
            <td style="padding:4px 8px">{listing.bedrooms}bd / {listing.bathrooms}ba</td>
          </tr>
          <tr>
            <td style="padding:4px 8px;color:#555">PITI (market rate)</td>
            <td style="padding:4px 8px;font-weight:bold">${piti.total_monthly:,.0f}/mo</td>
            <td style="padding:4px 8px;color:#555">HOA</td>
            <td style="padding:4px 8px">
              {"$" + f"{listing.hoa_monthly:,.0f}/mo" if listing.hoa_monthly else "None / Not reported"}
            </td>
          </tr>
          <tr>
            <td style="padding:4px 8px;color:#555">PITI breakdown</td>
            <td colspan="3" style="padding:4px 8px;font-size:0.85em;color:#444">{piti.formatted}</td>
          </tr>
          {assumable_row}
          {assumable_piti_row}
          <tr>
            <td style="padding:4px 8px;color:#555">Source</td>
            <td colspan="3" style="padding:4px 8px">
              <a href="{listing.url}" style="color:{color}">{listing.url}</a>
            </td>
          </tr>
        </table>
        <div style="margin-top:10px;font-size:0.85em;color:#555">
          <strong>Why this matched:</strong>
          <ul style="margin:4px 0">{why_items}</ul>
        </div>
      </div>
    </div>"""


def _build_digest_html(results: list[MatchResult]) -> str:
    profile_names = sorted({r.profile_name for r in results})
    critical = [r for r in results if r.alert_priority == AlertPriority.CRITICAL]
    high = [r for r in results if r.alert_priority == AlertPriority.HIGH]
    normal = [r for r in results if r.alert_priority == AlertPriority.NORMAL]

    cards = ""
    for section_label, section_results in [
        ("Assumable — Under Budget", critical),
        ("Assumable — Review Required", high),
        ("Standard Matches", normal),
    ]:
        if section_results:
            cards += f"<h2 style='font-family:sans-serif'>{section_label} ({len(section_results)})</h2>"
            cards += "".join(_listing_card_html(r) for r in section_results)

    return f"""
    <!DOCTYPE html>
    <html><body style="max-width:700px;margin:0 auto;padding:16px">
      <h1 style="font-family:sans-serif;border-bottom:2px solid #1565c0;padding-bottom:8px">
        find-a-home: {len(results)} New Match(es)
      </h1>
      <p style="font-family:sans-serif;color:#555">
        Profiles: {", ".join(profile_names)}<br>
        Summary: {len(critical)} assumable/under-budget &bull;
                 {len(high)} assumable/review &bull;
                 {len(normal)} standard
      </p>
      {cards}
      <p style="font-family:sans-serif;font-size:0.8em;color:#aaa;margin-top:32px">
        Sent by find-a-home &mdash; edit search_profiles.json to adjust criteria.
      </p>
    </body></html>"""


def _build_single_html(result: MatchResult) -> str:
    label = _PRIORITY_LABEL[result.alert_priority]
    return f"""
    <!DOCTYPE html>
    <html><body style="max-width:700px;margin:0 auto;padding:16px">
      <h1 style="font-family:sans-serif">{label}</h1>
      {_listing_card_html(result)}
    </body></html>"""


# ── Alert class ───────────────────────────────────────────────────────────────

class EmailAlert(BaseAlert):

    @property
    def is_configured(self) -> bool:
        return settings.email_enabled

    async def send(self, result: MatchResult) -> None:
        """Send an immediate alert for a single critical/high-priority listing."""
        if not self.is_configured:
            return
        label = _PRIORITY_LABEL[result.alert_priority]
        subject = (
            f"[find-a-home] {label} — "
            f"{result.listing.address}, {result.listing.city} "
            f"${result.piti.total_monthly:,.0f}/mo"
        )
        html = _build_single_html(result)
        await self._deliver(subject, html)

    async def send_batch(self, results: list[MatchResult]) -> None:
        """Send a digest email for all matches from a run."""
        if not self.is_configured or not results:
            return
        subject = f"[find-a-home] {len(results)} new match(es)"
        html = _build_digest_html(results)
        await self._deliver(subject, html)

    async def _deliver(self, subject: str, html: str) -> None:
        await asyncio.get_event_loop().run_in_executor(
            None, self._deliver_sync, subject, html
        )

    def _deliver_sync(self, subject: str, html: str) -> None:
        if settings.sendgrid_api_key:
            self._send_sendgrid(subject, html)
        else:
            logger.warning(
                "No SENDGRID_API_KEY set — email alert skipped. "
                "Set SENDGRID_API_KEY or configure SMTP credentials."
            )

    def _send_sendgrid(self, subject: str, html: str) -> None:
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail

            message = Mail(
                from_email=settings.alert_email_from,
                to_emails=settings.alert_email_to,
                subject=subject,
                html_content=html,
            )
            sg = SendGridAPIClient(settings.sendgrid_api_key)
            response = sg.send(message)
            logger.info("Email sent via SendGrid (status %s)", response.status_code)
        except Exception as exc:
            logger.error("SendGrid send failed: %s", exc)
