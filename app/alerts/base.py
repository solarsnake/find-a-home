"""Abstract alert interface — all notification channels implement this."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import MatchResult


class BaseAlert(ABC):
    """
    One alert = one notification channel (SMS, email, push notification, …).

    Future channels (iOS APNs, Slack, Discord) are new subclasses that
    implement send() — the engine calls them all uniformly.
    """

    @abstractmethod
    async def send(self, result: MatchResult) -> None:
        """
        Deliver a notification for a single match result.
        Should not raise — log errors internally and return gracefully.
        """
        ...

    @abstractmethod
    async def send_batch(self, results: list[MatchResult]) -> None:
        """
        Deliver a summary notification for multiple results at once.
        Used by the email channel for a digest report.
        """
        ...

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if the necessary credentials are set in the environment."""
        ...
