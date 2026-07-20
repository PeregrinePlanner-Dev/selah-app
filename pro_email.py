"""Selah for Ministry — transactional email, added 2026-07-13.

Thin wrapper around the Resend Python SDK. Kept as its own module for the
same reason pro_org.py/pro_billing.py/pro_auth.py are separate -- additive
only, nothing here touches the free tool.

Sending domain: selahexploringtheology.com, verified in Resend under a
dedicated account (separate from the sibling "Peregrine" project's own
Resend account -- Rick's call, 2026-07-13: free-tier accounts are capped at
1 verified domain each, and keeping usage/billing isolated per project is
good practice regardless of that cap).

Corrected 2026-07-20: FROM_ADDRESS previously pointed at a "send." subdomain
that was never actually verified in Resend, despite this file's original
comment claiming Cloudflare auto-configured it. Discovered live while
debugging a Supabase custom-SMTP failure that hit the exact same wrong
subdomain and got a hard 550 rejection from Resend naming it directly --
this file's sends were silently failing the same way the whole time, just
never surfaced because send_email() below treats any failure as a clean,
logged no-op rather than an error. Fixed to the actual verified root
domain. Worth Rick confirming with anyone who should have received a
welcome/seat-granted email since 2026-07-13 whether it ever arrived.

Same "clean no-op, not a crash" pattern as pro_billing.py's Stripe wiring:
if RESEND_API_KEY isn't set, send_email() logs and returns False instead of
raising, so a missing/misconfigured key never breaks the real action (e.g.
a roster removal still succeeds even if the notice email fails to send --
the access change is real regardless of whether the person was told about
it by email).
"""

import os

import resend

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
resend.api_key = RESEND_API_KEY

# Root domain -- the "send." subdomain previously used here was never
# actually verified in Resend (see correction note above, 2026-07-20).
FROM_ADDRESS = "Selah for Ministry <notifications@selahexploringtheology.com>"


def send_email(to: str, subject: str, html: str) -> bool:
    """Sends one email. Returns True if Resend accepted it, False on any
    failure (missing key, API error, etc.) -- callers should treat email as
    best-effort and never let a failure here block the real state change
    that triggered it."""
    if not RESEND_API_KEY:
        print(f"[EMAIL] RESEND_API_KEY not set -- skipping send to {to!r} (subject: {subject!r})")
        return False
    try:
        resend.Emails.send({
            "from": FROM_ADDRESS,
            "to": [to],
            "subject": subject,
            "html": html,
        })
        return True
    except Exception as e:
        print(f"[EMAIL] Failed to send to {to!r} (subject: {subject!r}): {e}")
        return False


def _wrap(body_html: str) -> str:
    """Minimal shared shell -- plain, sentence-case, no marketing tone,
    matching the app's own working-dashboard-not-a-sales-pitch principle."""
    return f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color:#111827; line-height:1.5; max-width:480px;">
      {body_html}
      <p style="color:#6b7280; font-size:0.8rem; margin-top:24px;">Selah for Ministry</p>
    </div>
    """


def send_roster_removal_email(to: str, org_name: str, upgrade_link: str = "") -> bool:
    """Sent when an admin removes someone from a church org's roster
    (pro_org.py remove_from_roster()) -- they're moved to their own
    individual trial (14 days / 25 exchanges), same as any fresh signup.

    upgrade_link (added 2026-07-13), when provided, carries a
    ?promo=winback query param that pro_app.html's checkPromoParam() reads
    and threads through to /pro/billing/checkout, auto-applying the
    selah_winback_30off_3mo Stripe coupon (30% off, first 3 months) --
    this is the moment someone who's just lost a church-provided seat is
    deciding whether to keep going on their own. Optional/blank-safe so
    this function still works if a caller has no request context to build
    the link from (url_for requires one)."""
    subject = f"You've been removed from {org_name} on Selah for Ministry"
    upgrade_html = (
        f'<p><a href="{upgrade_link}" style="color:#2f6b66; font-weight:600;">Keep going on your own -- 30% off your first 3 months</a></p>'
        if upgrade_link else ""
    )
    html = _wrap(f"""
      <p>You've been removed from <strong>{org_name}</strong>'s roster on Selah for Ministry.</p>
      <p>Your seat there is gone, but your account still works -- you've been moved to your own individual trial (14 days, 25 exchanges) so you're not locked out. Every session and everything you've studied so far is still there, exactly as you left it.</p>
      {upgrade_html}
      <p>If this wasn't expected, contact {org_name}'s admin directly.</p>
    """)
    return send_email(to, subject, html)


def send_waitlist_promoted_email(to: str, org_name: str, seat_type: str) -> bool:
    """Sent when someone waitlisted for a seat gets auto-promoted
    (pro_billing.py promote_waitlisted_if_room()) -- can be triggered by
    someone else's action entirely (an admin buying more seats, or another
    member being removed), so this is the only way the promoted person
    would find out without logging in on the chance of checking."""
    kind = "Leadership" if seat_type == "leader" else "Membership"
    subject = f"A seat opened up at {org_name}"
    html = _wrap(f"""
      <p>Good news -- a {kind} seat opened up at <strong>{org_name}</strong> on Selah for Ministry, and you've been moved off the waitlist.</p>
      <p>Sign in any time to start using it.</p>
    """)
    return send_email(to, subject, html)


def send_welcome_email(to: str, first_name: str) -> bool:
    """Sent once, right after an ordinary individual signup (not tied to a
    church invite) -- pro_auth.py signup()."""
    name = first_name or "there"
    subject = "Welcome to Selah for Ministry"
    html = _wrap(f"""
      <p>Hi {name},</p>
      <p>Your Selah for Ministry account is ready. You're on a 14-day trial with 25 exchanges to explore systematic theology at whatever depth you want to go.</p>
      <p>Sign in any time to get started.</p>
    """)
    return send_email(to, subject, html)


def send_seat_granted_email(to: str, first_name: str, org_name: str, seat_type: str) -> bool:
    """Sent once, right after someone successfully redeems a church invite
    and lands on a real (paid or comped) seat -- pro_auth.py signup()."""
    name = first_name or "there"
    kind = "Leadership" if seat_type == "leader" else "Membership"
    subject = f"You're in -- {org_name} on Selah for Ministry"
    html = _wrap(f"""
      <p>Hi {name},</p>
      <p>You now have a {kind} seat at <strong>{org_name}</strong> on Selah for Ministry. Sign in any time to get started.</p>
    """)
    return send_email(to, subject, html)


def send_suspended_email(to: str, org_name: str) -> bool:
    """Sent when an admin suspends a roster member -- pro_org.py
    suspend_member(). Their seat stays reserved; this is a moderation
    hold, not a removal."""
    subject = f"Your access at {org_name} has been suspended"
    html = _wrap(f"""
      <p>An admin at <strong>{org_name}</strong> has suspended your access on Selah for Ministry. Your seat is still reserved -- you just can't sign in and use it until it's reactivated.</p>
      <p>Contact {org_name}'s admin if you have questions.</p>
    """)
    return send_email(to, subject, html)


def send_reactivated_email(to: str, org_name: str) -> bool:
    """Sent when an admin clears a suspension -- pro_org.py
    reactivate_member()."""
    subject = f"Your access at {org_name} has been restored"
    html = _wrap(f"""
      <p>An admin at <strong>{org_name}</strong> has restored your access on Selah for Ministry. Sign in any time.</p>
    """)
    return send_email(to, subject, html)


def send_promoted_admin_email(to: str, org_name: str) -> bool:
    """Sent when an existing roster member is granted admin access --
    pro_org.py promote_admin()."""
    subject = f"You're now an admin at {org_name}"
    html = _wrap(f"""
      <p>You've been given admin access at <strong>{org_name}</strong> on Selah for Ministry. You can now manage seats, invitations, and the roster from the Church/Org dashboard.</p>
    """)
    return send_email(to, subject, html)


def send_demoted_admin_email(to: str, org_name: str) -> bool:
    """Sent when an admin's own access is revoked by another admin --
    pro_org.py demote_admin()."""
    subject = f"Your admin access at {org_name} has changed"
    html = _wrap(f"""
      <p>Your admin access at <strong>{org_name}</strong> on Selah for Ministry has been removed. You keep your own seat and access, just not the ability to manage seats, invitations, or the roster.</p>
    """)
    return send_email(to, subject, html)


def send_exchange_block_confirmation_email(to: str, org_name: str, seat_type: str, exchanges_added: int, new_cap: int) -> bool:
    """Sent to the admin who bought the block, once the webhook confirms
    the charge succeeded -- pro_billing.py _apply_church_exchange_block().
    A Stripe receipt covers the charge itself; this explains what it
    actually did to the pool."""
    kind = "Leadership" if seat_type == "leader" else "Membership"
    subject = f"Exchange block added -- {org_name}"
    html = _wrap(f"""
      <p>Your exchange block purchase for {org_name}'s {kind} pool is confirmed -- {exchanges_added} exchanges added.</p>
      <p>This month's {kind} cap is now {new_cap}.</p>
    """)
    return send_email(to, subject, html)


def send_account_deletion_email(to: str) -> bool:
    """Sent right after a self-serve account deletion completes --
    pro_auth.py delete_account(). The account and its data are already
    gone by the time this sends; it's a receipt, not a warning."""
    subject = "Your Selah for Ministry account has been deleted"
    html = _wrap(f"""
      <p>Your Selah for Ministry account and all associated conversation history have been permanently deleted, as requested. This can't be undone.</p>
      <p>If you didn't request this, contact us immediately at admin@selahexploringtheology.com.</p>
    """)
    return send_email(to, subject, html)


def send_cancellation_reminder_email(to: str, org_name: str, seat_type: str, period_end: str) -> bool:
    """Sent to org admins a few days before a canceled subscription's paid
    period actually ends -- pro_scheduler.py's daily job, added 2026-07-13
    (Task #41). Access is still live and paid-for at this point
    (cancel_at_period_end=true, status still 'active'); this is a heads-up
    with a real window to reverse course, not a notice that anything has
    happened yet. If they do nothing, the whole {seat_type} roster
    transfers to individual trials automatically once the period ends --
    that's the cascade this reminder is warning about."""
    kind = "Leadership" if seat_type == "leader" else "Membership"
    subject = f"{org_name}'s {kind} plan ends {period_end} -- action needed?"
    html = _wrap(f"""
      <p><strong>{org_name}</strong>'s {kind} plan on Selah for Ministry is set to end on <strong>{period_end}</strong> and won't renew.</p>
      <p>Access continues as normal until then -- nothing changes today. If this is expected, there's nothing to do. If it's not, you can reactivate any time before {period_end} to keep the plan going without interruption.</p>
      <p>If the plan does lapse, everyone on the {kind} roster is automatically moved to their own individual trial (14 days, 25 exchanges) so no one is locked out -- you'll get a summary once that happens.</p>
    """)
    return send_email(to, subject, html)


def send_cascade_admin_summary_email(to: str, org_name: str, seat_type: str, migrated_count: int) -> bool:
    """Sent to org admins once the whole-org cancellation cascade actually
    runs for one seat pool -- pro_scheduler.py's daily job, added
    2026-07-13 (Task #41). By the time this sends, the transfer has already
    happened for everyone affected -- each person also got their own
    send_roster_removal_email() individually; this is the admin-facing
    rollup, not a per-person notice."""
    kind = "Leadership" if seat_type == "leader" else "Membership"
    people = "person" if migrated_count == 1 else "people"
    subject = f"{org_name}'s {kind} plan has ended"
    html = _wrap(f"""
      <p><strong>{org_name}</strong>'s {kind} plan on Selah for Ministry has ended, and {migrated_count} {people} on that roster {"has" if migrated_count == 1 else "have"} been automatically moved to their own individual trial (14 days, 25 exchanges) so no one was locked out.</p>
      <p>Each of them was emailed directly about the change. If this wasn't expected, you can start a new {kind} plan any time from your Church/Org dashboard.</p>
    """)
    return send_email(to, subject, html)
