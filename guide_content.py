"""Selah for Church Staff: A Role-by-Role Guide -- public-facing role guide
pages, added 2026-08-01.

Built the same way Selah's existing theology nodes and theologian profiles
work (see engine.py): source content lives as plain markdown files, loaded
and converted to HTML once at startup, never re-parsed per request. This is
deliberately its own small module rather than folded into engine.py --
the guide is public marketing/enablement content (no login, no seat check),
while engine.py's content feeds the gated conversational product. Keeping
them separate means a change to one never risks the other.

Rick's framing for this feature (2026-08-01): not a revenue driver on its
own -- it's meant to help a church commit to signing up, by letting a
prospective or already-signed-up leader see a real, concrete preview of
what Selah produces for their specific role before/without needing an
account. So every page here is intentionally public: no login_required,
no seat_type or module_access check. A signed-in leader (any seat, admin
or not) can already see everything here too, simply because there's
nothing gating it.

Source markdown for each role was authored and edited directly in the
"Systematic Theology" vault (05- Future/Selah_Guide_*_2026-07-31.md) as
part of the ministry-role-prompt-workbook project; these are working
copies checked into the deploy tree so the live app doesn't depend on
that separate folder. Only the 11 roles that are genuine church-staff
positions are included here -- Authors & Podcasters and Seminary & Bible
College Students were deliberately scoped out of this guide (they're
individual-use cases, not a church role) per Rick's 2026-07-31 call; see
SESSION_LOG.md / the project file for that decision.
"""

from pathlib import Path
import markdown as _markdown

BASE_DIR = Path(__file__).parent
GUIDE_DIR = BASE_DIR / "guide"

_MD_EXTENSIONS = ["extra", "sane_lists"]

# Display order matches the role-card order already used in ministry.html /
# church.html's ROLES arrays, so the guide's own index page and the
# marketing-page popups stay in sync rather than drifting into two
# different orderings of the same 11 roles.
ROLE_ORDER = [
    ("senior-pastor", "Senior Pastor"),
    ("small-group-leader", "Small Group Leader"),
    ("youth-leader", "Youth Leader"),
    ("childrens-ministry", "Children's Ministry"),
    ("drama-worship-arts", "Drama & Worship Arts"),
    ("worship-music-leader", "Worship & Music Leader"),
    ("pastoral-care-counseling", "Pastoral Care & Counseling"),
    ("missions-outreach", "Missions & Outreach"),
    ("elder-board-training", "Elder / Board Training"),
    ("christian-school-teachers", "Christian School Teachers"),
    ("chaplaincy", "Chaplaincy"),
]

GUIDE_PAGES = {}
for _slug, _title in ROLE_ORDER:
    _path = GUIDE_DIR / f"{_slug}.md"
    _raw = _path.read_text(encoding="utf-8")
    # Drop the role's own leading "# Title" line -- the page template
    # renders the title itself (in the header, matching the site's h1
    # style), so keeping it in the converted body would duplicate it.
    _lines = _raw.splitlines()
    if _lines and _lines[0].startswith("# "):
        _lines = _lines[1:]
    _body_md = "\n".join(_lines).strip()
    GUIDE_PAGES[_slug] = {
        "title": _title,
        "html": _markdown.markdown(_body_md, extensions=_MD_EXTENSIONS),
    }

GUIDE_SLUGS = set(GUIDE_PAGES.keys())
