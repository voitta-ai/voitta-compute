"""Multi-account google_oauth core: legacy migration, selector
resolution, per-account token isolation, deterministic probe order,
wire redaction, and the duplicate-email guard on token exchange.

Settings I/O is redirected into tmp_path by pointing user_settings'
config-dir constants there — the same paths google_oauth reads and the
token cache keys off, so per-user isolation is exercised for real.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.services import google_oauth, user_settings


class _NoNetworkClient:
    """httpx.AsyncClient stand-in: any request raises — keeps the suite
    hermetic (disconnect()'s best-effort revoke swallows the error)."""

    def __init__(self, *a, **kw): ...

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *a, **kw):
        raise ConnectionError("network disabled in tests")


@pytest.fixture
def settings_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated settings dir + empty token cache + no current user +
    no outbound network."""
    monkeypatch.setattr(user_settings, "USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(google_oauth.httpx, "AsyncClient", _NoNetworkClient)
    from app.services import current_user

    current_user.set_current_email(None)
    google_oauth._token_cache.clear()
    yield tmp_path
    google_oauth._token_cache.clear()


def _tok(email: str, scopes: str = (
    "openid email "
    "https://www.googleapis.com/auth/drive.readonly "
    "https://www.googleapis.com/auth/spreadsheets"
)) -> dict:
    return {
        "access_token": f"at-{email}",
        "refresh_token": f"rt-{email}",
        "expires_at": time.time() + 3600,
        "scope": scopes,
        "account_email": email,
        "token_type": "Bearer",
    }


def _seed_two_accounts() -> tuple[str, str]:
    """work (default, full scopes) + personal (drive-only scope)."""
    a = google_oauth.create_account("Work", "cid-1", "sec-1")
    b = google_oauth.create_account("Personal", "cid-2", "sec-2")
    google_oauth._set_tokens(a, _tok("roman@agnitio.ai"))
    google_oauth._set_tokens(
        b,
        _tok(
            "roman.semein@gmail.com",
            scopes="openid email https://www.googleapis.com/auth/drive.readonly",
        ),
    )
    return a, b


# ---- legacy migration -------------------------------------------------------


def test_legacy_flat_blob_migrates_once(settings_home: Path):
    user_settings.write({
        "googleOAuth": {
            "clientId": "legacy-cid",
            "clientSecret": "legacy-sec",
            "tokens": _tok("old@user.com"),
        }
    })
    oauth = google_oauth._load_oauth()
    assert set(oauth) == {"defaultAccount", "accounts"}
    (aid, entry), = oauth["accounts"].items()
    assert oauth["defaultAccount"] == aid
    assert entry["clientId"] == "legacy-cid"
    assert entry["label"] == "old@user.com"
    assert entry["tokens"]["refresh_token"] == "rt-old@user.com"
    # Persisted: the raw file now holds the migrated shape.
    on_disk = user_settings.read()["googleOAuth"]
    assert on_disk["accounts"][aid]["clientId"] == "legacy-cid"
    # Grant survives the migration end-to-end.
    assert google_oauth.is_connected(aid)
    assert google_oauth.account_email(aid) == "old@user.com"


def test_empty_settings_no_migration_write(settings_home: Path):
    oauth = google_oauth._load_oauth()
    assert oauth == {"defaultAccount": None, "accounts": {}}
    assert not (settings_home / "settings.json").exists()


# ---- selector resolution ----------------------------------------------------


def test_resolve_by_id_email_label_and_default(settings_home: Path):
    a, b = _seed_two_accounts()
    assert google_oauth.resolve_account(a) == a
    assert google_oauth.resolve_account("ROMAN@agnitio.ai") == a
    assert google_oauth.resolve_account("personal") == b
    assert google_oauth.resolve_account(None) == a  # default = first created


def test_resolve_unknown_lists_accounts(settings_home: Path):
    _seed_two_accounts()
    with pytest.raises(google_oauth.UnknownAccount) as exc:
        google_oauth.resolve_account("nope@nowhere.com")
    assert "roman@agnitio.ai" in str(exc.value)
    assert "roman.semein@gmail.com" in str(exc.value)


def test_resolve_ambiguous_label_is_error(settings_home: Path):
    a, b = _seed_two_accounts()
    google_oauth._load_oauth()  # ensure shape
    oauth = google_oauth._load_oauth()
    oauth["accounts"][b]["label"] = "Work"  # collide with a's label
    google_oauth._save_oauth(oauth)
    with pytest.raises(google_oauth.UnknownAccount, match="ambiguous"):
        google_oauth.resolve_account("work")


def test_no_default_when_unconfigured(settings_home: Path):
    with pytest.raises(google_oauth.UnknownAccount, match="no default"):
        google_oauth.resolve_account(None)


# ---- per-account state ------------------------------------------------------


def test_scope_and_connection_gates(settings_home: Path):
    a, b = _seed_two_accounts()
    assert google_oauth.is_connected()            # any
    assert google_oauth.has_sheets_scope()        # any
    assert google_oauth.has_sheets_scope(a)
    assert not google_oauth.has_sheets_scope(b)   # personal is drive-only
    assert not google_oauth._needs_reauth(a)
    assert google_oauth._needs_reauth(b)          # missing sheets scope


def test_probe_order_default_first_and_scope_filtered(settings_home: Path):
    a, b = _seed_two_accounts()
    assert google_oauth.connected_account_ids() == [a, b]
    google_oauth.set_default_account(b)
    assert google_oauth.connected_account_ids() == [b, a]
    assert google_oauth.connected_account_ids(google_oauth.SHEETS_SCOPE) == [a]


def test_delete_account_moves_default_and_drops_cache(settings_home: Path):
    import asyncio

    a, b = _seed_two_accounts()
    asyncio.run(google_oauth.delete_account(a))
    assert google_oauth.resolve_account(None) == b
    assert google_oauth.account_email(a) is None
    assert google_oauth._token_cache.get(google_oauth._cache_key(a)) is None


def test_creds_change_disconnects_label_change_does_not(settings_home: Path):
    import asyncio

    a, _b = _seed_two_accounts()
    asyncio.run(google_oauth.update_account(a, label="Renamed"))
    assert google_oauth.is_connected(a)           # label-only: grant kept
    assert google_oauth.account_label(a) == "Renamed"
    asyncio.run(google_oauth.update_account(a, client_id="new-cid"))
    assert not google_oauth.is_connected(a)       # creds change: revoked


def test_token_cache_isolated_per_user_settings_path(
    settings_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Server mode: two users' settings files must never share cached
    tokens even for identical account ids."""
    from app.services import current_user

    a, _ = _seed_two_accounts()
    assert google_oauth._get_tokens(a)["account_email"] == "roman@agnitio.ai"

    monkeypatch.setattr(
        current_user, "USER_DATA_ROOT", settings_home / "data", raising=True
    )
    token = current_user.set_current_email("other@user.com")
    try:
        # Same account id, different user → no settings, no cache hit.
        assert google_oauth._get_tokens(a) is None
        assert not google_oauth.is_connected()
    finally:
        current_user.reset_current_email(token)
    assert google_oauth._get_tokens(a)["account_email"] == "roman@agnitio.ai"


# ---- describe_accounts (dynamic tool description) ---------------------------


def test_describe_accounts_roster(settings_home: Path):
    assert google_oauth.describe_accounts() == ""
    _seed_two_accounts()
    text = google_oauth.describe_accounts()
    assert "roman@agnitio.ai" in text and "[default]" in text
    assert "roman.semein@gmail.com" in text
    assert "[no Sheets scope]" in text


# ---- wire redaction ---------------------------------------------------------


def test_redacted_for_wire_strips_all_tokens(settings_home: Path):
    _seed_two_accounts()
    from app.settings import redacted_for_wire

    g = redacted_for_wire()["googleOAuth"]
    for acct in g["accounts"].values():
        assert "tokens" not in acct
        assert "clientId" in acct
    emails = {a.get("account_email") for a in g["accounts"].values()}
    assert emails == {"roman@agnitio.ai", "roman.semein@gmail.com"}
    # Nothing token-shaped anywhere in the redacted payload.
    import json

    dumped = json.dumps(g)
    assert "at-roman" not in dumped and "rt-roman" not in dumped


# ---- duplicate-email guard on exchange -------------------------------------


def test_exchange_rejects_email_already_on_other_account(
    settings_home: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio
    import base64
    import json as _json

    a, b = _seed_two_accounts()

    # Fake Google token endpoint returning work's email for account b.
    payload = base64.urlsafe_b64encode(
        _json.dumps({"email": "roman@agnitio.ai"}).encode()
    ).decode().rstrip("=")
    id_token = f"x.{payload}.y"

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "access_token": "new-at",
                "refresh_token": "new-rt",
                "expires_in": 3600,
                "scope": "",
                "id_token": id_token,
            }

    class _Client:
        def __init__(self, *a, **kw): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(google_oauth.httpx, "AsyncClient", _Client)
    with pytest.raises(RuntimeError, match="already connected"):
        asyncio.run(google_oauth.exchange_code("code", b))
    # And the existing grants were not clobbered.
    assert google_oauth.account_email(b) == "roman.semein@gmail.com"
