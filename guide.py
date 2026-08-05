"""Selah for Church Staff: A Role-by-Role Guide -- public routes, added
2026-08-01. See guide_content.py's module docstring for the full
reasoning (why this is public, why it's a separate module from the
gated Pro/free chat routes).

Two routes only: /guide (index, lists all 11 roles) and /guide/<slug>
(one role's page). Neither uses @login_required or any seat/module
check -- deliberately public, per Rick's explicit call: this is meant
to help a prospective or already-signed-up church commit, not to be a
gated product feature in its own right.
"""

from flask import Blueprint, render_template, abort

from guide_content import GUIDE_PAGES, ROLE_ORDER

guide_bp = Blueprint("guide", __name__, url_prefix="/guide")


@guide_bp.route("", strict_slashes=False)
def guide_home():
    roles = [{"slug": slug, "title": title} for slug, title in ROLE_ORDER]
    return render_template("guide.html", is_home=True, roles=roles)


@guide_bp.route("/<slug>")
def guide_role(slug):
    page = GUIDE_PAGES.get(slug)
    if not page:
        abort(404)
    return render_template(
        "guide.html", is_home=False, title=page["title"], content_html=page["html"]
    )
