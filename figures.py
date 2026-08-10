"""Theologian & Philosopher public reference pages -- routes, added
2026-08-10 (sample pass -- see figures_content.py's module docstring for
full background on why this is being rebuilt from scratch).

Three routes:
  /figures               -- hub page, links to the two lists below. Public.
  /figures/theologians    -- full 64+2 roster, grouped by era, name opens a
                             popup with a short bio. Public (Rick's call:
                             matches the free tier already citing this same
                             roster in nodes/*.md).
  /figures/philosophers    -- full 33-figure roster, same layout. Gated to
                             paid accounts (Rick's call: matches the
                             Philosophy Layer's existing paid-only status).

Gating note: the philosophers route below uses login_required (any signed-
in Pro account) plus a simple tier_slug check (blocks 'free'/'lapsed'/no-
subscription-row). This is a SIMPLER check than pro_chat.py's -- it does
not do the church-seat-scoped (leader vs. member) subscription lookup that
route needs, because a static reference page carries no per-seat cost the
way a chat call does. Flagged here explicitly as a known simplification
for Rick's review alongside the sample pages themselves, not silently
assumed correct -- if the real requirement turns out to need the fuller
church-seat-aware check, that's a straightforward follow-up once the page
itself is approved.
"""

from flask import Blueprint, render_template, abort

from pro_auth import login_required, get_user_supabase, get_service_client, query_with_jwt_fallback
from pro_billing import _get_org_id_and_email
from figures_content import (
    THEOLOGIANS, THEOLOGIAN_ERAS,
    PHILOSOPHERS, PHILOSOPHER_ERAS,
    grouped_by_era,
)

figures_bp = Blueprint("figures", __name__, url_prefix="/figures")


@figures_bp.route("", strict_slashes=False)
def figures_hub():
    return render_template("figures.html", view="hub")


@figures_bp.route("/theologians")
def theologians():
    groups = grouped_by_era(THEOLOGIANS, THEOLOGIAN_ERAS)
    return render_template(
        "figures.html", view="theologians",
        page_title="Theologians", groups=groups,
        entries=THEOLOGIANS,
    )


@figures_bp.route("/philosophers")
@login_required
def philosophers():
    organization_id, _ = _get_org_id_and_email()
    if not organization_id:
        abort(403)

    sb = get_user_supabase()
    sub_resp = query_with_jwt_fallback(
        lambda: sb.table("subscriptions")
            .select("tier_slug")
            .eq("organization_id", organization_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute(),
        lambda: get_service_client().table("subscriptions")
            .select("tier_slug")
            .eq("organization_id", organization_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute(),
    )
    tier_slug = sub_resp.data[0]["tier_slug"] if sub_resp.data else "free"
    if tier_slug in ("free", "lapsed"):
        # Not a hard 403 -- render the same template in a "gated" state so
        # a Pro-logged-in-but-unpaid visitor sees a real upgrade CTA
        # instead of a bare error page, same spirit as pro_chat.py's
        # LAPSED_MESSAGE handling.
        return render_template("figures.html", view="philosophers_gated")

    groups = grouped_by_era(PHILOSOPHERS, PHILOSOPHER_ERAS)
    return render_template(
        "figures.html", view="philosophers",
        page_title="Philosophers & Apologists", groups=groups,
        entries=PHILOSOPHERS,
    )
