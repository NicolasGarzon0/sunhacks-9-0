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
    # worker (chunk 5)
    result_away_score: int | None = None
    result_home_score: int | None = None
    result_winner_side: int | None = None
    result_first_seen_at: datetime | None = None
    final_confirmed_at: datetime | None = None
    last_polled_at: datetime | None = None
    provider_calls: int = 0
    # chain parity (chunk 3A): closing the Bet account (reclaims the rent)
    chain_closed_at: datetime | None = None
    close_bet_attempts: int = 0
    close_bet_next_attempt_at: datetime | None = None
    close_bet_last_error: str | None = None


@dataclass(frozen=True)
class PayoutTask:
    id: str
    bet_id: str
    wallet: str
    kind: str  # win | refund | close_loser
    amount_base: int
    status: str  # pending | done | failed
    attempts: int
    next_attempt_at: datetime | None
    last_error: str | None
    tx_ref: str | None


@dataclass(frozen=True)
class Snapshot:
    """A position as it stood when funding closed. Views read this, because on chain a claimed or closed position is gone."""

    bet_id: str
    wallet: str
    side: int
    amount_base: int
    payout_base: int | None
    status: str  # held | paid | refunded | closed_loser


SNAPSHOT_COLUMNS = "bet_id::text as bet_id, wallet, side, amount_base, payout_base, status"
TASK_COLUMNS = "id::text as id, bet_id::text as bet_id, wallet, kind, amount_base, status, attempts, next_attempt_at, last_error, tx_ref"


BET_COLUMNS = (
    "id::text as id, proposer_handle, game_id, league, home_id, home_name, away_id, away_name, "
    "kickoff_utc, status, created_at, side0_name, side1_name, chain_bet_id, fee_bps, treasury, min_stake_base, "
    "max_stake_base, max_participants, funding_closes_at, settle_after, opened_at, locked_at, settled_at, "
    "winning_side, total_pool_base, fee_paid_base, failure_reason, refund_reason, payouts_completed_at, "
    "result_away_score, result_home_score, result_winner_side, result_first_seen_at, final_confirmed_at, "
    "last_polled_at, provider_calls, chain_closed_at, close_bet_attempts, close_bet_next_attempt_at, close_bet_last_error"
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

    # --- worker (chunk 5) ---

    async def bets_by_status(self, *statuses: str) -> list[Bet]:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"select {BET_COLUMNS} from bets where status = any(%s) order by created_at", (list(statuses),)
            )
            return [Bet(**r) for r in await cur.fetchall()]

    async def unpaid_closed_bets(self) -> list[Bet]:
        """Settled or refunded markets whose payouts are not complete yet."""
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"select {BET_COLUMNS} from bets where status in ('settled', 'refunded') "
                "and payouts_completed_at is null order by created_at"
            )
            return [Bet(**r) for r in await cur.fetchall()]

    async def settle_bet(self, bet_id: str, winning_side: int, fee_paid_base: int, total_pool_base: int, at: datetime) -> bool:
        return await self._transition(
            "update bets set status = 'settled', winning_side = %s, fee_paid_base = %s, total_pool_base = %s, "
            "settled_at = %s where id = %s and status = 'locked'",
            (winning_side, fee_paid_base, total_pool_base, at, bet_id),
        )

    async def set_refund_intent(self, bet_id: str, reason: str) -> None:
        """Record why a refund is about to happen (before the chain call), so a crash can be repaired with the right reason."""
        async with self.pool.connection() as conn:
            await conn.execute(
                "update bets set refund_reason = %s where id = %s and status in ('open', 'locked') and refund_reason is null",
                (reason, bet_id),
            )

    async def record_result_read(self, bet_id: str, home: int, away: int, winner: int | None, at: datetime) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "update bets set result_home_score = %s, result_away_score = %s, result_winner_side = %s, "
                "result_first_seen_at = %s where id = %s",
                (home, away, winner, at, bet_id),
            )

    async def clear_result_read(self, bet_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "update bets set result_home_score = null, result_away_score = null, result_winner_side = null, "
                "result_first_seen_at = null where id = %s and final_confirmed_at is null",
                (bet_id,),
            )

    async def mark_final_confirmed(self, bet_id: str, at: datetime) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("update bets set final_confirmed_at = %s where id = %s", (at, bet_id))

    async def mark_polled(self, bet_id: str, at: datetime) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "update bets set last_polled_at = %s, provider_calls = provider_calls + 1 where id = %s", (at, bet_id)
            )

    # payout tasks

    async def create_payout_tasks(self, bet_id: str, rows: list[tuple[str, str, int]]) -> int:
        """rows: (wallet, kind, amount_base). Idempotent: one task per bet and wallet."""
        created = 0
        async with self.pool.connection() as conn:
            for wallet, kind, amount in rows:
                cur = await conn.execute(
                    "insert into payout_tasks (bet_id, wallet, kind, amount_base) "
                    "values (%s, %s, %s, %s) on conflict (bet_id, wallet) do nothing",
                    (bet_id, wallet, kind, amount),
                )
                created += cur.rowcount
        return created

    async def due_payout_tasks(self, now: datetime, limit: int = 50) -> list[PayoutTask]:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"select {TASK_COLUMNS} from payout_tasks where status = 'pending' "
                "and (next_attempt_at is null or next_attempt_at <= %s) order by created_at limit %s",
                (now, limit),
            )
            return [PayoutTask(**r) for r in await cur.fetchall()]

    async def payout_tasks(self, bet_id: str) -> list[PayoutTask]:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(f"select {TASK_COLUMNS} from payout_tasks where bet_id = %s order by wallet", (bet_id,))
            return [PayoutTask(**r) for r in await cur.fetchall()]

    async def get_payout_task(self, task_id: str) -> PayoutTask | None:
        try:
            uuid.UUID(str(task_id))
        except ValueError:
            return None
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(f"select {TASK_COLUMNS} from payout_tasks where id = %s", (task_id,))
            row = await cur.fetchone()
            return PayoutTask(**row) if row else None

    async def task_done(self, task_id: str, amount: int, tx_ref: str | None, at: datetime) -> None:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "update payout_tasks set status = 'done', amount_base = %s, tx_ref = %s, attempts = attempts + 1, "
                    "last_error = null, next_attempt_at = null, updated_at = %s where id = %s",
                    (amount, tx_ref, at, task_id),
                )
                # keep the snapshot in step: win -> paid, refund -> refunded, close_loser -> closed_loser (payout 0)
                await conn.execute(
                    "update position_snapshots s set "
                    "status = case t.kind when 'win' then 'paid' when 'refund' then 'refunded' else 'closed_loser' end, "
                    "payout_base = case when t.kind = 'close_loser' then 0 else %s end, updated_at = %s "
                    "from payout_tasks t where t.id = %s and s.bet_id = t.bet_id and s.wallet = t.wallet",
                    (amount, at, task_id),
                )

    async def task_retry_later(self, task_id: str, error: str, next_at: datetime, at: datetime) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "update payout_tasks set attempts = attempts + 1, last_error = %s, next_attempt_at = %s, updated_at = %s "
                "where id = %s",
                (error[:200], next_at, at, task_id),
            )

    async def task_failed(self, task_id: str, error: str, at: datetime) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "update payout_tasks set status = 'failed', attempts = attempts + 1, last_error = %s, "
                "next_attempt_at = null, updated_at = %s where id = %s",
                (error[:200], at, task_id),
            )

    async def retry_failed_task(self, task_id: str, at: datetime) -> bool:
        return await self._transition(
            "update payout_tasks set status = 'pending', attempts = 0, next_attempt_at = %s, updated_at = %s "
            "where id = %s and status = 'failed'",
            (at, at, task_id),
        )

    async def open_task_count(self, bet_id: str) -> int:
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "select count(*) from payout_tasks where bet_id = %s and status <> 'done' and kind <> 'close_loser'", (bet_id,)
            )).fetchone()
            return int(row[0])

    async def pending_task_count(self, bet_id: str) -> int:
        """Every task that is not done, of any kind (claims and loser closes). close_bet waits for all of them."""
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "select count(*) from payout_tasks where bet_id = %s and status <> 'done'", (bet_id,)
            )).fetchone()
            return int(row[0])

    # position snapshots

    async def upsert_snapshots(self, bet_id: str, rows: list[tuple[str, int, int]]) -> int:
        """rows: (wallet, side, amount_base). Adds new positions and follows top ups while a position is still held;
        a snapshot that already moved on (paid, refunded, closed_loser) is never touched."""
        n = 0
        async with self.pool.connection() as conn:
            for wallet, side, amount in rows:
                cur = await conn.execute(
                    "insert into position_snapshots (bet_id, wallet, side, amount_base) values (%s, %s, %s, %s) "
                    "on conflict (bet_id, wallet) do update set side = excluded.side, amount_base = excluded.amount_base, "
                    "updated_at = now() where position_snapshots.status = 'held' "
                    "and (position_snapshots.side, position_snapshots.amount_base) is distinct from (excluded.side, excluded.amount_base)",
                    (bet_id, wallet, side, amount),
                )
                n += cur.rowcount
        return n

    async def snapshots(self, bet_id: str) -> list[Snapshot]:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"select {SNAPSHOT_COLUMNS} from position_snapshots where bet_id = %s order by created_at, wallet", (bet_id,)
            )
            return [Snapshot(**r) for r in await cur.fetchall()]

    # closing the Bet account (rent)

    async def bets_to_close(self, now: datetime) -> list[Bet]:
        """Settled or refunded markets with every claim done whose Bet account is still open on chain."""
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"select {BET_COLUMNS} from bets where status in ('settled', 'refunded') and payouts_completed_at is not null "
                "and chain_closed_at is null and (close_bet_next_attempt_at is null or close_bet_next_attempt_at <= %s) "
                "order by created_at",
                (now,),
            )
            return [Bet(**r) for r in await cur.fetchall()]

    async def mark_chain_closed(self, bet_id: str, at: datetime) -> bool:
        return await self._transition(
            "update bets set chain_closed_at = %s, close_bet_last_error = null, close_bet_next_attempt_at = null "
            "where id = %s and chain_closed_at is null",
            (at, bet_id),
        )

    async def close_bet_failed(self, bet_id: str, error: str, next_at: datetime) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "update bets set close_bet_attempts = close_bet_attempts + 1, close_bet_last_error = %s, "
                "close_bet_next_attempt_at = %s where id = %s",
                (error[:200], next_at, bet_id),
            )

    # provider budget

    async def reserve_provider_call(self, month: str, budget: int) -> bool:
        """Count one real provider call for the month. False (and nothing counted) once the budget is used up."""
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into provider_usage (month, calls) values (%s, 0) on conflict (month) do nothing", (month,)
            )
            cur = await conn.execute(
                "update provider_usage set calls = calls + 1 where month = %s and calls < %s", (month, budget)
            )
            return cur.rowcount == 1

    async def provider_calls_in(self, month: str) -> int:
        async with self.pool.connection() as conn:
            row = await (await conn.execute("select calls from provider_usage where month = %s", (month,))).fetchone()
            return int(row[0]) if row else 0

    # status counts

    async def worker_counts(self) -> dict:
        async with self.pool.connection() as conn:
            bets = dict(await (await conn.execute("select status, count(*) from bets group by status")).fetchall())
            tasks = dict(await (await conn.execute("select status, count(*) from payout_tasks group by status")).fetchall())
            notices = dict(await (await conn.execute("select status, count(*) from bet_notices group by status")).fetchall())
            perm = (await (await conn.execute("select count(*) from bet_notices where permanent_failure")).fetchone())[0]
            closed = (await (await conn.execute("select count(*) from bets where chain_closed_at is not null")).fetchone())[0]
        return {
            "bets": bets, "payout_tasks": tasks, "notices": notices, "notices_permanent_failure": int(perm),
            "chain_closed_bets": int(closed),
        }

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

    # --- test USDC airdrop policy (migration 0008) ---

    async def reserve_airdrop(self, wallet: str, bet_id: str, ip: str, per_wallet_cap: int, daily_cap: int, want: int,
                              now: datetime) -> tuple[str, int, int]:
        """Atomically decide how much this wallet may still get. Returns (outcome, amount, already_received):
        outcome is 'reserved' (a row was inserted for `amount`), 'full' (the wallet already has its whole allowance)
        or 'daily_cap' (the global daily cap would be exceeded). Serialized by an advisory lock so two requests
        cannot both pass the caps."""
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("select pg_advisory_xact_lock(%s)", (0x4842_4149_5244,))  # "HBAIRD"
                got = int((await (await conn.execute(
                    "select coalesce(sum(amount_base), 0) from airdrops where wallet = %s", (wallet,))).fetchone())[0])
                remaining = per_wallet_cap - got
                if remaining <= 0:
                    return "full", 0, got
                today = int((await (await conn.execute(
                    "select coalesce(sum(amount_base), 0) from airdrops where created_at >= %s", (day_start,))).fetchone())[0])
                amount = min(want, remaining)
                if today + amount > daily_cap:
                    return "daily_cap", 0, got
                await conn.execute(
                    "insert into airdrops (wallet, amount_base, bet_id, ip, created_at) values (%s, %s, %s, %s, %s)",
                    (wallet, amount, bet_id, ip[:64], now),
                )
                return "reserved", amount, got

    async def finish_airdrop(self, wallet: str, bet_id: str, tx_ref: str | None, ok: bool, amount: int, now: datetime) -> None:
        """Record the transaction on the reserved row, or release the reservation when the mint failed."""
        async with self.pool.connection() as conn:
            if ok:
                await conn.execute(
                    "update airdrops set tx_ref = %s where id = (select id from airdrops where wallet = %s and bet_id = %s "
                    "and tx_ref is null and amount_base = %s order by created_at desc limit 1)",
                    (tx_ref, wallet, bet_id, amount),
                )
            else:
                await conn.execute(
                    "delete from airdrops where id = (select id from airdrops where wallet = %s and bet_id = %s "
                    "and tx_ref is null and amount_base = %s order by created_at desc limit 1)",
                    (wallet, bet_id, amount),
                )

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

    async def mark_notice(
        self, bet_id: str, kind: str, status: str, error: str | None = None, *,
        next_attempt_at: datetime | None = None, permanent: bool = False,
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "update bet_notices set status = %s, attempts = attempts + 1, last_error = %s, "
                "next_attempt_at = %s, permanent_failure = %s, "
                "sent_at = case when %s = 'sent' then now() else sent_at end where bet_id = %s and kind = %s",
                (status, error, next_attempt_at, permanent, status, bet_id, kind),
            )

    async def due_notices(self, now: datetime, max_attempts: int) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "select bet_id::text as bet_id, kind, attempts from bet_notices "
                "where status in ('pending', 'failed') and not permanent_failure and attempts < %s "
                "and (next_attempt_at is null or next_attempt_at <= %s) order by created_at",
                (max_attempts, now),
            )
            return list(await cur.fetchall())

    async def list_notices(self, bet_id: str) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "select kind, idempotency_key, status, attempts, last_error, permanent_failure from bet_notices where bet_id = %s order by kind",
                (bet_id,),
            )
            return list(await cur.fetchall())
