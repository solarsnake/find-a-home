"""
PITI (Principal, Interest, Taxes, Insurance) financial calculations.

Pure functions — no I/O, no side effects.
Easily unit-testable and reusable from web/iOS layers.
"""

from __future__ import annotations

import math
from typing import Optional

from app.models import PITIBreakdown, SearchProfile, TAX_RATES, TaxRegion


def monthly_principal_interest(
    loan_amount: float,
    annual_rate: float,
    years: int = 30,
) -> float:
    """
    Standard amortising mortgage payment formula.

    M = P · [r(1+r)^n] / [(1+r)^n − 1]

    Args:
        loan_amount:  Principal (purchase price minus down payment).
        annual_rate:  Annual interest rate as a decimal (e.g. 0.065 for 6.5 %).
        years:        Loan term in years (default 30).

    Returns:
        Monthly payment covering principal and interest only.
    """
    if loan_amount <= 0:
        return 0.0
    if annual_rate == 0:
        # Edge case: interest-free (shouldn't occur in practice)
        return loan_amount / (years * 12)

    r = annual_rate / 12
    n = years * 12
    factor = math.pow(1 + r, n)
    return loan_amount * (r * factor) / (factor - 1)


def calculate_piti(
    price: float,
    profile: SearchProfile,
    annual_rate: Optional[float] = None,
    loan_amount_override: Optional[float] = None,
) -> PITIBreakdown:
    """
    Build a complete PITI breakdown for a given price and search profile.

    Args:
        price:                 Listing price.
        profile:               SearchProfile holding down payment, rate, insurance.
        annual_rate:           Override the profile rate (used for assumable loans).
        loan_amount_override:  Override loan principal (used for assumable loans).

    Returns:
        PITIBreakdown with all four components and a total.
    """
    rate = annual_rate if annual_rate is not None else profile.interest_rate
    loan = (
        loan_amount_override
        if loan_amount_override is not None
        else max(0.0, price - profile.down_payment)
    )

    tax_rate = TAX_RATES.get(profile.tax_region, 0.012)

    pi = monthly_principal_interest(loan, rate)
    taxes = (price * tax_rate) / 12
    insurance = profile.monthly_insurance

    # PMI applies when LTV > 80% (down payment < 20% of purchase price).
    # Typical rate: 0.85% of loan amount annually.
    # Not applied when an explicit loan_amount_override is given (e.g. assumable balance).
    ltv = loan / price if price > 0 else 0
    pmi = (loan * 0.0085 / 12) if (loan_amount_override is None and ltv > 0.80) else 0.0

    total = pi + taxes + insurance + pmi

    return PITIBreakdown(
        loan_amount=round(loan, 2),
        annual_rate=rate,
        principal_interest=round(pi, 2),
        monthly_taxes=round(taxes, 2),
        monthly_insurance=round(insurance, 2),
        monthly_pmi=round(pmi, 2),
        total_monthly=round(total, 2),
    )


def max_affordable_price(
    profile: SearchProfile,
    tax_region: TaxRegion,
    years: int = 30,
) -> float:
    """
    Back-solve: what is the highest price where PITI ≤ max_monthly_piti?
    Useful for surfacing a "max price" hint in the UI.
    """
    tax_rate = TAX_RATES.get(tax_region, 0.012)
    budget_for_pi = profile.max_monthly_piti - profile.monthly_insurance

    r = profile.interest_rate / 12
    n = years * 12
    factor = math.pow(1 + r, n)
    pi_per_dollar_loan = (r * factor) / (factor - 1)
    # taxes per dollar of price (monthly)
    tax_per_dollar = tax_rate / 12

    # budget_for_pi = loan * pi_per_dollar_loan + price * tax_per_dollar
    # loan = price - down_payment
    # => budget_for_pi = (price - dp) * pi_per_dollar_loan + price * tax_per_dollar
    # => price * (pi_per_dollar_loan + tax_per_dollar) = budget_for_pi + dp * pi_per_dollar_loan
    denom = pi_per_dollar_loan + tax_per_dollar
    numerator = budget_for_pi + profile.down_payment * pi_per_dollar_loan
    return round(numerator / denom, 2) if denom > 0 else 0.0
