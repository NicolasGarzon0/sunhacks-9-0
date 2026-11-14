"""0005 must be re-runnable and must converge a drifted variant of its tables back to the file's definition."""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest

SQL = (Path(__file__).resolve().parent.parent / "migrations" / "0005_markets.sql").read_text()
MIGRATIONS = sorted((Path(__file__).resolve().parent.parent / "migrations").glob("*.sql"))

# The variant that was once applied to the live database: no status default, no kind check, a foreign key and
# no winning_side check on the mock chain, and an extra updated_at on positions.
DRIFT = """
alter table bet_notices alter column status drop default;
alter table bet_notices drop constraint bet_notices_kind_check;
alter table mock_chain_bets drop constraint mock_chain_bets_winning_side_check;
alter table mock_chain_bets add constraint mock_chain_bets_bet_id_fkey foreign key (bet_id) references bets (id) on delete cascade;
alter table mock_chain_positions add column updated_at timestamptz not null default now();
"""

CATALOG = """
select 'default' as k, coalesce(column_default, '') as v from information_schema.columns
  where table_schema = current_schema() and table_name = 'bet_notices' and column_name = 'status'
union all select 'con', conname from pg_constraint where connamespace = (select oid from pg_namespace where nspname = current_schema())
  and conrelid::regclass::text in ('bet_notices', 'mock_chain_bets', 'mock_chain_positions')
union all select 'col', table_name || '.' || column_name from information_schema.columns
  where table_schema = current_schema() and table_name in ('mock_chain_positions', 'mock_chain_bets', 'bet_notices')
order by 1, 2
"""


@pytest.fixture
def schema_conn(pg_url):
    schema = f"mig_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f'create schema "{schema}"')
        conn.execute(f'set search_path to "{schema}"')
        for f in MIGRATIONS:
            if f.name < "0005":
                conn.execute(f.read_text())
        yield conn
        conn.execute(f'drop schema "{schema}" cascade')


def catalog(conn):
    return conn.execute(CATALOG).fetchall()


def test_running_0005_twice_changes_nothing(schema_conn):
    schema_conn.execute(SQL)
    first = catalog(schema_conn)
    schema_conn.execute(SQL)
    assert catalog(schema_conn) == first
    assert ("default", "'pending'::text") in first and ("con", "bet_notices_kind_check") in first
    assert ("con", "mock_chain_bets_winning_side_check") in first
    assert ("con", "mock_chain_bets_bet_id_fkey") not in first and ("col", "mock_chain_positions.updated_at") not in first


def test_a_drifted_variant_converges_to_the_file(schema_conn):
    schema_conn.execute(SQL)
    clean = catalog(schema_conn)
    schema_conn.execute(DRIFT)
    assert catalog(schema_conn) != clean  # the drift is real
    schema_conn.execute(SQL)
    assert catalog(schema_conn) == clean
