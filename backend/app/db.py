from __future__ import annotations

from psycopg_pool import AsyncConnectionPool


def make_pool(database_url: str, *, options: str | None = None, max_size: int = 10) -> AsyncConnectionPool:
    """Create (unopened) pool. prepare_threshold=None keeps it compatible with Supabase's transaction pooler.

    `options` lets tests pin a search_path to a throwaway schema.
    min_size=0: connections open on demand, so bad credentials fail per request instead of a background
    reconnect loop (which trips Supabase's pooler circuit breaker).
    """
    kwargs: dict = {"autocommit": True, "prepare_threshold": None}
    if options:
        kwargs["options"] = options
    return AsyncConnectionPool(
        conninfo=database_url,
        kwargs=kwargs,
        min_size=0,
        max_size=max_size,
        open=False,
        timeout=10,
    )
