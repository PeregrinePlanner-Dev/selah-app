"""Selah for Ministry -- Stripe billing blueprint.

Individual Pro, self-serve, three tiers (per Rick's 2026-07-09 pricing
finalization, commit 22c3d2c): Explore $17/mo ($170/yr, 120 exchanges),
Pursue $28/mo ($280/yr, 225 exchanges), Immerse $49/mo ($490/yr, 400
exchanges). Church/Org and the congregation Member Add-On are deliberately
NOT wired to Checkout here -- seat-based purchasing isn't built yet (Phase
3), and both are still a "contact us" sales motion on ministry.html, not a
self-serve flow. Wiring only what's actually sellable today keeps this from
promising a checkout experience that doesn't exist.

Kept as its own module, registered onto the existing Flask app additively,
same pattern as pro_auth.py/pro_chat.py -- the free tool's routes and the
rest of Pro stay untouched by this.

Setup required before this does anything (all via env vars, none of which
exist yet as of this writing -- see Selah_Pro_Build_Roadmap.md Section 14
for the "7 Stripe products" count: 3 tiers x 2 billing cycles + 1 overage
block):
  STRIPE_SECRET_KEY               -- Stripe dashboard > Developers > API keys
  STRIPE_WEBHOOK_SECRET            -- shown when you register this endpoint's
                                      URL (https://<domain>/pro/billing/webhook)
                                      as a webhook destination in the Stripe
                                      dashboard
  STRIPE_PRICE_EXPLORE_MONTHLY     -- $17/mo, 120 exchanges/mo
  STRIPE_PRICE_EXPLORE_ANNUAL      -- $170/yr
  STRIPE_PRICE_PURSUE_MONTHLY      -- $28/mo, 225 exchanges/mo
  STRIPE_PRICE_PURSUE_ANNUAL       -- $280/yr
  STRIPE_PRICE_IMMERSE_MONTHLY     -- $49/mo, 400 exchanges/mo
  STRIPE_PRICE_IMMERSE_ANNUAL      -- $490/yr
  STRIPE_PRICE_EXCHANGE_BLOCK      -- $15 one-time, 100 exchanges. Price ID
                                      wiring only -- NOT a working purchase
                                      flow yet. Redeeming a purchased block
                                      into someone's actual exchange balance
                                      (rollover/overage credit) is a real,
                                      undesigned feature (roadmap task #77 --
                                      corrected 2026-07-10, was wrongly
                                      citing #76 too, which is the unrelated
                                      forgot-password item) -- creating this
                                      Price now so the Stripe product
                                      catalog is complete and ready whenever
                                      that mechanism is built, not because
                                      checkout for it works today.
  Any annual price left unset falls back to that tier's monthly price, same
  graceful-degrade behavior as the old single-tier version -- a tier can go
  live monthly-only without its annual Price existing yet.
Until STRIPE_SECRET_KEY is set, every route here returns a clean 503 rather
than a confusing crash, so the app can deploy with this code in place before
the keys actually exist.
"""

import calendar
import os
from datetime import date, datetime, timezone

import stripe
from flask import Blueprint, request, jsonify, url_for, session

from pro_auth import login_required, get_user_supabase, get_service_client

pro_billing_bp = Blueprint("pro_billing", __name__, url_prefix="/pro/billing")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# Three tiers x two billing cycles. Prices are immutable in Stripe once
# created -- this is what makes the 2-year price-lock promise mechanically
# real: a future list-price change means creating a new Price object and
# pointing new Checkout Sessions at it, never editing one out from under
# existing subscribers.
TIER_PRICE_IDS = {
    "explore": {
        "monthly": os.environ.get("STRIPE_PRICE_EXPLORE_MONTHLY", ""),
        "annual": os.environ.get("STRIPE_PRICE_EXPLORE_ANNUAL", ""),
    },
    "pursue": {
        "monthly": os.environ.get("STRIPE_PRICE_PURSUE_MONTHLY", ""),
        "annual": os.environ.get("STRIPE_PRICE_PURSUE_ANNUAL", ""),
    },
    "immerse": {
        "monthly": os.environ.get("STRIPE_PRICE_IMMERSE_MONTHLY", ""),
        "annual": os.environ.get("STRIPE_PRICE_IMMERSE_ANNUAL", ""),
    },
}

# Price-ID wiring only, no checkout route yet -- see the module docstring's
# note on STRIPE_PRICE_EXCHANGE_BLOCK above.
STRIPE_PRICE_EXCHANGE_BLOCK = os.environ.get("STRIPE_PRICE_EXCHANGE_BLOCK", "")

# What each tier+cycle actually costs and which TIER_CONVERSATION_CAPS key
# (pro_chat.py) it maps to -- kept here rather than re-derived from Stripe,
# since Stripe's Price objects don't carry our own tier-slug naming.
_TIER_INFO = {
    "explore": {"tier_slug": "individual_explore", "monthly": 17.00, "annual": 170.00},
    "pursue":  {"tier_slug": "individual_pursue",  "monthly": 28.00, "annual": 280.00},
    "immerse": {"tier_slug": "individual_immerse", "monthly": 49.00, "annual": 490.00},
}

# Maps a Price ID back to what we persist on our own side after a
# checkout/renewal -- Stripe's webhook payloads only carry the price ID, not
# a ready-made "$17, monthly, explore" summary, so this is the one place
# that translation lives. Extend TIER_PRICE_IDS/_TIER_INFO above (not this
# loop) if/when a new tier or a Church/Org self-serve Price is added.
PRICE_MAP = {}
for _tier, _cycles in TIER_PRICE_IDS.items():
    for _cycle, _price_id in _cycles.items():
        if _price_id:
            PRICE_MAP[_price_id] = {
                "billing_cycle": _cycle,
                "current_price": _TIER_INFO[_tier][_cycle],
                "tier_slug": _TIER_INFO[_tier]["tier_slug"],
            }

# Superseded 2026-07-10: the free trial now happens entirely BEFORE Stripe
# is ever involved (pro_chat.py starts a homegrown 14-day/25-exchange trial
# on someone's first exchange, no card required -- see TRIAL_EXCHANGE_CAP
# there). Checkout no longer grants a second Stripe-side trial_period_days
# on top of that; conversion is a real charge starting immediately, whether
# someone converts mid-trial (clicked Upgrade directly) or after their
# trial already ended. Left defined (unused) rather than deleted, in case a
# future promo wants a Stripe-side trial again for a specific campaign.
TRIAL_DAYS = 14

# ─────────────────── Church/Org seat billing (2026-07-12) ───────────────────
# Unlike Individual Pro's TIER_PRICE_IDS above (a separate flat Price per
# tier+cycle), Church/Org seats are ONE Stripe Price per seat type, each
# configured in the Stripe dashboard as volume-tiered pricing (billing_scheme
# 'tiered', tiers_mode 'volume') -- Section 11's explicit design: total
# quantity on a single subscription item determines the per-seat rate for
# every seat, not a blended rate, and not a separate Price object per
# bracket. Not yet created in Stripe -- env vars below will be empty until
# that dashboard work happens, same "clean 503, not a crash" pattern as
# TIER_PRICE_IDS above.
CHURCH_SEAT_PRICE_IDS = {
    "leader": os.environ.get("STRIPE_PRICE_CHURCH_LEADERSHIP", ""),
    "member": os.environ.get("STRIPE_PRICE_CHURCH_MEMBER", ""),
}

CHURCH_SEAT_TIER_SLUGS = {
    "leader": "church_leadership",
    "member": "church_member",
}

# Reverse lookup for the webhook -- which seat_type a given Price ID belongs
# to, mirroring PRICE_MAP's role for Individual Pro but keyed differently
# since there's no billing_cycle/current_price to pre-compute (volume-tiered
# price varies by quantity, not a flat number -- current_price gets derived
# from the actual invoice line item at sync time instead, see
# _sync_church_subscription below).
CHURCH_PRICE_TO_SEAT_TYPE = {
    price_id: seat_type
    for seat_type, price_id in CHURCH_SEAT_PRICE_IDS.items()
    if price_id
}

# Membership is not sold on its own (Section 16) -- it's only ever an
# add-on to an org that already has an active Leadership subscription.
# Enforced in create_church_checkout_session below.

# Exchange blocks (2026-07-12, Rick's call): a one-time, one-flat-price
# top-up for whichever pooled seat_type is running low -- NOT a separate
# price per seat type (unlike the seats themselves above); the seat_type
# only determines which pool's usage_records.conversations_cap gets
# credited, not what's charged. Purchasable in quantity (buy 3 blocks =
# 300 extra exchanges) via the Stripe line item's own quantity, not a
# volume-tiered Price -- there's no discount-at-scale intent here the way
# there is for seats. $15/100 exchanges chosen to match the same number
# floated (but never built) for Individual Pro's own overage block --
# healthy margin against the $0.045/exchange assumption Section 17's seat
# pricing was locked against ($0.15/exchange retail vs ~$0.045 cost).
# Genuinely new scope: Section 17's locked Church/Org pricing never
# designed an overage path at all, only flat monthly pooled caps.
STRIPE_PRICE_CHURCH_EXCHANGE_BLOCK = os.environ.get("STRIPE_PRICE_CHURCH_EXCHANGE_BLOCK", "")
CHURCH_EXCHANGES_PER_BLOCK = 100

# Base monthly pooled cap per seat_type, duplicated from pro_chat.py's
# TIER_CONVERSATION_CAPS (church_leadership/church_member) rather than
# imported -- pro_chat.py doesn't import pro_billing.py today and adding
# that edge purely for two integers wasn't worth it. Needed here only for
# the rare case a block is bought before this month's usage_records row
# exists yet (see _apply_church_exchange_block below); keep in sync with
# pro_chat.py's TIER_CONVERSATION_CAPS if either ever changes.
BASE_CHURCH_CAP = {"leader": 200, "member": 100}

# Volume-tier brackets, duplicated from the actual Stripe Price objects
# (price_1TsOtu3UvhMXNeQuBmBBPR1G / price_1TsOu43UvhMXNeQuqLxlrnpf) rather
# than fetched live -- needed here only to tell the admin, before they
# confirm a seat-quantity change, what rate the WHOLE new quantity will
# bill at (Stripe's tiers_mode='volume' means crossing a bracket re-prices
# every seat, not just the new ones -- Rick, 2026-07-13, flagged this as a
# real clarity gap: an admin going from 3 to 5 Leadership seats needs to see
# that all 5 move to the $12 bracket, not just "here's today's charge").
# Each tuple is (upper bound inclusive, price/seat); last entry's bound is
# None for "and up". Keep in sync with Stripe if either price ever changes.
CHURCH_SEAT_TIERS = {
    "leader": [(4, 14.00), (9, 12.00), (None, 10.00)],
    "member": [(24, 8.00), (99, 7.50), (999, 7.00), (None, 6.50)],
}


def _price_per_seat(seat_type: str, quantity: int) -> float:
    for upper, price in CHURCH_SEAT_TIERS[seat_type]:
        if upper is None or quantity <= upper:
            return price
    return CHURCH_SEAT_TIERS[seat_type][-1][1]


def _get_org_id_and_email():
    """Shared lookup -- every route here needs the caller's own
    organization_id (subscriptions/stripe_customer_id are stored per-org,
    matching the multi-seat shape the rest of the schema already uses, even
    though Individual Pro today is always a 1-profile org)."""
    sb = get_user_supabase()
    profile_resp = sb.table("profiles").select("organization_id, email").limit(1).execute()
    if not profile_resp.data:
        return None, None
    return profile_resp.data[0]["organization_id"], profile_resp.data[0]["email"]


def _get_org_id_email_and_admin_status():
    """Same as _get_org_id_and_email(), plus is_org_admin -- every
    Church/Org seat-billing route below is an admin-only action (buying
    seats, changing quantity spends the church's money and changes real
    people's access), never something any seat-holder can trigger."""
    sb = get_user_supabase()
    profile_resp = (
        sb.table("profiles")
        .select("organization_id, email, is_org_admin")
        .limit(1)
        .execute()
    )
    if not profile_resp.data:
        return None, None, False
    row = profile_resp.data[0]
    return row["organization_id"], row["email"], bool(row.get("is_org_admin"))


@pro_billing_bp.route("/checkout", methods=["POST"])
@login_required
def create_checkout_session():
    """Starts a Stripe Checkout session for one of the three Individual Pro
    tiers. Returns a URL for the frontend to redirect to (Stripe's hosted
    page) rather than redirecting server-side, since this is called via
    fetch() -- no card data ever touches this server, so there's no PCI
    burden here at all.

    body: {"tier": "explore" | "pursue" | "immerse", "plan": "monthly" |
    "annual"} -- tier is required, no default: guessing the wrong tier means
    charging the wrong price, so an invalid/missing tier is a hard 400, not
    a silent fallback. plan defaults to "monthly" and silently falls back to
    that tier's monthly price if the annual Price isn't configured yet, so a
    tier can launch monthly-only."""
    if not stripe.api_key:
        return jsonify({"error": "Billing isn't set up yet -- check back soon."}), 503

    body = request.json or {}
    tier = body.get("tier", "")
    plan = body.get("plan", "monthly")

    if tier not in TIER_PRICE_IDS:
        return jsonify({"error": f"Unknown plan tier: {tier!r}"}), 400

    price_id = (
        TIER_PRICE_IDS[tier].get("annual")
        if plan == "annual" and TIER_PRICE_IDS[tier].get("annual")
        else TIER_PRICE_IDS[tier].get("monthly")
    )
    if not price_id:
        return jsonify({"error": "No Stripe price is configured for this plan yet."}), 503

    organization_id, email = _get_org_id_and_email()
    if not organization_id:
        return jsonify({"error": "no profile found for this account"}), 400

    # Reuse an existing Stripe Customer if this org already has one (e.g. a
    # lapsed/canceled subscriber resubscribing) instead of minting a
    # duplicate -- keeps their payment history and invoices in one place in
    # the Stripe dashboard rather than scattered across customer records.
    svc = get_service_client()
    existing = (
        svc.table("subscriptions")
        .select("stripe_customer_id")
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )
    existing_customer_id = (
        existing.data[0]["stripe_customer_id"]
        if existing.data and existing.data[0].get("stripe_customer_id")
        else None
    )

    checkout_kwargs = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": url_for("pro_chat.pro_app", _external=True) + "?checkout=success",
        "cancel_url": url_for("pro.pro_home", _external=True) + "?checkout=cancelled",
        "client_reference_id": organization_id,
        "metadata": {"organization_id": organization_id},
        "subscription_data": {
            "metadata": {"organization_id": organization_id},
        },
    }
    if existing_customer_id:
        checkout_kwargs["customer"] = existing_customer_id
    elif email:
        checkout_kwargs["customer_email"] = email

    try:
        checkout_session = stripe.checkout.Session.create(**checkout_kwargs)
    except Exception as e:
        return jsonify({"error": f"Could not start checkout: {e}"}), 502

    return jsonify({"url": checkout_session.url})


@pro_billing_bp.route("/portal", methods=["POST"])
@login_required
def create_portal_session():
    """Redirects to Stripe's hosted Customer Portal -- self-serve cancel,
    card update, and invoice history, none of which this app builds itself."""
    if not stripe.api_key:
        return jsonify({"error": "Billing isn't set up yet -- check back soon."}), 503

    organization_id, _ = _get_org_id_and_email()
    if not organization_id:
        return jsonify({"error": "no profile found for this account"}), 400

    svc = get_service_client()
    sub_resp = (
        svc.table("subscriptions")
        .select("stripe_customer_id")
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )
    customer_id = sub_resp.data[0]["stripe_customer_id"] if sub_resp.data else None
    if not customer_id:
        return jsonify({"error": "No billing account yet -- start a subscription first."}), 400

    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=url_for("pro_chat.pro_app", _external=True),
        )
    except Exception as e:
        return jsonify({"error": f"Could not open the billing portal: {e}"}), 502

    return jsonify({"url": portal_session.url})


@pro_billing_bp.route("/status", methods=["GET"])
@login_required
def billing_status():
    """Lightweight read for the frontend to decide whether to show
    'Upgrade' or 'Manage Billing' -- no Stripe API call, just our own
    already-synced subscriptions row. Returns a free/active default if no
    subscriptions row exists at all (shouldn't happen post-signup, since
    handle_new_user() always creates one, but this keeps the endpoint
    honest rather than erroring if that ever changes)."""
    organization_id, _ = _get_org_id_and_email()
    if not organization_id:
        return jsonify({"error": "no profile found for this account"}), 400

    sb = get_user_supabase()
    sub_resp = (
        sb.table("subscriptions")
        .select("tier_slug, status, billing_cycle, current_price, current_period_end, cancel_at_period_end, trial_end, price_lock_expires_at")
        .eq("organization_id", organization_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not sub_resp.data:
        return jsonify({"tier_slug": "free", "status": "active"})

    result = dict(sub_resp.data[0])

    # Exchanges used this billing month -- exposed so the frontend can
    # decide whether to open the plan-choice modal on page load itself
    # (Rick's "door 3": a new session with no credits left) without having
    # to send a throwaway chat message just to find out. Same usage_records
    # lookup pro_chat.py's _check_and_reserve_usage uses, read-only here.
    usage_resp = (
        sb.table("usage_records")
        .select("conversations_used, conversations_cap")
        .eq("organization_id", organization_id)
        .eq("billing_month", date.today().replace(day=1).isoformat())
        .is_("module_slug", "null")
        .limit(1)
        .execute()
    )
    if usage_resp.data:
        result["exchanges_used"] = usage_resp.data[0]["conversations_used"]
        result["exchanges_cap"] = usage_resp.data[0]["conversations_cap"]
    else:
        result["exchanges_used"] = 0
        result["exchanges_cap"] = None

    return jsonify(result)


# ────────────────── Church/Org seat checkout & management ──────────────────

MAX_ORG_ADMINS = 4  # Rick's call, 2026-07-12: "multi-admin is vital... minimum of 3-4."


@pro_billing_bp.route("/church/start", methods=["POST"])
@login_required
def start_church_org():
    """Bootstraps a brand-new church org for the CALLING profile -- solves
    a real chicken-and-egg gap: every signup lands on their own
    org_type='individual' org with is_org_admin=false by default
    (handle_new_user()), but create_church_checkout_session above requires
    is_org_admin=true. Something has to make the very first admin. Flips
    the caller's own org to org_type='church' and sets themselves as its
    first admin (of up to MAX_ORG_ADMINS) -- after this, /church/checkout
    works normally. Refuses if this org already has any Leadership/
    Membership subscription (already a church org, calling this again would
    be a no-op at best, confusing at worst) or if the caller already has
    other people in their org (shouldn't happen for a solo individual org,
    but don't silently reorganize an org that somehow isn't solo).

    body: {"org_name": str (optional), "postal_code": str (required)}.
    postal_code is required at this layer (Rick's call, 2026-07-12) -- the
    real-world problem it solves is "First Baptist Church" existing in
    hundreds of towns, with nothing on the organizations row to tell them
    apart in Stripe, support conversations, or this same admin's own
    dashboard. org_name lets the founder replace the default (their own
    email, set at signup) with the church's actual name at the same moment
    -- optional since a solo admin might not have the final name decided
    yet and can always be revisited later; postal_code has no such
    "decide later" grace since disambiguation matters from the first
    purchase onward."""
    body = request.json or {}
    postal_code = (body.get("postal_code") or "").strip()
    org_name = (body.get("org_name") or "").strip() or None
    if not postal_code:
        return jsonify({"error": "postal_code is required -- it's what tells your organization apart from every other church with the same name."}), 400

    organization_id, _email, _is_admin = _get_org_id_email_and_admin_status()
    if not organization_id:
        return jsonify({"error": "no profile found for this account"}), 400

    svc = get_service_client()

    org_check = svc.table("organizations").select("org_type").eq("id", organization_id).limit(1).execute()
    if org_check.data and org_check.data[0]["org_type"] == "church":
        return jsonify({"error": "This is already a church organization."}), 400

    roster = svc.table("profiles").select("id").eq("organization_id", organization_id).execute()
    if len(roster.data or []) > 1:
        return jsonify({"error": "This organization isn't a solo account -- contact Selah admin directly to set up Church/Org."}), 400

    org_update = {"org_type": "church", "postal_code": postal_code}
    if org_name:
        org_update["name"] = org_name
    svc.table("organizations").update(org_update).eq("id", organization_id).execute()
    svc.table("profiles").update({"is_org_admin": True, "seat_type": "leader", "seat_status": "comped"}).eq("id", session["sb_user_id"]).execute()
    # seat_status='comped' (not 'paid') for the founding admin's own seat --
    # they haven't bought a Leadership seat block yet at this exact moment
    # (that's the /church/checkout call right after this), so 'paid' would
    # be a lie about money that hasn't moved; comped is the accurate status
    # until their own org's first real Leadership purchase includes them in
    # its seat count. Matches the existing comped-seat precedent (Rick,
    # Clark) rather than inventing a new status for this one moment.

    return jsonify({"ok": True, "note": "This is now a church organization. Purchase Leadership seats to activate it."})


@pro_billing_bp.route("/church/checkout", methods=["POST"])
@login_required
def create_church_checkout_session():
    """Starts Checkout for a NEW Leadership or Membership seat subscription
    -- the org's first purchase of that seat type. Admin-only (buying seats
    spends the church's money). body: {"seat_type": "leader"|"member",
    "quantity": int} -- quantity is required with no default, same
    no-silent-guessing principle as Individual Pro's required "tier".
    Membership can't be purchased standalone (Section 16: it's opened BY a
    Leadership purchase, never sold on its own) -- checked against the org's
    existing subscriptions before allowing it."""
    if not stripe.api_key:
        return jsonify({"error": "Billing isn't set up yet -- check back soon."}), 503

    body = request.json or {}
    seat_type = body.get("seat_type", "")
    quantity = body.get("quantity")

    if seat_type not in CHURCH_SEAT_PRICE_IDS:
        return jsonify({"error": f"Unknown seat type: {seat_type!r}"}), 400
    if not isinstance(quantity, int) or quantity < 1:
        return jsonify({"error": "quantity must be a positive integer"}), 400
    if seat_type == "member" and quantity < 10:
        return jsonify({"error": "Membership seats have a 10-seat minimum (it's a volume discount -- Section 11)."}), 400

    price_id = CHURCH_SEAT_PRICE_IDS[seat_type]
    if not price_id:
        return jsonify({"error": "No Stripe price is configured for this seat type yet."}), 503

    organization_id, email, is_admin = _get_org_id_email_and_admin_status()
    if not organization_id:
        return jsonify({"error": "no profile found for this account"}), 400
    if not is_admin:
        return jsonify({"error": "Only an organization admin can purchase seats."}), 403

    svc = get_service_client()

    if seat_type == "member":
        leadership = (
            svc.table("subscriptions")
            .select("id")
            .eq("organization_id", organization_id)
            .eq("tier_slug", "church_leadership")
            .in_("status", ["active", "trialing"])
            .limit(1)
            .execute()
        )
        if not leadership.data:
            return jsonify({"error": "Membership seats require an active Leadership subscription first (Section 16 -- Membership is opened by Leadership, not sold on its own)."}), 400

    existing = (
        svc.table("subscriptions")
        .select("stripe_customer_id")
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )
    existing_customer_id = (
        existing.data[0]["stripe_customer_id"]
        if existing.data and existing.data[0].get("stripe_customer_id")
        else None
    )

    checkout_kwargs = {
        "mode": "subscription",
        # Church/Org checkout lands the admin back on the church dashboard
        # (not the plain chat app, unlike Individual Pro's checkout above) --
        # they came from there to buy seats, and that's exactly where
        # they'd next want to see the purchase reflected once the webhook
        # confirms it.
        "success_url": url_for("pro_org.org_dashboard", _external=True) + "?checkout=success",
        "cancel_url": url_for("pro_org.org_dashboard", _external=True) + "?checkout=cancelled",
        "line_items": [{"price": price_id, "quantity": quantity}],
        "client_reference_id": organization_id,
        "metadata": {"organization_id": organization_id, "seat_type": seat_type},
        "subscription_data": {
            "metadata": {"organization_id": organization_id, "seat_type": seat_type},
        },
    }
    if existing_customer_id:
        checkout_kwargs["customer"] = existing_customer_id
    elif email:
        checkout_kwargs["customer_email"] = email

    try:
        checkout_session = stripe.checkout.Session.create(**checkout_kwargs)
    except Exception as e:
        return jsonify({"error": f"Could not start checkout: {e}"}), 502

    return jsonify({"url": checkout_session.url})


@pro_billing_bp.route("/church/blocks/checkout", methods=["POST"])
@login_required
def create_church_block_checkout_session():
    """Starts a ONE-TIME (mode='payment', not 'subscription') Checkout
    session for exchange blocks -- a top-up for whichever pooled seat_type
    is running low this month. Admin-only, same reasoning as seat purchases:
    spends the church's money. body: {"seat_type": "leader"|"member",
    "quantity": int} -- quantity here is the number of BLOCKS (each worth
    CHURCH_EXCHANGES_PER_BLOCK exchanges), not a seat count; the Stripe line
    item's own quantity is what lets someone buy more than one block in a
    single purchase, per Rick's call.

    Actual crediting of usage_records.conversations_cap does NOT happen
    here -- same pattern as seat purchases, only on the webhook's
    checkout.session.completed once payment is actually confirmed (see
    _apply_church_exchange_block below). This route only starts the
    payment."""
    if not stripe.api_key:
        return jsonify({"error": "Billing isn't set up yet -- check back soon."}), 503
    if not STRIPE_PRICE_CHURCH_EXCHANGE_BLOCK:
        return jsonify({"error": "Exchange blocks aren't configured yet -- check back soon."}), 503

    body = request.json or {}
    seat_type = body.get("seat_type", "")
    quantity = body.get("quantity")

    if seat_type not in CHURCH_SEAT_TIER_SLUGS:
        return jsonify({"error": f"Unknown seat type: {seat_type!r}"}), 400
    if not isinstance(quantity, int) or quantity < 1:
        return jsonify({"error": "quantity must be a positive integer (number of blocks)"}), 400

    organization_id, email, is_admin = _get_org_id_email_and_admin_status()
    if not organization_id:
        return jsonify({"error": "no profile found for this account"}), 400
    if not is_admin:
        return jsonify({"error": "Only an organization admin can purchase exchange blocks."}), 403

    svc = get_service_client()
    existing = (
        svc.table("subscriptions")
        .select("stripe_customer_id")
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )
    existing_customer_id = (
        existing.data[0]["stripe_customer_id"]
        if existing.data and existing.data[0].get("stripe_customer_id")
        else None
    )

    checkout_kwargs = {
        "mode": "payment",
        "success_url": url_for("pro_org.org_dashboard", _external=True) + "?checkout=block_success",
        "cancel_url": url_for("pro_org.org_dashboard", _external=True) + "?checkout=block_cancelled",
        "line_items": [{"price": STRIPE_PRICE_CHURCH_EXCHANGE_BLOCK, "quantity": quantity}],
        "client_reference_id": organization_id,
        # No subscription_data here at all -- mode='payment' sessions don't
        # have one. metadata on the SESSION itself is what the webhook reads
        # (see stripe_webhook's purchase_type branch), unlike the seat
        # routes above where subscription_data.metadata matters more since
        # that's what persists onto the resulting Subscription object.
        "metadata": {
            "organization_id": organization_id,
            "seat_type": seat_type,
            "purchase_type": "church_exchange_block",
            "block_quantity": str(quantity),
        },
    }
    if existing_customer_id:
        checkout_kwargs["customer"] = existing_customer_id
    elif email:
        checkout_kwargs["customer_email"] = email

    try:
        checkout_session = stripe.checkout.Session.create(**checkout_kwargs)
    except Exception as e:
        return jsonify({"error": f"Could not start checkout: {e}"}), 502

    return jsonify({"url": checkout_session.url})


def promote_waitlisted_if_room(organization_id: str, seat_type: str) -> int:
    """Promotes as many waitlisted (seat_status='pending') profiles of the
    given seat_type as there's now room for, oldest-waitlisted first (a
    real fairness call, not arbitrary -- first-come-first-served for who
    gets the freed/new seat). Called from two places a seat pool's
    occupied-vs-purchased balance can shift: pro_org.py's roster removal
    (frees one seat immediately), and _sync_church_subscription_row below
    (a seat-quantity increase actually CONFIRMED paid via the Stripe
    webhook -- deliberately not called from the /seats/update request
    itself, which only starts a proration charge that could still fail).

    Returns how many profiles were promoted (used by callers purely for
    logging/testing, not branched on). Uses the service-role client --
    seat_status is real access/billing-adjacent state, never written
    through a user's own RLS-scoped token."""
    svc = get_service_client()
    tier_slug = CHURCH_SEAT_TIER_SLUGS.get(seat_type)
    if not tier_slug:
        return 0

    sub = (
        svc.table("subscriptions")
        .select("seat_quantity")
        .eq("organization_id", organization_id)
        .eq("tier_slug", tier_slug)
        .in_("status", ["active", "trialing"])
        .limit(1)
        .execute()
    )
    purchased = sub.data[0].get("seat_quantity") if sub.data else None
    if not purchased:
        return 0

    occupied_resp = (
        svc.table("profiles")
        .select("id")
        .eq("organization_id", organization_id)
        .eq("seat_type", seat_type)
        .in_("seat_status", ["paid", "comped"])
        .execute()
    )
    open_slots = purchased - len(occupied_resp.data or [])
    if open_slots <= 0:
        return 0

    waiting = (
        svc.table("profiles")
        .select("id, waitlisted_at")
        .eq("organization_id", organization_id)
        .eq("seat_type", seat_type)
        .eq("seat_status", "pending")
        .order("waitlisted_at")
        .limit(open_slots)
        .execute()
    )
    for row in (waiting.data or []):
        svc.table("profiles").update({
            "seat_status": "paid",
            "waitlisted_at": None,
        }).eq("id", row["id"]).execute()

    return len(waiting.data or [])


def _get_church_stripe_subscription(organization_id, seat_type):
    """Shared lookup for the preview/update routes below -- the existing
    Stripe Subscription object for this org's seat type, or None."""
    svc = get_service_client()
    row = (
        svc.table("subscriptions")
        .select("stripe_subscription_id")
        .eq("organization_id", organization_id)
        .eq("tier_slug", CHURCH_SEAT_TIER_SLUGS[seat_type])
        .in_("status", ["active", "trialing"])
        .limit(1)
        .execute()
    )
    if not row.data or not row.data[0].get("stripe_subscription_id"):
        return None
    return stripe.Subscription.retrieve(row.data[0]["stripe_subscription_id"])


@pro_billing_bp.route("/church/seats/preview", methods=["POST"])
@login_required
def preview_seat_change():
    """Non-destructive preview of what a seat-quantity increase would cost
    RIGHT NOW via Stripe's upcoming-invoice preview -- Section 11's explicit
    requirement: the church admin sees the prorated charge before confirming
    anything, since the actual update route bills immediately
    (create_prorations), not at the next cycle. body: {"seat_type":
    "leader"|"member", "new_quantity": int}."""
    if not stripe.api_key:
        return jsonify({"error": "Billing isn't set up yet -- check back soon."}), 503

    body = request.json or {}
    seat_type = body.get("seat_type", "")
    new_quantity = body.get("new_quantity")

    if seat_type not in CHURCH_SEAT_PRICE_IDS:
        return jsonify({"error": f"Unknown seat type: {seat_type!r}"}), 400
    if not isinstance(new_quantity, int) or new_quantity < 1:
        return jsonify({"error": "new_quantity must be a positive integer"}), 400

    organization_id, _email, is_admin = _get_org_id_email_and_admin_status()
    if not organization_id:
        return jsonify({"error": "no profile found for this account"}), 400
    if not is_admin:
        return jsonify({"error": "Only an organization admin can change seat counts."}), 403

    sub = _get_church_stripe_subscription(organization_id, seat_type)
    if not sub:
        return jsonify({"error": f"No active {seat_type} subscription found -- use /church/checkout for a first purchase."}), 400

    item = sub["items"]["data"][0]
    try:
        upcoming = stripe.Invoice.upcoming(
            customer=sub["customer"],
            subscription=sub["id"],
            subscription_items=[{"id": item["id"], "quantity": new_quantity}],
            subscription_proration_behavior="create_prorations",
        )
    except Exception as e:
        return jsonify({"error": f"Could not preview the change: {e}"}), 502

    current_quantity = item["quantity"]
    current_price_per_seat = _price_per_seat(seat_type, current_quantity)
    new_price_per_seat = _price_per_seat(seat_type, new_quantity)

    return jsonify({
        "current_quantity": current_quantity,
        "new_quantity": new_quantity,
        "current_price_per_seat": current_price_per_seat,
        "new_price_per_seat": new_price_per_seat,
        "new_monthly_total": round(new_price_per_seat * new_quantity, 2),
        "prorated_amount_due_now": upcoming["amount_due"] / 100.0,
        "currency": upcoming["currency"],
    })


@pro_billing_bp.route("/church/seats/update", methods=["POST"])
@login_required
def update_seat_quantity():
    """Actually applies a seat-quantity change, billing the proration
    immediately (Section 11's reversed decision: seats are usable right
    away, so deferring their cost to the next cycle would eat a full
    cycle of usage cost with zero matching revenue). Real seat POOL access
    still only increases once the invoice.paid webhook confirms the
    prorated charge actually succeeded (see _sync_church_subscription) --
    this route just starts that; it doesn't grant access itself. body:
    {"seat_type": "leader"|"member", "new_quantity": int}."""
    if not stripe.api_key:
        return jsonify({"error": "Billing isn't set up yet -- check back soon."}), 503

    body = request.json or {}
    seat_type = body.get("seat_type", "")
    new_quantity = body.get("new_quantity")

    if seat_type not in CHURCH_SEAT_PRICE_IDS:
        return jsonify({"error": f"Unknown seat type: {seat_type!r}"}), 400
    if not isinstance(new_quantity, int) or new_quantity < 1:
        return jsonify({"error": "new_quantity must be a positive integer"}), 400
    if seat_type == "member" and new_quantity < 10:
        return jsonify({"error": "Membership seats have a 10-seat minimum."}), 400

    organization_id, _email, is_admin = _get_org_id_email_and_admin_status()
    if not organization_id:
        return jsonify({"error": "no profile found for this account"}), 400
    if not is_admin:
        return jsonify({"error": "Only an organization admin can change seat counts."}), 403

    sub = _get_church_stripe_subscription(organization_id, seat_type)
    if not sub:
        return jsonify({"error": f"No active {seat_type} subscription found -- use /church/checkout for a first purchase."}), 400

    item = sub["items"]["data"][0]
    if new_quantity < item["quantity"]:
        # Decreasing seat count (e.g. downsizing) is real functionality but
        # genuinely different (what happens to the now-excess occupied
        # seats? who gets removed?) -- not designed yet, deliberately out
        # of scope for today. Increases only for now.
        return jsonify({"error": "Reducing seat count isn't supported yet -- contact Selah admin directly for now."}), 400

    try:
        stripe.Subscription.modify(
            sub["id"],
            items=[{"id": item["id"], "quantity": new_quantity}],
            proration_behavior="create_prorations",
        )
        stripe.Invoice.create(customer=sub["customer"], subscription=sub["id"])
        # Immediate invoicing per Section 11's decision -- create_prorations
        # alone schedules the proration as a line item on the NEXT invoice,
        # not a standalone one billed now. Explicitly creating+finalizing
        # (via auto_advance default) a real invoice right after the quantity
        # change is what actually charges it immediately rather than
        # silently deferring to next cycle despite the proration_behavior
        # setting -- easy to get wrong, flagging the reasoning here.
    except Exception as e:
        return jsonify({"error": f"Could not update seat count: {e}"}), 502

    return jsonify({
        "ok": True,
        "note": "Seat purchase is processing -- your organization's available seats will update automatically once payment is confirmed.",
    })


def _apply_church_exchange_block(organization_id: str, metadata: dict) -> None:
    """Credits a confirmed exchange-block purchase onto the CURRENT
    calendar month's pooled usage_records row for (organization_id,
    seat_type) -- a block is a this-month top-up, not a permanent balance
    increase, matching how the pooled cap itself already resets every
    billing_month (no rollover mechanism exists yet -- roadmap task #77,
    a separate, still-undesigned piece).

    Called only from stripe_webhook() on a confirmed checkout.session.completed
    for a mode='payment' session carrying purchase_type='church_exchange_block'
    metadata -- never from the /blocks/checkout route itself, same
    "only credit on confirmed payment, not on starting checkout" pattern
    seat purchases already follow.

    Row creation (the rare case someone buys a block before this month's
    usage_records row exists at all -- normally it already exists, since
    you'd only be buying a block because you're already near/at this
    month's cap) uses BASE_CHURCH_CAP as the starting point, same
    check-then-insert-tolerate-duplicate-key pattern pro_chat.py's
    _check_and_reserve_usage() already uses for the same table."""
    seat_type = metadata.get("seat_type")
    block_quantity_raw = metadata.get("block_quantity")
    if not organization_id or seat_type not in BASE_CHURCH_CAP or not block_quantity_raw:
        print(f"[STRIPE WEBHOOK] church exchange block purchase missing org/seat_type/block_quantity metadata -- org={organization_id!r} meta={metadata!r}, skipping credit")
        return
    try:
        block_quantity = int(block_quantity_raw)
    except (TypeError, ValueError):
        print(f"[STRIPE WEBHOOK] church exchange block purchase had a non-integer block_quantity {block_quantity_raw!r}, skipping credit")
        return

    exchanges_to_add = block_quantity * CHURCH_EXCHANGES_PER_BLOCK
    billing_month = date.today().replace(day=1).isoformat()
    svc = get_service_client()

    existing = (
        svc.table("usage_records")
        .select("id, conversations_cap")
        .eq("organization_id", organization_id)
        .eq("billing_month", billing_month)
        .is_("module_slug", "null")
        .eq("seat_type", seat_type)
        .limit(1)
        .execute()
    )
    if existing.data:
        row = existing.data[0]
        new_cap = (row.get("conversations_cap") or 0) + exchanges_to_add
        svc.table("usage_records").update({"conversations_cap": new_cap}).eq("id", row["id"]).execute()
    else:
        try:
            svc.table("usage_records").insert({
                "organization_id": organization_id,
                "module_slug": None,
                "seat_type": seat_type,
                "billing_month": billing_month,
                "conversations_used": 0,
                "conversations_cap": BASE_CHURCH_CAP[seat_type] + exchanges_to_add,
                "hard_cap": True,
            }).execute()
        except Exception:
            # Lost the create race (e.g. a chat turn landed the same
            # instant and created the row first via the normal cap-check
            # path) -- re-fetch and update instead of losing the credit.
            retry = (
                svc.table("usage_records")
                .select("id, conversations_cap")
                .eq("organization_id", organization_id)
                .eq("billing_month", billing_month)
                .is_("module_slug", "null")
                .eq("seat_type", seat_type)
                .limit(1)
                .execute()
            )
            if retry.data:
                row = retry.data[0]
                new_cap = (row.get("conversations_cap") or 0) + exchanges_to_add
                svc.table("usage_records").update({"conversations_cap": new_cap}).eq("id", row["id"]).execute()


# ─────────────────────────── Webhook ───────────────────────────

# Maps Stripe's own subscription.status values to our subscriptions.status
# CHECK constraint ('active','trialing','past_due','canceled'). Stripe has
# a few statuses ours doesn't distinguish (unpaid, incomplete, paused) --
# each folds into the closest of our four rather than adding new allowed
# values to the DB constraint for edge cases that behave the same way from
# this app's point of view (usage should be gated the same as past_due).
_STRIPE_STATUS_MAP = {
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "canceled": "canceled",
    "unpaid": "past_due",
    "incomplete": "past_due",
    "incomplete_expired": "canceled",
    "paused": "past_due",
}


def _epoch_to_iso(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


PRICE_LOCK_MONTHS = 24


def _add_months(dt: datetime, months: int) -> datetime:
    """Calendar-aware month addition (timedelta(days=730) drifts against
    real 24-month periods across leap years). Clamps to the last real day
    of the target month for edge cases like Jan 31 + 1 month."""
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _sync_church_subscription_row(subscription, organization_id, seat_type):
    """Church/Org counterpart to _sync_subscription_row below -- kept
    separate rather than folded into one branching function because the
    shapes genuinely differ: an org can hold TWO simultaneous subscriptions
    (church_leadership AND church_member), so matching by organization_id
    alone (as the individual-tier function does, via .limit(1)) would
    silently find/overwrite the WRONG one. Matches on (organization_id,
    tier_slug) instead. No price-lock logic here -- that 24-month promise
    was specifically an Individual Pro commitment (Section 14), never
    extended to seats. billing_cycle/current_price intentionally left null
    for now -- Stripe's volume-tiered pricing means the real per-seat rate
    varies by quantity bracket, not a flat number the way Individual Pro's
    PRICE_MAP already has one; computing an accurate effective rate needs
    the invoice line item, not just the subscription object, and is real
    follow-up work, not done in this pass."""
    svc = get_service_client()
    tier_slug = CHURCH_SEAT_TIER_SLUGS[seat_type]

    items = (subscription.get("items") or {}).get("data") or []
    seat_quantity = items[0]["quantity"] if items else None

    stripe_status = subscription.get("status", "active")
    our_status = _STRIPE_STATUS_MAP.get(stripe_status, "active")

    update_fields = {
        "tier_slug": tier_slug,
        "status": our_status,
        "stripe_customer_id": subscription.get("customer"),
        "stripe_subscription_id": subscription.get("id"),
        "cancel_at_period_end": subscription.get("cancel_at_period_end", False),
        "seat_quantity": seat_quantity,
    }
    if subscription.get("current_period_end"):
        update_fields["current_period_end"] = _epoch_to_iso(subscription["current_period_end"])

    existing = (
        svc.table("subscriptions")
        .select("id")
        .eq("organization_id", organization_id)
        .eq("tier_slug", tier_slug)
        .limit(1)
        .execute()
    )
    if existing.data:
        svc.table("subscriptions").update(update_fields).eq("id", existing.data[0]["id"]).execute()
    else:
        update_fields["organization_id"] = organization_id
        svc.table("subscriptions").insert(update_fields).execute()

    # A seat-quantity increase just got CONFIRMED (this is a webhook firing
    # off a real Stripe event, not the /seats/update request itself, which
    # only starts a proration charge that could still fail) -- check
    # whether that frees up room for anyone on this seat_type's waitlist.
    # Harmless no-op if seat_quantity didn't grow or nobody's waiting.
    if seat_quantity:
        promote_waitlisted_if_room(organization_id, seat_type)


def _sync_subscription_row(subscription, organization_id=None):
    """Upserts our subscriptions row from a Stripe Subscription object --
    the single source of truth this pulls from on every relevant webhook
    event, rather than trusting any one event type to carry everything.
    Uses the service-role client since webhooks have no logged-in user
    session to scope an RLS-aware client to."""
    svc = get_service_client()

    organization_id = organization_id or (subscription.get("metadata") or {}).get("organization_id")
    if not organization_id:
        # Nothing to key off of -- log and bail rather than guessing. Should
        # only happen if a subscription was created outside this app's own
        # Checkout flow (e.g. manually in the Stripe dashboard) without the
        # organization_id metadata this code always sets on creation.
        print(f"[STRIPE WEBHOOK] subscription {subscription.get('id')} has no organization_id metadata, skipping sync")
        return

    # Church/Org seats branch off here entirely -- see
    # _sync_church_subscription_row's docstring for why this can't share the
    # rest of this function's organization_id-only matching logic.
    items_preview = (subscription.get("items") or {}).get("data") or []
    price_id_preview = items_preview[0]["price"]["id"] if items_preview else None
    if price_id_preview in CHURCH_PRICE_TO_SEAT_TYPE:
        _sync_church_subscription_row(subscription, organization_id, CHURCH_PRICE_TO_SEAT_TYPE[price_id_preview])
        return

    # Looked up before building update_fields, not after, so the
    # unknown-price fallback below can tell a brand-new row from an
    # existing one (see comment there). Matching by organization_id alone
    # is still correct here (not the bug it looks like) -- an org's
    # org_type is either 'individual' or 'church', never both, so an
    # individual-tier org can never simultaneously hold a church_leadership/
    # church_member row for this .limit(1) to collide with. Church/Org rows
    # are already routed to _sync_church_subscription_row above and never
    # reach this line.
    existing = (
        svc.table("subscriptions")
        .select("id, price_lock_expires_at")
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )
    is_new_row = not existing.data
    # NOT the same thing as is_new_row (see below): since handle_new_user()
    # gives every signup a subscriptions row on day one ('trial'/'active'),
    # every real conversion is an UPDATE to an already-existing row, not an
    # INSERT -- is_new_row is False for essentially every real subscriber.
    # price_lock_expires_at IS NULL is the actual "has this org ever had a
    # real paid price before" signal.
    needs_price_lock = is_new_row or (existing.data and existing.data[0].get("price_lock_expires_at") is None)

    items = (subscription.get("items") or {}).get("data") or []
    price_id = items[0]["price"]["id"] if items else None
    price_info = PRICE_MAP.get(price_id, {})

    stripe_status = subscription.get("status", "active")
    our_status = _STRIPE_STATUS_MAP.get(stripe_status, "active")

    update_fields = {
        "status": our_status,
        "stripe_customer_id": subscription.get("customer"),
        "stripe_subscription_id": subscription.get("id"),
        "cancel_at_period_end": subscription.get("cancel_at_period_end", False),
    }
    if needs_price_lock and price_info:
        # Anchor the 24-month price-lock promise to the moment this org
        # FIRST gets a real paid price -- set once, here, and never touched
        # again by any later renewal/upgrade/downgrade sync. Gated on
        # needs_price_lock (price_lock_expires_at IS NULL), not is_new_row:
        # every signup already has a subscriptions row from handle_new_user
        # ('trial'/'active'), so a real conversion is always an UPDATE, not
        # an INSERT -- is_new_row alone would never fire this. Also gated on
        # price_info being known -- never anchor a lock date to a guessed
        # tier_slug (see the unrecognized-price branches below). Note:
        # customer.subscription.deleted (below) sets tier_slug='free' but
        # leaves price_lock_expires_at as-is, so a later resubscribe still
        # has it non-NULL and correctly skips re-anchoring -- the original
        # first-subscribe date sticks even across a cancel/resubscribe
        # cycle. That's a real policy choice, not an oversight: revisit if
        # the business instead wants a lapsed-then-returning subscriber's
        # lock clock to restart.
        started_at = (
            datetime.fromtimestamp(subscription["created"], tz=timezone.utc)
            if subscription.get("created")
            else datetime.now(timezone.utc)
        )
        update_fields["price_lock_expires_at"] = _add_months(started_at, PRICE_LOCK_MONTHS).isoformat()
    if price_info:
        # Known price -- set the real tier and billing details.
        update_fields["tier_slug"] = price_info["tier_slug"]
        update_fields["billing_cycle"] = price_info["billing_cycle"]
        update_fields["current_price"] = price_info["current_price"]
    elif is_new_row:
        # Unrecognized price_id with NO subscriptions row at all -- rare
        # now that handle_new_user() always creates one at signup (would
        # only happen if that trigger somehow failed). Most likely cause of
        # the unrecognized price itself: a subscription created directly in
        # the Stripe dashboard, or a STRIPE_PRICE_* env var that's missing/
        # wrong. The NOT NULL tier_slug column still needs *something*, so
        # this defaults to the cheapest real tier as the safer of two wrong
        # guesses (under-provisioning, not over-), and logs loudly so it
        # gets caught rather than silently mis-billed.
        print(f"[STRIPE WEBHOOK] price {price_id!r} not in PRICE_MAP -- check STRIPE_PRICE_* env vars (new row, defaulted to individual_explore)")
        update_fields["tier_slug"] = "individual_explore"
    else:
        # Unrecognized price on an EXISTING row (the common case -- every
        # org has one from signup) -- leave tier_slug out of update_fields
        # entirely rather than guess. If this row's prior tier_slug was a
        # real paid tier, that's obviously right; if it was still a
        # placeholder ('trial'/'free'/'beta'), leaving it unchanged is
        # still the safer of the two options -- better a stuck placeholder
        # that gets caught by the loud log line than a silently wrong paid
        # tier assignment.
        print(f"[STRIPE WEBHOOK] price {price_id!r} not in PRICE_MAP -- check STRIPE_PRICE_* env vars (existing row, tier_slug left unchanged)")
    if subscription.get("current_period_end"):
        update_fields["current_period_end"] = _epoch_to_iso(subscription["current_period_end"])
    if subscription.get("trial_end"):
        update_fields["trial_end"] = _epoch_to_iso(subscription["trial_end"])

    if existing.data:
        svc.table("subscriptions").update(update_fields).eq("organization_id", organization_id).execute()
    else:
        update_fields["organization_id"] = organization_id
        svc.table("subscriptions").insert(update_fields).execute()


@pro_billing_bp.route("/webhook", methods=["POST"])
def stripe_webhook():
    """Stripe's server-to-server callback -- deliberately NOT behind
    login_required (Stripe isn't a logged-in browser session). Authenticated
    instead by verifying the signature Stripe attaches to the raw request
    body against STRIPE_WEBHOOK_SECRET (shown when this endpoint's URL is
    registered as a webhook destination in the Stripe dashboard). Never
    trust an unsigned payload here -- construct_event() raises if the
    signature doesn't match, which is treated as a rejected request, not a
    sync attempt."""
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "webhook not configured"}), 503

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        return jsonify({"error": f"invalid webhook payload: {e}"}), 400

    event_type = event["type"]
    data_object = event["data"]["object"]

    if event_type == "checkout.session.completed":
        metadata = data_object.get("metadata") or {}
        organization_id = data_object.get("client_reference_id") or metadata.get("organization_id")

        if metadata.get("purchase_type") == "church_exchange_block":
            # One-time payment (mode='payment'), not a subscription -- this
            # session never has a `subscription` field at all, so it has to
            # be caught here, before the subscription-fetch branch below
            # (which would otherwise just silently no-op on a missing
            # subscription_id and never credit anything).
            _apply_church_exchange_block(organization_id, metadata)
        else:
            # Pull the Subscription object fresh here (rather than trusting
            # Checkout Session's own summary fields) so this syncs from the
            # exact same shape customer.subscription.* events use later --
            # one code path, not two slightly-different ones.
            subscription_id = data_object.get("subscription")
            if subscription_id:
                subscription = stripe.Subscription.retrieve(subscription_id)
                _sync_subscription_row(subscription, organization_id=organization_id)

    elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
        _sync_subscription_row(data_object)

    elif event_type == "customer.subscription.deleted":
        # Subscription fully ended (distinct from cancel_at_period_end=true,
        # which arrives as customer.subscription.updated while status is
        # still 'active' until the period actually ends) -- drop back to
        # the free tier's cap, same lifecycle as any lapsed SaaS
        # subscription. Revisit if a different landing tier is ever wanted
        # for lapsed-but-recent subscribers.
        #
        # Church/Org note (2026-07-12): an org can hold TWO simultaneous
        # subscriptions (church_leadership + church_member) -- the naive
        # "update every subscriptions row for this organization_id" would
        # wrongly wipe BOTH when only one of them actually ended (e.g.
        # Membership lapses but Leadership is still active). Scope the
        # update to the specific tier_slug this deleted subscription was,
        # same (organization_id, tier_slug) matching _sync_church_subscription_row
        # uses. Individual Pro orgs only ever have one row, so this is a
        # no-op behavior change for them -- eq("tier_slug", ...) still
        # matches their single row either way.
        organization_id = (data_object.get("metadata") or {}).get("organization_id")
        if organization_id:
            items = (data_object.get("items") or {}).get("data") or []
            price_id = items[0]["price"]["id"] if items else None
            seat_type = CHURCH_PRICE_TO_SEAT_TYPE.get(price_id)
            tier_slug = CHURCH_SEAT_TIER_SLUGS[seat_type] if seat_type else None

            svc = get_service_client()
            query = svc.table("subscriptions").update({
                "tier_slug": "free",
                "status": "canceled",
            }).eq("organization_id", organization_id)
            if tier_slug:
                query = query.eq("tier_slug", tier_slug)
            query.execute()

    # invoice.payment_failed is deliberately not handled separately here --
    # Stripe already emits customer.subscription.updated with status
    # 'past_due' in the same event cascade, which _sync_subscription_row
    # above already covers. Revisit only if/when this app sends its own
    # payment-failure emails (no transactional email infrastructure exists
    # in this codebase today).

    return jsonify({"received": True})
