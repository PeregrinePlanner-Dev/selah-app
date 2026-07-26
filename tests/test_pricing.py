"""Phase 1 test suite, added 2026-07-24 (Selah_Structured_Audit_2026-07-24.md
finding: no automated test suite existed anywhere in this codebase). Mirrors
Peregrine's own Phase 1 approach: offline, no live Supabase/Stripe/Anthropic
calls, run in well under a second. Covers the pricing-consolidation fix from
this same session -- DISPLAY_PRICING/CHURCH_SEAT_DISPLAY are exactly the kind
of "computed once, read by three templates" logic that silently drifting
would be hard to notice visually but easy to catch here.

pro_billing.py imports cleanly with no real API keys (stripe.api_key is just
set to an empty string, not validated at import time) -- confirmed directly
before writing these tests, not assumed.
"""

import pro_billing


def test_display_pricing_matches_tier_info():
    """DISPLAY_PRICING must always be a formatted mirror of TIER_INFO -- if
    someone edits TIER_INFO's dollar amounts without touching this test,
    the test should still pass (proving the derivation is live, not a
    second hand-copied constant)."""
    for tier, info in pro_billing.TIER_INFO.items():
        assert pro_billing.DISPLAY_PRICING[tier]["monthly"] == pro_billing._fmt_price(info["monthly"])
        assert pro_billing.DISPLAY_PRICING[tier]["annual"] == pro_billing._fmt_price(info["annual"])


def test_display_pricing_current_values():
    """Pins today's actual live prices (2026-07-24) -- catches a real
    accidental change to TIER_INFO, not just a derivation bug. If this
    starts failing because pricing genuinely changed, update it alongside
    the change, the same way ministry.html/pro_app.html now do
    automatically without a template edit."""
    assert pro_billing.DISPLAY_PRICING == {
        "explore": {"monthly": "17", "annual": "170"},
        "pursue": {"monthly": "28", "annual": "280"},
        "immerse": {"monthly": "49", "annual": "490"},
    }


def test_church_seat_display_matches_tiers():
    for seat_type, brackets in pro_billing.CHURCH_SEAT_TIERS.items():
        expected = [pro_billing._fmt_price(price) for _upper, price in brackets]
        assert pro_billing.CHURCH_SEAT_DISPLAY[seat_type] == expected


def test_church_seat_display_current_values():
    assert pro_billing.CHURCH_SEAT_DISPLAY == {
        "leader": ["14", "12", "10"],
        "member": ["8", "7.50", "7", "6.50"],
    }


def test_fmt_price_whole_dollars_no_decimal():
    assert pro_billing._fmt_price(14.00) == "14"
    assert pro_billing._fmt_price(490.00) == "490"


def test_fmt_price_cents_keeps_two_decimals():
    assert pro_billing._fmt_price(7.50) == "7.50"
    assert pro_billing._fmt_price(6.50) == "6.50"


def test_price_per_seat_bracket_boundaries():
    """_price_per_seat() is the live function the admin dashboard and
    checkout flow actually call to price a seat-quantity change -- worth
    testing its bracket edges directly (off-by-one on a volume-tier
    boundary is a real billing bug, not a cosmetic one)."""
    assert pro_billing._price_per_seat("leader", 1) == 14.00
    assert pro_billing._price_per_seat("leader", 4) == 14.00
    assert pro_billing._price_per_seat("leader", 5) == 12.00
    assert pro_billing._price_per_seat("leader", 9) == 12.00
    assert pro_billing._price_per_seat("leader", 10) == 10.00
    assert pro_billing._price_per_seat("leader", 500) == 10.00

    assert pro_billing._price_per_seat("member", 24) == 8.00
    assert pro_billing._price_per_seat("member", 25) == 7.50
    assert pro_billing._price_per_seat("member", 1000) == 6.50
