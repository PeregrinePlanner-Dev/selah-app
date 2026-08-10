import os
import time
import re
import anthropic
from collections import defaultdict
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from dotenv import load_dotenv
from anthropic import Anthropic
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

from pro_auth import pro_bp, get_service_client, csrf_token, csrf_valid
from pro_chat import pro_chat_bp, _check_and_reserve_usage
from pro_billing import pro_billing_bp, DISPLAY_PRICING, CHURCH_SEAT_DISPLAY
from pro_org import pro_org_bp
from pro_scheduler import pro_scheduler_bp
from pro_email import send_email
from free_gate import free_gate_bp, is_free_gate_authenticated, current_free_org_id, clear_inactivity_flag
from guide import guide_bp
from figures import figures_bp
from engine import (
    NODES, NODE_DISPLAY_NAMES, NODE_NAMES, MAX_HISTORY,
    route_to_node, build_system_blocks, parse_response,
    format_convo_for_haiku, ANCHOR_CHIPS_QUERY, strip_tags,
    attach_scripture_verification, WORKER_TIMEOUT_SECONDS,
    HAIKU_SAFE_MARGIN_SECONDS, haiku_client,
)

load_dotenv()

# Error tracking, added 2026-07-24 (Selah_Structured_Audit_2026-07-24.md
# finding: no observability tool connected -- this week's full-site outage
# was diagnosed entirely by hand, Render Events tab + manual log grep,
# because nothing surfaces exceptions automatically. Mirrors Peregrine's
# existing Sentry setup: errors-only (traces_sample_rate=0.0, no
# performance-monitoring cost), send_default_pii=False. Deliberately
# optional/non-blocking -- SENTRY_DSN doesn't exist yet as of this commit
# (Sentry project creation is disabled for members on the artistyle org;
# Rick needs to either enable that in Sentry's org settings or create the
# "selah" project himself and hand back the DSN) -- the app must keep
# working with zero Sentry coverage until that DSN is set, the same way
# Peregrine's own Sentry wiring shipped before its DSN was confirmed live.
_sentry_dsn = os.environ.get("SENTRY_DSN", "")
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        integrations=[FlaskIntegration()],
        traces_sample_rate=0.0,
        send_default_pii=False,
    )
else:
    print("NOTE: SENTRY_DSN not set -- error tracking is not active. See Selah_Structured_Audit_2026-07-24.md.")


def _validate_required_secrets() -> None:
    """Fail loudly at startup instead of booting successfully and failing
    confusingly on first use. Added 2026-07-24 (Selah_Structured_Audit_
    2026-07-24.md finding): every required secret in this codebase was
    previously read via os.environ.get(key, "") or a default, with zero
    presence check anywhere -- app.secret_key even fell back to a literal,
    publicly-known insecure string if unset. Split into two tiers:
    HARD-required (nothing in the app works correctly without these --
    refuse to boot, same as BUILD_PROTOCOL.md's equivalent Peregrine fix)
    and IMPORTANT (a real feature degrades without these, but the rest of
    the app -- chat, auth -- can still serve traffic, so log CRITICAL and
    keep running rather than taking the whole site down over a missing
    Stripe key). Every one of these is already expected to be set on the
    live Render deploy today, so this should be a silent no-op in
    production and only ever fire if a future deploy accidentally drops
    one -- exactly the failure mode this exists to catch fast instead of
    letting it surface as a confusing 500 later."""
    hard_required = {
        "SUPABASE_URL": "every auth/chat route needs this",
        "SUPABASE_ANON_KEY": "Pro and free-tier auth can't function without it",
        "SUPABASE_SERVICE_ROLE_KEY": "usage caps, admin actions, and the free-tier tier-assignment trigger all depend on it",
        "FLASK_SECRET_KEY": "without a real value, sessions would be signed with a publicly-known fallback string",
        "ANTHROPIC_API_KEY_FREE": "the free tool can't generate a single response without it",
    }
    missing_hard = [k for k in hard_required if not os.environ.get(k)]
    if missing_hard:
        lines = "\n".join(f"  - {k}: {hard_required[k]}" for k in missing_hard)
        raise SystemExit("CRITICAL: refusing to start -- required secret(s) missing:\n" + lines)

    important = {
        "STRIPE_SECRET_KEY": "Stripe billing will fail on every checkout/portal call",
        "STRIPE_WEBHOOK_SECRET": "Stripe webhooks (subscription updates, cancellations) will be rejected -- billing state can silently go stale",
        "RESEND_API_KEY": "transactional email (invites, password resets, receipts) will silently fail to send",
    }
    missing_important = [k for k in important if not os.environ.get(k)]
    if missing_important:
        print("CRITICAL: app is starting with missing secret(s) -- real functionality will be broken:")
        for k in missing_important:
            print(f"  - {k}: {important[k]}")


_validate_required_secrets()

app = Flask(__name__)

# ── Anthropic Workspace split, 2026-07-20 ───────────────────────────────────
# The free tool now calls its own Anthropic Workspace/API key (a hard,
# Anthropic-enforced spend cap independent of the app's own usage counters)
# instead of sharing engine.py's client -- previously every tier ran through
# one key, so the free-tier's $-budget ceiling was only ever a soft, app-side
# count, never actually enforced by Anthropic itself. engine.py's own
# `client` (imported by pro_chat.py) is now implicitly the Pro-tier client,
# unchanged -- Pro needed no code changes since it already read the shared
# key. ANTHROPIC_API_KEY_FREE is a key scoped to a separate Workspace with
# its own spend limit set directly in the Anthropic Console (Settings ->
# Manage -> Spend limits within that Workspace). Per Rick, 2026-07-31: reset
# to $100/mo (previously $25, provisional) -- not independently re-verified
# against the live Console by this session, just recorded as reported. Keep
# free_gate.py's own FREE_TIER_MONTHLY_BUDGET constant in sync with whatever
# that real Anthropic limit actually is, since a mismatch means Anthropic
# could hard-block requests before the app's own capacity panel thinks
# there's a problem.
# timeout=50.0 added 2026-07-31, max_retries=0 added 2026-08-01 -- see
# engine.py's client= comment for the full incident/reasoning (mirrors the
# same fix applied there; the same crash shape hit this free-tier path too).
free_client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY_FREE"), timeout=50.0, max_retries=0)

# Selah for Ministry (Pro) auth -- additive only, registered as a separate
# blueprint under /pro/*. The free tool's existing routes below are
# untouched by this. Added 2026-07-07.
app.secret_key = os.environ.get("FLASK_SECRET_KEY")  # presence already guaranteed by _validate_required_secrets() above -- no insecure fallback
app.register_blueprint(pro_bp)
app.register_blueprint(pro_chat_bp)
app.register_blueprint(pro_billing_bp)
app.register_blueprint(pro_org_bp)
app.register_blueprint(pro_scheduler_bp)

# Selah for Church Staff guide -- public, no login/seat check. Added
# 2026-08-01. See guide.py / guide_content.py module docstrings for why
# this is deliberately public rather than gated like the routes above.
app.register_blueprint(guide_bp)

# Theologian & Philosopher public reference pages -- sample pass, added
# 2026-08-10. Theologians page public (login_required not applied);
# Philosophers page gated to paid accounts inside figures.py itself
# (@login_required + a tier_slug check), not at the blueprint level, since
# the hub page (/figures) and the Theologians page must stay reachable
# without login. See figures.py / figures_content.py module docstrings.
app.register_blueprint(figures_bp)

# Free-tier mandatory sign-in gate -- additive, added 2026-07-17. See
# free_gate.py's module docstring and 05- Future/Selah_Decisions_2026-07-17.md
# for the full reasoning (invite-only, 30 exchanges/account/month, $50/month
# budget ceiling). _check_and_reserve_usage is reused directly from
# pro_chat.py rather than duplicated -- it already works generically off
# (organization_id, tier_slug), and tier_slug='free' already existed in both
# the subscriptions table's CHECK constraint and TIER_CONVERSATION_CAPS
# before this change, suggesting the schema was designed anticipating this.
app.register_blueprint(free_gate_bp)

FREE_TIER_CAP_HIT_MESSAGE = (
    "You've reached this month's message limit for the free tier. It resets "
    "at the start of next month -- thanks for your patience, and for being "
    "part of this."
)

# Node content, routing, system-prompt assembly (build_system_blocks), and
# response parsing (parse_response) all live in engine.py now (extracted
# 2026-07-07) -- the free tool and the Pro chat route (pro_chat.py) share
# one engine instead of each keeping its own copy. Nothing about their
# behavior changed in this refactor, only where the code lives.

# ── Anonymous abuse/cost cap ───────────────────────────────────────────────────
# Free tier has no accounts, so this is IP-based -- not tied to identity,
# nothing persisted beyond the current minute/day, purely a guard against
# runaway/bot API cost. Two dimensions, not one:
#
#   1. Burst/rate limit (per IP per minute) -- this is the REAL abuse signal.
#      Genuine automated abuse is characterized by request RATE, not just total
#      volume. A shared connection with many real people on it at once -- e.g. a
#      youth group meeting where a leader's login/network serves a whole room --
#      is paced by human typing speed and won't trip this even though many
#      distinct people are using it. (Raised 2026-07-05, Session 20: the original
#      flat daily-only cap didn't account for exactly this "one identifier, many
#      real humans" shape, which the congregation/youth-group access model
#      creates by design.)
#   2. Daily cap (per IP) -- a looser backstop against slow, sustained abuse that
#      deliberately stays under the burst threshold but runs for hours.
#
# Once real Pro/church accounts exist, authenticated institutional traffic should
# be metered against that organization's own subscription cap (see
# usage_records/conversations_cap in Selah_Pro_Infrastructure_Plan.md) instead of
# this anonymous IP limiter -- this block is a free/anonymous-tier safety net
# only, not meant to apply once someone is on a paid, authenticated plan.
# Decided 2026-07-05 (Session 20 roadmap item) -- see DEVELOPMENT_ROADMAP.md.
MINUTE_RATE_CAP = int(os.environ.get("MINUTE_RATE_CAP", "30"))
DAILY_TURN_CAP  = int(os.environ.get("DAILY_TURN_CAP", "1200"))

minute_tracker: dict = defaultdict(dict)  # {ip: {"YYYY-MM-DDTHH:MM": count}}
usage_tracker:  dict = defaultdict(dict)  # {ip: {"YYYY-MM-DD": count}}

RATE_LIMIT_MESSAGE_BURST = (
    "Selah's getting a lot of messages from this connection all at once -- "
    "give it just a moment and try again."
)
RATE_LIMIT_MESSAGE_DAILY = (
    "Selah's seen a lot of company today, so replies from this connection are "
    "paused until tomorrow to keep things running smoothly for everyone. "
    "Thanks for your patience -- come back soon."
)

def get_client_ip() -> str:
    """Real client IP behind Render's proxy, falling back to remote_addr."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"

def check_rate_limit(ip: str) -> str | None:
    """Returns None if this request is allowed (and increments both counters as
    a side effect), or a reason string ('burst' or 'daily') if it should be
    blocked. Call exactly once per billable API call (i.e. once per /chat
    request, once per /upload_session request), not once per underlying
    Anthropic call."""
    now    = datetime.utcnow()
    today  = now.date().isoformat()
    minute = now.strftime("%Y-%m-%dT%H:%M")

    ip_day = usage_tracker[ip]
    for d in list(ip_day.keys()):        # keep only today's entry -- self-cleaning
        if d != today:
            del ip_day[d]

    ip_minute = minute_tracker[ip]
    for m in list(ip_minute.keys()):     # keep only the current minute's entry
        if m != minute:
            del ip_minute[m]

    if ip_minute.get(minute, 0) >= MINUTE_RATE_CAP:
        return "burst"
    if ip_day.get(today, 0) >= DAILY_TURN_CAP:
        return "daily"

    ip_minute[minute] = ip_minute.get(minute, 0) + 1
    ip_day[today]     = ip_day.get(today, 0) + 1
    return None

# ── In-memory conversations ───────────────────────────────────────────────────
conversations: dict = {}

# ── UTM / referrer capture, added 2026-07-24 (confirmed priority by Rick, ────
# 2026-07-20; profiles had no marketing-attribution columns at all before
# today). First-touch model: captured once per browser session on the first
# GET that carries any utm_* param or a cross-site referrer, then carried in
# the Flask session cookie (shared across the free tool's fg_* and Pro's
# sb_* auth, since both ultimately write to the same profiles table) until
# signup actually happens -- pro_auth.signup() and free_gate._complete_signin()
# both do a best-effort, write-once (WHERE utm_source IS NULL) profiles
# update with whatever landed here. A user who never signs up costs nothing
# extra; a user who signs up days after their first visit still gets
# correctly attributed to that first visit, not to whatever page happened to
# host the signup form.
_UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")


@app.before_request
def _capture_utm_attribution():
    if "utm_captured" in session:
        return
    found = {k: request.args.get(k, "").strip() for k in _UTM_KEYS}
    referrer = (request.referrer or "").strip()
    if any(found.values()) or referrer:
        for k, v in found.items():
            if v:
                session[k] = v[:200]  # defensive cap -- these are attacker-controlled query params
        if referrer:
            session["signup_referrer"] = referrer[:500]
    # Marks this session as "checked," regardless of whether anything was
    # found -- so a later page view in the same session (now with no UTM
    # params, since campaign links only carry them on the first click)
    # doesn't get treated as a fresh, param-less "visit" that overwrites
    # nothing but also doesn't need re-checking every request.
    session["utm_captured"] = True


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # Once ministry.selahexploringtheology.com DNS points at this same Render
    # service, requests arriving on that host serve the landing page instead
    # of the main app -- no separate hosting/service needed. Until DNS is
    # live, preview at /ministry on the existing domain.
    if request.host.startswith("ministry."):
        return render_template("ministry.html", pricing=DISPLAY_PRICING)
    # Free-tier gate, added 2026-07-17 -- the ministry landing page, /church,
    # and the /invite explainer page (below) deliberately stay open; only
    # the actual chat tool requires sign-in now.
    if not is_free_gate_authenticated():
        return redirect(url_for("free_gate.access_home"))
    return render_template(
        "index.html", nodes=NODE_NAMES, node_display_names=NODE_DISPLAY_NAMES,
        user_email=session.get("fg_email", ""),
    )

@app.route("/ministry")
def ministry():
    return render_template("ministry.html", pricing=DISPLAY_PRICING)

@app.route("/personal")
def personal():
    # Added 2026-08-02 -- Selah for Personal Study. Same underlying Pro
    # product/pricing as /ministry (Explore/Pursue/Immerse, same /pro/
    # signup), just a separate landing page for individuals studying on
    # their own rather than teaching/leading others. See ministry.html's
    # existing church-teaser pattern -- this page's own teaser points the
    # other direction, up to /ministry, for anyone who lands here but is
    # actually teaching or leading a group.
    return render_template("personal.html", pricing=DISPLAY_PRICING)

@app.route("/church")
def church():
    # Dedicated Church/Org marketing page -- linked from the brief teaser
    # section on ministry.html. Pricing is locked (Task #7), pulled from
    # pro_billing.py's CHURCH_SEAT_TIERS. 2026-07-13 comment claimed this
    # was already dynamic -- it wasn't (church.html hardcoded all 7 figures
    # independently, confirmed 2026-07-24 while fixing the same gap for
    # ministry.html/pro_app.html); now actually wired via CHURCH_SEAT_DISPLAY.
    return render_template("church.html", seats=CHURCH_SEAT_DISPLAY)

@app.route("/support")
def support():
    # Dedicated giving page -- geared toward individual donors (both giving
    # options link out to Ko-fi; churches/orgs are pointed to /church instead).
    # Added 2026-07-18 at Rick's request, replacing the plan to rely solely
    # on Ko-fi's own hosted page.
    return render_template("support.html")

# ── Unified "which door" entry point ────────────────────────────────────────
# Added 2026-07-23, directly out of Clark's real Pro-login incident: Selah has
# two structurally separate sign-in systems (free tool at /access, Selah for
# Ministry at /pro) that someone has to already know to pick between -- Clark
# didn't, guessed wrong, and it looked like a broken app. /start removes the
# guess: one email box, and the server tells you which door is yours based on
# what's actually in the database, rather than making a person self-diagnose.
# Deliberately NOT wired in as the new default for "/" or the marketing pages'
# existing sign-in links yet -- that's a bigger content decision (do we want
# every visitor funneled through an extra step, even ones who already know
# where they're going) left for Rick to make deliberately, not slipped in
# under today's time pressure. For now this is an additive, linked-to option
# from both existing login pages (see access.html / pro_login.html footers).
_start_lookup_attempts: dict = defaultdict(list)  # {"ip:1.2.3.4": [timestamps]}
START_LOOKUP_LIMIT = int(os.environ.get("START_LOOKUP_LIMIT", "20"))
START_LOOKUP_WINDOW_SECONDS = int(os.environ.get("START_LOOKUP_WINDOW_SECONDS", "600"))  # 10 min


def _start_lookup_allowed(ip: str) -> bool:
    """Generous, read-only-abuse guard -- this endpoint has no side effects
    (it's a lookup, not a signup/login attempt), so the limit here exists only
    to blunt email-enumeration scraping, not to protect an account. Same
    self-cleaning trailing-window pattern as pro_auth.py's rate limiter,
    duplicated rather than imported to keep app.py and pro_auth.py's existing
    no-cross-imports-from-each-other pattern intact."""
    now = time.time()
    attempts = _start_lookup_attempts[f"ip:{ip}"]
    attempts[:] = [t for t in attempts if now - t < START_LOOKUP_WINDOW_SECONDS]
    if len(attempts) >= START_LOOKUP_LIMIT:
        return False
    attempts.append(now)
    return True


@app.route("/start")
def start():
    return render_template("start.html")


@app.route("/start/route", methods=["POST"])
def start_route():
    """Looks up an email against public.profiles (which stores email
    directly -- confirmed by checking the schema rather than assuming) joined
    to its organization's subscriptions.tier_slug, and tells the client which
    login page to send the visitor to. Read-only, via the service client
    (bypasses RLS -- there's no logged-in user yet to scope this to). Never
    reveals anything beyond "pro" vs "free" vs "unknown" -- no account details,
    no confirmation of exact match beyond that routing decision."""
    if not _start_lookup_allowed(get_client_ip()):
        return jsonify({"error": "Too many attempts from this connection -- please wait a few minutes and try again."}), 429

    body = request.json or {}
    email = (body.get("email") or "").strip()
    if not email:
        return jsonify({"error": "Enter your email."}), 400

    destination = "/access/"
    notice = None
    try:
        svc = get_service_client()
        prof = (
            svc.table("profiles")
            .select("organization_id")
            .ilike("email", email)
            .limit(1)
            .execute()
        )
        if prof.data and prof.data[0].get("organization_id"):
            org_id = prof.data[0]["organization_id"]
            # Fetch ALL subscription rows for this org, not just one -- found
            # live while building this (Clark's own org, checked directly):
            # an org can carry a stale leftover 'free' subscriptions row
            # alongside its real paid tier(s) picked up later (e.g. a solo
            # signup that got upgraded to a Church plan without the old row
            # ever being cleared). Grabbing an arbitrary single row with
            # .limit(1) risked landing on the stale 'free' one and sending a
            # real Pro/Church user to the wrong door -- the exact bug this
            # page exists to prevent. "Pro" if ANY row is a non-free tier.
            sub = (
                svc.table("subscriptions")
                .select("tier_slug")
                .eq("organization_id", org_id)
                .execute()
            )
            tier_slugs = [row.get("tier_slug") for row in (sub.data or [])]
            if any(t and t != "free" for t in tier_slugs):
                destination = "/pro/"
                notice = "We found a Selah for Ministry account for this email -- sign in below."
    except Exception:
        # Never let a lookup failure block someone from getting *somewhere* --
        # worst case, default to the free tool's door, which handles both new
        # and returning free-tier users gracefully either way.
        pass

    return jsonify({"destination": destination, "notice": notice})


@app.route("/invite")
def invite():
    # Shareable invitation page for the free tool -- built 2026-07-08 at Rick's
    # request after his pastor friend (Clark Cothern) asked for something he
    # could send to his congregation and pastoral network. Additive only, no
    # existing route touched.
    return render_template("invite.html")

@app.route("/legal")
def legal():
    return render_template("legal.html")

# ── Privacy/data-request contact form ───────────────────────────────────────
# Added 2026-07-25, mid-Termly-questionnaire: Florida, Nebraska, and Texas
# each require at least two methods for submitting privacy requests, and
# email (arrowroot56@gmail.com, already in legal.html Section 12) was the
# only real channel that existed. This is the second one -- deliberately
# minimal (no ticketing, no auto-routing by request type) so today's Termly
# answer is honest rather than aspirational; a fuller build can come later.
# Sends via the same Resend pipeline/observability as every other
# transactional email in the app (pro_email.send_email -> email_send_log).
_contact_submit_attempts: dict = defaultdict(list)  # {"ip:1.2.3.4": [timestamps]}
CONTACT_SUBMIT_LIMIT = int(os.environ.get("CONTACT_SUBMIT_LIMIT", "5"))
CONTACT_SUBMIT_WINDOW_SECONDS = int(os.environ.get("CONTACT_SUBMIT_WINDOW_SECONDS", "600"))  # 10 min


def _contact_submit_allowed(ip: str) -> bool:
    """Same self-cleaning trailing-window pattern as _start_lookup_allowed
    above -- generous limit, this just blunts basic spam floods, not a
    security boundary."""
    now = time.time()
    attempts = _contact_submit_attempts[f"ip:{ip}"]
    attempts[:] = [t for t in attempts if now - t < CONTACT_SUBMIT_WINDOW_SECONDS]
    if len(attempts) >= CONTACT_SUBMIT_LIMIT:
        return False
    attempts.append(now)
    return True


@app.route("/privacy-contact")
def privacy_contact():
    return render_template(
        "privacy_contact.html",
        csrf_token=csrf_token(),
        sent=request.args.get("sent") == "1",
        error=request.args.get("error", ""),
    )


@app.route("/privacy-contact/submit", methods=["POST"])
def privacy_contact_submit():
    if not csrf_valid():
        return redirect(url_for("privacy_contact", error="Your session expired -- please try again."))

    # Honeypot -- real users never see this field (hidden via CSS on the
    # template); a bot that fills every input trips it silently, no error
    # shown back, so scrapers don't learn which field to skip.
    if request.form.get("website", "").strip():
        return redirect(url_for("privacy_contact", sent="1"))

    if not _contact_submit_allowed(get_client_ip()):
        return redirect(url_for("privacy_contact", error="Too many submissions from this connection -- please wait a few minutes and try again."))

    name         = request.form.get("name", "").strip()[:200]
    email        = request.form.get("email", "").strip()[:200]
    request_type = request.form.get("request_type", "").strip()[:100]
    message      = request.form.get("message", "").strip()[:5000]

    if not email or not message:
        return redirect(url_for("privacy_contact", error="Email and message are required."))

    body = f"""
      <p><strong>New privacy/contact request from selahexploringtheology.com</strong></p>
      <p><strong>Name:</strong> {name or '(not provided)'}</p>
      <p><strong>Email:</strong> {email}</p>
      <p><strong>Request type:</strong> {request_type or '(not specified)'}</p>
      <p><strong>Message:</strong><br>{message}</p>
    """
    send_email("arrowroot56@gmail.com", f"Selah privacy contact form: {request_type or 'General'}", body)

    return redirect(url_for("privacy_contact", sent="1"))

@app.route("/church-guide")
def church_guide():
    # Public, no-login static page -- same pattern as /legal and /support --
    # so it's shareable as a plain link (e.g. in the promoted-admin email)
    # before someone's even logged in, added 2026-07-24 per the audit finding
    # that no written admin documentation existed anywhere.
    return render_template("church_admin_guide.html")

@app.route("/chat", methods=["POST"])
def chat():
    # request_start added 2026-08-01 -- see pro_chat.py's matching comment
    # (PYTHON-7/8/9 recurred a third time; this measures real elapsed time
    # instead of assuming a fixed worst case). Captured before the gate
    # check below since that's fast/local (no network call), so it doesn't
    # meaningfully affect the budget math.
    request_start = time.monotonic()

    # Free-tier gate, added 2026-07-17. Checked before the rate limiter --
    # no point counting an unauthenticated request against the IP limiter at
    # all if it's about to be rejected anyway.
    if not is_free_gate_authenticated():
        return jsonify({"error": "sign_in_required", "redirect": "/access"}), 401

    data       = request.json
    message    = data.get("message", "").strip()
    force_node = data.get("node")

    # Session key is the signed-in account's own id, NOT whatever session_id
    # the client sends -- this is what actually makes the gate meaningful
    # (a stable identity to check the cap against) rather than cosmetic.
    # Client-supplied session_id is accepted in the request body for
    # backward JS compatibility but no longer used for dict keying.
    session_id = session["fg_user_id"]

    if not message:
        return jsonify({"error": "empty message"}), 400

    limit_hit = check_rate_limit(get_client_ip())
    if limit_hit:
        msg = RATE_LIMIT_MESSAGE_BURST if limit_hit == "burst" else RATE_LIMIT_MESSAGE_DAILY
        return jsonify({
            "reply":    msg,
            "question": "",
            "sources":  [],
            "node":     "",
            "anchor":   "",
            "chips":    [],
            "turn":     0,
        })

    # Per-account monthly cap, added 2026-07-17 -- reuses pro_chat.py's
    # TIER_CONVERSATION_CAPS['free'] (already env-overridable via
    # FREE_TIER_MONTHLY_CAP, decided at 30/month) via the exact same
    # (organization_id, tier_slug) pattern Individual Pro already uses.
    # Checked after the IP rate limiter (a cheap in-memory check) but before
    # touching the DB or calling Anthropic.
    if not _check_and_reserve_usage(current_free_org_id(), "free"):
        return jsonify({
            "reply":    FREE_TIER_CAP_HIT_MESSAGE,
            "question": "",
            "sources":  [],
            "node":     "",
            "anchor":   "",
            "chips":    [],
            "turn":     0,
        })

    # Proof of life for the inactivity-nudge scheduler (pro_scheduler.py) --
    # a real exchange clears any previously-set nudge flag so a future quiet
    # stretch triggers a fresh nudge instead of staying silenced forever.
    clear_inactivity_flag(session_id)

    if session_id not in conversations:
        conversations[session_id] = {
            "messages": [], "node": None, "anchor": "", "turn": 0
        }

    convo = conversations[session_id]

    if force_node and force_node in NODES:
        convo["node"] = force_node
    elif convo["node"] is None:
        convo["node"] = route_to_node(message)

    active_node = convo["node"]

    convo["messages"].append({"role": "user", "content": message})

    # ── Main response (Sonnet) ────────────────────────────────────────────────
    # System prompt is split into independently-cached blocks (see
    # engine.build_system_blocks) -- the MASTER_PROMPT and RESPONSE_FORMAT
    # layers are identical across every node/user app-wide, so they stay warm
    # from ANY request; only the smaller node-specific layer needs re-caching
    # when that node's traffic goes quiet. 1-hour ephemeral TTL (not the
    # 5-minute default) added 2026-07-09 so normal reading/reflection pauses
    # between turns don't force a cache rewrite.
    # Only the last MAX_HISTORY messages are sent to cap growing context costs.
    # Strip technical tags from history so Sonnet doesn't see prior [SOURCE:] tags
    # and interpret them as "sourcing already done" -- which caused it to stop tagging.
    clean_history = [
        {"role": m["role"], "content": strip_tags(m["content"])}
        for m in convo["messages"][-MAX_HISTORY:]
    ]

    try:
        response = free_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=build_system_blocks(active_node),
            messages=clean_history,
        )
    except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
        # Added 2026-07-31 after a real incident on the Pro side (Sentry
        # PYTHON-7/8/9) -- this call was unguarded, so a slow/stuck Anthropic
        # response took down the whole gunicorn worker instead of failing
        # gracefully. See engine.py's client= comment for the full story.
        # convo["messages"] only lives in the in-memory `conversations` dict
        # for this tier, and it isn't written back until after a real reply
        # exists, so returning early here doesn't leave anything half-saved.
        sentry_sdk.capture_exception(e)
        return jsonify({
            "reply": "That response is taking longer than expected. Please try again in a moment.",
            "question": "",
            "sources": [],
            "node": active_node,
            "anchor": convo.get("anchor", ""),
            "chips": [],
            "turn": convo.get("turn", 0),
        })
    raw_text = response.content[0].text
    parsed   = parse_response(raw_text)

    convo["messages"].append({"role": "assistant", "content": raw_text})
    convo["turn"] += 1

    if not convo["anchor"]:
        convo["anchor"] = f"Exploring {active_node}."

    # ── Combined anchor + chips + source -- one Haiku call ────────────────────
    chips   = []
    sources = parsed["sources"]
    elapsed = time.monotonic() - request_start
    remaining = WORKER_TIMEOUT_SECONDS - elapsed
    if remaining < HAIKU_SAFE_MARGIN_SECONDS:
        # Hard skip added 2026-08-01 -- see pro_chat.py's matching comment
        # (PYTHON-7/8/9 recurred a third time; same fix applied here since
        # this free-tier path has the identical two-call shape).
        sentry_sdk.capture_message(
            f"Skipped free /chat Haiku follow-up: only {remaining:.1f}s left "
            f"of {WORKER_TIMEOUT_SECONDS}s worker budget after main reply.",
            level="warning",
        )
    else:
        try:
            convo_text = format_convo_for_haiku(convo["messages"])
            # haiku_client (engine.py) -- see pro_chat.py's matching comment;
            # switched from free_client + a per-call timeout= kwarg after
            # that override failed to actually bound the call in production.
            haiku_resp = haiku_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=700,
                messages=[{
                    "role": "user",
                    "content": ANCHOR_CHIPS_QUERY.format(convo_text=convo_text)
                }],
            )
            haiku_text = haiku_resp.content[0].text.strip()

            anchor_match = re.search(r'ANCHOR:\s*(.+?)(?=\nCHIP_|\Z)', haiku_text, re.DOTALL)
            chip_matches = re.findall(r'CHIP_\d+:\s*(.+)', haiku_text)

            if anchor_match:
                convo["anchor"] = anchor_match.group(1).strip()
            chips = [c.strip() for c in chip_matches if c.strip()]
            convo["chips"] = chips

            # Parse all SOURCE blocks from Haiku if Sonnet tags produced nothing
            if not sources:
                blocks = re.split(r'SOURCE_END', haiku_text)
                for block in blocks:
                    type_m    = re.search(r'SOURCE_TYPE:\s*(\S+)',                              block)
                    label_m   = re.search(r'SOURCE_LABEL:\s*(.+)',                              block)
                    content_m = re.search(r'SOURCE_CONTENT:\s*(.+?)(?=\nSOURCE_|\Z)', block, re.DOTALL)
                    t_val = type_m.group(1).strip().lower()   if type_m    else ""
                    l_val = label_m.group(1).strip().lower()  if label_m   else ""
                    c_val = content_m.group(1).strip().lower() if content_m else ""
                    if (type_m and t_val not in ("none", "")
                            and label_m and l_val not in ("none", "none identified", "")
                            and content_m and c_val not in ("none", "none identified", "")):
                        sources.append({
                            "type":    type_m.group(1).strip(),
                            "label":   label_m.group(1).strip(),
                            "content": content_m.group(1).strip(),
                        })

        except Exception as e:
            print(f"[ANCHOR/CHIPS/SOURCE ERROR] {e}")

    # Non-blocking reference-existence check on any scripture-type sources
    # this turn produced -- see engine.attach_scripture_verification().
    sources = attach_scripture_verification(sources)

    return jsonify({
        "reply":    parsed["reply"],
        "question": parsed["question"],
        "sources":  sources,
        "node":     active_node,
        "anchor":   convo["anchor"],
        "chips":    chips,
        "turn":     convo["turn"],
    })

@app.route("/reset", methods=["POST"])
def reset():
    if not is_free_gate_authenticated():
        return jsonify({"error": "sign_in_required", "redirect": "/access"}), 401
    sid = session["fg_user_id"]
    if sid in conversations:
        del conversations[sid]
    return jsonify({"ok": True})

@app.route("/export", methods=["POST"])
def export():
    """Return a plain-text session transcript for saving."""
    if not is_free_gate_authenticated():
        return jsonify({"error": "sign_in_required", "redirect": "/access"}), 401
    data    = request.json
    sid     = session["fg_user_id"]
    sources = data.get("sources", [])
    convo   = conversations.get(sid, {})
    msgs    = convo.get("messages", [])
    anchor  = convo.get("anchor", "")
    node    = convo.get("node", "")

    lines = [f"Selah Session Export\nNode: {node}\n\n=== Session Anchor ===\n{anchor}\n\n=== Conversation ===\n"]
    for m in msgs:
        role = "You" if m["role"] == "user" else "Selah"
        text = re.sub(r'\[QUESTION:.*?\]', '', m["content"], flags=re.DOTALL)
        text = re.sub(r'\[SOURCE:.*?\]',   '', text,         flags=re.DOTALL).strip()
        lines.append(f"{role}:\n{text}\n")

    if sources:
        lines.append("\n=== Sources Cited ===\n")
        for s in sources:
            kind  = "Scripture" if s.get("type") == "scripture" else "Theologian"
            label = s.get("label", "")
            content = s.get("content", "")
            lines.append(f"{kind} — {label}\n{content}\n")

    return jsonify({"text": "\n".join(lines)})

@app.route("/upload_session", methods=["POST"])
def upload_session():
    """Seed a new session from a previously downloaded recap file."""
    if not is_free_gate_authenticated():
        return jsonify({"error": "sign_in_required", "redirect": "/access"}), 401

    data       = request.json
    session_id = session["fg_user_id"]
    content    = data.get("content", "")

    limit_hit = check_rate_limit(get_client_ip())
    if limit_hit:
        msg = RATE_LIMIT_MESSAGE_BURST if limit_hit == "burst" else RATE_LIMIT_MESSAGE_DAILY
        return jsonify({
            "greeting": msg,
            "node":     "",
            "anchor":   "",
        })

    # Same per-account cap as /chat -- this route makes two real Anthropic
    # calls (context brief + greeting), so it counts against the same
    # monthly allowance rather than being a free way around the cap.
    if not _check_and_reserve_usage(current_free_org_id(), "free"):
        return jsonify({
            "greeting": FREE_TIER_CAP_HIT_MESSAGE,
            "node":     "",
            "anchor":   "",
        })

    clear_inactivity_flag(session_id)

    node = "Grace"
    node_match = re.search(r"Node:\s*(.+)", content)
    if node_match:
        found = node_match.group(1).strip()
        if found in NODES:
            node = found

    prev_anchor = ""
    anchor_match = re.search(r"=== Session Anchor ===(.*?)=== Conversation ===", content, re.DOTALL)
    if anchor_match:
        prev_anchor = anchor_match.group(1).strip()

    # Parse full conversation into message pairs
    all_messages = []
    convo_match = re.search(r"=== Conversation ===(.*?)(?:=== Sources Cited ===|\Z)", content, re.DOTALL)
    if convo_match:
        convo_text = convo_match.group(1).strip()
        turns = re.split(r'\n(?=You:\n|Selah:\n)', convo_text)
        for turn in turns:
            turn = turn.strip()
            if turn.startswith("You:\n"):
                all_messages.append({"role": "user", "content": turn[5:].strip()})
            elif turn.startswith("Selah:\n"):
                all_messages.append({"role": "assistant", "content": turn[7:].strip()})

    # Build full transcript text for Haiku to summarize
    full_transcript = "\n\n".join(
        f"{'Person' if m['role']=='user' else 'Selah'}: {m['content']}"
        for m in all_messages
    )

    # Have Haiku generate a context brief capturing personal details and key tensions
    context_prompt = (
        "Read this theology conversation carefully and write a compact CONTEXT BRIEF "
        "that a returning conversation partner would need to serve this person well.\n\n"
        "Include:\n"
        "- Key personal details shared (life situation, age, relationships, history, wounds named)\n"
        "- The specific struggles, fears, or unresolved tensions they voiced\n"
        "- The theological themes explored and how they connected to the person's life\n"
        "- The exact question or tension where the conversation ended\n"
        "- Anything they said that carries particular emotional or spiritual weight\n\n"
        "Write in plain prose, 150-200 words. This is for internal context only — not shown to the user.\n\n"
        f"CONVERSATION:\n{full_transcript[:6000]}"
    )

    try:
        context_resp = free_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": context_prompt}],
        )
        context_brief = context_resp.content[0].text.strip()

        # Last 6 exchanges (12 messages) for conversational thread
        recent_messages = all_messages[-12:]

        # Build greeting using context brief + last exchanges
        last_exchanges = "\n\n".join(
            f"{'Person' if m['role']=='user' else 'Selah'}: {m['content']}"
            for m in all_messages[-4:]
        )
        returning_prompt = (
            f"Context brief from prior session:\n{context_brief}\n\n"
            f"Last exchanges:\n{last_exchanges}\n\n"
            "Write a warm returning-session opening of 2-3 sentences only. "
            "Reference the specific tension or question they left unresolved, then ask one focused reflection prompt. "
            "Do NOT mention how much time has passed — you don't know. "
            "No headers. No bullet points. No numbered lists. Plain prose only."
        )

        greeting_resp = free_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": returning_prompt}],
        )
        greeting = greeting_resp.content[0].text.strip()
    except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
        # Added 2026-07-31, same incident/reasoning as /chat's catch above.
        # Both calls in this recap-restore flow are wrapped together since
        # they're sequential steps of one logical operation -- either one
        # stalling means there's no usable greeting to seed the session with.
        sentry_sdk.capture_exception(e)
        return jsonify({
            "greeting": "That took longer than expected to load. Please try uploading your recap again in a moment.",
            "node": "",
            "anchor": "",
        })

    # Seed: hidden context brief, then last 6 exchanges, then greeting
    seed_messages = (
        [{"role": "user",      "content": f"[SESSION CONTEXT — not shown to user:\n{context_brief}]"},
         {"role": "assistant", "content": "Understood. I have the full context from the prior session."}]
        + recent_messages
        + [{"role": "assistant", "content": greeting}]
    )

    conversations[session_id] = {
        "messages": seed_messages,
        "node":   node,
        "anchor": prev_anchor,
        "turn":   0,
    }

    return jsonify({"greeting": greeting, "node": node, "anchor": prev_anchor})

if __name__ == "__main__":
    print(f"Selah running --> http://localhost:5000")
    print(f"Nodes loaded: {len(NODES)}")
    # Gated on an env var, not hardcoded -- flagged in the 2026-07-14 audit
    # (Section 3.4a) as a latent footgun (Flask debug mode exposes a remote
    # code-execution console on unhandled errors). Not an active risk today
    # since gunicorn/the Procfile never executes this line, but cheap to fix
    # while this file's already open. Set FLASK_DEBUG=1 locally to opt in.
    app.run(debug=os.environ.get("FLASK_DEBUG", "") == "1", port=5000)
