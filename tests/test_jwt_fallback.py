"""Tests for query_with_jwt_fallback()/_is_jwt_expired() in pro_auth.py,
added 2026-07-30 in direct response to Sentry PYTHON-3/4/5/6 (live
2026-07-28/29, all "APIError: JWT expired", all failing on the first
Supabase call in billing_status/org_status/list_sessions/pro_chat).

Root cause (see pro_auth.py's module comment above these two functions for
the full writeup): pro_app.html fires several endpoints concurrently on
page load; Supabase's single-use rotating refresh token means only one of
those requests' proactive-refresh attempts can win, and every losing
request can end up handing Postgrest a genuinely expired access token --
not recoverable within that request's own lifecycle (client-side session
cookie, 2 separate gunicorn worker processes). The fix falls back to the
service-role client, explicitly filtered by the caller, for exactly that
one error.

Only query_with_jwt_fallback()/_is_jwt_expired() are unit-tested here --
both are pure/callable-based and don't need a live Supabase connection. The
four real call sites (pro_billing._get_org_id_and_email/
_get_org_id_email_and_admin_status, pro_org.org_status,
pro_chat.list_sessions/pro_chat) are exercised manually against the real
DB, same as this codebase's other Supabase-touching routes (see
test_trial_abuse.py's docstring for the same convention)."""

import pro_auth


class _FakeAPIError(Exception):
    pass


def test_is_jwt_expired_matches_the_real_postgrest_message():
    # Real message text confirmed via the Sentry PYTHON-3/4/5/6 stacktraces,
    # not guessed.
    assert pro_auth._is_jwt_expired(_FakeAPIError("APIError: JWT expired")) is True


def test_is_jwt_expired_is_case_insensitive():
    assert pro_auth._is_jwt_expired(_FakeAPIError("jwt EXPIRED")) is True


def test_is_jwt_expired_false_for_unrelated_errors():
    assert pro_auth._is_jwt_expired(_FakeAPIError("Invalid Refresh Token: Already Used")) is False
    assert pro_auth._is_jwt_expired(ValueError("no profile found")) is False


def test_returns_user_query_result_when_it_succeeds():
    """The overwhelmingly common case -- RLS stays the real security
    boundary and the fallback never runs."""
    calls = {"service": 0}

    def user_query():
        return "real-rls-scoped-result"

    def service_query():
        calls["service"] += 1
        return "fallback-result"

    result = pro_auth.query_with_jwt_fallback(user_query, service_query)
    assert result == "real-rls-scoped-result"
    assert calls["service"] == 0


def test_falls_back_to_service_query_on_jwt_expired():
    def user_query():
        raise _FakeAPIError("APIError: JWT expired")

    def service_query():
        return "service-role-fallback-result"

    result = pro_auth.query_with_jwt_fallback(user_query, service_query)
    assert result == "service-role-fallback-result"


def test_reraises_non_jwt_errors_without_calling_fallback():
    """Confirms this wrapper doesn't quietly swallow or mask a real,
    unrelated bug (e.g. a genuine missing-profile or network error) by
    routing it through the fallback path too."""
    calls = {"service": 0}

    def user_query():
        raise ValueError("some other real bug")

    def service_query():
        calls["service"] += 1
        return "should never be reached"

    try:
        pro_auth.query_with_jwt_fallback(user_query, service_query)
        assert False, "expected ValueError to propagate"
    except ValueError as e:
        assert "some other real bug" in str(e)
    assert calls["service"] == 0
