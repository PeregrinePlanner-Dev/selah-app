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

from pro_email import send_welcome_email, send_seat_granted_email, send_account_deletion_email

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
            .select("invite_resolution, seat_status, seat_type, organization_id")
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
            # Landed on a real (paid or comped) church seat via invite --
            # send the seat-granted notice instead of the plain welcome.
            if row.get("seat_status") in ("paid", "comped") and row.get("seat_type"):
                org_name = "your organization"
                if row.get("organization_id"):
                    org_resp = (
                        svc.table("organizations")
                        .select("name")
                        .eq("id", row["organization_id"])
                        .limit(1)
                        .execute()
                    )
                    org_name = (org_resp.data[0].get("name") if org_resp.data else None) or org_name
                send_seat_granted_email(email, first_name, org_name, row["seat_type"])
            else:
                send_welcome_email(email, first_name)
        else:
            send_welcome_email(email, first_name)
    except Exception:
        # Never let this status check be the reason signup itself fails --
        # worst case the person just doesn't see the notice or the email.
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


@pro_bp.route("/forgot-password", methods=["POST"])
def forgot_password_request():
    """Sends a password-reset email via Supabase's own reset flow, added
    2026-07-13. reset_password_for_email() emails a recovery link that
    redirects the browser to /pro/reset-password with session tokens in
    the URL FRAGMENT (never sent to this server directly -- that page's
    own JS reads them and POSTs to reset_password_submit() below).

    Uses Supabase's own built-in reset email (subject/body configurable in
    the Supabase dashboard's Auth > Email Templates), NOT pro_email.py/
    Resend -- reset_password_for_email() is the single well-documented,
    stable call that both sends the email AND sets up the recovery token
    the way Supabase's own redirect flow expects. Revisit if branding
    consistency with the rest of pro_email.py's templates matters enough
    to justify swapping to admin.generate_link() + a Resend template.

    REQUIRES a matching entry in the Supabase dashboard's Authentication ->
    URL Configuration -> Redirect URLs allow-list (e.g.
    https://selah-app.onrender.com/pro/reset-password) -- without it,
    Supabase silently ignores redirect_to and sends the link to the
    project's default Site URL instead, which would land on the wrong page
    with no error shown anywhere. Not something this code can verify or
    fix itself.

    Always redirects with the same generic notice regardless of whether
    the email actually has an account -- standard anti-enumeration
    practice, so this endpoint can't be used to check who has a Selah
    account."""
    email = request.form.get("email", "").strip()
    generic_notice = "If an account exists for that email, a password reset link is on its way."
    if not email:
        return redirect(url_for("pro.pro_home", notice=generic_notice))

    try:
        get_supabase().auth.reset_password_for_email(
            email,
            {"redirect_to": url_for("pro.reset_password_page", _external=True)},
        )
    except Exception:
        # Never reveal whether this failed because the email doesn't exist
        # vs. a real error -- same generic message either way.
        pass

    return redirect(url_for("pro.pro_home", notice=generic_notice))


@pro_bp.route("/reset-password", methods=["GET"])
def reset_password_page():
    """Renders the 'set a new password' page. The actual recovery tokens
    arrive in the URL FRAGMENT (#access_token=...&refresh_token=...&
    type=recovery), which browsers never send to the server on a plain
    GET -- reset_password.html's own inline JS reads window.location.hash
    and POSTs the tokens to reset_password_submit() below alongside the
    new password. supabase-py has no client-side 'detect session in URL'
    magic the way the JS SDK does, so this hand-off is required. If
    someone lands here with no fragment at all (direct navigation, or an
    already-used/expired link), the page's own JS shows an error instead
    of the form -- nothing server-side to check at this GET."""
    return render_template("reset_password.html")


@pro_bp.route("/reset-password", methods=["POST"])
def reset_password_submit():
    """JSON endpoint reset_password.html's JS calls with the fragment
    tokens plus the new password. set_session() first (proves the tokens
    are a real, unexpired Supabase recovery session) then update_user()
    actually changes the password -- the same two-call sequence Supabase's
    own docs specify for a Python/server backend. Uses a throwaway client,
    never the shared get_supabase()/get_user_supabase() singletons, so an
    invalid or expired recovery token from one request can't affect any
    other concurrent request in this process."""
    body = request.json or {}
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    new_password = body.get("new_password", "")

    if not access_token or not refresh_token:
        return jsonify({"error": "This reset link is invalid or has expired -- request a new one."}), 400
    if len(new_password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400

    try:
        sb = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        sb.auth.set_session(access_token, refresh_token)
        sb.auth.update_user({"password": new_password})
    except Exception as e:
        return jsonify({"error": f"Could not reset password -- the link may have expired: {e}"}), 400

    # Log them straight into the app -- they just proved account ownership
    # via the emailed recovery link, no reason to make them log in again
    # with the password they just set.
    try:
        user_resp = sb.auth.get_user()
        session["sb_access_token"] = access_token
        session["sb_refresh_token"] = refresh_token
        session["sb_email"] = user_resp.user.email if user_resp and user_resp.user else None
        session["sb_user_id"] = user_resp.user.id if user_resp and user_resp.user else None
    except Exception:
        pass

    return jsonify({"ok": True})


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

    user_email = session.get("sb_email")

    try:
        get_service_client().auth.admin.delete_user(user_id)
    except Exception as e:
        return redirect(url_for("pro_chat.pro_app", error=f"Could not delete account: {e}"))

    if user_email:
        send_account_deletion_email(user_email)

    session.pop("sb_access_token", None)
    session.pop("sb_refresh_token", None)
    session.pop("sb_expires_at", None)
    session.pop("sb_email", None)
    session.pop("sb_user_id", None)
    return redirect(url_for("pro.pro_home", notice="Your account and all associated data have been permanently deleted."))


@pro_bp.route("/account/change-password", methods=["POST"])
@login_required
def change_password():
    """Logged-in password change, added 2026-07-13 -- distinct from the
    forgot-password flow above (that's for someone locked OUT; this is for
    someone who still has access and just wants to change it). body:
    {"current_password": str, "new_password": str}, JSON.

    Verifies the current password via sign_in_with_password() before
    allowing the change -- a real safety check: without it, anyone who
    found an already-open, unattended session could silently lock the
    real owner out. That check runs on a throwaway client, never the
    shared get_supabase() singleton (which login()/signup() also use) --
    calling sign_in_with_password on that shared, module-level instance
    would overwrite its internal session state for every concurrent
    request in this process, a real risk here since this route runs a
    second, DIFFERENT person's credentials through it on every call.

    update_user() itself needs set_session() first, same as
    reset_password_submit() above -- get_user_supabase()'s
    postgrest.auth(token) only authenticates REST table calls, not the
    auth.* client itself."""
    body = request.json or {}
    current_password = body.get("current_password", "")
    new_password = body.get("new_password", "")

    email = session.get("sb_email")
    if not email:
        return jsonify({"error": "Not logged in."}), 401
    if len(new_password) < 6:
        return jsonify({"error": "New password must be at least 6 characters."}), 400

    try:
        create_client(SUPABASE_URL, SUPABASE_ANON_KEY).auth.sign_in_with_password(
            {"email": email, "password": current_password}
        )
    except Exception:
        return jsonify({"error": "Current password is incorrect."}), 400

    _ensure_fresh_access_token()
    try:
        sb = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        sb.auth.set_session(session["sb_access_token"], session["sb_refresh_token"])
        sb.auth.update_user({"password": new_password})
    except Exception as e:
        return jsonify({"error": f"Could not update password: {e}"}), 400

    return jsonify({"ok": True})


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
