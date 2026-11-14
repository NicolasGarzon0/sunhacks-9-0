from __future__ import annotations

import json

import pytest

from app.config import ConfigError, load_settings
from app.logging import Logger, mask_handle, mask_text


def test_missing_values_are_all_named():
    with pytest.raises(ConfigError) as e:
        load_settings({}, env_file=None)
    msg = str(e.value)
    for name in ("DATABASE_URL", "GATEWAY_SHARED_SECRET", "ADMIN_SECRET"):
        assert name in msg


def test_defaults_and_repr_hides_secrets():
    s = load_settings(
        {"DATABASE_URL": "postgresql://u:pw@h/db", "GATEWAY_SHARED_SECRET": "gsecret", "ADMIN_SECRET": "asecret"},
        env_file=None,
    )
    assert s.port == 8788 and s.gateway_url == "http://localhost:8787"
    assert "pw" not in repr(s) and "gsecret" not in repr(s) and "asecret" not in repr(s)


BASE = {"DATABASE_URL": "x", "GATEWAY_SHARED_SECRET": "x", "ADMIN_SECRET": "x"}


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
    f.write_text('# c\nDATABASE_URL="postgresql://x"\nexport GATEWAY_SHARED_SECRET=g\nADMIN_SECRET=a\nPORT=8787\n')
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
