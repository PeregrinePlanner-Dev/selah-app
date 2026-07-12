"""Selah for Ministry — Pro auth blueprint.

Kept as a separate module, registered onto the existing Flask app, rather than
folded into app.py directly -- the free tool's routes (/, /chat, /export,
/upload_session) must stay completely untouched by this work. Everything
Pro-specific lives here and is additive only.

Session handling: on successful login/signup, the Supabase session's access
token and refresh token are stored in Flask's signed session cookie (requires
app.secret_key to be set in app.py). This is a lightweight MVP pattern --
tokens live in a signed-but-not-encrypted cookie -- adequate for this phase
since RLS is the real security boundary (a stolen cookie only grants access
Supabase's own policies already allow that user), but worth revisiting if
Pro ever handles more sensitive data than it does today.
"""

import os
import time
from functools import wraps

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from supabase import create_client, Client

pro_bp = Blueprint("pro", __name__, url_prefix="/pro")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

_supabase_client: Client | None = None
_service_client: Client | None = None


def get_service_client() -> Client:
    """Client authenticated with the service role key -- bypasses RLS
    entirely. Deliberately kept separate from get_user_supabase() and used
    ONLY for usage_records (the cap-enforcement table): a user's own
    RLS-scoped token must never be able to read-then-write its own usage
    counter, the same way the free tool's anonymous rate limiter isn't
    something a client request can reset. Never expose this key to a
    browser -- it belongs in a server-side env var only."""
    global _service_client
    if _service_client is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set -- the "
                "usage-cap gate cannot function without them. Set both as "
                "environment variables (see .env.example). Get the service "
                "role key from the Supabase dashboard: Settings -> API."
            )
        _service_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _service_client


def get_supabase() -> Client:
    """Lazy singleton -- avoids constructing a client at import time if env
    vars aren't set yet (e.g. during local dev without a .env filled in)."""
    global _supabase_client
    if _supabase_client is None:
        if not SUPABASE_URL or not SUPABASE_ANON_KEY:
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_ANON_KEY not set -- Pro auth routes "
                "cannot function without them. Set both as environment "
                "variables (see .env.example)."
            )
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    return _supabase_client


def login_required(view):
    """Route decorator -- redirects to /pro (login screen) if no valid
    session is present. Does not itself re-verify the token against Supabase
    on every request in this first pass -- that's a known simplification,
    noted for the next hardening pass once this is live."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("sb_access_token"):
            return redirect(url_for("pro.pro_home"))
        return view(*args, **kwargs)
    return wrapped


@pro_bp.route("/")
def pro_home():
    """Login/signup screen if not authenticated; otherwise straight into the
    real Selah for Ministry chat UI. Updated 2026-07-07 -- pro_app.html now
    exists (pro_chat.pro_app), so the earlier placeholder landing spot is no
    longer needed."""
    if session.get("sb_access_token"):
        return redirect(url_for("pro_chat.pro_app"))
    return render_template(
        "pro_login.html",
        error=request.args.get("error", ""),
        notice=request.args.get("notice", ""),
    )


# Maps handle_new_user()'s profiles.invite_resolution values (set only when
# an invite_code was submitted but didn't resolve -- bad/expired/used/wrong
# email) to a plain-language message. Section 11's explicit correction: a
# failed invite must never silently land someone on an ordinary signup
# without telling them -- this is that message.
_INVITE_RESOLUTION_MESSAGES = {
    "invalid_code": "That invite link/code isn't recognized -- your account was created as a regular signup instead. Double-check the link with your church, or continue on your own.",
    "expired": "That invite link/code has expired -- your account was created as a regular signup instead. Ask your church for a fresh one, or continue on your own.",
    "already_used": "That invite link has already been used by someone else -- Leadership invites are single-use. Your account was created as a regular signup instead; ask your church admin for a new invite.",
    "wrong_email": "That invite was issued to a different email address -- Leadership invites are tied to one specific person. Your account was created as a regular signup instead; ask your church admin to reissue it to this email.",
}


@pro_bp.route("/signup", methods=["POST"])
def signup():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    # Rick's call, 2026-07-12: a church admin managing a roster doesn't
    # necessarily know everyone's email offhand -- a real name is what
    # they actually recognize people by. Split first/last (not one
    # full_name field, an earlier same-session pass corrected) -- lets the
    # roster sort alphabetically by surname and lets future transactional
    # email personalize with a first name alone. Required server-side, not
    # just the HTML form's `required` attribute, which a direct POST could
    # skip.
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    # Present on Church/Org invite links (both Leadership single-use and
    # Membership shared-code) -- carried through Supabase Auth's signup
    # metadata into raw_user_meta_data, read by the handle_new_user()
    # trigger, which does the actual (race-safe) resolution. Blank/absent
    # for an ordinary signup, same as today.
    invite_code = request.form.get("invite_code", "").strip() or None

    if not email or not password:
        return redirect(url_for("pro.pro_home", error="Email and password are required."))
    if not first_name or not last_name:
        return redirect(url_for("pro.pro_home", error="First and last name are required."))

    try:
        signup_kwargs = {"email": email, "password": password}
        if invite_code:
            signup_kwargs["options"] = {"data": {"invite_code": invite_code}}
        result = get_supabase().auth.sign_up(signup_kwargs)
    except Exception as e:
        return redirect(url_for("pro.pro_home", error=f"Signup failed: {e}"))

    # If email confirmation is required (Supabase default), there may be no
    # session yet -- send the user back with a clear message rather than
    # silently failing to log them in. Note: an invite_resolution problem or
    # a pending/waitlisted seat can't be surfaced here in that case either,
    # since there's no session yet to read the profile with -- it'll show
    # the first time they actually log in instead (see login() below).
    if result.session is None:
        return redirect(url_for(
            "pro.pro_home",
            error="Account created -- check your email to confirm before logging in."
        ))

    session["sb_access_token"] = result.session.access_token
    session["sb_refresh_token"] = result.session.refresh_token
    session["sb_expires_at"] = result.session.expires_at
    session["sb_email"] = email
    session["sb_user_id"] = result.user.id

    # Save the name onto the profile handle_new_user() just created --
    # simplest to do here as a direct update via the service client rather
    # than threading it through Supabase Auth's signup metadata and having
    # the DB trigger set it (the pattern invite_code already uses), since
    # that would mean editing the trigger function itself for two extra
    # columns. Best-effort: never let this be the reason signup itself
    # fails, worst case the profile just has no name yet.
    try:
        get_service_client().table("profiles").update({
            "first_name": first_name,
            "last_name": last_name,
        }).eq("id", result.user.id).execute()
    except Exception:
        pass

    # Tell them plainly if the invite they used didn't actually work, or if
    # it worked but the pool was full (pending/waitlisted) -- never let
    # either of those pass silently. Read via the service client since RLS
    # for a brand-new profile under a church org isn't guaranteed to be
    # self-readable in every seat_status case, and this check should never
    # itself be the thing that breaks signup.
    try:
        svc = get_service_client()
        prof = (
            svc.table("profiles")
            .select("invite_resolution, seat_status, seat_type")
            .eq("id", result.user.id)
            .limit(1)
            .execute()
        )
        if prof.data:
            row = prof.data[0]
            if row.get("invite_resolution") in _INVITE_RESOLUTION_MESSAGES:
                return redirect(url_for(
                    "pro_chat.pro_app",
                    notice=_INVITE_RESOLUTION_MESSAGES[row["invite_resolution"]],
                ))
            if row.get("seat_status") == "pending":
                kind = "Leadership" if row.get("seat_type") == "leader" else "Membership"
                return redirect(url_for(
                    "pro_chat.pro_app",
                    notice=f"Your church's {kind} seats are all full right now -- you're on the waitlist and will get access automatically as soon as a seat opens.",
                ))
    except Exception:
        # Never let this status check be the reason signup itself fails --
        # worst case the person just doesn't see the notice.
        pass

    return redirect(url_for("pro.pro_home"))


@pro_bp.route("/login", methods=["POST"])
def login():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    if not email or not password:
        return redirect(url_for("pro.pro_home", error="Email and password are required."))

    try:
        result = get_supabase().auth.sign_in_with_password({"email": email, "password": password})
    except Exception as e:
        return redirect(url_for("pro.pro_home", error="Login failed -- check your email and password."))

    session["sb_access_token"] = result.session.access_token
    session["sb_refresh_token"] = result.session.refresh_token
    session["sb_expires_at"] = result.session.expires_at
    session["sb_email"] = email
    session["sb_user_id"] = result.user.id
    return redirect(url_for("pro.pro_home"))


@pro_bp.route("/logout", methods=["POST"])
def logout():
    session.pop("sb_access_token", None)
    session.pop("sb_refresh_token", None)
    session.pop("sb_expires_at", None)
    session.pop("sb_email", None)
    session.pop("sb_user_id", None)
    return redirect(url_for("pro.pro_home"))


@pro_bp.route("/account/delete", methods=["POST"])
@login_required
def delete_account():
    """Self-serve, immediate, permanent account deletion -- no grace period
    (Rick's explicit call, 2026-07-12: voluntary self-delete is immediate;
    the 14-day trial-transition window only applies to the two INVOLUNTARY
    cases -- a church's access lapsing or a single person being removed
    from a roster -- not to someone deleting their own account by choice).

    Deletes the auth.users row via the admin API, which cascades to
    profiles (ON DELETE CASCADE, pre-existing) and planning_sessions'
    conversation history (ON DELETE CASCADE, added 2026-07-12 specifically
    to make this route's promise real -- see the DB migration notes). This
    is the technical backing for legal.html's existing "we may... delete
    the associated data" language, which had no mechanism behind it before
    today.

    Does NOT touch the organizations/subscriptions/usage_records rows for
    any org this person belonged to -- deleting one member must never
    affect a shared church org's data for everyone else still on it. An
    orphaned solo individual's own org row is left in place too (harmless,
    near-zero cost) rather than added complexity for a same-day build --
    real cleanup of those can be a periodic job later, not blocking this."""
    user_id = session.get("sb_user_id")
    if not user_id:
        return redirect(url_for("pro.pro_home", error="Not logged in."))

    try:
        get_service_client().auth.admin.delete_user(user_id)
    except Exception as e:
        return redirect(url_for("pro_chat.pro_app", error=f"Could not delete account: {e}"))

    session.pop("sb_access_token", None)
    session.pop("sb_refresh_token", None)
    session.pop("sb_expires_at", None)
    session.pop("sb_email", None)
    session.pop("sb_user_id", None)
    return redirect(url_for("pro.pro_home", notice="Your account and all associated data have been permanently deleted."))


# Refresh the access token this many seconds before its actual expiry, so a
# request that's mid-flight when the token would otherwise lapse still gets a
# valid one. Supabase access tokens default to a 1-hour lifetime -- without
# this, any Pro session left open past that hour starts getting silent 401s
# from PostgREST on every subsequent request (profiles, planning_sessions,
# usage_records alike), which show up to the user as a generic "something
# went wrong" since nothing here was catching or refreshing on expiry. Found
# 2026-07-08 via a live Supabase API log check after a Teaching Outline
# generation failed -- the request never got past a 401'd /rest/v1/profiles
# call, well after this same session's chat turns had worked fine earlier.
_TOKEN_REFRESH_BUFFER_SECONDS = 60


def _ensure_fresh_access_token() -> None:
    """Proactively refreshes session['sb_access_token'] using the stored
    refresh token if it's expired, about to be, or unknown.

    Session cookies created before 'sb_expires_at' existed won't have it set
    at all -- originally this treated "missing" as "leave it alone," on the
    assumption those sessions would simply re-authenticate on their next
    login. In practice that meant anyone ALREADY logged in when this fix
    deployed stayed stuck getting 401s indefinitely, since nothing about
    their existing cookie ever changes without a fresh login -- confirmed
    2026-07-08 via Supabase API logs showing continued 401s on
    planning_sessions with zero refresh-token grant calls attempted. Fixed
    by treating a missing expires_at the same as an expired one: refresh
    proactively. Harmless if the current token was actually still fine --
    refresh_session() just issues a new one either way -- and it self-heals
    every already-logged-in session on its very next request, no manual
    sign-out/sign-in required.

    If the refresh itself fails (refresh token revoked or truly dead, e.g.
    after a password change elsewhere, or weeks of inactivity), the stale
    session is cleared so the NEXT request cleanly hits login_required's
    redirect instead of repeating a confusing generic error -- but the
    current request still raises, same as before this fix, since there's no
    valid token to hand back either way."""
    refresh_token = session.get("sb_refresh_token")
    if not refresh_token:
        return
    expires_at = session.get("sb_expires_at")
    if expires_at is not None and time.time() < expires_at - _TOKEN_REFRESH_BUFFER_SECONDS:
        return

    try:
        result = get_supabase().auth.refresh_session(refresh_token)
    except Exception:
        session.pop("sb_access_token", None)
        session.pop("sb_refresh_token", None)
        session.pop("sb_expires_at", None)
        raise

    session["sb_access_token"] = result.session.access_token
    session["sb_refresh_token"] = result.session.refresh_token
    session["sb_expires_at"] = result.session.expires_at


def get_user_supabase() -> Client:
    """Build a Supabase client scoped to the CURRENTLY LOGGED-IN user's own
    access token (not the bare anon client from get_supabase() above), so RLS
    policies evaluate auth.uid() as that real user. Constructed fresh per
    request since Flask is stateless between requests -- there is no
    connection to reuse. Shared here so pro_chat.py (and anything else
    Pro-side) doesn't duplicate this logic."""
    _ensure_fresh_access_token()
    sb = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    sb.postgrest.auth(session["sb_access_token"])
    return sb
