"""Selah free tool -- mandatory sign-in gate, added 2026-07-17.

Decisions this implements (see 05- Future/Selah_Decisions_2026-07-17.md and
Selah_Session_Summary_2026-07-17.md): the free tool moves from fully
anonymous to email-verified accounts, invite-only for now, 30
exchanges/account/month, on a $50/month budget ceiling. Confirmed by Rick:
"if we're going to cap, we need mandatory sign in with email verification."

Code-based OTP, not a clickable magic link -- deliberate substitution, not a
silent deviation from "magic-link" wording. A true clickable magic link
returns its token in the URL fragment (#access_token=...), which a
traditional server-rendered Flask route (no client-side Supabase SDK on this
page) cannot read -- the fragment never reaches the server. The 6-digit
email OTP code is the same passwordless-email-verification mechanism in
spirit (Supabase sends it via the same underlying flow), just typed into a
form instead of clicked, and is the reliable choice for this app's
architecture.

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
from datetime import datetime, timezone

from flask import Blueprint, render_template, request, jsonify, session

from pro_auth import get_supabase, get_service_client

free_gate_bp = Blueprint("free_gate", __name__, url_prefix="/access")

SIGNUP_SOURCE = "free_gate"


def _clear_free_session() -> None:
    for k in ("fg_access_token", "fg_refresh_token", "fg_expires_at",
              "fg_email", "fg_user_id", "fg_organization_id"):
        session.pop(k, None)


def is_free_gate_authenticated() -> bool:
    return bool(session.get("fg_user_id") and session.get("fg_organization_id"))


def current_free_org_id() -> str | None:
    return session.get("fg_organization_id")


@free_gate_bp.route("/")
def access_home():
    """Sign-in screen: email + (first-time-only) invite code, then a second
    step to enter the emailed code. Both steps rendered by the same template,
    client-side JS switches which panel shows -- no separate route needed for
    the two-step flow."""
    if is_free_gate_authenticated():
        return jsonify({"already_signed_in": True}), 200 if request.args.get("format") == "json" else render_template("access.html", already_signed_in=True)
    return render_template("access.html", already_signed_in=False)


@free_gate_bp.route("/request", methods=["POST"])
def access_request():
    """Step 1: takes email (+ invite_code, required only for a brand-new
    signup -- ignored for a returning account since the trigger only runs on
    first creation). Sends the OTP email. An invalid/expired/already-used
    invite code fails HERE, before any email is sent -- the trigger raises
    inside the same transaction Supabase uses to create the auth.users row,
    which surfaces as an exception from sign_in_with_otp() itself."""
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
    """Step 2: takes email + the 6-digit code from the email, completes
    sign-in, and looks up the organization_id the trigger already created so
    every later request can go straight to the cap-check without a lookup."""
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
        return jsonify({"error": "That code didn't work -- it may be wrong or expired. Try again or request a new one."}), 400

    if not result.session or not result.user:
        return jsonify({"error": "That code didn't work -- it may be wrong or expired. Try again or request a new one."}), 400

    svc = get_service_client()
    profile = (
        svc.table("profiles")
        .select("organization_id")
        .eq("id", result.user.id)
        .limit(1)
        .execute()
    )
    if not profile.data:
        # Shouldn't happen -- the trigger creates this row in the same
        # transaction as the auth.users insert -- but fail cleanly rather
        # than leaving a half-signed-in session if it somehow does.
        _clear_free_session()
        return jsonify({"error": "Something went wrong setting up your account. Please try again."}), 400

    session["fg_access_token"] = result.session.access_token
    session["fg_refresh_token"] = result.session.refresh_token
    session["fg_expires_at"] = result.session.expires_at
    session["fg_email"] = email
    session["fg_user_id"] = result.user.id
    session["fg_organization_id"] = profile.data[0]["organization_id"]

    return jsonify({"ok": True})


@free_gate_bp.route("/logout", methods=["POST"])
def access_logout():
    _clear_free_session()
    return jsonify({"ok": True})
