"""Public market API (/markets/{token}/...). Every status, every error mapping, CORS."""

from __future__ import annotations

from datetime import timedelta

import pytest

from .helpers import ALICE, deposits, force_settle, open_bet, w

ORIGIN = "https://deposit.test"


@pytest.fixture
async def mk(market, provider, repo, chain):
    bet = await open_bet(market, provider, repo)
    return bet, market.signer.token(bet.id)


async def get(client, token, path="", **params):
    r = await client.get(f"/markets/{token}{path}", params=params)
    return r.status_code, r.json()


async def post(client, token, path, body=None, content=None):
    r = await client.post(f"/markets/{token}{path}", json=body) if content is None else await client.post(
        f"/markets/{token}{path}", content=content, headers={"Content-Type": "application/json"}
    )
    return r.status_code, r.json()


async def test_view_open(client, mk, chain):
    bet, token = mk
    await deposits(chain, bet.id, [(1, 0, 10), (2, 1, 10)])
    code, v = await get(client, token)
    assert code == 200 and v["ok"] and v["status"] == "open" and v["bet_id"] == bet.id
    assert v["teams"] == {"side0": {"name": "Seattle Seahawks", "role": "away"}, "side1": {"name": "Arizona Cardinals", "role": "home"}}
    assert v["kickoff_utc"] == "2026-11-15T21:05:00Z" and v["funding_closes_at"] == "2026-11-15T20:05:00Z"
    assert v["pools"] == {"side0_base": 10_000_000, "side1_base": 10_000_000, "side0_usd": "10.00", "side1_usd": "10.00"}
    assert (v["total_pool_base"], v["total_pool_usd"], v["participant_count"]) == (20_000_000, "20.00", 2)
    assert v["limits"] == {
        "min_stake_base": 1_000_000, "min_stake_usd": "1.00", "max_stake_base": 20_000_000, "max_stake_usd": "20.00",
        "max_participants": 20,
    }
    assert v["fee_bps"] == 500 and v["multipliers"] == {"side0": "1.95", "side1": "1.95"}
    assert v["fee_line"] == "A 5% fee on the losing side covers operating costs." and "game_pending" not in v


async def test_view_empty_market_has_no_multipliers_yet(client, mk):
    _, token = mk
    _, v = await get(client, token)
    assert v["multipliers"] == {"side0": None, "side1": None} and v["total_pool_base"] == 0


async def test_view_locked_settled_refunded_failed(client, market, provider, repo, chain):
    # locked
    a = await open_bet(market, provider, repo, team="cardinals")
    await deposits(chain, a.id, [(1, 0, 10), (2, 1, 20), (3, 1, 10)])
    await market.close_funding(a.id)
    ta = market.signer.token(a.id)
    _, v = await get(client, ta)
    assert v["status"] == "locked" and v["game_pending"] is True and v["pools"]["side1_usd"] == "30.00"
    assert v["multipliers"] == {"side0": "3.85", "side1": "1.31"}  # 38.5 / 10 and 39.5 / 30, floored
    # settled: side 1 wins
    await force_settle(repo, market, a.id, 1)
    code, v = await get(client, ta)
    assert code == 200 and v["status"] == "settled" and v["winning_side"] == 1
    assert (v["fee_paid_base"], v["distributable_base"], v["final_multiplier"]) == (500_000, 39_500_000, "1.31")
    assert v["fee_paid_usd"] == "0.50" and v["distributable_usd"] == "39.50"
    # refunded
    b = await open_bet(market, provider, repo, handle="+15550002222")
    await deposits(chain, b.id, [(5, 0, 4)])
    await market.close_funding(b.id)
    _, v = await get(client, market.signer.token(b.id))
    assert v["status"] == "refunded" and v["refund_reason"] == "too_few_participants" and v["multipliers"] is None
    # failed (never opened on the chain)
    c = await open_bet(market, provider, repo, handle="+15550003333")
    await repo.fail_bet(c.id, "boom")
    code, v = await get(client, market.signer.token(c.id))
    assert code == 200 and v["status"] == "failed" and "failure_reason" not in v


async def test_participants_and_position_in_every_status(client, market, provider, repo, chain):
    bet = await open_bet(market, provider, repo)
    t = market.signer.token(bet.id)
    await deposits(chain, bet.id, [(1, 0, 10), (2, 1, 20), (3, 1, 10)])
    assert (await post(client, t, "/participants", {"wallet": w(2), "display_name": "  Dana   Lee "}))[1]["display_name"] == "Dana Lee"

    async def check(status, payouts):
        code, p = await get(client, t, "/participants")
        assert code == 200 and [(x["wallet"], x["side"], x["stake_usd"], x["display_name"]) for x in p["participants"]] == [
            (w(1), 0, "10.00", None), (w(2), 1, "20.00", "Dana Lee"), (w(3), 1, "10.00", None),
        ]
        for n, expected in payouts.items():
            code, pos = await get(client, t, "/position", wallet=w(n))
            assert code == 200 and pos["status"] == status and pos["has_position"] and pos["paid"] is False
            assert pos["payout_base"] == expected, (status, n)
        code, none = await get(client, t, "/position", wallet=w(9))
        assert code == 200 and none["has_position"] is False and none["side"] is None

    await check("open", {1: None, 2: None, 3: None})
    await market.close_funding(bet.id)
    await check("locked", {1: None, 2: None, 3: None})
    await force_settle(repo, market, bet.id, 1)  # side 1 holds 30 vs losing 10: fee 0.5, distributable 39.5
    await check("settled", {1: 0, 2: 26_333_333, 3: 13_166_666})
    await chain.claim(bet.id, w(3))
    _, pos = await get(client, t, "/position", wallet=w(3))
    assert pos["paid"] is True and pos["payout_base"] == 13_166_666
    # a refunded market pays stakes back
    other = await open_bet(market, provider, repo, handle="+15550002222")
    t2 = market.signer.token(other.id)
    await deposits(chain, other.id, [(1, 0, 6)])
    await market.close_funding(other.id)
    _, pos = await get(client, t2, "/position", wallet=w(1))
    assert (pos["status"], pos["payout_base"], pos["payout_usd"]) == ("refunded", 6_000_000, "6.00")
    code, bad = await get(client, t2, "/position", wallet="nope")
    assert (code, bad["code"]) == (400, "INVALID_WALLET")
    assert (await get(client, t2, "/position"))[0] == 400  # missing wallet


async def test_preview_is_net_of_fee_and_only_while_open(client, mk, chain, market):
    bet, token = mk
    await deposits(chain, bet.id, [(1, 0, 20), (2, 0, 20), (3, 0, 10), (4, 1, 20), (5, 1, 10)])
    # 20 on side 1 wins: pools 50 / 50, fee 2.50, distributable 97.50, this wallet holds 20 of 50
    code, p = await get(client, token, "/preview", side=1, amount_usd="20")
    assert code == 200 and (p["payout_base"], p["payout_usd"], p["profit_usd"]) == (39_000_000, "39.00", "19.00")
    assert p["multiplier"] == "1.95" and p["fee_line"] == "A 5% fee on the losing side covers operating costs."
    assert (await get(client, token, "/preview", side=0, amount_usd="0.5"))[1]["code"] == "BELOW_MIN"
    assert (await get(client, token, "/preview", side=0, amount_usd="21"))[1]["code"] == "ABOVE_MAX"
    assert (await get(client, token, "/preview", side=3, amount_usd="5"))[:2] == (400, {"ok": False, "code": "INVALID_SIDE", "detail": "side must be 0 or 1"})
    assert (await get(client, token, "/preview", side="", amount_usd="5"))[0] == 400
    assert (await get(client, token, "/preview", side=0, amount_usd="abc"))[0] == 400
    assert (await get(client, token, "/preview", side=0, amount_usd="1.2.3"))[0] == 400
    await market.close_funding(bet.id)
    code, e = await get(client, token, "/preview", side=1, amount_usd="20")
    assert (code, e["code"], e["status"]) == (409, "MARKET_NOT_OPEN", "locked")


@pytest.mark.parametrize(
    "name, ok",
    [("Dana", True), ("  Dana Lee  ", True), ("x" * 24, True), ("x" * 25, False), ("", False), ("   ", False),
     ("https://evil.example", False), ("visit evil.com", False), ("www.evil", False), ("bad\u0007name", False)],
)
async def test_display_name_validation(client, mk, name, ok):
    _, token = mk
    code, body = await post(client, token, "/participants", {"wallet": w(1), "display_name": name})
    assert (code == 200) is ok
    if ok:
        assert body["display_name"] == " ".join(name.split())
    else:
        assert (code, body["code"]) == (400, "BAD_DISPLAY_NAME")


async def test_display_name_update_replaces_and_bad_inputs(client, mk, repo):
    bet, token = mk
    await post(client, token, "/participants", {"wallet": w(1), "display_name": "First"})
    await post(client, token, "/participants", {"wallet": w(1), "display_name": "Second"})
    assert await repo.display_names(bet.id) == {w(1): "Second"}
    code, bad = await post(client, token, "/participants", {"wallet": "bad", "display_name": "x"})
    assert (code, bad["code"]) == (400, "INVALID_WALLET")
    assert (await post(client, token, "/participants", content="[1]"))[1]["code"] == "BAD_REQUEST"
    assert (await post(client, token, "/participants", content="not json"))[0] == 400
    assert (await post(client, token, "/participants", {"wallet": w(1)}))[1]["code"] == "BAD_DISPLAY_NAME"


async def test_deposit_success_returns_the_updated_market_view(client, mk, chain):
    bet, token = mk
    code, v = await post(client, token, "/deposits", {"wallet": w(1), "side": 0, "amount_usd": "12.50", "display_name": "Ann"})
    assert code == 200 and v["ok"] and v["pools"]["side0_base"] == 12_500_000 and v["participant_count"] == 1
    code, v = await post(client, token, "/deposits", {"wallet": w(1), "side": 0, "amount_usd": 5})  # top up with a JSON integer
    assert code == 200 and v["pools"]["side0_usd"] == "17.50" and v["participant_count"] == 1
    code, v = await post(client, token, "/deposits", {"wallet": w(2), "side": "1", "amount_usd": 2.25})  # JSON number: Decimal, never float
    assert code == 200 and v["pools"]["side1_base"] == 2_250_000
    _, p = await get(client, token, "/participants")
    assert p["participants"][0]["display_name"] == "Ann"


@pytest.mark.parametrize(
    "body, status, code",
    [
        ({"wallet": w(1), "side": 0, "amount_usd": "0.99"}, 409, "BELOW_MIN"),
        ({"wallet": w(1), "side": 0, "amount_usd": "20.01"}, 409, "ABOVE_MAX"),
        ({"wallet": "bad", "side": 0, "amount_usd": "5"}, 400, "INVALID_WALLET"),
        ({"wallet": w(1), "side": 2, "amount_usd": "5"}, 400, "INVALID_SIDE"),
        ({"wallet": w(1), "side": True, "amount_usd": "5"}, 400, "INVALID_SIDE"),
        ({"wallet": w(1), "side": 0, "amount_usd": "x"}, 400, "BAD_REQUEST"),
        ({"wallet": w(1), "side": 0, "amount_usd": "5.0000001"}, 400, "BAD_REQUEST"),
        ({"wallet": w(1), "side": 0, "amount_usd": "-5"}, 400, "BAD_REQUEST"),
        ({"wallet": w(1), "side": 0}, 400, "BAD_REQUEST"),
        ({"wallet": w(1), "side": 0, "amount_usd": "5", "display_name": "http://x.io"}, 400, "BAD_DISPLAY_NAME"),
    ],
)
async def test_deposit_error_mapping(client, mk, body, status, code):
    bet, token = mk
    s, e = await post(client, token, "/deposits", body)
    assert (s, e["ok"], e["code"]) == (status, False, code)
    assert (await get(client, token))[1]["participant_count"] == 0  # nothing was stored


async def test_deposit_rule_errors_from_the_chain(client, mk):
    bet, token = mk
    await post(client, token, "/deposits", {"wallet": w(1), "side": 0, "amount_usd": "15"})
    assert (await post(client, token, "/deposits", {"wallet": w(1), "side": 1, "amount_usd": "5"}))[1]["code"] == "ALREADY_OTHER_SIDE"
    assert (await post(client, token, "/deposits", {"wallet": w(1), "side": 0, "amount_usd": "6"}))[1]["code"] == "ABOVE_MAX"


async def test_market_full_for_new_wallets(client, market, provider, repo):
    from dataclasses import replace

    market.settings = replace(market.settings, max_participants=2)
    bet = await open_bet(market, provider, repo)
    t = market.signer.token(bet.id)
    await post(client, t, "/deposits", {"wallet": w(1), "side": 0, "amount_usd": "5"})
    # the second participant fills the market and closes it, so a third wallet sees a closed market
    await post(client, t, "/deposits", {"wallet": w(2), "side": 0, "amount_usd": "5"})
    code, e = await post(client, t, "/deposits", {"wallet": w(3), "side": 1, "amount_usd": "5"})
    assert (code, e["code"]) == (409, "MARKET_NOT_OPEN")
    assert (await get(client, t))[1]["status"] == "refunded"  # one sided


async def test_deposits_and_previews_outside_open_are_409(client, market, provider, repo, chain):
    bet = await open_bet(market, provider, repo)
    t = market.signer.token(bet.id)
    await deposits(chain, bet.id, [(1, 0, 5), (2, 1, 5)])
    await market.close_funding(bet.id)
    for call in (
        lambda: post(client, t, "/deposits", {"wallet": w(1), "side": 0, "amount_usd": "5"}),
        lambda: get(client, t, "/preview", side=0, amount_usd="5"),
    ):
        code, e = await call()
        assert (code, e["code"], e["status"], e["ok"]) == (409, "MARKET_NOT_OPEN", "locked", False)


async def test_deposits_are_disabled_outside_mock_mode(client, mk, chain):
    bet, token = mk
    chain.mode = "real"
    code, e = await post(client, token, "/deposits", {"wallet": w(1), "side": 0, "amount_usd": "5"})
    assert (code, e["code"]) == (404, "NOT_FOUND")
    assert (await get(client, token))[0] == 200  # reads still work


async def test_bad_and_expired_tokens(client, mk, market, repo, clock):
    bet, token = mk
    code, e = await get(client, token[:-2] + "xx")
    assert (code, e["code"], e["ok"]) == (404, "BAD_TOKEN", False)
    assert (await get(client, "garbage"))[1]["code"] == "BAD_TOKEN"
    unknown = market.signer.token("4cd57c06-fc4a-406d-9bdb-ae48507af330")  # validly signed, no such bet
    assert (await get(client, unknown))[1]["code"] == "BAD_TOKEN"
    # still valid long after funding closed and after kickoff
    clock.current = bet.funding_closes_at + timedelta(hours=3)
    assert (await get(client, token))[0] == 200
    # hard ceiling: settle_after + 7 days
    clock.current = bet.settle_after + timedelta(days=7)
    code, e = await get(client, token)
    assert (code, e["code"]) == (410, "LINK_EXPIRED")
    for path in ("/participants", "/preview", "/position"):
        assert (await get(client, token, path))[0] == 410


async def test_grace_window_after_payouts(client, mk, market, repo, clock):
    bet, token = mk
    await market.close_funding(bet.id)  # empty: refunded
    done = bet.settle_after
    clock.current = done
    await repo.mark_payouts_complete(bet.id, done)
    clock.current = done + timedelta(hours=1, minutes=59)
    assert (await get(client, token))[0] == 200
    clock.current = done + timedelta(hours=2, minutes=1)
    assert (await get(client, token))[0] == 410


async def test_cors_allows_only_the_deposit_page_origin(client, mk):
    _, token = mk
    ok = await client.get(f"/markets/{token}", headers={"Origin": ORIGIN})
    assert ok.headers["access-control-allow-origin"] == ORIGIN
    other = await client.get(f"/markets/{token}", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers
    pre = await client.options(
        f"/markets/{token}/deposits",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"},
    )
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == ORIGIN
    assert "POST" in pre.headers["access-control-allow-methods"]
    bad_pre = await client.options(
        f"/markets/{token}/deposits", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
    )
    assert "access-control-allow-origin" not in bad_pre.headers


async def test_no_floats_in_any_response_and_tokens_never_logged(client, mk, chain, logs):
    bet, token = mk
    await post(client, token, "/deposits", {"wallet": w(1), "side": 0, "amount_usd": "7.5"})
    await post(client, token, "/deposits", {"wallet": w(2), "side": 1, "amount_usd": "2.5"})

    def no_floats(v):
        if isinstance(v, float):
            raise AssertionError(f"float in response: {v}")
        if isinstance(v, dict):
            [no_floats(x) for x in v.values()]
        if isinstance(v, list):
            [no_floats(x) for x in v]

    for path, params in [("", {}), ("/participants", {}), ("/position", {"wallet": w(1)}), ("/preview", {"side": 1, "amount_usd": "3"})]:
        no_floats((await get(client, token, path, **params))[1])
    assert not any(token in line for line in logs.lines)


async def test_only_malformed_input_is_400_and_every_other_chain_error_stays_409(client, mk):
    """INVALID_WALLET and INVALID_SIDE are 400; the market rule errors are 409."""
    bet, token = mk
    await post(client, token, "/deposits", {"wallet": w(1), "side": 0, "amount_usd": "15"})
    cases = [
        ({"wallet": "bad", "side": 0, "amount_usd": "5"}, 400, "INVALID_WALLET"),
        ({"wallet": w(2), "side": 7, "amount_usd": "5"}, 400, "INVALID_SIDE"),
        ({"wallet": w(2), "side": 0, "amount_usd": "0.5"}, 409, "BELOW_MIN"),
        ({"wallet": w(2), "side": 0, "amount_usd": "25"}, 409, "ABOVE_MAX"),
        ({"wallet": w(1), "side": 1, "amount_usd": "2"}, 409, "ALREADY_OTHER_SIDE"),
        ({"wallet": w(1), "side": 0, "amount_usd": "6"}, 409, "ABOVE_MAX"),
    ]
    for body, status, code in cases:
        s, e = await post(client, token, "/deposits", body)
        assert (s, e["code"]) == (status, code), body
