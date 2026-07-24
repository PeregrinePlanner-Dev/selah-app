"""Tests for the CSRF fix added 2026-07-24 (Selah_Structured_Audit_2026-07-24.md
finding #1: admin/account-mutating POST routes had no CSRF protection --
free_admin.html's 6 forms and pro_app.html's account-delete form were
submittable cross-site via a bare auto-submitting <form> on any other page,
no JS or preflight needed since the app sends no CORS/SameSite hardening).

pro_auth.csrf_token()/csrf_valid() read/write Flask's `session` and `request`
proxies directly rather than taking them as arguments, so these tests use a
throwaway Flask app + test_request_context() to get real request/session
proxies instead of trying to fake Flask's internals.
"""

import secrets

import pytest
from flask import Flask, session

import pro_auth


@pytest.fixture
def app():
    app = Flask(__name__)
    app.secret_key = "test-only-secret-not-used-anywhere-real"
    return app


def test_csrf_token_mints_and_persists_within_session(app):
    with app.test_request_context("/"):
        token1 = pro_auth.csrf_token()
        token2 = pro_auth.csrf_token()
        assert token1 == token2
        assert len(token1) == 64  # secrets.token_hex(32) -> 64 hex chars


def test_csrf_token_is_stored_in_session(app):
    with app.test_request_context("/"):
        token = pro_auth.csrf_token()
        assert session["csrf_token"] == token


def test_csrf_valid_accepts_matching_token(app):
    with app.test_request_context("/", method="POST", data={"csrf_token": ""}):
        token = pro_auth.csrf_token()
    # Simulate a real request: session already has a token, form submits it.
    with app.test_request_context("/", method="POST", data={"csrf_token": token}):
        session["csrf_token"] = token
        assert pro_auth.csrf_valid() is True


def test_csrf_valid_rejects_missing_field(app):
    with app.test_request_context("/", method="POST"):
        session["csrf_token"] = secrets.token_hex(32)
        assert pro_auth.csrf_valid() is False


def test_csrf_valid_rejects_wrong_token(app):
    with app.test_request_context("/", method="POST", data={"csrf_token": "attacker-guess"}):
        session["csrf_token"] = secrets.token_hex(32)
        assert pro_auth.csrf_valid() is False


def test_csrf_valid_rejects_when_session_has_no_token_yet(app):
    """The actual cross-site attack scenario this fix closes: attacker's
    page has no session cookie value to read, so it can only submit a
    guessed or blank token -- and a session that never called csrf_token()
    (e.g. old cookie predating this fix) must fail closed, not open."""
    with app.test_request_context("/", method="POST", data={"csrf_token": "anything"}):
        assert pro_auth.csrf_valid() is False


def test_csrf_valid_never_raises_on_empty_everything(app):
    with app.test_request_context("/", method="POST"):
        assert pro_auth.csrf_valid() is False
