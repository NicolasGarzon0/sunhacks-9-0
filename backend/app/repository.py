"""All database access goes through this module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

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
                "select handle, fields, status, pending_field, pending_replacement, updated_at "
                "from drafts where handle = %s",
                (handle,),
            )
            row = await cur.fetchone()
            return Draft(**row) if row else None

    async def save_draft(self, draft: Draft) -> None:
        """Insert or replace the single draft for this proposer handle."""
        async with self.pool.connection() as conn:
            await conn.execute(
                "insert into drafts (handle, fields, status, pending_field, pending_replacement, updated_at) "
                "values (%s, %s, %s, %s, %s, %s) "
                "on conflict (handle) do update set fields = excluded.fields, status = excluded.status, "
                "pending_field = excluded.pending_field, pending_replacement = excluded.pending_replacement, "
                "updated_at = excluded.updated_at",
                (
                    draft.handle,
                    Jsonb(draft.fields),
                    draft.status,
                    draft.pending_field,
                    Jsonb(draft.pending_replacement) if draft.pending_replacement is not None else None,
                    draft.updated_at,
                ),
            )

    async def delete_draft(self, handle: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("delete from drafts where handle = %s", (handle,))
