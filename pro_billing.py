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
                                      undesigned feature (roadmap tasks #76/
                                      #77) -- creating this Price now so the
                                      Stripe product catalog is complete and
                                      ready whenever that mechanism is built,
                                      not because checkout for it works today.
  Any annual price left unset falls back to that tier's monthly price, same
  graceful-degrade behavior as the old single-tier version -- a tier can go
  live monthly-only without its annual Price existing yet.
Until STRIPE_SECRET_KEY is set, every route here returns a clean 503 rather
than a confusing crash, so the app can deploy with this code in place before
the keys actually exist.
"""

import os
from datetime import datetime, timezone

import stripe
from flask import Blueprint, request, jsonify, url_for

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

TRIAL_DAYS = 14


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
            "trial_period_days": TRIAL_DAYS,
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
        .select("tier_slug, status, billing_cycle, current_price, current_period_end, cancel_at_period_end, trial_end")
        .eq("organization_id", organization_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not sub_resp.data:
        return jsonify({"tier_slug": "free", "status": "active"})
    return jsonify(sub_resp.data[0])


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

    # Looked up before building update_fields, not after, so the
    # unknown-price fallback below can tell a brand-new row from an
    # existing one (see comment there).
    existing = (
        svc.table("subscriptions")
        .select("id")
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )
    is_new_row = not existing.data

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
    if price_info:
        # Known price -- set the real tier and billing details.
        update_fields["tier_slug"] = price_info["tier_slug"]
        update_fields["billing_cycle"] = price_info["billing_cycle"]
        update_fields["current_price"] = price_info["current_price"]
    elif is_new_row:
        # Unrecognized price_id on a brand-new row -- don't guess silently.
        # Most likely cause: a subscription created directly in the Stripe
        # dashboard, or a STRIPE_PRICE_* env var that's missing/wrong. The
        # NOT NULL tier_slug column still needs *something*, so this
        # defaults to the cheapest real tier as the safer of two wrong
        # guesses (under-provisioning, not over-), and logs loudly so it
        # gets caught rather than silently mis-billed.
        print(f"[STRIPE WEBHOOK] price {price_id!r} not in PRICE_MAP -- check STRIPE_PRICE_* env vars (new row, defaulted to individual_explore)")
        update_fields["tier_slug"] = "individual_explore"
    else:
        # Unrecognized price on an EXISTING row -- leave tier_slug out of
        # update_fields entirely so this update doesn't clobber the row's
        # already-correct tier with a wrong guess.
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
        # Pull the Subscription object fresh here (rather than trusting
        # Checkout Session's own summary fields) so this syncs from the
        # exact same shape customer.subscription.* events use later --
        # one code path, not two slightly-different ones.
        organization_id = data_object.get("client_reference_id") or (data_object.get("metadata") or {}).get("organization_id")
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
        organization_id = (data_object.get("metadata") or {}).get("organization_id")
        if organization_id:
            svc = get_service_client()
            svc.table("subscriptions").update({
                "tier_slug": "free",
                "status": "canceled",
            }).eq("organization_id", organization_id).execute()

    # invoice.payment_failed is deliberately not handled separately here --
    # Stripe already emits customer.subscription.updated with status
    # 'past_due' in the same event cascade, which _sync_subscription_row
    # above already covers. Revisit only if/when this app sends its own
    # payment-failure emails (no transactional email infrastructure exists
    # in this codebase today).

    return jsonify({"received": True})
