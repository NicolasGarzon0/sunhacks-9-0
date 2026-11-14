"""All database access goes through this module."""

from __future__ import annotations

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
    status: str
    created_at: datetime


BET_COLUMNS = (
    "id::text as id, proposer_handle, game_id, league, home_id, home_name, away_id, away_name, "
    "kickoff_utc, status, created_at"
)
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

    async def create_or_get_bet(self, proposer_handle: str, game: dict) -> tuple[Bet, bool]:
        """One active confirmed bet per game per proposer. Returns (bet, created)."""
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                "insert into bets (proposer_handle, game_id, league, home_id, home_name, away_id, away_name, "
                "kickoff_utc, status) values (%s, %s, %s, %s, %s, %s, %s, %s, 'game_confirmed') "
                f"on conflict (proposer_handle, game_id) where status = 'game_confirmed' do nothing returning {BET_COLUMNS}",
                (
                    proposer_handle, game["game_id"], game["league"], game["home"]["id"], game["home"]["name"],
                    game["away"]["id"], game["away"]["name"], game["kickoff_utc"],
                ),
            )
            row = await cur.fetchone()
            if row:
                return Bet(**row), True
            await cur.execute(
                f"select {BET_COLUMNS} from bets where proposer_handle = %s and game_id = %s "
                "and status = 'game_confirmed'",
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
