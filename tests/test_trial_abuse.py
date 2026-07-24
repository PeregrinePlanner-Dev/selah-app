"""Tests for the plus-alias trial-abuse deterrent added to pro_auth.py
2026-07-24 (Rick flagged the gap: the card-free 14-day/25-exchange Pro
trial, and the ongoing free tier, both have no deterrent against repeat
signups). Full review of options considered: 05- Future/
Selah_Structured_Audit_2026-07-24.md.

Only _is_plus_alias_email() is unit-tested here -- it's a pure function.
The two call sites (pro_auth.signup(), free_gate.access_request()) both
require a live/mocked Supabase call and are exercised manually against the
real DB instead, same as this codebase's other Supabase-touching routes.
"""

import pro_auth


def test_detects_plus_alias():
    assert pro_auth._is_plus_alias_email("rick+trial1@gmail.com") is True
    assert pro_auth._is_plus_alias_email("rick+anything@example.com") is True


def test_multiple_plus_signs_still_detected():
    assert pro_auth._is_plus_alias_email("r+i+c+k@example.com") is True


def test_plain_email_not_flagged():
    assert pro_auth._is_plus_alias_email("rick@gmail.com") is False
    assert pro_auth._is_plus_alias_email("rick.artistyle@example.com") is False


def test_plus_in_domain_part_not_flagged():
    """The check is scoped to the local part (before @) on purpose -- a
    "+" can't appear in a real domain name, but this confirms the split
    logic doesn't accidentally scan the whole string."""
    assert pro_auth._is_plus_alias_email("rick@sub+domain.example.com") is False


def test_empty_and_malformed_input_does_not_raise():
    assert pro_auth._is_plus_alias_email("") is False
    assert pro_auth._is_plus_alias_email("not-an-email") is False
    assert pro_auth._is_plus_alias_email("@") is False
