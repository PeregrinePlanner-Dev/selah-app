"""Selah free tool -- mandatory sign-in gate, added 2026-07-17.

Decisions this implements (see 05- Future/Selah_Decisions_2026-07-17.md and
Selah_Session_Summary_2026-07-17.md): the free tool moves from fully
anonymous to email-verified accounts, invite-only for now, 30
exchanges/account/month, on a $50/month budget ceiling. Confirmed by Rick:
"if we're going to cap, we need mandatory sign in with email verification."

BUG FIX, 2026-07-17, same day, found by Rick's own first live test: the
original version of this file only built the typed-code path (see the
VERIFY-BY-CODE section below), reasoning that a clicked link's token comes
back in a URL fragment a server-rendered Flask route can't read. That
reasoning about the fragment problem was correct, but incomplete -- it
missed that Supabase's actual default email template puts the clickable
link front and center (this is what Rick's test showed: email arrived,
clicked the link, landed on the code-entry screen with no code to enter,
since nothing in that screen ever prompted him to look for one). Rather
than depend on Rick manually customizing Supabase's email template (a
dashboard change neither of us could verify from here), this version
handles the link properly instead -- three real paths now, most-likely-used
first, all converging on the same _complete_signin() helper:

  1. VERIFY-BY-LINK, server-side (/access/callback): Supabase's newer
     token_hash + type query-string pattern, fully server-readable, no JS
     needed. Tried first if present.
  2. VERIFY-BY-LINK, fragment-based (/access/session): the older implicit-
     grant pattern (#access_token=...) -- unreadable server-side by design,
     so access.html's own JS checks window.location.hash on page load and
     POSTs the tokens here if found.
  3. VERIFY-BY-CODE (/access/verify): the original typed-6-digit-code path,
     kept as a real fallback, not removed -- if Supabase's template shows
     the token below the link (many do), someone can still just type it.

Reuses the existing Pro auth infrastructure rather than inventing a parallel
one: get_supabase()/get_service_client() from pro_auth.py, and the
tier-capped usage-check pattern from pro_chat.py -- this tier is just
tier_slug='free' on the same organizations/profiles/subscriptions/
usage_records schema Individual Pro already uses, provisioned by the same
handle_new_user() trigger (extended, not replaced -- see the
free_tier_gate_invites_and_trigger migration).

Deliberately NOT in scope for this pass: migrating free-tier conversation
state into planning_sessions (Postgres). That's the free tool's real
concurrency/redeploy-loss ceiling, already tracked separately as its own,
larger fix (Selah_Marketing_Referral_and_Scale_Readiness_Plan_v2.md, Track
2.1 -- "Phase 0"). What this file does is a real improvement on today's
fully-anonymous, random-UUID-per-page-load state (a signed-in user's
conversation now lives at a stable key tied to their account, not a fresh
UUID every reload) without pretending to have solved the redeploy-durability
problem, which needs its own dedicated pass.
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for

from pro_auth import get_supabase, get_service_client
from pro_email import send_email
from pro_chat import _billing_month_today

free_gate_bp = Blueprint("free_gate", __name__, url_prefix="/access")

SIGNUP_SOURCE = "free_gate"

GENERIC_LINK_ERROR = (
    "That link didn't work -- it may be expired or already used. Request a "
    "fresh one below."
)
GENERIC_CODE_ERROR = (
    "That code didn't work -- it may be wrong or expired. Try again or "
    "request a new one."
)
WRONG_EMAIL_ERROR = (
    "That invite code was sent to a different email address. Enter the "
    "email it was originally sent to, or ask whoever invited you for a "
    "fresh code."
)

# Gate for the invite-issuing admin page below -- set FREE_TIER_ADMIN_KEY in
# the environment (Render dashboard). No admin-role system exists for free-
# tier accounts (unlike Church/Org's is_org_admin, which is scoped to a
# specific organization) -- this is just Rick/Clark personally issuing
# invites, so a single shared secret is the right amount of gate for now,
# not a new permissions model.
FREE_TIER_ADMIN_KEY = os.environ.get("FREE_TIER_ADMIN_KEY", "").strip()

# Second, narrower key, added 2026-07-19 -- Rick is letting Clark issue
# invites during beta but the two shouldn't share a credential (so it can
# be revoked/rotated for one person without touching the other, and so
# Clark's key can be scoped to less than everything Rick can see). Right
# now both roles land on the exact same invite-only page -- there's
# nothing founder-only built yet -- but the role is already tracked
# (fg_admin_role, "founder" vs "inviter") so future additions to this page
# (the waitlist/dashboard work scoped in
# 05- Future/Selah_Founder_Dashboard_and_Waitlist_Scope_2026-07-19.md) can
# gate themselves to founder-only via _is_founder() without a rebuild of
# the login system. Confirmed with Rick: Clark's screen should stay
# invite-only, not grow the waitlist queue into his view too.
INVITER_ADMIN_KEY = os.environ.get("INVITER_ADMIN_KEY", "").strip()
DEFAULT_INVITE_EXPIRY_DAYS = 14

# Waitlist, added 2026-07-19 -- where a notification goes when someone with
# no code (or who can't give but wants in) submits the waitlist form below.
# Rick only, per his own call: no need to also loop in Clark here, distinct
# from the invite-issuing key split above which both of them use. Defaults
# to the address already established elsewhere in the codebase as the
# admin contact (pro_email.py); overridable if Rick wants this routed
# somewhere else without a code change.
FOUNDER_NOTIFICATION_EMAIL = os.environ.get("FOUNDER_NOTIFICATION_EMAIL", "admin@selahexploringtheology.com")

# Free-tier capacity panel, added 2026-07-19 -- the numbers settled in
# 05- Future/Selah_Founder_Dashboard_and_Waitlist_Scope_2026-07-19.md, all
# still starting points with no real usage data behind them yet. Real cost
# basis (exchanges x blended rate), not any Stripe retail price -- see that
# doc's correction. All three env-overridable without a code change once
# real numbers exist to replace the guesses.
FREE_TIER_COST_PER_EXCHANGE = float(os.environ.get("FREE_TIER_COST_PER_EXCHANGE", "0.45"))
FREE_TIER_MONTHLY_BUDGET = float(os.environ.get("FREE_TIER_MONTHLY_BUDGET", "50"))
FREE_TIER_CAPACITY_MARGIN = float(os.environ.get("FREE_TIER_CAPACITY_MARGIN", "0.25"))


def _clear_free_session() -> None:
    for k in ("fg_access_token", "fg_refresh_token", "fg_expires_at",
              "fg_email", "fg_user_id", "fg_organization_id"):
        session.pop(k, None)


def is_free_gate_authenticated() -> bool:
    return bool(session.get("fg_user_id") and session.get("fg_organization_id"))


def current_free_org_id() -> str | None:
    return session.get("fg_organization_id")


def _redirect_to() -> str:
    """Where Supabase should send the user back to after clicking the email
    link -- the callback route specifically, not just '/', so a server-
    readable token_hash (path 1 above) has somewhere to land. Computed from
    the live request rather than hardcoded, so this works the same on a
    Render preview URL and the real domain without an env var."""
    return request.host_url.rstrip("/") + url_for("free_gate.access_callback")


def _complete_signin(user_id: str, email: str, access_token: str,
                      refresh_token: str, expires_at) -> bool:
    """Shared by all three verification paths -- looks up the
    organization_id the handle_new_user() trigger already created, and sets
    the Flask session. Returns False (and leaves no partial session) if the
    profile lookup comes back empty, which shouldn't happen since the
    trigger creates it in the same transaction as the auth.users row, but
    fails cleanly rather than leaving a half-signed-in cookie if it somehow
    does."""
    svc = get_service_client()
    profile = (
        svc.table("profiles")
        .select("organization_id")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    if not profile.data:
        _clear_free_session()
        return False

    session["fg_access_token"] = access_token
    session["fg_refresh_token"] = refresh_token
    session["fg_expires_at"] = expires_at
    session["fg_email"] = email
    session["fg_user_id"] = user_id
    session["fg_organization_id"] = profile.data[0]["organization_id"]
    return True


@free_gate_bp.route("/")
def access_home():
    """Sign-in screen: email + (first-time-only) invite code, then a
    waiting screen while the emailed link is clicked -- shown either
    because the user just submitted their email, or as a fallback if the
    link click (handled client-side, see access.html) didn't complete
    automatically."""
    if is_free_gate_authenticated():
        return render_template("access.html", already_signed_in=True)
    return render_template("access.html", already_signed_in=False)


@free_gate_bp.route("/status")
def access_status():
    """Polled by access.html while someone's waiting on the emailed link.
    Real-world flow, found from Rick's own test: Supabase's email shows a
    link, not a visible code, and clicking it opens a NEW tab (standard
    email-client/browser behavior) -- so the tab where the email was
    requested has no idea the other tab just finished signing in. Session
    cookies are shared across tabs of the same browser, though, so once the
    new tab completes sign-in, this endpoint (hit from the ORIGINAL tab)
    picks that up on the very next poll and lets that tab redirect itself
    too, instead of leaving the person looking at a stale 'waiting' screen
    next to an already-signed-in tab with no explanation."""
    return jsonify({"authenticated": is_free_gate_authenticated()})


@free_gate_bp.route("/callback")
def access_callback():
    """VERIFY-BY-LINK, server-side path. Supabase's token_hash+type
    query-string pattern is fully readable here (unlike the fragment-based
    #access_token= pattern, which never reaches the server) -- if present,
    this completes sign-in with no JS involved at all. If token_hash isn't
    present, this is either the fragment-based flow instead (access.html's
    own JS handles that on load) or a plain visit to this URL -- either way,
    just show the normal sign-in screen rather than erroring."""
    token_hash = request.args.get("token_hash")
    otp_type = request.args.get("type", "email")

    if not token_hash:
        return render_template("access.html", already_signed_in=is_free_gate_authenticated())

    try:
        result = get_supabase().auth.verify_otp({
            "token_hash": token_hash, "type": otp_type,
        })
    except Exception:
        return render_template("access.html", already_signed_in=False, link_error=GENERIC_LINK_ERROR)

    if not result.session or not result.user:
        return render_template("access.html", already_signed_in=False, link_error=GENERIC_LINK_ERROR)

    ok = _complete_signin(
        result.user.id, result.user.email or "",
        result.session.access_token, result.session.refresh_token,
        result.session.expires_at,
    )
    if not ok:
        return render_template("access.html", already_signed_in=False,
                                link_error="Something went wrong setting up your account. Please try again.")
    return redirect(url_for("index"))


@free_gate_bp.route("/session", methods=["POST"])
def access_session():
    """VERIFY-BY-LINK, fragment-based path. access.html's own JS calls this
    on page load if window.location.hash contains access_token= -- the
    older Supabase implicit-grant pattern, where the token comes back after
    a '#' and so is only ever visible to client-side JS, never to this
    server directly. By the time this route runs, the browser has already
    done the only part that needed to happen client-side (reading the
    hash); everything after that is the same server-side session setup as
    every other path."""
    data = request.json or {}
    access_token = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")

    if not access_token or not refresh_token:
        return jsonify({"error": "Missing sign-in tokens."}), 400

    try:
        result = get_supabase().auth.set_session(access_token, refresh_token)
    except Exception:
        return jsonify({"error": GENERIC_LINK_ERROR}), 400

    if not result.session or not result.user:
        return jsonify({"error": GENERIC_LINK_ERROR}), 400

    ok = _complete_signin(
        result.user.id, result.user.email or "",
        result.session.access_token, result.session.refresh_token,
        result.session.expires_at,
    )
    if not ok:
        return jsonify({"error": "Something went wrong setting up your account. Please try again."}), 400
    return jsonify({"ok": True})


@free_gate_bp.route("/request", methods=["POST"])
def access_request():
    """Step 1 of the typed-code path (also what triggers the email for the
    link paths above -- one email, three ways to complete it). Takes email
    (+ invite_code, required only for a brand-new signup -- ignored for a
    returning account since the trigger only runs on first creation). An
    invalid/expired/already-used invite code fails HERE, before any email
    is sent -- the trigger raises inside the same transaction Supabase uses
    to create the auth.users row, which surfaces as an exception from
    sign_in_with_otp() itself."""
    data = request.json or {}
    email = data.get("email", "").strip().lower()
    invite_code = data.get("invite_code", "").strip()
    first_name = data.get("first_name", "").strip()
    last_name = data.get("last_name", "").strip()

    if not email or "@" not in email:
        return jsonify({"error": "Enter a valid email address."}), 400

    try:
        get_supabase().auth.sign_in_with_otp({
            "email": email,
            "options": {
                "should_create_user": True,
                "email_redirect_to": _redirect_to(),
                "data": {
                    "signup_source": SIGNUP_SOURCE,
                    "invite_code": invite_code,
                    "first_name": first_name,
                    "last_name": last_name,
                },
            },
        })
    except Exception as e:
        msg = str(e)
        if "free_tier_invite_wrong_email" in msg:
            return jsonify({"error": WRONG_EMAIL_ERROR}), 400
        if "invalid_or_used_free_tier_invite_code" in msg:
            return jsonify({
                "error": "That invite code isn't valid, has already been used, "
                         "or has expired. Double-check it, or ask whoever "
                         "invited you for a fresh one."
            }), 400
        return jsonify({"error": f"Couldn't send the code: {e}"}), 400

    return jsonify({"ok": True, "email": email})


@free_gate_bp.route("/verify", methods=["POST"])
def access_verify():
    """VERIFY-BY-CODE fallback: email + the 6-digit code from the email."""
    data = request.json or {}
    email = data.get("email", "").strip().lower()
    code = data.get("code", "").strip()

    if not email or not code:
        return jsonify({"error": "Enter the code from your email."}), 400

    try:
        result = get_supabase().auth.verify_otp({
            "email": email, "token": code, "type": "email",
        })
    except Exception:
        return jsonify({"error": GENERIC_CODE_ERROR}), 400

    if not result.session or not result.user:
        return jsonify({"error": GENERIC_CODE_ERROR}), 400

    ok = _complete_signin(
        result.user.id, email,
        result.session.access_token, result.session.refresh_token,
        result.session.expires_at,
    )
    if not ok:
        return jsonify({"error": "Something went wrong setting up your account. Please try again."}), 400
    return jsonify({"ok": True})


@free_gate_bp.route("/logout", methods=["POST"])
def access_logout():
    _clear_free_session()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Waitlist, added 2026-07-19 -- the actual gap this whole day's work traced
# back to: someone with no invite code (access.html) or who wants in but
# can't give (support.html's "Sponsor someone else" card) had no way to
# register that interest at all before this. Public route, no auth --
# whoever's asking may not have an account yet. Writes only via the
# service-role client (see the waitlist_requests migration comment) --
# deliberately no anon INSERT policy, same pattern as org_audit_log and
# congregation_topic_pulse. No "why are you here" field -- Rick, 2026-07-19:
# showing interest is the whole signal, nothing gained by making someone
# categorize it upfront. `source` is captured automatically from which page
# linked here, never asked.
# ---------------------------------------------------------------------------

def _notify_founder_of_waitlist_request(name: str, email: str, note: str, source: str) -> None:
    """Best-effort -- a failed notification email should never fail the
    person's actual submission. Goes to FOUNDER_NOTIFICATION_EMAIL only
    (Rick's call, same session): the waitlist admin view is founder-only
    too, so this matches who can act on it."""
    admin_link = request.host_url.rstrip("/") + url_for("free_gate.admin_home")
    who = f"{name} ({email})" if name else email
    note_html = f"<p><strong>Note:</strong> {note}</p>" if note else ""
    html = f"""
      <p>{who} joined the Selah waitlist, from {source}.</p>
      {note_html}
      <p><a href="{admin_link}">Review the waitlist</a></p>
    """
    send_email(FOUNDER_NOTIFICATION_EMAIL, "New Selah waitlist request", html)


@free_gate_bp.route("/waitlist", methods=["POST"])
def access_waitlist_submit():
    """Public submit -- called from both access.html (no invite code) and
    support.html (wants access, not giving). body: {name, email, note,
    source}, JSON. source is set by the calling page's JS, not user input."""
    data = request.json or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    note = (data.get("note") or "").strip()
    source = data.get("source") or "access"
    if source not in ("access", "support"):
        source = "access"

    if not email or "@" not in email:
        return jsonify({"error": "Enter a valid email address."}), 400

    try:
        get_service_client().table("waitlist_requests").insert({
            "name": name or None,
            "email": email,
            "note": note or None,
            "source": source,
        }).execute()
    except Exception as e:
        print(f"[FREE_GATE] waitlist insert failed for {email!r}: {e}")
        return jsonify({"error": "Something went wrong. Please try again."}), 500

    try:
        _notify_founder_of_waitlist_request(name, email, note, source)
    except Exception as e:
        print(f"[FREE_GATE] waitlist notification failed for {email!r}: {e}")

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Self-release + inactivity nudge, added 2026-07-19. The free tier's
# invite-only gate exists because of a real $50/month budget ceiling (see
# module docstring) -- an invited account that's gone quiet still counts
# against that ceiling indefinitely, with no way today for anyone (the
# account holder or Rick) to give that room back to the next person on the
# waitlist. Two halves: this route (anyone can release their own seat any
# time, no reason needed) and _send_free_tier_inactivity_nudges() in
# pro_scheduler.py (proactively asks someone who's gone quiet whether they
# still want it). Both end the same way -- _release_free_seat() below,
# reusing pro_auth.py's exact delete_account() mechanism (admin
# delete_user(), cascades to profiles/planning_sessions) rather than
# inventing a softer "deactivated" state. Matches Rick's existing call on
# Individual Pro self-delete (2026-07-12): voluntary release is immediate,
# no grace period -- the grace-period cases are the involuntary ones
# (a church's access lapsing), not this.
# ---------------------------------------------------------------------------

def clear_inactivity_flag(user_id: str) -> None:
    """Called from app.py right after a real chat exchange succeeds --
    proof of life. Clears any previously-set nudge flag so a future quiet
    stretch triggers a fresh nudge instead of being permanently silenced by
    one old send. Best-effort: a failure here should never block the chat
    response that triggered it."""
    try:
        get_service_client().table("profiles").update({
            "inactivity_nudge_sent_at": None
        }).eq("id", user_id).execute()
    except Exception as e:
        print(f"[FREE_GATE] failed to clear inactivity flag for {user_id}: {e}")


def _release_free_seat(user_id: str, email: str | None, first_name: str | None = None,
                        auto: bool = False) -> bool:
    """Shared by the self-serve route below and the scheduler's auto-release
    job. Deletes the auth.users row via the admin API -- cascades to
    profiles and planning_sessions the same way pro_auth.py's
    delete_account() does for Individual Pro. Returns True on success;
    caller decides what to tell the user.

    auto=True is the scheduler's involuntary release (60 days inactive, no
    response to the 30-day nudge) -- gets its own email copy, since "thank
    you for giving it back" is wrong when nobody chose anything. Both
    versions end the same way (a fresh invite gets them back in), so
    neither is framed as a door closing."""
    try:
        get_service_client().auth.admin.delete_user(user_id)
    except Exception as e:
        print(f"[FREE_GATE] release failed for {user_id}: {e}")
        return False
    if email:
        greeting = f"Hi {first_name}," if first_name else "Hi,"
        if auto:
            subject = "Your Selah seat was released after 60 days of inactivity"
            body = f"""
              <p>{greeting}</p>
              <p>Your Selah account hadn't been used in 60 days, including the two weeks since we checked in to ask -- so it's been released to free up the spot for someone on the waitlist.</p>
              <p>Want back in? Just ask for a fresh invite any time.</p>
            """
        else:
            subject = "Your Selah seat has been released"
            body = f"""
              <p>{greeting}</p>
              <p>Your Selah account has been released, and the spot's been freed up for the next person waiting for an invite. Thank you for giving it back rather than letting it sit idle.</p>
              <p>Want back in later? Just ask for a fresh invite any time.</p>
            """
        send_email(email, subject, body)
    return True


def send_free_tier_inactivity_nudge_email(email: str, sign_in_link: str, first_name: str | None = None) -> bool:
    """Called from pro_scheduler.py's daily job, not from a live request --
    kept here rather than pro_email.py since every other free-tier email
    already lives in this file (the invite email above), not in that
    module's Ministry-branded helpers. Deliberately does not delete or
    release anything itself -- just asks, and links to sign-in rather than
    straight to /access/release, so nothing destructive ever happens from
    an email click alone (a scanning/prefetching email client hitting a
    live one-click delete link is a real, known failure mode -- this makes
    that impossible by construction). States the 60-day auto-release
    plainly rather than leaving it a silent surprise -- consistent with the
    project's own honest-communication standard elsewhere (no fine-print
    surprises)."""
    greeting = f"Hi {first_name}," if first_name else "Hi,"
    subject = "Still using Selah?"
    html = f"""
      <p>{greeting}</p>
      <p>It's been 30 days since you've used Selah's free tool. No pressure -- but if you're not using it right now, releasing your spot opens it up for someone waiting for an invite. If we don't hear anything, an inactive seat is released automatically after 60 days total, so this doesn't just sit as a silent deadline.</p>
      <p><a href="{sign_in_link}">Sign in</a> to keep going, or release your seat from there if you're done with it. Signing in and using Selah at all resets this.</p>
    """
    return send_email(email, subject, html)


@free_gate_bp.route("/release", methods=["POST"])
def access_release():
    """Self-serve, immediate, no reason required -- signed-in free-tier
    users only (checks the fg_* session, same gate as the chat routes in
    app.py). No confirmation step server-side; access.html's own JS asks
    via a plain confirm() dialog before this ever gets hit, same lightweight
    pattern as everything else on that page."""
    if not is_free_gate_authenticated():
        return redirect(url_for("free_gate.access_home"))

    user_id = session.get("fg_user_id")
    email = session.get("fg_email")
    ok = _release_free_seat(user_id, email)
    _clear_free_session()
    if not ok:
        return render_template("access.html", already_signed_in=False,
                                link_error="Something went wrong releasing your seat. Please try again.")
    return render_template("access.html", already_signed_in=False,
                            link_error=None, released=True)


# ---------------------------------------------------------------------------
# Invite-issuing admin page, added 2026-07-17 (later same day as the gate
# itself), prompted directly by Rick catching a real gap: the original 25
# seeded codes (see Selah_Free_Tier_Invite_Codes_2026-07-17.md) prove
# possession of a code, not that the person using it is who it was meant
# for -- invited_email existed as a column on free_tier_invites but was
# never set or checked. The migration applied alongside this (see
# free_tier_invite_email_match) makes the DB trigger enforce that match
# when invited_email IS set; codes issued from here always set it. The
# original 25 stay as open/general codes (invited_email null), unaffected.
# ---------------------------------------------------------------------------

def _admin_role() -> str | None:
    role = session.get("fg_admin_role")
    return role if role in ("founder", "inviter") else None


def _is_admin() -> bool:
    """True for either role -- both can reach this page and issue invites."""
    return bool(_admin_role())


def _is_founder() -> bool:
    """True only for Rick's key. Gates the waitlist section below -- the
    first thing actually built behind this check, per the plan noted when
    _is_founder() was added: additions to this page stay founder-only from
    day one rather than needing a follow-up change to the login system."""
    return _admin_role() == "founder"


def _free_tier_capacity_snapshot() -> dict:
    """Founder-only panel, added 2026-07-19 -- the number the scoping doc
    calls "the dashboard's most useful piece." Computed fresh on every
    /access/admin load rather than cached/scheduled -- the query is cheap
    at today's account volume, and "recalculated weekly" from the doc is
    satisfied trivially by always being current, no separate job needed
    unless this ever gets expensive enough to matter.

    "Active account" = a free-tier org with at least one exchange recorded
    THIS billing month (conversations_used > 0) -- there's no daily-grain
    activity data to do a true trailing-30-day window against (usage_records
    is billing_month, i.e. calendar-month, granularity), so this is
    calendar-month-to-date, not a rolling 30 days. Close enough to the
    doc's intent without inventing data that doesn't exist; noted here so
    the approximation is a documented choice, not a silent one.

    avg_cost_per_active_account falls back to the worst-case per-account
    figure (30 exchanges x blended rate) when there's no active-account
    data yet to average -- otherwise a brand-new month with zero usage so
    far would divide by zero and imply infinite headroom, which is wrong
    in the opposite direction from the worst-case-only math this whole
    mechanism replaced."""
    try:
        svc = get_service_client()
        billing_month = _billing_month_today()

        free_orgs = svc.table("subscriptions").select("organization_id").eq("tier_slug", "free").execute()
        org_ids = [r["organization_id"] for r in (free_orgs.data or [])]
        total_accounts = len(org_ids)

        if not org_ids:
            return {
                "total_accounts": 0, "active_accounts": 0, "spend_this_month": 0.0,
                "budget": FREE_TIER_MONTHLY_BUDGET, "remaining_budget": FREE_TIER_MONTHLY_BUDGET,
                "avg_cost_per_active_account": 0.0, "implied_capacity": 0,
            }

        usage = (
            svc.table("usage_records")
            .select("organization_id, conversations_used")
            .in_("organization_id", org_ids)
            .eq("billing_month", billing_month)
            .is_("module_slug", "null")
            .execute()
        )
        rows = usage.data or []
        active_rows = [r for r in rows if (r.get("conversations_used") or 0) > 0]
        total_exchanges = sum((r.get("conversations_used") or 0) for r in rows)
        spend_this_month = round(total_exchanges * FREE_TIER_COST_PER_EXCHANGE, 2)
        active_accounts = len(active_rows)

        worst_case_fallback = FREE_TIER_COST_PER_EXCHANGE * 30
        avg_cost = (spend_this_month / active_accounts) if active_accounts else worst_case_fallback
        remaining_budget = max(0.0, FREE_TIER_MONTHLY_BUDGET - spend_this_month)
        implied_capacity_raw = (remaining_budget / avg_cost) if avg_cost > 0 else 0
        implied_capacity = max(0, int(implied_capacity_raw * (1 - FREE_TIER_CAPACITY_MARGIN)))

        return {
            "total_accounts": total_accounts,
            "active_accounts": active_accounts,
            "spend_this_month": spend_this_month,
            "budget": FREE_TIER_MONTHLY_BUDGET,
            "remaining_budget": round(remaining_budget, 2),
            "avg_cost_per_active_account": round(avg_cost, 2),
            "implied_capacity": implied_capacity,
        }
    except Exception as e:
        print(f"[FREE_GATE] capacity snapshot failed: {e}")
        return None


_STATUS_SEVERITY = {"canceled": 3, "past_due": 2, "trialing": 1, "active": 0}

# Small, known-disposable-email domains, seeded from what actually showed
# up in a live Supabase auth-log check earlier this session (besttempmail,
# justdefinition, inbox.eu) plus a few of the most common general ones.
# Not exhaustive -- a determined signup can always find a domain not on
# this list -- but a cheap, real signal with zero false-positive risk: a
# domain either is one of these or it isn't.
_DISPOSABLE_EMAIL_DOMAINS = {
    "besttempmail.com", "justdefinition.com", "inbox.eu", "mailinator.com",
    "guerrillamail.com", "10minutemail.com", "temp-mail.org", "throwawaymail.com",
    "yopmail.com", "sharklasers.com", "trashmail.com", "getnada.com",
    "dispostable.com", "fakeinbox.com", "tempmailo.com", "maildrop.cc",
    # Added 2026-07-20 -- the fabricated one-off domains from that day's
    # scripted signup wave (see SESSION_LOG.md). Not on any public
    # disposable-domain list; these are exactly the kind of domain a bot
    # rotates through that a static list will always lag behind -- see the
    # audit doc's entropy-check recommendation for a more durable fix.
    "immenseignite.info", "analismail.com", "mailtb.com",
}


def _parse_ts_local(value) -> datetime:
    """Same tolerant ISO-8601 parse pro_scheduler.py's _parse_ts() uses --
    duplicated here rather than imported since it's a two-line utility, not
    worth a cross-module dependency for."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _free_tier_invite_queue_snapshot() -> dict:
    """Founder-only, read-only, added 2026-07-19 -- the doc's "outstanding
    vs. used" invite view. free_tier_invites rows never disappear once
    created, so without this, a batch of invites seeded or sent months ago
    and never redeemed is invisible -- nothing surfaces that they're just
    sitting there. `stale` = outstanding (used_at IS NULL) AND past its own
    expires_at -- these can never be redeemed anymore (the DB trigger
    enforces that, see handle_new_user()), so they're pure noise, worth
    knowing about but not actionable the way a still-valid outstanding
    invite is."""
    try:
        resp = (
            get_service_client()
            .table("free_tier_invites")
            .select("token, invited_email, used_at, created_at, expires_at")
            .order("created_at", desc=True)
            .execute()
        )
        rows = resp.data or []
        now = datetime.now(timezone.utc)
        outstanding = []
        used_count = 0
        for r in rows:
            if r.get("used_at"):
                used_count += 1
                continue
            expires_at = r.get("expires_at")
            is_expired = False
            if expires_at:
                try:
                    is_expired = _parse_ts_local(expires_at) < now
                except Exception:
                    is_expired = False
            outstanding.append({
                "token": r["token"],
                "invited_email": r.get("invited_email"),
                "created_at": r.get("created_at"),
                "expires_at": expires_at,
                "expired": is_expired,
            })
        return {
            "outstanding": outstanding,
            "outstanding_count": len(outstanding),
            "stale_count": sum(1 for o in outstanding if o["expired"]),
            "used_count": used_count,
        }
    except Exception as e:
        print(f"[FREE_GATE] invite queue snapshot failed: {e}")
        return None


def _flagged_signups_snapshot(days: int = 14) -> list:
    """Founder-only, read-only, added 2026-07-19 -- a real but partial
    build of the scoping doc's "flagged signups" item. Only the disposable-
    email-domain half is actually possible from here: the other half
    (repeat failed logins, the pattern spotted in the auth-log check
    earlier this session) lives only in Supabase's platform-level auth
    logs, not in any table this app's own Postgres client can query --
    checked directly: auth.audit_log_entries exists but has zero rows on
    this project, so it isn't a usable source today. Doing the failed-
    login half for real would mean either a Supabase Management API
    integration (a separate credential, a real build) or this app logging
    its own failed attempts going forward -- neither done here. This flags
    signups from known disposable domains in the last `days` days; nothing
    more. Labeled honestly in the UI rather than presented as the whole
    "flagged signups" feature the doc described."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        resp = (
            get_service_client()
            .table("profiles")
            .select("email, first_name, last_name, created_at")
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .execute()
        )
        flagged = []
        for p in (resp.data or []):
            email = (p.get("email") or "").lower()
            domain = email.rsplit("@", 1)[-1] if "@" in email else ""
            if domain in _DISPOSABLE_EMAIL_DOMAINS:
                name = " ".join(filter(None, [p.get("first_name"), p.get("last_name")])) or None
                flagged.append({"email": p.get("email"), "name": name, "created_at": p.get("created_at"), "domain": domain})
        return flagged
    except Exception as e:
        print(f"[FREE_GATE] flagged signups snapshot failed: {e}")
        return []


def _church_org_activity_snapshot() -> list:
    """Founder-only, read-only, added 2026-07-19, collapsed to one row per
    org the same day after Rick's own live look at it -- the first version
    listed one row per subscription record, which meant three rows for a
    single church in practice: the two real seat pools (Leadership,
    Membership -- Section 17) PLUS a leftover base 'church'-tier_slug
    subscription row with no seat_type and no seat_quantity, a pre-seat-
    split legacy record that's still sitting in the table. Three-lines-per-
    church doesn't scale once there are many churches, per Rick, so this
    now returns one dict per organization with every subscription row
    nested under `pools` (still there, nothing lost) and an `overall_status`
    computed from the worst status across them -- canceled beats past_due
    beats trialing beats active -- so a single glance shows whether
    anything about this org needs attention, with the per-pool breakdown
    available on demand rather than forced onto the page at all times.

    org_type != 'individual' excludes free-tier and solo Individual Pro
    accounts -- this view is specifically the multi-seat Church/Seminary/
    Berea side, not every organizations row. "Occupied" is
    profiles.seat_status IN ('paid','comped') -- a real, filled seat, not
    seat_quantity itself (purchased capacity, can run ahead of the roster)
    -- same definition pro_billing.py already uses elsewhere."""
    try:
        svc = get_service_client()
        orgs = (
            svc.table("organizations")
            .select("id, name, org_type, created_at")
            .neq("org_type", "individual")
            .execute()
        )
        org_rows = orgs.data or []
        if not org_rows:
            return []
        org_ids = [o["id"] for o in org_rows]

        subs = (
            svc.table("subscriptions")
            .select("organization_id, tier_slug, status, seat_quantity, seat_type, cancel_at_period_end")
            .in_("organization_id", org_ids)
            .execute()
        )

        profiles = (
            svc.table("profiles")
            .select("organization_id, seat_type")
            .in_("organization_id", org_ids)
            .in_("seat_status", ["paid", "comped"])
            .execute()
        )
        occupied_counts: dict = {}
        for p in (profiles.data or []):
            key = (p["organization_id"], p.get("seat_type"))
            occupied_counts[key] = occupied_counts.get(key, 0) + 1

        pools_by_org: dict = {}
        for s in (subs.data or []):
            pools_by_org.setdefault(s["organization_id"], []).append({
                "tier_slug": s["tier_slug"],
                "seat_type": s.get("seat_type"),
                "seats_purchased": s.get("seat_quantity"),
                "seats_occupied": occupied_counts.get((s["organization_id"], s.get("seat_type")), 0),
                "status": s["status"],
                "cancel_at_period_end": s.get("cancel_at_period_end"),
            })

        result = []
        for org in org_rows:
            pools = pools_by_org.get(org["id"], [])
            seat_pools = [p for p in pools if p["seat_type"]]  # excludes the bare legacy row
            overall_status = "active"
            any_cancel_flag = False
            for p in pools:
                if _STATUS_SEVERITY.get(p["status"], 0) > _STATUS_SEVERITY.get(overall_status, 0):
                    overall_status = p["status"]
                if p.get("cancel_at_period_end"):
                    any_cancel_flag = True
            result.append({
                "org_name": org.get("name") or "(unnamed)",
                "org_type": org["org_type"],
                "created_at": org["created_at"],
                "seat_pools": seat_pools,
                "pools": pools,
                "overall_status": overall_status,
                "cancel_at_period_end": any_cancel_flag,
            })
        result.sort(key=lambda r: r["org_name"])
        return result
    except Exception as e:
        print(f"[FREE_GATE] church/org activity snapshot failed: {e}")
        return []


def _pending_waitlist() -> list:
    """Oldest-first, founder-only. Called from admin_home() and from the
    invite/decline actions below (so the list re-renders current after
    either action) -- kept as its own function rather than inlined so both
    call sites can't drift out of sync on ordering/filtering."""
    try:
        resp = (
            get_service_client()
            .table("waitlist_requests")
            .select("id, created_at, name, email, source, note")
            .eq("status", "pending")
            .order("created_at")
            .execute()
        )
        return resp.data or []
    except Exception as e:
        print(f"[FREE_GATE] failed to load waitlist: {e}")
        return []


def _founder_admin_context() -> dict:
    """Bundles everything founder-only on /access/admin (waitlist queue,
    capacity snapshot) into one dict every render_template call on this
    page spreads in with **_founder_admin_context() -- added so the four
    call sites (admin_home, admin_invite, admin_waitlist_invite,
    admin_waitlist_decline) can't drift out of sync on what a founder
    reload actually shows. Returns Clark-safe empty values when not a
    founder, so callers never need their own is_founder branch."""
    is_founder = _is_founder()
    return {
        "is_founder": is_founder,
        "waitlist": _pending_waitlist() if is_founder else [],
        "capacity": _free_tier_capacity_snapshot() if is_founder else None,
        "church_orgs": _church_org_activity_snapshot() if is_founder else [],
        "invite_queue": _free_tier_invite_queue_snapshot() if is_founder else None,
        "flagged_signups": _flagged_signups_snapshot() if is_founder else [],
    }


@free_gate_bp.route("/admin", methods=["GET"])
def admin_home():
    """Single page, two states: a one-time shared-secret login (no per-
    person admin accounts exist for the free tier -- this is just Rick/
    Clark personally issuing invites, a shared key is the right amount of
    gate for that), then the actual invite form once unlocked. Two keys
    map to two roles (see INVITER_ADMIN_KEY above) -- the waitlist section
    and capacity panel are the things that actually differ between them:
    founder-only, Clark's screen stays the invite form alone."""
    return render_template("free_admin.html", logged_in=_is_admin(), **_founder_admin_context())


@free_gate_bp.route("/admin/login", methods=["POST"])
def admin_login():
    key = (request.form.get("key") or "").strip()
    if not FREE_TIER_ADMIN_KEY:
        return render_template("free_admin.html", logged_in=False,
                                error="FREE_TIER_ADMIN_KEY isn't set on the server -- nothing to check against.")
    if key and key == FREE_TIER_ADMIN_KEY:
        session["fg_admin_role"] = "founder"
        return redirect(url_for("free_gate.admin_home"))
    if key and INVITER_ADMIN_KEY and key == INVITER_ADMIN_KEY:
        session["fg_admin_role"] = "inviter"
        return redirect(url_for("free_gate.admin_home"))
    return render_template("free_admin.html", logged_in=False, error="Wrong key.")


@free_gate_bp.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("fg_admin_role", None)
    return redirect(url_for("free_gate.admin_home"))


def _create_and_send_invite(name: str, email: str) -> dict:
    """The actual invite-creation logic, factored out 2026-07-19 so the
    waitlist's "Send invite" action (below) can reuse it exactly rather
    than duplicating it -- one place that creates a free_tier_invites row
    and emails it, whether triggered from the manual form or from a
    waitlist row."""
    token = "SELAH-" + secrets.token_hex(3).upper()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=DEFAULT_INVITE_EXPIRY_DAYS)).isoformat()

    svc = get_service_client()
    svc.table("free_tier_invites").insert({
        "token": token,
        "invited_email": email,
        "expires_at": expires_at,
    }).execute()

    invite_link = (request.host_url.rstrip("/") + url_for("free_gate.access_home")
                   + "?" + urlencode({"email": email, "code": token}))
    greeting = f"Hi {name}," if name else "Hi,"
    html = f"""
      <p>{greeting}</p>
      <p>You've been invited to try Selah's free exploration tool. <strong style="color:#b45309;">This invite expires in {DEFAULT_INVITE_EXPIRY_DAYS} days</strong> -- don't wait too long to claim it.</p>
      <p><a href="{invite_link}" style="font-weight:600;">Click here to get started</a> -- it'll have your email and invite code
      ({token}) already filled in. First time only; after that you'll just sign in with your email.</p>
    """
    sent = send_email(email, f"You're invited to Selah -- expires in {DEFAULT_INVITE_EXPIRY_DAYS} days", html)
    return {"email": email, "token": token, "link": invite_link, "sent": sent}


@free_gate_bp.route("/admin/invite", methods=["POST"])
def admin_invite():
    """Creates one email-scoped invite and emails it directly -- Rick's own
    part is just a name and an email, nothing to relay by hand. Falls back
    to showing the code/link on-screen if the email send fails (Resend
    down, bad address typo, etc.) so the invite isn't silently lost."""
    if not _is_admin():
        return redirect(url_for("free_gate.admin_home"))

    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()

    if not email or "@" not in email:
        return render_template("free_admin.html", logged_in=True,
                                error="Enter a valid email address.", **_founder_admin_context())

    result = _create_and_send_invite(name, email)

    return render_template("free_admin.html", logged_in=True, result=result, **_founder_admin_context())


@free_gate_bp.route("/admin/waitlist/invite", methods=["POST"])
def admin_waitlist_invite():
    """Founder-only -- sends a real invite to a waitlisted person (reuses
    _create_and_send_invite(), the exact same path as the manual form
    above) and marks their row resolved. Clark can't reach this even with
    a valid session cookie -- checked server-side, not just hidden in the
    template, since a POST endpoint is reachable directly regardless of
    what the UI shows."""
    if not _is_founder():
        return redirect(url_for("free_gate.admin_home"))

    row_id = (request.form.get("id") or "").strip()
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    if not row_id or not email:
        return redirect(url_for("free_gate.admin_home"))

    result = _create_and_send_invite(name, email)

    svc = get_service_client()
    svc.table("waitlist_requests").update({
        "status": "invited",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", row_id).execute()

    return render_template("free_admin.html", logged_in=True, result=result, **_founder_admin_context())


@free_gate_bp.route("/admin/waitlist/decline", methods=["POST"])
def admin_waitlist_decline():
    """Founder-only -- clears a row (spam, duplicate, changed their mind)
    without sending an invite. No email sent either direction; someone who
    submitted a name/email to a waitlist form isn't owed a rejection notice
    the same way a real applicant would be."""
    if not _is_founder():
        return redirect(url_for("free_gate.admin_home"))

    row_id = (request.form.get("id") or "").strip()
    if row_id:
        get_service_client().table("waitlist_requests").update({
            "status": "declined",
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", row_id).execute()

    return render_template("free_admin.html", logged_in=True, **_founder_admin_context())
