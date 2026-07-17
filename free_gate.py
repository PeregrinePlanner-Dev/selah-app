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

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for

from pro_auth import get_supabase, get_service_client

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
