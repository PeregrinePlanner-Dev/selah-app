"""Tests for UTM/referrer first-touch attribution, added to app.py 2026-07-24
(confirmed priority by Rick on 2026-07-20; profiles had zero marketing-
attribution columns before this). Covers only the capture hook
(_capture_utm_attribution in app.py) -- the write-once-to-profiles halves in
pro_auth.signup() and free_gate._complete_signin() both require a live/mocked
Supabase call and are exercised manually against the real DB instead (same
approach already used for this codebase's other Supabase-touching routes,
which have no mocked-Supabase test layer yet -- see SELAH_BUILD_PROTOCOL.md
Gate 6, Phases 2-3 not yet built).

app.py can't be imported directly in this sandbox (engine.py's module-level
Anthropic() client hits a SOCKS-proxy artifact here, not a real bug -- see
test_secret_validation.py's docstring for the same issue). Isolates
_capture_utm_attribution() the same way: extract its source via ast, exec it
against a throwaway Flask app's real request/session proxies.
"""

import ast
import pathlib

import pytest
from flask import Flask, request, session

APP_PY = pathlib.Path(__file__).parent.parent / "app.py"


def _load_capture_fn():
    source = APP_PY.read_text()
    tree = ast.parse(source)
    # _UTM_KEYS is a module-level tuple the function's globals need.
    utm_keys_node = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_UTM_KEYS" for t in node.targets)
    )
    func_node = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_capture_utm_attribution"
    )
    func_node.decorator_list = []  # drop @app.before_request -- app isn't defined here, and
    # exec'ing this in isolation is about testing the function's own logic, not registering it.
    namespace = {"request": request, "session": session}
    exec(ast.unparse(utm_keys_node), namespace)
    exec(ast.unparse(func_node), namespace)
    return namespace["_capture_utm_attribution"]


@pytest.fixture
def app():
    app = Flask(__name__)
    app.secret_key = "test-only-secret-not-used-anywhere-real"
    return app


def test_captures_utm_params_on_first_visit(app):
    capture = _load_capture_fn()
    with app.test_request_context("/?utm_source=facebook&utm_medium=cpc&utm_campaign=summer_launch"):
        capture()
        assert session["utm_source"] == "facebook"
        assert session["utm_medium"] == "cpc"
        assert session["utm_campaign"] == "summer_launch"
        assert session["utm_captured"] is True


def test_captures_referrer_when_no_utm_params(app):
    capture = _load_capture_fn()
    with app.test_request_context("/", headers={"Referer": "https://www.google.com/search?q=selah"}):
        capture()
        assert session["signup_referrer"] == "https://www.google.com/search?q=selah"


def test_no_capture_on_bare_direct_visit(app):
    """Direct navigation, no campaign params, no referrer -- nothing should
    be written except the utm_captured marker itself, and no utm_* keys
    should appear in the session at all (distinguishes 'checked, found
    nothing' from 'never checked')."""
    capture = _load_capture_fn()
    with app.test_request_context("/"):
        capture()
        assert session["utm_captured"] is True
        assert "utm_source" not in session
        assert "signup_referrer" not in session


def test_does_not_overwrite_already_captured_session(app):
    """Core first-touch guarantee: a second page view later in the same
    session, even with different (or no) UTM params, must not clobber the
    first visit's attribution."""
    capture = _load_capture_fn()
    with app.test_request_context("/?utm_source=facebook"):
        capture()
        assert session["utm_source"] == "facebook"
        # Simulate a second request reusing the same session dict.
        session_snapshot = dict(session)

    with app.test_request_context("/?utm_source=google&utm_campaign=different"):
        for k, v in session_snapshot.items():
            session[k] = v
        capture()
        # Second call is a no-op because utm_captured is already set.
        assert session["utm_source"] == "facebook"
        assert "utm_campaign" not in session


def test_query_param_length_is_capped():
    """Defensive cap on attacker-controlled query params -- these get
    written straight into a DB column with no other validation."""
    capture_source = ast.unparse(
        next(
            node for node in ast.parse(APP_PY.read_text()).body
            if isinstance(node, ast.FunctionDef) and node.name == "_capture_utm_attribution"
        )
    )
    assert "[:200]" in capture_source  # utm_* fields capped
    assert "[:500]" in capture_source  # referrer capped (URLs run longer)


def test_utm_keys_constant_matches_migrated_columns():
    """Sanity check that the code's captured keys match what was actually
    migrated onto profiles (utm_source/medium/campaign/term/content) --
    catches a typo'd column name silently going nowhere."""
    tree = ast.parse(APP_PY.read_text())
    utm_keys_node = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_UTM_KEYS" for t in node.targets)
    )
    namespace = {}
    exec(ast.unparse(utm_keys_node), namespace)
    assert namespace["_UTM_KEYS"] == (
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"
    )
