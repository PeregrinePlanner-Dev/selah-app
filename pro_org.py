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

import secrets
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, request, jsonify, session, render_template, url_for

from pro_auth import login_required, get_user_supabase, get_service_client
from pro_billing import MAX_ORG_ADMINS, promote_waitlisted_if_room, CHURCH_SEAT_TIER_SLUGS
from pro_email import (
    send_roster_removal_email,
    send_suspended_email,
    send_reactivated_email,
    send_promoted_admin_email,
    send_demoted_admin_email,
)

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


def _get_admin_org_id():
    """Returns (organization_id, None) if the caller is an org admin,
    (None, error_response) otherwise -- every route below starts with this
    same check, so it's centralized rather than copy-pasted five times."""
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

    Email notification of this transfer (Rick's decision: they should be
    told, with an offer already implicit in the fact that they're landing
    on a normal trial) is NOT sent here -- the transactional email system
    is explicitly deferred to a later session. The technical transfer
    happens regardless; only the notification is missing for now."""
    organization_id, err = _get_admin_org_id()
    if err:
        return err

    body = request.json or {}
    profile_id = body.get("profile_id")
    if not profile_id:
        return jsonify({"error": "profile_id required"}), 400

    if profile_id == session.get("sb_user_id"):
        return jsonify({"error": "Can't remove yourself from the roster through this route."}), 400

    svc = get_service_client()

    target = (
        svc.table("profiles")
        .select("id, email, is_org_admin, seat_type")
        .eq("id", profile_id)
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )
    if not target.data:
        return jsonify({"error": "That person isn't on your roster."}), 404
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
            return jsonify({"error": "Can't remove the last remaining admin -- promote someone else first."}), 400

    transfer_profile_to_individual_trial(svc, profile_id, target_row.get("email"))

    # This just freed a seat -- if anyone's waitlisted for this same
    # seat_type at this org, promote the longest-waiting one automatically
    # rather than leaving them stuck until an admin happens to notice.
    promoted = 0
    if target_row.get("seat_type"):
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

    return jsonify({
        "ok": True,
        "note": "Removed from roster. Their account now has its own individual trial (14 days / 25 exchanges)."
                + (" They've been emailed." if email_sent else " Email notice could not be sent -- check Render logs."),
        "waitlisted_promoted": promoted,
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
