from __future__ import annotations

import json

import pytest

from app.config import ConfigError, load_settings
from app.logging import Logger, mask_handle, mask_text


def test_missing_values_are_all_named():
    with pytest.raises(ConfigError) as e:
        load_settings({}, env_file=None)
    msg = str(e.value)
    for name in ("DATABASE_URL", "GATEWAY_SHARED_SECRET", "ADMIN_SECRET", "ANTHROPIC_API_KEY"):
        assert name in msg


def test_defaults_and_repr_hides_secrets():
    s = load_settings(
        {"DATABASE_URL": "postgresql://u:pw@h/db", "GATEWAY_SHARED_SECRET": "gsecret", "ADMIN_SECRET": "asecret",
         "ANTHROPIC_API_KEY": "sk-ant-secret", "LINK_SIGNING_SECRET": "linksecret" * 4,
         "DEPOSIT_PAGE_URL": "http://localhost:5173"},
        env_file=None,
    )
    assert s.port == 8788 and s.gateway_url == "http://localhost:8787"
    assert "linksecret" not in repr(s)
    assert "pw" not in repr(s) and "gsecret" not in repr(s) and "asecret" not in repr(s) and "sk-ant" not in repr(s)
    assert s.haiku_model == "claude-haiku-5-5" and s.local_timezone == "America/Phoenix"
    assert not hasattr(s, "max_stake_usd")


BASE = {
    "DATABASE_URL": "x", "GATEWAY_SHARED_SECRET": "x", "ADMIN_SECRET": "x", "ANTHROPIC_API_KEY": "x",
    "LINK_SIGNING_SECRET": "s" * 32, "DEPOSIT_PAGE_URL": "http://localhost:5173",
}


def test_missing_anthropic_key_fails_fast():
    env = {k: v for k, v in BASE.items() if k != "ANTHROPIC_API_KEY"}
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load_settings(env, env_file=None)


def test_bad_timezone():
    with pytest.raises(ConfigError, match="LOCAL_TIMEZONE"):
        load_settings({**BASE, "LOCAL_TIMEZONE": "Mars/Base"}, env_file=None)


def test_bad_port():
    with pytest.raises(ConfigError, match="BACKEND_PORT"):
        load_settings({**BASE, "BACKEND_PORT": "abc"}, env_file=None)


def test_gateway_port_is_ignored():
    # Root .env PORT is the gateway's; the backend must not pick it up.
    assert load_settings({**BASE, "PORT": "8787"}, env_file=None).port == 8788
    assert load_settings({**BASE, "PORT": "8787", "BACKEND_URL": "http://localhost:9001"}, env_file=None).port == 9001
    assert load_settings({**BASE, "BACKEND_URL": "http://localhost:9001", "BACKEND_PORT": "9100"}, env_file=None).port == 9100


def test_env_file_parsing(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# c\nDATABASE_URL="postgresql://x"\nexport GATEWAY_SHARED_SECRET=g\nADMIN_SECRET=a\nANTHROPIC_API_KEY=k\nPORT=8787\nLINK_SIGNING_SECRET=' + "s" * 32 + '\nDEPOSIT_PAGE_URL=http://localhost:5173\n')
    s = load_settings({}, env_file=f)
    assert s.database_url == "postgresql://x" and s.port == 8788


def test_masking():
    assert mask_handle("+15551234567") == "***4567"
    assert mask_handle("abc") == "***"
    assert mask_text("failed for +1 (555) 123-4567 now") == "failed for ***4567 now"
    assert mask_text("mail bob@example.com") == "mail ***.com"
    assert mask_text("order 12345") == "order 12345"


def test_logger_masks_handle_fields_and_filters_level():
    lines: list[str] = []
    log = Logger("info", write=lines.append)
    log.debug("hidden", text="secret text")
    log.info("x", handle="+15551234567", to="+15557654321", detail="err +15550001111")
    assert len(lines) == 1
    e = json.loads(lines[0])
    assert e["handle"] == "***4567" and e["to"] == "***4321" and e["detail"] == "err ***1111"


# --- pool market settings (chunk 4) ---

def test_market_defaults():
    s = load_settings(BASE, env_file=None)
    assert (s.min_stake_base, s.max_stake_base, s.max_participants) == (1_000_000, 20_000_000, 20)
    assert (s.fee_bps, s.chain_mode, s.link_grace_hours) == (500, "mock", 2)
    assert (s.funding_minutes_before_kickoff, s.settle_hours_after_kickoff) == (60, 6)
    assert s.treasury_pubkey.startswith("MockTreasury")  # clearly fake, mock mode only
    assert s.deposit_page_url == "http://localhost:5173" and s.deposit_page_origin == "http://localhost:5173"
    assert "ssssssss" not in repr(s)


def test_market_overrides_and_dollar_conversion():
    s = load_settings(
        {**BASE, "MIN_STAKE_USD": "2.50", "MAX_STAKE_USD": "100", "MAX_PARTICIPANTS": "5", "FEE_BPS": "1000",
         "DEPOSIT_PAGE_URL": "https://bets.example.com/app/", "TREASURY_PUBKEY": "RealLooking",
         "DEPOSIT_PAGE_ORIGIN": "https://bets.example.com", "FUNDING_MINUTES_BEFORE_KICKOFF": "5"},
        env_file=None,
    )
    assert (s.min_stake_base, s.max_stake_base, s.max_participants, s.fee_bps) == (2_500_000, 100_000_000, 5, 1000)
    assert s.deposit_page_url == "https://bets.example.com/app" and s.deposit_page_origin == "https://bets.example.com"
    assert s.treasury_pubkey == "RealLooking" and s.funding_minutes_before_kickoff == 5


@pytest.mark.parametrize(
    "override, message",
    [
        ({"FEE_BPS": "1001"}, "FEE_BPS"),
        ({"FEE_BPS": "abc"}, "FEE_BPS"),
        ({"FEE_BPS": "-1"}, "FEE_BPS"),
        ({"MIN_STAKE_USD": "0"}, "MIN_STAKE_USD"),
        ({"MIN_STAKE_USD": "1.5.5"}, "MIN_STAKE_USD"),
        ({"MAX_STAKE_USD": "0.5"}, "MAX_STAKE_USD"),
        ({"MAX_PARTICIPANTS": "1"}, "MAX_PARTICIPANTS"),
        ({"LINK_SIGNING_SECRET": "too short"}, "LINK_SIGNING_SECRET"),
        ({"DEPOSIT_PAGE_URL": "ftp://x"}, "DEPOSIT_PAGE_URL"),
        ({"DEPOSIT_PAGE_ORIGIN": "https://x.test/path"}, "DEPOSIT_PAGE_ORIGIN"),
        ({"CHAIN_MODE": "maybe"}, "CHAIN_MODE"),
        ({"CHAIN_MODE": "real"}, "TREASURY_PUBKEY"),
        ({"CHAIN_MODE": "real", "TREASURY_PUBKEY": "x"}, "not implemented"),
    ],
)
def test_market_config_errors_are_clear(override, message):
    with pytest.raises(ConfigError, match=message):
        load_settings({**BASE, **override}, env_file=None)


def test_missing_link_settings_are_named():
    env = {k: v for k, v in BASE.items() if k not in ("LINK_SIGNING_SECRET", "DEPOSIT_PAGE_URL")}
    with pytest.raises(ConfigError) as e:
        load_settings(env, env_file=None)
    assert "LINK_SIGNING_SECRET" in str(e.value) and "DEPOSIT_PAGE_URL" in str(e.value)
