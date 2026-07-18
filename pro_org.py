"""Selah for Ministry -- Church/Org roster management blueprint.

Admin-only operations on a church's roster: view who's on it, remove
someone (freeing their seat and transferring them to their own individual
trial account, per Rick's 2026-07-12 decision), generate invite
links/codes, and manage which profiles hold the (capped) admin role.

Kept as its own module, registered onto the existing Flask app additively,
same pattern as pro_auth.py/pro_chat.py/pro_billing.py -- nothing here
touches the free tool or any non-Church/Org Pro route.

Every route in this file is admin-only (is_org_admin=true on the caller's
own profile) -- this is roster/money-adjacent territory (removing someone
changes real access, generating an invite can add a real paid seat), never
something an ordinary seat-holder can trigger themselves.
"""

import os
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, request, jsonify, session, render_template, url_for

from engine import NODE_DISPLAY_NAMES
from pro_auth import login_required, get_user_supabase, get_service_client, _check_and_record
from pro_billing import MAX_ORG_ADMINS, promote_waitlisted_if_room, CHURCH_SEAT_TIER_SLUGS
from pro_email import (
    send_roster_removal_email,
    send_suspended_email,
    send_reactivated_email,
    send_promoted_admin_email,
    send_demoted_admin_email,
)

# Congregation topic-aggregation dashboard (2026-07-15, Rick-approved scoping):
# minimum cohort of 5 distinct members before a topic renders at all ("5 to
# start is fine with the caveat that it's a small sample size, real (just
# small)"); org needs 30 days of history before the dashboard activates
# ("minimum 30"); rolling 90-day display window, compared against the prior
# 90-day window for trend ("rolling 90").
TOPIC_PULSE_MIN_COHORT = 5
TOPIC_PULSE_MIN_ORG_AGE_DAYS = 30
TOPIC_PULSE_WINDOW_DAYS = 90

pro_org_bp = Blueprint("pro_org", __name__, url_prefix="/pro/org")

# Member invites are a shared, reusable link/code for the whole
# congregation (Section 11) -- no natural single-use expiry event the way a
# named Leadership invite has (consumed the moment that one person signs
# up). Still given a default expiry rather than living forever, so a code
# pasted into an old bulletin/email years ago doesn't stay valid
# indefinitely. Admin can always generate a fresh one.
DEFAULT_MEMBER_INVITE_EXPIRY_DAYS = 90
# Leadership invites are tied to one specific person -- meant to be used
# promptly, not sit around. Shorter default; admin can regenerate if it
# lapses before the invitee signs up.
DEFAULT_LEADER_INVITE_EXPIRY_DAYS = 14


# ── Admin-action abuse guard ────────────────────────────────────────────────
# Added 2026-07-18, same pass as pro_auth.py's login/signup limiter (Selah
# Full-Stack Audit Section 3.4a named both files). Lives here rather than in
# each route because every route in this file funnels through
# _get_admin_org_id() below -- one choke point covers all eleven call sites,
# reads and writes alike, for free. Keyed by admin user id, not IP -- these
# routes are already behind login_required, so the real risk is a
# compromised or scripted session hammering roster/invite actions, not an
# anonymous attacker. Threshold is generous on purpose: a real admin loading
# the dashboard can trigger several of these in one page view (status,
# roster, invites, audit-log, topics), and this shouldn't get in the way of
# a legitimate admin working through a long roster.
ORG_ACTION_LIMIT = int(os.environ.get("ORG_ACTION_LIMIT", "60"))
ORG_ACTION_WINDOW_SECONDS = int(os.environ.get("ORG_ACTION_WINDOW_SECONDS", "300"))  # 5 min
_org_action_attempts: dict = defaultdict(list)


def _get_admin_org_id():
    """Returns (organization_id, None) if the caller is an org admin,
    (None, error_response) otherwise -- every route below starts with this
    same check, so it's centralized rather than copy-pasted five times.
    Also the single choke point for this file's admin-action rate limit
    (see the block above) -- covering it here means every route gets it
    for free, including any written after this fix landed."""
    admin_key = f"user:{session.get('sb_user_id', 'unknown')}"
    if not _check_and_record(_org_action_attempts, admin_key, ORG_ACTION_LIMIT, ORG_ACTION_WINDOW_SECONDS):
        return None, (jsonify({"error": "Too many actions in a short time -- please wait a few minutes and try again."}), 429)

    sb = get_user_supabase()
    profile_resp = (
        sb.table("profiles")
        .select("organization_id, is_org_admin")
        .limit(1)
        .execute()
    )
    if not profile_resp.data:
        return None, (jsonify({"error": "no profile found for this account"}), 400)
    row = profile_resp.data[0]
    if not row.get("is_org_admin"):
        return None, (jsonify({"error": "Only an organization admin can do this."}), 403)
    return row["organization_id"], None


def _log_org_audit_event(
    svc, organization_id: str, action: str,
    target_id: str | None = None, target_email: str | None = None,
    detail: dict | None = None,
) -> None:
    """Writes one row to org_audit_log -- the audit's own minimal design
    (actor, action, target, timestamp), see the migration's table comment
    for the full rationale. Called AFTER the real mutation succeeds in each
    of the five roster/admin routes below, never before -- a failed action
    shouldn't leave a log entry claiming it happened.

    actor_id/actor_email come from the caller's own session (this file's
    admin gate already confirmed they're a real org admin before any of
    these routes get this far) -- not passed as a parameter, so every call
    site logs the actual person who clicked the button, not whoever the
    target of the action happens to be.

    Deliberately swallows its own errors rather than failing the underlying
    action -- a missed audit-log write is a real gap worth noticing later,
    but it should never be the reason a legitimate suspend/remove/promote
    fails for the person using the feature."""
    try:
        svc.table("org_audit_log").insert({
            "organization_id": organization_id,
            "actor_id": session.get("sb_user_id"),
            "actor_email": session.get("sb_email"),
            "action": action,
            "target_id": target_id,
            "target_email": target_email,
            "detail": detail,
        }).execute()
    except Exception:
        pass


def _get_org_name(svc, organization_id: str) -> str:
    """Shared lookup for the notice-email call sites below -- avoids
    repeating the same organizations query in suspend/reactivate/promote/
    demote/remove."""
    resp = svc.table("organizations").select("name").eq("id", organization_id).limit(1).execute()
    return (resp.data[0].get("name") if resp.data else None) or "your organization"


def transfer_profile_to_individual_trial(svc, profile_id: str, email: str) -> str:
    """Moves one profile onto a brand-new individual org on the standard
    14-day/25-exchange trial -- the exact mechanism handle_new_user() gives
    every fresh signup (Rick's 2026-07-12 decision, originally built for
    remove_from_roster() below). Extracted 2026-07-13 (Task #41) so
    pro_scheduler.py's whole-org cancellation cascade can reuse the
    identical transfer, person by person, instead of duplicating it.

    Does NOT send any email or touch waitlist promotion -- those differ by
    caller (remove_from_roster() promotes a waitlisted replacement since
    one seat just freed up in an otherwise-live pool; the cascade never
    does, since the whole pool is gone). Callers handle both themselves.

    Returns the new organization_id."""
    new_org = (
        svc.table("organizations")
        .insert({"name": email or "New User", "org_type": "individual"})
        .execute()
    )
    new_org_id = new_org.data[0]["id"]

    svc.table("profiles").update({
        "organization_id": new_org_id,
        "seat_type": None,
        "seat_status": None,
        "is_org_admin": False,
        "waitlisted_at": None,
        "suspended_at": None,
    }).eq("id", profile_id).execute()

    # Same shape handle_new_user() gives a brand-new signup -- 'trial'/
    # 'active'/trial_end NULL, "hasn't started yet." pro_chat.py flips this
    # to 'trialing' and starts the 14-day clock the moment their first real
    # exchange happens on the new org, exactly like any fresh account.
    svc.table("subscriptions").insert({
        "organization_id": new_org_id,
        "tier_slug": "trial",
        "status": "active",
    }).execute()

    return new_org_id


@pro_org_bp.route("/roster", methods=["GET"])
@login_required
def get_roster():
    """Full roster for the caller's own org -- every profile's seat_type,
    seat_status (paid/comped/pending/etc.), admin flag, and waitlist/
    suspension timestamps. Uses the service client (not the caller's own
    RLS-scoped token) purely for a consistent, complete read regardless of
    exactly how the "org admins can view their org's roster" RLS policy is
    scoped -- the admin check above is what actually gates this, not RLS."""
    organization_id, err = _get_admin_org_id()
    if err:
        return err

    svc = get_service_client()
    roster = (
        svc.table("profiles")
        .select("id, email, first_name, last_name, seat_type, seat_status, is_org_admin, waitlisted_at, suspended_at")
        .eq("organization_id", organization_id)
        .order("seat_type")
        .order("last_name")
        .execute()
    )
    return jsonify({"roster": roster.data})


def _execute_roster_removal(
    svc, organization_id: str, profile_id: str, acting_user_id: str,
    promote_waitlist: bool = True, log_detail_extra: dict | None = None,
):
    """Shared removal logic -- transfer to individual trial, audit log,
    email notice, optional waitlist promotion. Extracted 2026-07-15 so the
    seat-decrease flow (pro_billing.py's /church/seats/decrease, imported
    here locally to dodge the circular pro_org<->pro_billing import) can
    reuse the exact same guards and mechanics as an ordinary roster removal
    instead of a second, easy-to-drift copy of this logic.

    promote_waitlist=False is what the seat-decrease flow uses: a removal
    that's happening BECAUSE the pool itself is shrinking shouldn't also
    hand that just-freed seat to someone on the waitlist -- there's no
    seat left to give them. Ordinary roster removal (unchanged pool size)
    keeps promote_waitlist=True, its existing behavior.

    Callers must already know acting_user_id is a confirmed org admin for
    organization_id -- this function only carries the self-removal and
    last-admin guards, not the admin check itself.

    Returns (ok: bool, error_message: str | None, result: dict | None)."""
    if profile_id == acting_user_id:
        return False, "Can't remove yourself from the roster through this route.", None

    target = (
        svc.table("profiles")
        .select("id, email, is_org_admin, seat_type")
        .eq("id", profile_id)
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )
    if not target.data:
        return False, "That person isn't on your roster.", None
    target_row = target.data[0]

    if target_row.get("is_org_admin"):
        admin_count = (
            svc.table("profiles")
            .select("id")
            .eq("organization_id", organization_id)
            .eq("is_org_admin", True)
            .execute()
        )
        if len(admin_count.data or []) <= 1:
            return False, "Can't remove the last remaining admin -- promote someone else first.", None

    transfer_profile_to_individual_trial(svc, profile_id, target_row.get("email"))

    detail = {"seat_type": target_row.get("seat_type"), "was_admin": bool(target_row.get("is_org_admin"))}
    if log_detail_extra:
        detail.update(log_detail_extra)
    _log_org_audit_event(svc, organization_id, "roster.remove", target_id=profile_id, target_email=target_row.get("email"), detail=detail)

    promoted = 0
    if promote_waitlist and target_row.get("seat_type"):
        promoted = promote_waitlisted_if_room(organization_id, target_row["seat_type"])

    email_sent = False
    if target_row.get("email"):
        # ?promo=winback -- read by pro_app.html's checkPromoParam() and
        # threaded through to /pro/billing/checkout, pre-applying the
        # selah_winback_30off_3mo Stripe coupon (30% off, 3 months). Added
        # 2026-07-13 as an independent win-back offer for anyone who's just
        # lost a church-provided seat and landed back on the free individual
        # trial -- see pro_email.py's send_roster_removal_email() docstring.
        upgrade_link = url_for("pro_chat.pro_app", promo="winback", _external=True)
        email_sent = send_roster_removal_email(target_row["email"], _get_org_name(svc, organization_id), upgrade_link)

    return True, None, {"email_sent": email_sent, "promoted": promoted, "seat_type": target_row.get("seat_type")}


@pro_org_bp.route("/roster/remove", methods=["POST"])
@login_required
def remove_from_roster():
    """Removes one person from the org's roster, freeing their seat, and
    transfers them into a brand-new individual org on the standard 14-day/
    25-exchange trial -- NOT a new deletion countdown, deliberate reuse of
    the exact mechanism handle_new_user() already gives every fresh signup
    (Rick's explicit call, 2026-07-12: "transfer use b. 14 day/25 free
    exchanges during interim/transition"). Their planning_sessions history
    stays with their profile id, untouched -- only organization_id and
    seat_type/seat_status/is_org_admin reset. This is a real access change,
    not a data-loss one.

    Two guards beyond the base admin check, both judgment calls in the
    absence of an explicit Rick decision, flagged here as such rather than
    silently assumed:
      - Can't remove yourself through this route (an admin accidentally
        locking themselves out mid-action while managing a roster is a
        real, easy-to-hit mistake). Leaving the org or deleting the account
        entirely are separate, deliberate actions elsewhere.
      - Can't remove the last remaining admin, full stop -- "multi-admin is
        vital, crap happens" (Rick, 2026-07-12) implies continuity matters;
        an org silently ending up with zero admins would be a real support
        fire with no easy self-serve recovery.

    Both guards, plus the transfer/audit/email mechanics, now live in
    _execute_roster_removal() above (extracted 2026-07-15) -- this route is
    a thin wrapper over it with promote_waitlist=True, its original
    behavior, unchanged."""
    organization_id, err = _get_admin_org_id()
    if err:
        return err

    body = request.json or {}
    profile_id = body.get("profile_id")
    if not profile_id:
        return jsonify({"error": "profile_id required"}), 400

    svc = get_service_client()
    ok, error, result = _execute_roster_removal(svc, organization_id, profile_id, session.get("sb_user_id"), promote_waitlist=True)
    if not ok:
        status = 404 if error == "That person isn't on your roster." else 400
        return jsonify({"error": error}), status

    return jsonify({
        "ok": True,
        "note": "Removed from roster. Their account now has its own individual trial (14 days / 25 exchanges)."
                + (" They've been emailed." if result["email_sent"] else " Email notice could not be sent -- check Render logs."),
        "waitlisted_promoted": result["promoted"],
    })


@pro_org_bp.route("/invites", methods=["GET"])
@login_required
def list_invites():
    """All invite tokens for the org -- lets the admin see/copy an existing
    unused Membership code instead of blindly generating a new one every
    time, and see which named Leadership invites are still pending."""
    organization_id, err = _get_admin_org_id()
    if err:
        return err

    svc = get_service_client()
    invites = (
        svc.table("org_invite_tokens")
        .select("id, token, invite_type, invited_email, used_at, used_by, created_at, expires_at")
        .eq("organization_id", organization_id)
        .order("created_at", desc=True)
        .execute()
    )
    return jsonify({"invites": invites.data})


@pro_org_bp.route("/invites", methods=["POST"])
@login_required
def create_invite():
    """Generates a new invite token. body: {"invite_type": "leader"|
    "member", "invited_email": str} -- invited_email is REQUIRED for
    "leader" (Section 11: Leadership invites are single-use, tied to one
    named person -- handle_new_user()'s resolution logic checks the
    signup's email against this exact field) and ignored for "member"
    (a shared, reusable code for the whole congregation, not tied to
    anyone).

    Doesn't check seat-pool capacity before issuing an invite -- a full
    pool doesn't mean "stop inviting," it means the next person who signs
    up on it lands on the waitlist (seat_status='pending'), which
    handle_new_user() already handles. Admins can keep sharing a Membership
    code past capacity on purpose (e.g. announcing to the whole
    congregation at once, expecting some no-shows)."""
    organization_id, err = _get_admin_org_id()
    if err:
        return err

    body = request.json or {}
    invite_type = body.get("invite_type", "")
    invited_email = (body.get("invited_email") or "").strip() or None

    if invite_type not in ("leader", "member"):
        return jsonify({"error": f"Unknown invite_type: {invite_type!r}"}), 400
    if invite_type == "leader" and not invited_email:
        return jsonify({"error": "invited_email is required for a Leadership invite -- it's single-use and tied to one person."}), 400

    expiry_days = DEFAULT_LEADER_INVITE_EXPIRY_DAYS if invite_type == "leader" else DEFAULT_MEMBER_INVITE_EXPIRY_DAYS
    expires_at = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat()

    # URL-safe, unguessable -- this token is the entire authentication for
    # redeeming a (potentially paid) seat, same security bar as a password-
    # reset token.
    token = secrets.token_urlsafe(24)

    svc = get_service_client()
    svc.table("org_invite_tokens").insert({
        "organization_id": organization_id,
        "token": token,
        "invite_type": invite_type,
        "invited_email": invited_email if invite_type == "leader" else None,
        "expires_at": expires_at,
    }).execute()

    return jsonify({
        "ok": True,
        "token": token,
        "invite_type": invite_type,
        "expires_at": expires_at,
        # Frontend's job to build the actual shareable URL (e.g.
        # /pro?invite=<token> pre-filling the signup form's invite_code
        # field) -- not assumed here since the exact signup page routing
        # isn't this module's concern.
    })


@pro_org_bp.route("/invites/revoke", methods=["POST"])
@login_required
def revoke_invite():
    """Deletes an unused invite token -- e.g. a mistake, a Leadership
    invite sent to the wrong email, or rotating a Membership code that
    leaked somewhere it shouldn't have. Refuses to delete an already-used
    token (used_at IS NOT NULL) -- that's a real historical record of who
    joined how, not something to silently erase; a used token also can't be
    redeemed again regardless (handle_new_user() already checks used_at for
    leader-type tokens), so there's nothing live to protect by deleting it."""
    organization_id, err = _get_admin_org_id()
    if err:
        return err

    body = request.json or {}
    token_id = body.get("token_id")
    if not token_id:
        return jsonify({"error": "token_id required"}), 400

    svc = get_service_client()
    existing = (
        svc.table("org_invite_tokens")
        .select("id, used_at")
        .eq("id", token_id)
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )
    if not existing.data:
        return jsonify({"error": "Invite not found."}), 404
    if existing.data[0].get("used_at"):
        return jsonify({"error": "That invite has already been used -- nothing to revoke."}), 400

    svc.table("org_invite_tokens").delete().eq("id", token_id).execute()
    return jsonify({"ok": True})


@pro_org_bp.route("/roster/suspend", methods=["POST"])
@login_required
def suspend_member():
    """Blocks a roster member's chat access without removing them from the
    org or freeing their seat -- distinct from remove_from_roster above,
    which is permanent and transfers them out. Suspend is the "hold, don't
    lose the seat/data" action (e.g. a temporary conduct issue, a
    leave-of-absence). Sets profiles.suspended_at; pro_chat.py checks this
    before the usage-cap gate and blocks the request entirely, regardless
    of seat_status or comped status -- a suspension is a moderation hold,
    not a billing state, so a comped seat doesn't bypass it the way it
    bypasses the usage cap.

    Same self-suspend guard as remove_from_roster -- an admin locking
    themselves out mid-action is exactly the mistake that guard exists to
    prevent, same reasoning applies here."""
    organization_id, err = _get_admin_org_id()
    if err:
        return err

    body = request.json or {}
    profile_id = body.get("profile_id")
    if not profile_id:
        return jsonify({"error": "profile_id required"}), 400
    if profile_id == session.get("sb_user_id"):
        return jsonify({"error": "Can't suspend yourself through this route."}), 400

    svc = get_service_client()
    target = (
        svc.table("profiles")
        .select("id, email")
        .eq("id", profile_id)
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )
    if not target.data:
        return jsonify({"error": "That person isn't on your roster."}), 404

    svc.table("profiles").update({
        "suspended_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", profile_id).execute()

    _log_org_audit_event(svc, organization_id, "roster.suspend", target_id=profile_id, target_email=target.data[0].get("email"))

    if target.data[0].get("email"):
        send_suspended_email(target.data[0]["email"], _get_org_name(svc, organization_id))

    return jsonify({"ok": True})


@pro_org_bp.route("/roster/reactivate", methods=["POST"])
@login_required
def reactivate_member():
    """Clears a suspension -- the person's seat, seat_status, and access
    resume exactly as they were before suspend_member() was called (nothing
    else about their profile changes, only suspended_at)."""
    organization_id, err = _get_admin_org_id()
    if err:
        return err

    body = request.json or {}
    profile_id = body.get("profile_id")
    if not profile_id:
        return jsonify({"error": "profile_id required"}), 400

    svc = get_service_client()
    target = (
        svc.table("profiles")
        .select("id, email")
        .eq("id", profile_id)
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )
    if not target.data:
        return jsonify({"error": "That person isn't on your roster."}), 404

    svc.table("profiles").update({"suspended_at": None}).eq("id", profile_id).execute()

    _log_org_audit_event(svc, organization_id, "roster.reactivate", target_id=profile_id, target_email=target.data[0].get("email"))

    if target.data[0].get("email"):
        send_reactivated_email(target.data[0]["email"], _get_org_name(svc, organization_id))

    return jsonify({"ok": True})


@pro_org_bp.route("/admins/promote", methods=["POST"])
@login_required
def promote_admin():
    """Grants is_org_admin to an existing roster member. body:
    {"profile_id": str}. Enforces MAX_ORG_ADMINS (4, Rick's call,
    2026-07-12) -- imported from pro_billing.py rather than redefined here,
    one number, one source of truth."""
    organization_id, err = _get_admin_org_id()
    if err:
        return err

    body = request.json or {}
    profile_id = body.get("profile_id")
    if not profile_id:
        return jsonify({"error": "profile_id required"}), 400

    svc = get_service_client()

    target = (
        svc.table("profiles")
        .select("id, email, is_org_admin")
        .eq("id", profile_id)
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )
    if not target.data:
        return jsonify({"error": "That person isn't on your roster."}), 404
    if target.data[0].get("is_org_admin"):
        return jsonify({"error": "Already an admin."}), 400

    admin_count = (
        svc.table("profiles")
        .select("id")
        .eq("organization_id", organization_id)
        .eq("is_org_admin", True)
        .execute()
    )
    if len(admin_count.data or []) >= MAX_ORG_ADMINS:
        return jsonify({"error": f"This organization already has the maximum of {MAX_ORG_ADMINS} admins."}), 400

    svc.table("profiles").update({"is_org_admin": True}).eq("id", profile_id).execute()

    _log_org_audit_event(svc, organization_id, "admin.promote", target_id=profile_id, target_email=target.data[0].get("email"))

    if target.data[0].get("email"):
        send_promoted_admin_email(target.data[0]["email"], _get_org_name(svc, organization_id))

    return jsonify({"ok": True})


@pro_org_bp.route("/admins/demote", methods=["POST"])
@login_required
def demote_admin():
    """Revokes is_org_admin from a roster member. body: {"profile_id": str}.
    Same last-admin guard as remove_from_roster above -- an org can never be
    left with zero admins through this route."""
    organization_id, err = _get_admin_org_id()
    if err:
        return err

    body = request.json or {}
    profile_id = body.get("profile_id")
    if not profile_id:
        return jsonify({"error": "profile_id required"}), 400

    svc = get_service_client()

    target = (
        svc.table("profiles")
        .select("id, email, is_org_admin")
        .eq("id", profile_id)
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )
    if not target.data:
        return jsonify({"error": "That person isn't on your roster."}), 404
    if not target.data[0].get("is_org_admin"):
        return jsonify({"error": "Not currently an admin."}), 400

    admin_count = (
        svc.table("profiles")
        .select("id")
        .eq("organization_id", organization_id)
        .eq("is_org_admin", True)
        .execute()
    )
    if len(admin_count.data or []) <= 1:
        return jsonify({"error": "Can't remove the last remaining admin -- promote someone else first."}), 400

    svc.table("profiles").update({"is_org_admin": False}).eq("id", profile_id).execute()

    _log_org_audit_event(svc, organization_id, "admin.demote", target_id=profile_id, target_email=target.data[0].get("email"))

    if target.data[0].get("email"):
        send_demoted_admin_email(target.data[0]["email"], _get_org_name(svc, organization_id))

    return jsonify({"ok": True})


@pro_org_bp.route("/status", methods=["GET"])
@login_required
def org_status():
    """Lightweight read the church dashboard page uses on load to decide
    which of its three views to show: 'Start a Church' (org_type still
    'individual'), 'you're a seat holder, not an admin' (church org,
    is_org_admin false), or the full admin dashboard (church org, admin).
    No admin gate here -- unlike every other route in this file, a
    non-admin needs this exact read to know they're NOT an admin, and
    seat-count totals aren't sensitive information within someone's own
    org."""
    sb = get_user_supabase()
    profile_resp = (
        sb.table("profiles")
        .select("organization_id, is_org_admin, seat_type")
        .limit(1)
        .execute()
    )
    if not profile_resp.data:
        return jsonify({"error": "no profile found for this account"}), 400
    row = profile_resp.data[0]
    organization_id = row["organization_id"]

    org_resp = sb.table("organizations").select("org_type, name, postal_code").eq("id", organization_id).limit(1).execute()
    org_type = org_resp.data[0]["org_type"] if org_resp.data else "individual"
    org_name = org_resp.data[0].get("name") if org_resp.data else None
    org_postal_code = org_resp.data[0].get("postal_code") if org_resp.data else None

    result = {
        "organization_id": organization_id,
        "org_type": org_type,
        "org_name": org_name,
        "org_postal_code": org_postal_code,
        "is_org_admin": bool(row.get("is_org_admin")),
        "seat_type": row.get("seat_type"),
    }

    if org_type == "church":
        svc = get_service_client()
        subs = (
            svc.table("subscriptions")
            .select("tier_slug, seat_quantity")
            .eq("organization_id", organization_id)
            .in_("tier_slug", list(CHURCH_SEAT_TIER_SLUGS.values()))
            .in_("status", ["active", "trialing"])
            .execute()
        )
        by_tier = {r["tier_slug"]: r.get("seat_quantity") for r in (subs.data or [])}
        result["leader_seats_purchased"] = by_tier.get(CHURCH_SEAT_TIER_SLUGS["leader"])
        result["member_seats_purchased"] = by_tier.get(CHURCH_SEAT_TIER_SLUGS["member"])

        # Exchanges used this month, per pooled seat_type -- Rick, 2026-07-13:
        # admins need to see actual community activity, not just seat counts.
        # Same usage_records row _apply_church_exchange_block() in
        # pro_billing.py credits blocks onto (module_slug IS NULL, one row
        # per seat_type per calendar month).
        usage = (
            svc.table("usage_records")
            .select("seat_type, conversations_used, conversations_cap")
            .eq("organization_id", organization_id)
            .eq("billing_month", date.today().replace(day=1).isoformat())
            .is_("module_slug", "null")
            .in_("seat_type", ["leader", "member"])
            .execute()
        )
        usage_by_type = {r["seat_type"]: r for r in (usage.data or [])}
        result["leader_exchanges_used"] = (usage_by_type.get("leader") or {}).get("conversations_used", 0)
        result["leader_exchanges_cap"] = (usage_by_type.get("leader") or {}).get("conversations_cap")
        result["member_exchanges_used"] = (usage_by_type.get("member") or {}).get("conversations_used", 0)
        result["member_exchanges_cap"] = (usage_by_type.get("member") or {}).get("conversations_cap")

    return jsonify(result)


@pro_org_bp.route("/audit-log", methods=["GET"])
@login_required
def org_audit_log():
    """Recent roster/admin actions for the caller's own org -- resolves the
    "who removed/suspended/promoted whom" disputes the audit flagged as
    having no record beyond the transactional email sent to the affected
    person (Section 2.6). Admin-only, same gate as every other route here.

    Most-recent-200, no pagination in this first pass -- deliberately
    minimal per the audit's own scoping; a real church's roster-action
    volume is nowhere near what would make that a practical limitation."""
    organization_id, err = _get_admin_org_id()
    if err:
        return err

    svc = get_service_client()
    log = (
        svc.table("org_audit_log")
        .select("action, actor_email, target_email, detail, created_at")
        .eq("organization_id", organization_id)
        .order("created_at", desc=True)
        .limit(200)
        .execute()
    )
    return jsonify({"entries": log.data})


@pro_org_bp.route("/topics", methods=["GET"])
@login_required
def org_topics():
    """Congregation topic-aggregation dashboard -- what nodes/topics the
    Membership seat-holders have actually been engaging with, aggregated and
    anonymized. Admin-only, same _get_admin_org_id() gate as every other
    route in this file.

    Reads ONLY congregation_topic_pulse, never planning_sessions -- see that
    table's own migration comment for why (a member later hard-deleting
    their conversation must not erase this signal).

    Three gating rules, all Rick-approved 2026-07-15:
      - Membership seats only. congregation_topic_pulse is only ever written
        for seat_type == 'member' turns (pro_chat.py's _record_topic_pulse
        call site), so this route doesn't need its own filter for that --
        it's structural, not a query-time decision.
      - Dashboard doesn't activate at all until the org is
        TOPIC_PULSE_MIN_ORG_AGE_DAYS old, regardless of how much data
        exists -- a brand-new org's first week of activity isn't yet a
        meaningful pastoral signal.
      - A node only appears once at least TOPIC_PULSE_MIN_COHORT distinct
        members have touched it in the current window -- never returns a
        count of 1-4, which would functionally de-anonymize a small church's
        early adopters.

    user_id itself is never returned -- only used server-side to build the
    per-node distinct-member sets below."""
    organization_id, err = _get_admin_org_id()
    if err:
        return err

    svc = get_service_client()

    org_resp = svc.table("organizations").select("org_type, created_at").eq("id", organization_id).limit(1).execute()
    if not org_resp.data:
        return jsonify({"error": "organization not found"}), 404
    org_row = org_resp.data[0]

    if org_row.get("org_type") != "church":
        return jsonify({"active": False, "reason": "not_a_church_org", "topics": []})

    created_at = datetime.fromisoformat(org_row["created_at"].replace("Z", "+00:00"))
    org_age_days = (datetime.now(timezone.utc) - created_at).days
    if org_age_days < TOPIC_PULSE_MIN_ORG_AGE_DAYS:
        return jsonify({
            "active": False,
            "reason": "org_too_new",
            "days_until_active": TOPIC_PULSE_MIN_ORG_AGE_DAYS - org_age_days,
            "topics": [],
        })

    today = date.today()
    current_cutoff = today - timedelta(days=TOPIC_PULSE_WINDOW_DAYS)
    prior_cutoff = today - timedelta(days=TOPIC_PULSE_WINDOW_DAYS * 2)

    # Pulled as raw rows and bucketed here in Python rather than via a
    # GROUP BY -- volume per org is naturally small (bounded by roster size x
    # node count x ~26 weeks), and doing it here keeps both the current and
    # prior windows' distinct-member sets available for the trend comparison
    # below without a second round trip.
    rows_resp = (
        svc.table("congregation_topic_pulse")
        .select("node, week_start, user_id")
        .eq("organization_id", organization_id)
        .gte("week_start", prior_cutoff.isoformat())
        .execute()
    )

    current_members_by_node = defaultdict(set)
    prior_members_by_node = defaultdict(set)
    for r in (rows_resp.data or []):
        week_start = date.fromisoformat(r["week_start"])
        if week_start >= current_cutoff:
            current_members_by_node[r["node"]].add(r["user_id"])
        else:
            prior_members_by_node[r["node"]].add(r["user_id"])

    topics = []
    for node, members in current_members_by_node.items():
        distinct_members = len(members)
        if distinct_members < TOPIC_PULSE_MIN_COHORT:
            continue
        prior_distinct_members = len(prior_members_by_node.get(node, ()))
        if prior_distinct_members == 0:
            trend = "new"
        elif distinct_members > prior_distinct_members:
            trend = "up"
        elif distinct_members < prior_distinct_members:
            trend = "down"
        else:
            trend = "flat"
        topics.append({
            "node": node,
            "display_name": NODE_DISPLAY_NAMES.get(node, node),
            "distinct_members": distinct_members,
            "prior_distinct_members": prior_distinct_members,
            "trend": trend,
        })

    topics.sort(key=lambda t: t["distinct_members"], reverse=True)

    return jsonify({
        "active": True,
        "window_days": TOPIC_PULSE_WINDOW_DAYS,
        "min_cohort_size": TOPIC_PULSE_MIN_COHORT,
        "topics": topics,
    })


@pro_org_bp.route("/dashboard", methods=["GET"])
@login_required
def org_dashboard():
    """Renders the Church/Org admin page -- all the actual view-branching
    (start-a-church / not-an-admin / full dashboard) happens client-side
    off org_status() above, same pattern pro_app.html already uses for
    billing_status(). Kept as a plain render here rather than doing that
    branching server-side so the page never needs a hard redirect mid-flow
    (e.g. right after /church/start succeeds, this same page just re-reads
    its own status and swaps views in place)."""
    return render_template("church_dashboard.html", email=session.get("sb_email", ""))
