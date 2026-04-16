#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
find-a-home — CLI entry point.

Commands:
  python main.py run                         Run all enabled profiles
  python main.py run --profile "Escondido"   Run one specific profile
  python main.py run --dry-run               Run without sending alerts or marking seen
  python main.py run --sources zillow        Override which scrapers to use
  python main.py list-profiles               Show configured profiles + affordability stats
  python main.py test-alerts                 Send test SMS + email
  python main.py serve                       Start the FastAPI web server

All run logic is in app/engine.py — this file is a thin CLI wrapper.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.text import Text

from app.config import load_profiles, settings
from app.engine import Engine
from app.financial.calculator import calculate_piti, max_affordable_price
from app.models import AlertPriority, DataSource, MatchResult, SearchProfile

console = Console()


# ── Logging setup ─────────────────────────────────────────────────────────────

def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silence noisy third-party loggers unless verbose
    if not verbose:
        for lib in ("httpx", "httpcore", "playwright", "asyncio"):
            logging.getLogger(lib).setLevel(logging.WARNING)


# ── Result rendering ──────────────────────────────────────────────────────────

_PRIORITY_STYLE = {
    AlertPriority.CRITICAL: "bold red",
    AlertPriority.HIGH: "bold yellow",
    AlertPriority.NORMAL: "bold green",
}

_PRIORITY_LABEL = {
    AlertPriority.CRITICAL: "ASSUMABLE / UNDER BUDGET",
    AlertPriority.HIGH: "ASSUMABLE — REVIEW",
    AlertPriority.NORMAL: "MATCH",
}


def _render_result(result: MatchResult) -> None:
    listing = result.listing
    piti = result.piti
    style = _PRIORITY_STYLE[result.alert_priority]
    label = _PRIORITY_LABEL[result.alert_priority]

    lines: list[str] = [
        f"[bold]{listing.address}, {listing.city}, {listing.state} {listing.zip_code}[/bold]",
        f"Price: [bold]${listing.price:,.0f}[/bold]  |  "
        f"{listing.bedrooms}bd / {listing.bathrooms}ba"
        + (f"  |  {listing.sqft:,} sqft" if listing.sqft else ""),
        f"PITI (market): [bold]${piti.total_monthly:,.0f}/mo[/bold]  "
        f"({piti.formatted})",
        f"HOA: {'$' + f'{listing.hoa_monthly:,.0f}/mo' if listing.hoa_monthly else 'None / not reported'}",
        f"Source: [link={listing.url}]{listing.url}[/link]",
    ]

    if result.assumable.is_assumable:
        a = result.assumable
        lines.append("")
        lines.append("[bold yellow]Assumable Loan Details[/bold yellow]")
        if a.assumable_rate:
            lines.append(f"  Rate: {a.assumable_rate*100:.2f}%")
        if a.estimated_loan_balance:
            lines.append(f"  Est. balance: ${a.estimated_loan_balance:,.0f}")
        if a.equity_gap is not None:
            flag = "  [bold red]⚠ HIGH CASH REQUIRED[/bold red]" if a.high_cash_required else ""
            lines.append(f"  Equity gap: ${a.equity_gap:,.0f}{flag}")
        if result.assumable_piti:
            lines.append(
                f"  PITI at assumable rate: [bold]${result.assumable_piti.total_monthly:,.0f}/mo[/bold]"
            )

    lines.append("")
    lines.append("[dim]Why matched:[/dim]")
    for reason in result.why_matched:
        lines.append(f"  [dim]• {reason}[/dim]")

    console.print(
        Panel(
            "\n".join(lines),
            title=f"[{style}] {label} [{style}]",
            border_style=style.split()[-1],  # extract colour part
            expand=False,
        )
    )


def _render_summary_table(results: list[MatchResult]) -> None:
    table = Table(
        title=f"Run Summary — {len(results)} match(es)",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Priority", style="bold", width=10)
    table.add_column("Address", min_width=30)
    table.add_column("Price", justify="right")
    table.add_column("PITI/mo", justify="right")
    table.add_column("Profile")
    table.add_column("Source")

    for r in sorted(results, key=lambda x: x.alert_priority.value):
        style = _PRIORITY_STYLE[r.alert_priority]
        table.add_row(
            Text(_PRIORITY_LABEL[r.alert_priority], style=style),
            r.listing.short_address,
            f"${r.listing.price:,.0f}",
            f"${r.piti.total_monthly:,.0f}",
            r.profile_name,
            r.listing.source.value,
        )

    console.print(table)


# ── CLI commands ──────────────────────────────────────────────────────────────

@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx: click.Context, verbose: bool) -> None:
    """find-a-home: real estate deal finder with PITI + assumable loan analysis."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _configure_logging(verbose)


@cli.command()
# ── Profile selection ─────────────────────────────────────────────────────────
@click.option("--profile", "profile_name", default=None,
              help="Run only this profile by name")
@click.option("--dry-run", is_flag=True,
              help="Scrape and filter but do NOT send alerts or mark listings as seen")
@click.option("--sources", multiple=True,
              type=click.Choice(["zillow", "redfin", "realtor", "homes"], case_sensitive=False),
              help="Override scraper(s) for this run")
# ── Financial overrides ───────────────────────────────────────────────────────
@click.option("--max-piti", type=float, default=None,
              help="Max monthly PITI, e.g. 4500")
@click.option("--down-payment", type=float, default=None,
              help="Down payment, e.g. 100000")
@click.option("--rate", type=float, default=None,
              help="Interest rate as decimal, e.g. 0.065 for 6.5%%")
# ── Property overrides ────────────────────────────────────────────────────────
@click.option("--min-beds", type=int, default=None,
              help="Minimum bedrooms")
@click.option("--max-beds", type=int, default=None,
              help="Maximum bedrooms")
@click.option("--min-baths", type=float, default=None,
              help="Minimum bathrooms")
@click.option("--min-sqft", type=int, default=None,
              help="Minimum square footage")
@click.option("--max-sqft", type=int, default=None,
              help="Maximum square footage")
@click.option("--min-price", type=float, default=None,
              help="Minimum listing price")
@click.option("--max-price", type=float, default=None,
              help="Maximum listing price")
@click.option("--max-hoa", type=float, default=None,
              help="Max HOA fee/mo (0 = strict no-HOA only)")
# ── Special flags ─────────────────────────────────────────────────────────────
@click.option("--assumable-only", is_flag=True, default=False,
              help="Only show listings with assumable loan keywords")
@click.option("--has-solar", is_flag=True, default=False,
              help="Only show listings with solar mentioned")
@click.pass_context
def run(
    ctx: click.Context,
    profile_name: Optional[str],
    dry_run: bool,
    sources: tuple[str, ...],
    max_piti: Optional[float],
    down_payment: Optional[float],
    rate: Optional[float],
    min_beds: Optional[int],
    max_beds: Optional[int],
    min_baths: Optional[float],
    min_sqft: Optional[int],
    max_sqft: Optional[int],
    min_price: Optional[float],
    max_price: Optional[float],
    max_hoa: Optional[float],
    assumable_only: bool,
    has_solar: bool,
) -> None:
    """
    Run scrapers, apply filters, send alerts for new matches.

    All filter flags override the loaded profile for this run only —
    nothing is written back to search_profiles.json.

    Examples:

    \b
      # Run Hartwell with a tighter budget
      python main.py run --profile "Lake Hartwell GA/SC" --max-piti 3000

      # Find only assumable loans with solar, any area
      python main.py run --assumable-only --has-solar --dry-run

      # Quick check of Durham with 3-bed minimum
      python main.py run --profile "Durham NC" --min-beds 3 --dry-run
    """
    try:
        profiles = load_profiles()
    except FileNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    # Build override dict from any flags that were explicitly set
    overrides: dict = {}
    if max_piti is not None:      overrides["max_monthly_piti"] = max_piti
    if down_payment is not None:  overrides["down_payment"] = down_payment
    if rate is not None:          overrides["interest_rate"] = rate
    if min_beds is not None:      overrides["min_bedrooms"] = min_beds
    if max_beds is not None:      overrides["max_bedrooms"] = max_beds
    if min_baths is not None:     overrides["min_bathrooms"] = min_baths
    if min_sqft is not None:      overrides["min_sqft"] = min_sqft
    if max_sqft is not None:      overrides["max_sqft"] = max_sqft
    if min_price is not None:     overrides["min_price"] = min_price
    if max_price is not None:     overrides["max_price"] = max_price
    if max_hoa is not None:       overrides["max_hoa_monthly"] = max_hoa
    if assumable_only:            overrides["assumable_only"] = True
    if has_solar:                 overrides["requires_solar"] = True

    if overrides:
        profiles = [p.model_copy(update=overrides) for p in profiles]
        console.print(f"[cyan]Overrides applied:[/cyan] {overrides}")

    sources_override = [DataSource(s) for s in sources] if sources else None

    if dry_run:
        console.print("[yellow]Dry-run mode — alerts will NOT be sent.[/yellow]")

    engine = Engine(settings, dry_run=dry_run, sources_override=sources_override)

    async def _run() -> list[MatchResult]:
        return await engine.run(profiles, profile_name_filter=profile_name)

    results = asyncio.run(_run())

    if not results:
        console.print("[green]No new matches found this run.[/green]")
        return

    for result in results:
        _render_result(result)

    _render_summary_table(results)


@cli.command("list-profiles")
def list_profiles_cmd() -> None:
    """List all configured search profiles and their affordability envelope."""
    try:
        profiles = load_profiles()
    except FileNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    table = Table(title="Configured Search Profiles", box=box.ROUNDED)
    table.add_column("Name")
    table.add_column("Enabled", justify="center")
    table.add_column("Zips")
    table.add_column("Filters")
    table.add_column("Budget/mo")
    table.add_column("Max price (back-solve)")
    table.add_column("Sources")

    for p in profiles:
        max_p = max_affordable_price(p, p.tax_region)
        table.add_row(
            p.name,
            "✓" if p.enabled else "✗",
            ", ".join(p.zip_codes),
            f"{p.min_bedrooms}bd/{p.min_bathrooms}ba  HOA≤${p.max_hoa_monthly:.0f}",
            f"${p.max_monthly_piti:,.0f}",
            f"${max_p:,.0f}",
            ", ".join(s.value for s in p.sources),
        )

    console.print(table)


@cli.command("test-alerts")
def test_alerts() -> None:
    """Send a test SMS and email to verify your notification credentials."""
    import datetime
    from app.models import (
        AssumableDetails,
        PITIBreakdown,
        RawListing,
    )

    dummy_listing = RawListing(
        listing_id="test_000",
        source=DataSource.ZILLOW,
        url="https://www.zillow.com/homedetails/test",
        address="123 Test St",
        city="Escondido",
        state="CA",
        zip_code="92025",
        price=849_000,
        bedrooms=4,
        bathrooms=2.5,
        sqft=2_100,
        hoa_monthly=0,
        description="Assumable VA loan at 2.75% with balance around $400k.",
        scraped_at=datetime.datetime.utcnow(),
    )
    dummy_piti = PITIBreakdown(
        loan_amount=749_000,
        annual_rate=0.065,
        principal_interest=4_735.10,
        monthly_taxes=849.00,
        monthly_insurance=200.00,
        total_monthly=5_784.10,
    )
    dummy_result = MatchResult(
        listing=dummy_listing,
        profile_name="Test Profile",
        piti=dummy_piti,
        assumable=AssumableDetails(
            is_assumable=True,
            assumable_rate=0.0275,
            estimated_loan_balance=400_000,
            equity_gap=449_000,
            high_cash_required=True,
            matched_keywords=["assumable"],
        ),
        is_affordable=False,
        alert_priority=AlertPriority.HIGH,
        why_matched=["Test alert — verifying notification pipeline"],
    )

    sms = SMSAlert()
    email = EmailAlert()

    async def _send() -> None:
        if sms.is_configured:
            await sms.send(dummy_result)
            console.print("[green]SMS sent.[/green]")
        else:
            console.print("[yellow]SMS not configured — skipped.[/yellow]")
        if email.is_configured:
            await email.send(dummy_result)
            console.print("[green]Email sent.[/green]")
        else:
            console.print("[yellow]Email not configured — skipped.[/yellow]")

    from app.alerts.sms import SMSAlert
    from app.alerts.email_alert import EmailAlert
    asyncio.run(_send())


@cli.command("serve")
@click.option("--host", default=settings.api_host, help="Bind host")
@click.option("--port", default=settings.api_port, help="Bind port")
@click.option("--reload", is_flag=True, help="Enable auto-reload (development only)")
def serve(host: str, port: int, reload: bool) -> None:
    """Start the FastAPI web server (future web/iOS backend)."""
    try:
        import uvicorn
    except ImportError:
        console.print("[red]uvicorn not installed. Run: pip install uvicorn[standard][/red]")
        sys.exit(1)
    console.print(f"[green]Starting API server at http://{host}:{port}[/green]")
    uvicorn.run("api.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    cli()
