"""Tests for _validate_required_secrets(), added to app.py 2026-07-24
(Selah_Structured_Audit_2026-07-24.md finding #2: every secret was read via
os.environ.get(key, "")/a default with zero presence check, including
app.secret_key falling back to a literal public string when unset).

app.py cannot be imported directly in this sandbox -- engine.py (imported
transitively via pro_chat.py/free_gate.py) constructs an Anthropic() client
at module import time, which fails here with a SOCKS-proxy/httpx sandbox
artifact unrelated to real app correctness (confirmed live on Render).
So this isolates _validate_required_secrets() by extracting just that
function's source via ast and exec'ing it in a throwaway namespace with a
fake os.environ -- the same technique already used to isolation-test this
function's logic before the original commit.
"""

import ast
import pathlib

import pytest

APP_PY = pathlib.Path(__file__).parent.parent / "app.py"


def _load_validate_required_secrets():
    source = APP_PY.read_text()
    tree = ast.parse(source)
    func_node = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_validate_required_secrets"
    )
    func_source = ast.unparse(func_node)

    class _FakeOs:
        def __init__(self, environ):
            self.environ = environ

    namespace = {}
    exec(func_source, namespace)
    return namespace["_validate_required_secrets"], _FakeOs


ALL_HARD_REQUIRED = [
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "FLASK_SECRET_KEY",
    "ANTHROPIC_API_KEY_FREE",
]

ALL_IMPORTANT = [
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "RESEND_API_KEY",
]

FULL_VALID_ENV = {k: "present" for k in ALL_HARD_REQUIRED + ALL_IMPORTANT}


def _validator_with_env(env):
    """Loads a fresh copy of the function and points its captured globals
    at a fake os.environ -- a function's __globals__ dict is mutable and
    live, so overwriting "os" in it before calling is enough; no need to
    re-exec the function body per test."""
    validate, FakeOs = _load_validate_required_secrets()
    validate.__globals__["os"] = FakeOs(env)
    return validate


def test_boots_silently_when_everything_present(capsys):
    validate = _validator_with_env(dict(FULL_VALID_ENV))
    validate()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("missing_key", ALL_HARD_REQUIRED)
def test_refuses_to_start_when_any_hard_required_secret_missing(missing_key):
    env = dict(FULL_VALID_ENV)
    del env[missing_key]
    validate = _validator_with_env(env)
    with pytest.raises(SystemExit) as exc_info:
        validate()
    assert missing_key in str(exc_info.value)
    assert "CRITICAL" in str(exc_info.value)


def test_no_insecure_fallback_flask_secret_key_treated_as_missing():
    """The bug this fix actually closes: FLASK_SECRET_KEY empty string
    (unset in the real os.environ.get(key, default) pattern) must be
    treated as missing, not silently accepted."""
    env = dict(FULL_VALID_ENV)
    env["FLASK_SECRET_KEY"] = ""
    validate = _validator_with_env(env)
    with pytest.raises(SystemExit) as exc_info:
        validate()
    assert "FLASK_SECRET_KEY" in str(exc_info.value)


@pytest.mark.parametrize("missing_key", ALL_IMPORTANT)
def test_important_secret_missing_logs_but_does_not_exit(missing_key, capsys):
    env = dict(FULL_VALID_ENV)
    del env[missing_key]
    validate = _validator_with_env(env)
    validate()
    out = capsys.readouterr().out
    assert missing_key in out
    assert "CRITICAL" in out


def test_multiple_missing_hard_secrets_all_listed_in_one_error():
    env = dict(FULL_VALID_ENV)
    del env["SUPABASE_URL"]
    del env["ANTHROPIC_API_KEY_FREE"]
    validate = _validator_with_env(env)
    with pytest.raises(SystemExit) as exc_info:
        validate()
    message = str(exc_info.value)
    assert "SUPABASE_URL" in message
    assert "ANTHROPIC_API_KEY_FREE" in message
