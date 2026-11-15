"""Sponsored deposits and the airdrop through /markets/{token}/... with RealChainClient over a fake chain service."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from app.main import create_app
from markets.real_chain import RealChainClient

from .fake_chain_service import SECRET, FakeChainService
from .helpers import USDC, open_bet, w

BASE = "http://chain.test"


class Real:
    def __init__(self, app, client, fake, settings, repo, market, bet):
        self.app, self.c, self.fake, self.settings, self.repo, self.market, self.bet = app, client, fake, settings, repo, market, bet
        self.token = market.signer.token(bet.id)

    def url(self, path: str) -> str:
        return f"/markets/{self.token}{path}"

    async def post(self, path: str, body: dict):
        return await self.c.post(self.url(path), json=body)

    async def get(self, path: str, **params):
        return await self.c.get(self.url(path), params=params)

    async def fund(self, n: int, usd: int = 100, ata: bool = True):
        self.fake.set_wallet(w(n), usd * USDC, ata=ata)


@pytest.fixture
async def make_real(repo, gateway, logs, extractor, clock, provider, settings):
    clients: list[httpx.AsyncClient] = []

    async def make(handle: str = "+15550001111", **overrides) -> Real:
        fake = FakeChainService(clock)
        s = replace(settings, **{"chain_mode": "real", "chain_service_url": BASE, "chain_service_secret": SECRET, "airdrop_enabled": True, **overrides})
        http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))

        async def nosleep(_):
            pass

        chain = RealChainClient(BASE, SECRET, client=http, sleep=nosleep)
        app = create_app(s, repo=repo, gateway=gateway, logger=logs.logger, extractor=extractor, provider=provider, now=clock, chain_client=chain)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend.test")
        clients.extend([http, client])
        market = app.state.get_market()
        bet = await open_bet(market, provider, repo, handle=handle)
        return Real(app, client, fake, s, repo, market, bet)

    yield make
    for c in clients:
        await c.aclose()


def msg_for(bet_id, wallet, side, amount):
    return base64.b64encode(json.dumps([bet_id, wallet, side, amount]).encode()).decode()


async def tx(r: Real, n: int, side: int, usd, **extra):
    return await r.post("/deposit_tx", {"wallet": w(n), "side": side, "amount_usd": usd, **extra})


async def submit(r: Real, n: int, side: int, usd, base: int | None = None, shape: str = "message", **extra):
    amount = base if base is not None else int(float(usd) * USDC)
    m = msg_for(r.bet.id, w(n), side, amount)
    body = {"wallet": w(n), "side": side, "amount_usd": usd, **extra}
    body.update({"message_b64": m, "user_signature_b64": "c2ln"} if shape == "message" else {"signed_tx_b64": "SIGNED:" + m})
    return await r.post("/deposit_submit", body)


# ---------------- mock mode ----------------

async def test_all_four_endpoints_are_404_on_the_mock_chain(client, market, provider, repo):
    bet = await open_bet(market, provider, repo)
    t = market.signer.token(bet.id)
    for call in (
        client.get(f"/markets/{t}/wallet", params={"wallet": w(1)}),
        client.post(f"/markets/{t}/airdrop", json={"wallet": w(1)}),
        client.post(f"/markets/{t}/deposit_tx", json={"wallet": w(1), "side": 0, "amount_usd": "5"}),
        client.post(f"/markets/{t}/deposit_submit", json={"wallet": w(1), "side": 0, "amount_usd": "5"}),
    ):
        r = await call
        assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    r = await client.post(f"/markets/{t}/deposits", json={"wallet": w(1), "side": 0, "amount_usd": "5"})
    assert r.status_code == 200  # the mock deposit endpoint still works


# ---------------- wallet ----------------

async def test_wallet_reports_token_account_and_usdc(make_real):
    r = await make_real()
    body = (await r.get("/wallet", wallet=w(1))).json()
    assert (body["has_token_account"], body["usdc_base"], body["usdc_usd"]) == (False, 0, "0.00")
    await r.fund(1, 12)
    body = (await r.get("/wallet", wallet=w(1))).json()
    assert (body["has_token_account"], body["usdc_base"], body["usdc_usd"]) == (True, 12 * USDC, "12.00")
    bad = await r.get("/wallet", wallet="nope")
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_WALLET"
    assert (await r.c.get(f"/markets/garbage/wallet", params={"wallet": w(1)})).status_code == 404


# ---------------- airdrop ----------------

async def test_airdrop_mints_the_allowance_and_records_it(make_real):
    r = await make_real()
    res = await r.post("/airdrop", {"wallet": w(1)})
    body = res.json()
    assert res.status_code == 200 and body["minted_base"] == 25 * USDC and body["usdc_base"] == 25 * USDC and body["has_token_account"] is True
    assert body["signature"] == "airdrop-sig-1" and r.fake.mints == [(w(1), 25 * USDC)]
    async with r.repo.pool.connection() as conn:
        row = await (await conn.execute("select wallet, amount_base, bet_id::text, ip, tx_ref from airdrops")).fetchone()
    assert row == (w(1), 25 * USDC, r.bet.id, "127.0.0.1", "airdrop-sig-1")


async def test_a_repeat_request_returns_the_balance_and_mints_nothing(make_real):
    r = await make_real()
    await r.post("/airdrop", {"wallet": w(1)})
    again = await r.post("/airdrop", {"wallet": w(1)})
    body = again.json()
    assert again.status_code == 200 and body["ok"] and body["minted_base"] == 0 and body["usdc_base"] == 25 * USDC and body["signature"] is None
    assert len(r.fake.mints) == 1


async def test_the_per_wallet_cap_is_lifetime_and_partial_allowance_is_topped_up(make_real):
    r = await make_real()
    async with r.repo.pool.connection() as conn:  # the wallet already received 20 earlier
        await conn.execute("insert into airdrops (wallet, amount_base, bet_id, ip) values (%s, %s, %s, 'x')", (w(1), 20 * USDC, r.bet.id))
    body = (await r.post("/airdrop", {"wallet": w(1)})).json()
    assert body["minted_base"] == 5 * USDC
    assert (await r.post("/airdrop", {"wallet": w(1)})).json()["minted_base"] == 0


async def test_the_global_daily_cap(make_real):
    r = await make_real(airdrop_daily_cap_base=30 * USDC)
    assert (await r.post("/airdrop", {"wallet": w(1)})).status_code == 200  # 25 of 30
    blocked = await r.post("/airdrop", {"wallet": w(2)})
    assert blocked.status_code == 409 and blocked.json()["code"] == "AIRDROP_LIMIT"
    assert len(r.fake.mints) == 1
    # yesterday's airdrops do not count
    async with r.repo.pool.connection() as conn:
        await conn.execute("update airdrops set created_at = now() - interval '2 days'")
    assert (await r.post("/airdrop", {"wallet": w(2)})).status_code == 200


async def test_airdrop_disabled_flag(make_real):
    r = await make_real(airdrop_enabled=False)
    res = await r.post("/airdrop", {"wallet": w(1)})
    assert res.status_code == 409 and res.json()["code"] == "AIRDROP_DISABLED" and r.fake.mints == []


async def test_airdrop_rejects_bad_wallets_bodies_and_links(make_real):
    r = await make_real()
    for bad in ("", "short", "0" * 40, None, 12):
        res = await r.post("/airdrop", {"wallet": bad})
        assert res.status_code == 400 and res.json()["code"] == "INVALID_WALLET", bad
    assert (await r.c.post(r.url("/airdrop"), content=b"not json")).status_code == 400
    assert (await r.c.post("/markets/bogus/airdrop", json={"wallet": w(1)})).status_code == 404
    assert r.fake.mints == []


async def test_airdrop_per_ip_rate_limit(make_real):
    r = await make_real(airdrop_per_ip_per_hour=2)
    for n in (1, 2):
        assert (await r.post("/airdrop", {"wallet": w(n)})).status_code == 200
    res = await r.post("/airdrop", {"wallet": w(3)})
    assert res.status_code == 429 and res.json()["code"] == "RATE_LIMITED"


async def test_a_failed_mint_releases_the_reservation(make_real):
    r = await make_real()
    r.fake.fail_with["airdrop"] = (502, "RPC_ERROR")
    res = await r.post("/airdrop", {"wallet": w(1)})
    assert res.status_code == 502 and res.json()["code"] == "RPC_ERROR"
    async with r.repo.pool.connection() as conn:
        assert (await (await conn.execute("select count(*) from airdrops")).fetchone())[0] == 0
    assert (await r.post("/airdrop", {"wallet": w(1)})).json()["minted_base"] == 25 * USDC  # the allowance is intact


async def test_an_expired_link_cannot_airdrop(make_real, clock):
    r = await make_real()
    clock.current = r.bet.settle_after + timedelta(days=8)
    assert (await r.post("/airdrop", {"wallet": w(1)})).status_code == 410


# ---------------- deposit_tx ----------------

async def test_deposit_tx_returns_the_transaction_and_a_summary(make_real):
    r = await make_real()
    await r.fund(1)
    res = await tx(r, 1, 1, "5")
    body = res.json()
    assert res.status_code == 200 and body["ok"] and body["bet_id"] == r.bet.id
    assert body["transaction_b64"].startswith("UNSIGNED:") and body["message_b64"] == msg_for(r.bet.id, w(1), 1, 5 * USDC)
    assert body["blockhash"] == "BH1" and body["last_valid_block_height"] == 123 and body["expires_at"]
    s = body["summary"]
    assert (s["side"], s["team"], s["amount_base"], s["amount_usd"]) == (1, r.bet.side1_name, 5 * USDC, "5.00")
    assert s["fee_bps"] == 500 and s["fee_line"] == "A 5% fee on the losing side covers operating costs."
    assert s["payout_base"] == 5 * USDC and s["multiplier"] == "1.00"  # an empty pool: stake back
    assert r.bet.side1_name in s["text"] and "$5.00" in s["text"] and "5%" in s["text"]


async def test_deposit_tx_preview_is_net_of_fee_and_counts_a_top_up(make_real):
    r = await make_real()
    for n in (1, 2, 3):
        await r.fund(n)
    for n, side, usd in ((1, 0, "10"), (2, 1, "6")):
        await submit(r, n, side, usd)
    s = (await tx(r, 3, 1, "4")).json()["summary"]
    # side 1 would hold 6 + 4 = 10 against 10 on side 0: fee 0.5, distributable 19.5, 4 of 10 -> 7.8
    assert s["payout_base"] == 7_800_000 and s["multiplier"] == "1.95" and s["profit_base"] == 3_800_000
    top = (await tx(r, 2, 1, "4")).json()["summary"]  # w2 already holds 6: the whole 10 stake wins 19.5 * 10/10
    assert top["your_total_stake_base"] == 10 * USDC and top["payout_base"] == 19_500_000


@pytest.mark.parametrize("amount,code", [("1.0000001", "BAD_REQUEST"), ("-1", "BAD_REQUEST"), ("abc", "BAD_REQUEST"), ("", "BAD_REQUEST"), (True, "BAD_REQUEST")])
async def test_dollar_conversion_rejects_bad_amounts(make_real, amount, code):
    r = await make_real()
    await r.fund(1)
    res = await tx(r, 1, 0, amount)
    assert res.status_code == 400 and res.json()["code"] == code
    assert not any(q.url.path.startswith("/deposit") for q in r.fake.requests)


async def test_dollar_conversion_accepts_what_the_chain_expects(make_real):
    r = await make_real()
    await r.fund(1)
    for usd, base in (("5", 5 * USDC), ("5.5", 5_500_000), ("1.000001", 1_000_001), (5, 5 * USDC)):
        res = await tx(r, 1, 0, usd)
        assert res.status_code == 200 and res.json()["summary"]["amount_base"] == base, usd
    # a JSON number with decimals is parsed exactly, never as a float
    res = await r.c.post(r.url("/deposit_tx"), content=b'{"wallet":"%s","side":0,"amount_usd":5.5}' % w(1).encode(), headers={"content-type": "application/json"})
    assert res.json()["summary"]["amount_base"] == 5_500_000


async def test_deposit_tx_limits_and_input_errors(make_real):
    r = await make_real()
    await r.fund(1)
    assert (await tx(r, 1, 0, "0.5")).json()["code"] == "BELOW_MIN"
    assert (await tx(r, 1, 0, "21")).status_code == 409
    assert (await tx(r, 1, 0, "21")).json()["code"] == "ABOVE_MAX"
    for side in (2, "x", None, True):
        res = await tx(r, 1, side, "5")
        assert res.status_code == 400 and res.json()["code"] == "INVALID_SIDE", side
    assert (await r.post("/deposit_tx", {"wallet": "bad", "side": 0, "amount_usd": "5"})).json()["code"] == "INVALID_WALLET"


@pytest.mark.parametrize("code,status", [
    ("FUNDING_CLOSED", 409), ("MARKET_FULL", 409), ("ALREADY_OTHER_SIDE", 409), ("ABOVE_MAX", 409), ("BELOW_MIN", 409),
    ("NO_TOKEN_ACCOUNT", 409), ("INSUFFICIENT_USDC", 409), ("MARKET_NOT_OPEN", 409), ("UNTRUSTED_BET", 502),
    ("INVALID_PARAMS", 400), ("RATE_LIMITED", 429),
])
async def test_every_chain_service_code_maps_to_its_http_status(make_real, code, status):
    r = await make_real()
    await r.fund(1)
    r.fake.fail_with["build"] = (status if status != 502 else 400, code)  # the client maps by code, not by the service's status
    res = await tx(r, 1, 0, "5")
    assert res.status_code == status and res.json()["code"] == code


async def test_real_rules_come_back_from_the_chain(make_real):
    r = await make_real()
    await r.fund(1, 100)
    await r.fund(2, 3)
    assert (await tx(r, 3, 0, "5")).json()["code"] == "NO_TOKEN_ACCOUNT"
    assert (await tx(r, 2, 0, "5")).json()["code"] == "INSUFFICIENT_USDC"
    await submit(r, 1, 0, "5")
    res = await tx(r, 1, 1, "5")
    assert res.status_code == 409 and res.json()["code"] == "ALREADY_OTHER_SIDE"


async def test_deposit_tx_once_the_market_is_not_open(make_real, clock):
    r = await make_real()
    await r.fund(1)
    await r.fund(2)
    await submit(r, 1, 0, "5")
    await submit(r, 2, 1, "5")
    await r.market.close_funding(r.bet.id)
    res = await tx(r, 1, 0, "5")
    assert res.status_code == 409 and res.json()["code"] == "MARKET_NOT_OPEN" and res.json()["status"] == "locked"


async def test_deposit_tx_during_the_funding_cutoff(make_real, clock):
    r = await make_real()
    await r.fund(1)
    clock.current = r.bet.funding_closes_at - timedelta(seconds=10)
    res = await tx(r, 1, 0, "5")
    assert res.status_code == 409 and res.json()["code"] == "FUNDING_CLOSED"


async def test_a_chain_service_that_is_down_is_a_502(make_real):
    r = await make_real()
    await r.fund(1)
    r.fake.broken["POST build"] = 10
    res = await tx(r, 1, 0, "5")
    assert res.status_code == 502 and res.json()["code"] == "RPC_ERROR"


# ---------------- deposit_submit ----------------

async def test_submit_success_in_both_shapes_returns_the_signature_and_the_updated_view(make_real):
    r = await make_real()
    await r.fund(1)
    await r.fund(2)
    a = await submit(r, 1, 0, "5", shape="message")
    body = a.json()
    assert a.status_code == 200 and body["signature"] == "dep-sig-1" and body["explorer"].endswith("dep-sig-1") and body["already"] is False
    assert body["market"]["status"] == "open" and body["market"]["pools"]["side0_base"] == 5 * USDC and body["market"]["participant_count"] == 1
    b = await submit(r, 2, 1, "7", shape="signed")
    assert b.json()["signature"] == "dep-sig-2" and b.json()["market"]["pools"]["side1_base"] == 7 * USDC
    assert r.fake.wallets[w(1)]["usdc"] == 95 * USDC


async def test_submit_twice_is_safe(make_real):
    r = await make_real()
    await r.fund(1)
    first = (await submit(r, 1, 0, "5")).json()
    again = (await submit(r, 1, 0, "5")).json()
    assert again["signature"] == first["signature"] and again["already"] is True
    assert (await r.get("")).json()["pools"]["side0_base"] == 5 * USDC  # counted once


async def test_submit_top_up_on_the_same_side(make_real):
    r = await make_real()
    await r.fund(1)
    await submit(r, 1, 0, "5")
    res = await submit(r, 1, 0, "3")
    assert res.status_code == 200 and res.json()["market"]["pools"]["side0_base"] == 8 * USDC and res.json()["market"]["participant_count"] == 1


async def test_submit_stores_a_valid_display_name(make_real):
    r = await make_real()
    await r.fund(1)
    await submit(r, 1, 0, "5", display_name="  Dana   R ")
    assert (await r.repo.display_names(r.bet.id)) == {w(1): "Dana R"}
    listing = (await r.get("/participants")).json()["participants"]
    assert listing[0]["display_name"] == "Dana R"


async def test_a_bad_display_name_is_refused_before_any_chain_call(make_real):
    r = await make_real()
    await r.fund(1)
    before = len(r.fake.requests)
    for name in ("", "x" * 25, "see http://evil.test", "a\x00b", 5):
        res = await submit(r, 1, 0, "5", display_name=name)
        assert res.status_code == 400 and res.json()["code"] == "BAD_DISPLAY_NAME", repr(name)
    assert len(r.fake.requests) == before and r.fake.sent == {}


async def test_a_failed_submit_stores_no_name_and_maps_errors(make_real):
    r = await make_real()
    await r.fund(1)
    tampered = await submit(r, 1, 0, "5", base=6 * USDC, display_name="Dana")  # message says 6, request says 5
    assert tampered.status_code == 400 and tampered.json()["code"] == "TX_REJECTED"
    assert await r.repo.display_names(r.bet.id) == {}
    r.fake.expired = True
    expired = await submit(r, 1, 0, "5", display_name="Dana")
    assert expired.status_code == 409 and expired.json()["code"] == "TX_EXPIRED"
    assert await r.repo.display_names(r.bet.id) == {}
    r.fake.expired = False
    r.fake.fail_with["submit"] = (502, "SUBMIT_FAILED")
    assert (await submit(r, 1, 0, "5")).status_code == 502
    assert (await r.post("/deposit_submit", {"wallet": w(1), "side": 0, "amount_usd": "5"})).json()["code"] == "BAD_REQUEST"
    assert (await r.post("/deposit_submit", {"wallet": w(1), "side": 0, "amount_usd": "5", "message_b64": 7, "user_signature_b64": "x"})).status_code == 400
    assert r.fake.sent == {}


async def test_the_last_participant_closes_funding_at_once(make_real):
    r = await make_real(max_participants=2)
    assert r.bet.max_participants == 2
    await r.fund(1)
    await r.fund(2)
    first = await submit(r, 1, 0, "5")
    assert first.json()["market"]["status"] == "open"
    second = await submit(r, 2, 1, "5")
    assert second.json()["market"]["status"] == "locked"
    assert (await r.repo.get_bet(r.bet.id)).status == "locked"
    assert {s.wallet for s in await r.repo.snapshots(r.bet.id)} == {w(1), w(2)}
    await r.fund(3)
    res = await tx(r, 3, 0, "5")
    assert res.status_code == 409 and res.json()["code"] == "MARKET_NOT_OPEN"


# ---------------- public rate limits ----------------

async def test_public_endpoints_are_rate_limited_per_wallet_and_per_ip(make_real):
    r = await make_real(public_rate_per_wallet_per_min=2)
    await r.fund(1)
    assert (await r.get("/wallet", wallet=w(1))).status_code == 200
    assert (await tx(r, 1, 0, "5")).status_code == 200
    limited = await tx(r, 1, 0, "5")
    assert limited.status_code == 429 and limited.json()["code"] == "RATE_LIMITED"
    assert (await r.get("/wallet", wallet=w(2))).status_code == 200  # another wallet is fine
    q = await make_real(handle="+15550002222", public_rate_per_ip_per_min=3, public_rate_per_wallet_per_min=100)
    codes = [(await q.get("/wallet", wallet=w(n))).status_code for n in range(1, 6)]
    assert codes == [200, 200, 200, 429, 429]


async def test_proxy_headers_are_only_trusted_when_configured(make_real):
    r = await make_real(public_rate_per_ip_per_min=1, public_rate_per_wallet_per_min=100)
    assert (await r.c.get(r.url("/wallet"), params={"wallet": w(1)}, headers={"x-forwarded-for": "9.9.9.9"})).status_code == 200
    assert (await r.c.get(r.url("/wallet"), params={"wallet": w(1)}, headers={"x-forwarded-for": "8.8.8.8"})).status_code == 429  # ignored header: same peer
    t = await make_real(handle="+15550002222", public_rate_per_ip_per_min=1, public_rate_per_wallet_per_min=100, trust_proxy_headers=True)
    assert (await t.c.get(t.url("/wallet"), params={"wallet": w(1)}, headers={"x-forwarded-for": "9.9.9.9"})).status_code == 200
    assert (await t.c.get(t.url("/wallet"), params={"wallet": w(1)}, headers={"x-forwarded-for": "8.8.8.8"})).status_code == 200


# ---------------- RealChainClient additions ----------------

async def test_a_mint_is_never_repeated_after_the_service_answered(make_real):
    r = await make_real()
    calls = []
    real = r.fake.handle

    def handle(request):
        if request.url.path == "/airdrop":
            calls.append(1)
            return httpx.Response(503, text="busy")
        return real(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    from markets.real_chain import RealChainClient as C

    async def nosleep(_):
        pass

    c = C(BASE, SECRET, client=http, sleep=nosleep)
    from markets.chain import ChainError
    with pytest.raises(ChainError):
        await c.airdrop(w(1), 5 * USDC)
    assert len(calls) == 1
    await http.aclose()
