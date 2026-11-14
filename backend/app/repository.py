"""All database access goes through this module."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


@dataclass(frozen=True)
class RosterEntry:
    name: str
    display_name: str
    handle: str


@dataclass
class Draft:
    handle: str
    fields: dict
    status: str  # collecting | ready
    pending_field: str | None
    pending_replacement: dict | None
    updated_at: datetime
    candidates: list | None = None  # serialized games offered for confirmation (pending_field == "confirm")
    confirm_misses: int = 0


@dataclass(frozen=True)
class Bet:
    id: str
    proposer_handle: str
    game_id: str
    league: str
    home_id: str
    home_name: str
    away_id: str
    away_name: str
    kickoff_utc: datetime
    status: str  # game_confirmed | open | locked | settled | refunded | failed
    created_at: datetime
    # market (chunk 4); side 0 = away team, side 1 = home team
    side0_name: str | None = None
    side1_name: str | None = None
    chain_bet_id: str | None = None
    fee_bps: int | None = None
    treasury: str | None = None
    min_stake_base: int | None = None
    max_stake_base: int | None = None
    max_participants: int | None = None
    funding_closes_at: datetime | None = None
    settle_after: datetime | None = None
    opened_at: datetime | None = None
    locked_at: datetime | None = None
    settled_at: datetime | None = None
    winning_side: int | None = None
    total_pool_base: int | None = None
    fee_paid_base: int | None = None
    failure_reason: str | None = None
    refund_reason: str | None = None
    payouts_completed_at: datetime | None = None


BET_COLUMNS = (
    "id::text as id, proposer_handle, game_id, league, home_id, home_name, away_id, away_name, "
    "kickoff_utc, status, created_at, side0_name, side1_name, chain_bet_id, fee_bps, treasury, min_stake_base, "
    "max_stake_base, max_participants, funding_closes_at, settle_after, opened_at, locked_at, settled_at, "
    "winning_side, total_pool_base, fee_paid_base, failure_reason, refund_reason, payouts_completed_at"
)
ACTIVE_STATUSES = "('game_confirmed', 'open', 'locked')"  # the unique index predicate in migration 0005
SIM_COLUMNS = (
    "game_id, league, home_id, home_name, away_id, away_name, kickoff_utc, status, home_score, away_score, official"
)


def _entry(row: dict | None) -> RosterEntry | None:
    return RosterEntry(**row) if row else None


class Repository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def ping(self) -> bool:
        async with self.pool.connection() as conn:
            await conn.execute("select 1")
        return True

    async def record_inbound(self, message_id: str, handle: str, received_at: datetime) -> bool:
        """Insert the inbound message; True if new, False if message_id was already recorded."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "insert into inbound_messages (message_id, handle, received_at) values (%s, %s, %s) "
                "on conflict (message_id) do nothing returning message_id",
                (message_id, handle, received_at),
            )
            return await cur.fetchone() is not None

    async def mark_processed(self, message_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "update inbound_messages set processed_at = now() where message_id = %s", (message_id,)
            )

    async def get_roster_by_handle(self, handle: str) -> RosterEntry | None:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute("select name, display_name, handle from roster where handle = %s", (handle,))
            return _entry(await cur.fetchone())

    async def get_roster_by_name(self, name: str) -> RosterEntry | None:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "select name, display_name, handle from roster where name = %s", (name.strip().lower(),)
            )
            return _entry(await cur.fetchone())

    async def list_roster(self) -> list[RosterEntry]:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute("select name, display_name, handle from roster order by name")
            return [RosterEntry(**r) for r in await cur.fetchall()]

    async def upsert_roster(self, name: str, display_name: str, handle: str) -> RosterEntry:
        """Add or update by name. The handle is stored exactly as given.

        Raises psycopg.errors.UniqueViolation if the handle already belongs to another name.
        """
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "insert into roster (name, display_name, handle) values (%s, %s, %s) "
                "on conflict (name) do update set display_name = excluded.display_name, handle = excluded.handle "
                "returning name, display_name, handle",
                (name.strip().lower(), display_name, handle),
            )
            return RosterEntry(**(await cur.fetchone()))  # type: ignore[arg-type]

    async def get_draft(self, handle: str) -> Draft | None:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "select handle, fields, status, pending_field, pending_replacement, updated_at, "
                "candidates, confirm_misses from drafts where handle = %s",
                (handle,),
            )
            row = await cur.fetchone()
            return Draft(**row) if row else None

    async def save_draft(self, draft: Draft) -> None:
        """Insert or replace the single draft for this proposer handle."""
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into drafts (handle, fields, status, pending_field, pending_replacement, updated_at, "
                "candidates, confirm_misses) values (%s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (handle) do update set fields = excluded.fields, status = excluded.status, "
                "pending_field = excluded.pending_field, pending_replacement = excluded.pending_replacement, "
                "updated_at = excluded.updated_at, candidates = excluded.candidates, "
                "confirm_misses = excluded.confirm_misses",
                (
                    draft.handle,
                    Jsonb(draft.fields),
                    draft.status,
                    draft.pending_field,
                    Jsonb(draft.pending_replacement) if draft.pending_replacement is not None else None,
                    draft.updated_at,
                    Jsonb(draft.candidates) if draft.candidates is not None else None,
                    draft.confirm_misses,
                ),
            )

    async def delete_draft(self, handle: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("delete from drafts where handle = %s", (handle,))

    # --- simulated games (state for SimulatedProvider; persisted so a restart keeps it) ---

    async def count_sim_games(self) -> int:
        async with self.pool.connection() as conn:
            row = await (await conn.execute("select count(*) from sim_games")).fetchone()
            return int(row[0])

    async def replace_sim_games(self, rows: list[dict[str, Any]]) -> None:
        """Atomically replace the whole simulated schedule."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("delete from sim_games")
                async with conn.cursor() as cur:
                    await cur.executemany(
                        "insert into sim_games (game_id, league, home_id, home_name, away_id, away_name, "
                        "kickoff_utc, status) values (%(game_id)s, %(league)s, %(home_id)s, %(home_name)s, "
                        "%(away_id)s, %(away_name)s, %(kickoff_utc)s, 'scheduled')",
                        rows,
                    )

    async def insert_sim_game(self, row: dict[str, Any]) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into sim_games (game_id, league, home_id, home_name, away_id, away_name, kickoff_utc, status) "
                "values (%(game_id)s, %(league)s, %(home_id)s, %(home_name)s, %(away_id)s, %(away_name)s, "
                "%(kickoff_utc)s, 'scheduled') on conflict (game_id) do nothing",
                row,
            )

    async def find_sim_games(self, team_ids: list[str], start: datetime, end: datetime) -> list[dict]:
        """Scheduled games involving any of team_ids with kickoff in (start, end], soonest first."""
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"select {SIM_COLUMNS} from sim_games where (home_id = any(%s) or away_id = any(%s)) "
                "and status = 'scheduled' and kickoff_utc > %s and kickoff_utc <= %s order by kickoff_utc, game_id",
                (team_ids, team_ids, start, end),
            )
            return list(await cur.fetchall())

    async def get_sim_game(self, game_id: str) -> dict | None:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(f"select {SIM_COLUMNS} from sim_games where game_id = %s", (game_id,))
            return await cur.fetchone()

    async def set_sim_game_status(self, game_id: str, status: str) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "update sim_games set status = %s, updated_at = now() where game_id = %s", (status, game_id)
            )
            return cur.rowcount == 1

    async def finish_sim_game(self, game_id: str, home_score: int, away_score: int) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "update sim_games set status = 'final', home_score = %s, away_score = %s, official = true, "
                "updated_at = now() where game_id = %s",
                (home_score, away_score, game_id),
            )
            return cur.rowcount == 1

    # --- bets ---

    async def create_or_get_bet(self, proposer_handle: str, game: dict, snapshot: dict) -> tuple[Bet, bool]:
        """One active market (game_confirmed, open or locked) per game per proposer. Returns (bet, created).

        `snapshot` is the config at creation time (stored so later config changes never touch the market):
        fee_bps, treasury, min_stake_base, max_stake_base, max_participants, funding_closes_at, settle_after.
        """
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "insert into bets (proposer_handle, game_id, league, home_id, home_name, away_id, away_name, "
                "kickoff_utc, status, side0_name, side1_name, fee_bps, treasury, min_stake_base, max_stake_base, "
                "max_participants, funding_closes_at, settle_after) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, 'game_confirmed', %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                f"on conflict (proposer_handle, game_id) where status in {ACTIVE_STATUSES} do nothing "
                f"returning {BET_COLUMNS}",
                (
                    proposer_handle, game["game_id"], game["league"], game["home"]["id"], game["home"]["name"],
                    game["away"]["id"], game["away"]["name"], game["kickoff_utc"],
                    game["away"]["name"], game["home"]["name"], snapshot["fee_bps"], snapshot["treasury"],
                    snapshot["min_stake_base"], snapshot["max_stake_base"], snapshot["max_participants"],
                    snapshot["funding_closes_at"], snapshot["settle_after"],
                ),
            )
            row = await cur.fetchone()
            if row:
                return Bet(**row), True
            await cur.execute(
                f"select {BET_COLUMNS} from bets where proposer_handle = %s and game_id = %s "
                f"and status in {ACTIVE_STATUSES}",
                (proposer_handle, game["game_id"]),
            )
            return Bet(**(await cur.fetchone())), False  # type: ignore[arg-type]

    async def list_bets(self, proposer_handle: str) -> list[Bet]:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"select {BET_COLUMNS} from bets where proposer_handle = %s order by created_at", (proposer_handle,)
            )
            return [Bet(**r) for r in await cur.fetchall()]

    async def get_bet(self, bet_id: str) -> Bet | None:
        try:
            uuid.UUID(str(bet_id))
        except ValueError:
            return None
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(f"select {BET_COLUMNS} from bets where id = %s", (bet_id,))
            row = await cur.fetchone()
            return Bet(**row) if row else None

    async def _transition(self, sql: str, params: tuple) -> bool:
        async with self.pool.connection() as conn:
            return (await conn.execute(sql, params)).rowcount == 1

    # Conditional status changes: each returns True only for the call that actually made the change.

    async def set_bet_open(self, bet_id: str, chain_bet_id: str, at: datetime) -> bool:
        return await self._transition(
            "update bets set status = 'open', chain_bet_id = %s, opened_at = %s where id = %s and status = 'game_confirmed'",
            (chain_bet_id, at, bet_id),
        )

    async def fail_bet(self, bet_id: str, reason: str) -> bool:
        return await self._transition(
            "update bets set status = 'failed', failure_reason = %s where id = %s and status in ('game_confirmed', 'open')",
            (reason[:200], bet_id),
        )

    async def lock_bet(self, bet_id: str, total_pool_base: int, at: datetime) -> bool:
        return await self._transition(
            "update bets set status = 'locked', total_pool_base = %s, locked_at = %s where id = %s and status = 'open'",
            (total_pool_base, at, bet_id),
        )

    async def refund_bet(self, bet_id: str, reason: str, total_pool_base: int, at: datetime) -> bool:
        """open or locked -> refunded, no fee (fee_paid_base = 0). `at` is stored as settled_at (when the market closed out)."""
        return await self._transition(
            "update bets set status = 'refunded', refund_reason = %s, total_pool_base = %s, fee_paid_base = 0, "
            "settled_at = %s where id = %s and status in ('open', 'locked')",
            (reason, total_pool_base, at, bet_id),
        )

    async def mark_payouts_complete(self, bet_id: str, at: datetime) -> bool:
        return await self._transition(
            "update bets set payouts_completed_at = %s where id = %s and status in ('settled', 'refunded')",
            (at, bet_id),
        )

    # --- participants (display names) ---

    async def set_display_name(self, bet_id: str, wallet: str, name: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into bet_participants (bet_id, wallet, display_name) values (%s, %s, %s) "
                "on conflict (bet_id, wallet) do update set display_name = excluded.display_name",
                (bet_id, wallet, name),
            )

    async def display_names(self, bet_id: str) -> dict[str, str]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "select wallet, display_name from bet_participants where bet_id = %s and display_name is not null",
                (bet_id,),
            )
            return {w: n for w, n in await cur.fetchall()}

    # --- proposer notices (durable "send once") ---

    async def ensure_notice(self, bet_id: str, kind: str, key: str) -> tuple[str, int]:
        """Create the notice row if missing; returns (status, attempts)."""
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into bet_notices (bet_id, kind, idempotency_key, status) values (%s, %s, %s, 'pending') "
                "on conflict do nothing",
                (bet_id, kind, key),
            )
            row = await (await conn.execute(
                "select status, attempts from bet_notices where bet_id = %s and kind = %s", (bet_id, kind)
            )).fetchone()
            return row[0], row[1]

    async def mark_notice(self, bet_id: str, kind: str, status: str, error: str | None = None) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "update bet_notices set status = %s, attempts = attempts + 1, last_error = %s, "
                "sent_at = case when %s = 'sent' then now() else sent_at end where bet_id = %s and kind = %s",
                (status, error, status, bet_id, kind),
            )

    async def list_notices(self, bet_id: str) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "select kind, idempotency_key, status, attempts, last_error from bet_notices where bet_id = %s order by kind",
                (bet_id,),
            )
            return list(await cur.fetchall())
