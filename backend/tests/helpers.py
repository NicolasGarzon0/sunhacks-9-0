"""Shared helpers for market tests."""

from __future__ import annotations

ALICE = "+15550001111"
USDC = 1_000_000


def w(n: int) -> str:
    """A valid base58 style wallet (32 to 44 chars, no 0 O I l)."""
    return f"W{n}" + "x" * 40


async def open_bet(market, provider, repo, handle: str = ALICE, team: str = "cardinals"):
    """Open a market on the soonest game for `team` through the real service; returns the stored bet."""
    [game] = await provider.find_candidates(team, None, None)
    result = await market.open_market(handle, game)
    assert result.kind == "opened", result
    return [b for b in await repo.list_bets(handle) if b.game_id == game.game_id][-1]


async def deposits(chain, bet_id, rows):
    """rows: (wallet number, side, dollars)."""
    for n, side, usd in rows:
        await chain.deposit(bet_id, w(n), side, int(usd * USDC))


async def force_settle(repo, market, bet_id: str, winning_side: int):
    """Chunk 5 owns settlement. Tests lock, settle on the mock chain, and mirror it into the bets row."""
    bet = await market.close_funding(bet_id)
    assert bet.status == "locked"
    cb = await market.chain.settle(bet_id, winning_side)
    async with repo.pool.connection() as conn:
        await conn.execute(
            "update bets set status = 'settled', winning_side = %s, fee_paid_base = %s, settled_at = now() where id = %s",
            (winning_side, cb.fee_paid_base, bet_id),
        )
    return await repo.get_bet(bet_id)


async def locked_market(market, provider, repo, chain, rows=((1, 0, 10), (2, 1, 5), (3, 1, 20))):
    """An open market with deposits, locked through close_funding."""
    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, list(rows))
    bet = await market.close_funding(bet.id)
    assert bet.status == "locked"
    return bet


def sent(fake_gateway, suffix: str) -> list[dict]:
    return [s for s in fake_gateway.sends if s["idempotencyKey"].endswith(suffix)]
