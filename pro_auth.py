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
    return render_template("pro_login.html", error=request.args.get("error", ""))


@pro_bp.route("/signup", methods=["POST"])
def signup():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    if not email or not password:
        return redirect(url_for("pro.pro_home", error="Email and password are required."))

    try:
        result = get_supabase().auth.sign_up({"email": email, "password": password})
    except Exception as e:
        return redirect(url_for("pro.pro_home", error=f"Signup failed: {e}"))

    # If email confirmation is required (Supabase default), there may be no
    # session yet -- send the user back with a clear message rather than
    # silently failing to log them in.
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
    refresh token if it's expired or about to be. Session cookies created
    before this fix won't have 'sb_expires_at' set -- those are simply left
    alone (same as today's behavior) until the user logs in again, at which
    point expiry tracking starts.

    If the refresh itself fails (refresh token revoked or truly dead, e.g.
    after a password change elsewhere, or weeks of inactivity), the stale
    session is cleared so the NEXT request cleanly hits login_required's
    redirect instead of repeating a confusing generic error -- but the
    current request still raises, same as before this fix, since there's no
    valid token to hand back either way."""
    expires_at = session.get("sb_expires_at")
    refresh_token = session.get("sb_refresh_token")
    if not expires_at or not refresh_token:
        return
    if time.time() < expires_at - _TOKEN_REFRESH_BUFFER_SECONDS:
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
