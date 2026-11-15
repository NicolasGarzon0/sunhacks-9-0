"""Notice retries, worker texts, config, the call budget meter, link lifetime, admin endpoints, migration 0006."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg
import pytest

from app import templates as T
from app.config import ConfigError, load_settings
from games.provider import ProviderError
from games.realtime import RealtimeSportsProvider
from games.usage import UsageMeter

from .conftest import ADMIN_SECRET
from .helpers import locked_market, sent, w
from .test_close_funding import ADMIN
from .test_config_logging import BASE
from .test_realtime_provider import FakeAPI, Mono, NOW, PREFIX

MIN = timedelta(minutes=1)
SEC = timedelta(seconds=1)


def fail_with(fake_gateway, code):
    fake_gateway.responder = lambda body: httpx.Response(200, json={"ok": False, "code": code, "detail": "x"})


def succeed(fake_gateway):
    fake_gateway.responder = lambda body: httpx.Response(200, json={"ok": True, "messageId": "m"})


async def settled_after_tick(worker, market, provider, scripted, repo, chain, clock):
    bet = await locked_market(market, provider, repo, chain)
    clock.current = bet.kickoff_utc + 60 * MIN
    scripted.final(bet.game_id, 24, 17)
    return bet


# --- notices ---

async def test_a_transient_send_failure_retries_with_backoff_then_succeeds(worker, market, provider, scripted, repo, chain, clock, fake_gateway):
    bet = await settled_after_tick(worker, market, provider, scripted, repo, chain, clock)
    fail_with(fake_gateway, "SEND_FAILED")
    t0 = clock.current
    n_before = len(sent(fake_gateway, ":result"))
    await worker.tick()
    assert len(sent(fake_gateway, ":result")) == n_before + 1
    notice = {n["kind"]: n for n in await repo.list_notices(bet.id)}["result"]
    assert (notice["status"], notice["attempts"], notice["permanent_failure"]) == ("failed", 1, False)
    await worker.tick()
    assert len(sent(fake_gateway, ":result")) == n_before + 1  # still backing off
    succeed(fake_gateway)
    clock.current = t0 + 31 * SEC
    await worker.tick()
    assert {n["kind"]: n for n in await repo.list_notices(bet.id)}["result"]["status"] == "sent"
    count = len(sent(fake_gateway, ":result"))
    for _ in range(3):
        clock.current += 10 * MIN
        await worker.tick()
    assert len(sent(fake_gateway, ":result")) == count  # sent once, never again
    assert {s["idempotencyKey"] for s in sent(fake_gateway, ":result")} == {f"bet:{bet.id}:result"}


async def test_not_allowed_is_permanent_and_never_retried(worker, market, provider, scripted, repo, chain, clock, fake_gateway):
    bet = await settled_after_tick(worker, market, provider, scripted, repo, chain, clock)
    fail_with(fake_gateway, "NOT_ALLOWED")
    await worker.tick()
    count = len(sent(fake_gateway, ":result"))
    notice = {n["kind"]: n for n in await repo.list_notices(bet.id)}["result"]
    assert (notice["status"], notice["permanent_failure"], notice["last_error"]) == ("failed", True, "NOT_ALLOWED")
    for _ in range(3):
        clock.current += 1 * MIN * 60
        await worker.tick()
    assert len(sent(fake_gateway, ":result")) == count
    assert (await worker.status())["notices_permanent_failure"] >= 1


async def test_notice_attempts_stop_at_the_max(make_worker, market, provider, scripted, repo, chain, clock, fake_gateway):
    worker = make_worker(notice_max_attempts=2)
    bet = await settled_after_tick(worker, market, provider, scripted, repo, chain, clock)
    fail_with(fake_gateway, "RATE_LIMITED")
    for _ in range(5):
        await worker.tick()
        clock.current += 2 * MIN * 60
    notice = {n["kind"]: n for n in await repo.list_notices(bet.id)}["result"]
    assert notice["attempts"] == 2 and notice["status"] == "failed"


async def test_the_lock_text_that_failed_to_send_is_retried_by_the_worker(worker, market, provider, repo, chain, clock, fake_gateway):
    from .helpers import deposits, open_bet

    bet = await open_bet(market, provider, repo)
    await deposits(chain, bet.id, [(1, 0, 5), (2, 1, 5)])
    fail_with(fake_gateway, "SEND_FAILED")
    await market.close_funding(bet.id)
    succeed(fake_gateway)
    clock.current = clock.current + 31 * SEC
    await worker.tick()
    assert {n["kind"]: n["status"] for n in await repo.list_notices(bet.id)}["locked"] == "sent"
    [last] = sent(fake_gateway, ":locked")[-1:]
    assert last["text"].startswith("Betting is closed for")


# --- worker texts ---

async def test_result_text(worker, market, provider, scripted, repo, chain, clock, fake_gateway):
    bet = await settled_after_tick(worker, market, provider, scripted, repo, chain, clock)
    await worker.tick()
    [msg] = sent(fake_gateway, ":result")
    assert msg["to"] == "+15550001111" and msg["idempotencyKey"] == f"bet:{bet.id}:result"
    assert msg["text"] == (
        "Final score: Seattle Seahawks 17, Arizona Cardinals 24. Arizona Cardinals wins. The pool was $35. "
        "The fee was $0.50, 5% of the losing side. Payouts are being sent now. "
        "The link stays live for 2 hours after payouts finish."
    )


@pytest.mark.parametrize(
    "reason, phrase",
    [("cancelled", "The game was cancelled"), ("postponed", "The game was postponed"), ("tie", "The game ended in a tie"),
     ("no_result", "There was no final result in time"), ("one_sided", "The bet did not have people on both sides")],
)
async def test_refund_result_texts(reason, phrase):
    class B:
        away_name, home_name = "Seattle Seahawks", "Arizona Cardinals"

    text = T.market_refund_result(B, reason, timedelta(hours=2))
    assert text.startswith(phrase) and "refunded in full with no fee" in text and "2 hours" in text


def test_grace_labels():
    assert T.grace_label(timedelta(hours=1)) == "an hour" and T.grace_label(timedelta(minutes=3)) == "3 minutes"
    assert T.grace_label(timedelta(hours=2)) == "2 hours"


def test_no_hyphens_in_worker_templates():
    class B:
        away_name, home_name, winning_side, total_pool_base, fee_paid_base, fee_bps = "A B", "C D", 1, 35_000_000, 500_000, 500
        result_away_score, result_home_score = 3, 7

    texts = [T.market_result(B, timedelta(hours=2)), T.PAYOUTS_DELAYED]
    texts += [T.market_refund_result(B, r, timedelta(minutes=3)) for r in T.REFUND_REASONS]
    assert all("-" not in t and "—" not in t for t in texts)


# --- link lifetime ---

async def test_the_link_lives_until_payouts_complete_plus_the_grace_window(worker, client, market, provider, scripted, repo, chain, clock):
    bet = await settled_after_tick(worker, market, provider, scripted, repo, chain, clock)
    token = market.signer.token(bet.id)
    await worker.tick()
    done = await repo.get_bet(bet.id)
    assert done.payouts_completed_at == clock.current
    grace = market.settings.link_grace
    clock.current = done.payouts_completed_at + grace - SEC
    assert (await client.get(f"/markets/{token}")).status_code == 200
    clock.current = done.payouts_completed_at + grace
    r = await client.get(f"/markets/{token}")
    assert (r.status_code, r.json()["code"]) == (410, "LINK_EXPIRED")


def test_link_grace_minutes_override():
    s = load_settings({**BASE, "LINK_GRACE_MINUTES": "3"}, env_file=None)
    assert s.link_grace == timedelta(minutes=3)
    assert load_settings(BASE, env_file=None).link_grace == timedelta(hours=2)


# --- admin endpoints ---

async def test_admin_tick_and_status_need_the_admin_secret(client, market, provider, scripted, repo, chain, clock):
    assert (await client.post("/admin/worker/tick")).status_code == 401
    assert (await client.get("/admin/worker/status")).status_code == 401
    r = await client.post("/admin/worker/tick", headers=ADMIN)
    assert r.status_code == 200 and r.json()["ok"] is True
    s = (await client.get("/admin/worker/status", headers=ADMIN)).json()
    assert s["ok"] and s["enabled"] is False and "last_tick" in s and "payout_tasks" in s and "notices" in s


async def test_admin_result_endpoint_sets_a_score_and_a_tie(client, provider, repo):
    [game] = await provider.find_candidates("cardinals", None, None)
    r = await client.post(f"/admin/games/{game.game_id}/result", json={"home_score": 20, "away_score": 20}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["result"]["status"] == "final" and r.json()["result"]["home_score"] == 20
    back = await client.post(f"/admin/games/{game.game_id}/status", json={"status": "scheduled"}, headers=ADMIN)
    assert back.status_code == 200 and back.json()["status"] == "scheduled"
    assert (await client.post("/admin/games/nope/result", json={"home_score": 1, "away_score": 0}, headers=ADMIN)).status_code == 404


async def test_admin_bet_view_lists_payout_tasks(worker, client, market, provider, scripted, repo, chain, clock):
    bet = await settled_after_tick(worker, market, provider, scripted, repo, chain, clock)
    await worker.tick()
    view = (await client.get(f"/admin/bets/{bet.id}", headers=ADMIN)).json()
    assert [t["status"] for t in view["payout_tasks"]] == ["done", "done", "done"]  # two claims and a loser close


# --- config ---

def test_worker_defaults_for_the_simulated_provider():
    s = load_settings(BASE, env_file=None)
    assert s.worker_enabled is True and s.worker_tick_seconds == 15
    assert (s.result_poll_start_hours_after_kickoff, s.result_poll_interval_minutes, s.settle_confirm_wait_minutes) == (0, 0, 0)
    assert (s.sports_call_budget_monthly, s.claim_max_attempts, s.claim_backoff_seconds, s.notice_max_attempts) == (100, 8, 30, 6)


def test_worker_defaults_for_a_real_provider_and_env_overrides():
    s = load_settings({**BASE, "GAME_PROVIDER": "realtime", "SPORTS_API_KEY": "k"}, env_file=None)
    assert (s.result_poll_start_hours_after_kickoff, s.result_poll_interval_minutes, s.settle_confirm_wait_minutes) == (3, 10, 10)
    o = load_settings({**BASE, "RESULT_POLL_START_HOURS_AFTER_KICKOFF": "1", "SETTLE_CONFIRM_WAIT_MINUTES": "1",
                       "WORKER_ENABLED": "false", "WORKER_TICK_SECONDS": "5", "CLAIM_MAX_ATTEMPTS": "3"}, env_file=None)
    assert (o.result_poll_start_hours_after_kickoff, o.settle_confirm_wait_minutes, o.worker_enabled) == (1, 1, False)
    assert (o.worker_tick_seconds, o.claim_max_attempts) == (5, 3)


@pytest.mark.parametrize(
    "override, name",
    [({"WORKER_ENABLED": "maybe"}, "WORKER_ENABLED"), ({"WORKER_TICK_SECONDS": "0"}, "WORKER_TICK_SECONDS"),
     ({"RESULT_POLL_START_HOURS_AFTER_KICKOFF": "-1"}, "RESULT_POLL_START_HOURS_AFTER_KICKOFF"),
     ({"RESULT_POLL_START_HOURS_AFTER_KICKOFF": "x"}, "RESULT_POLL_START_HOURS_AFTER_KICKOFF"),
     ({"SETTLE_CONFIRM_WAIT_MINUTES": "-5"}, "SETTLE_CONFIRM_WAIT_MINUTES"),
     ({"SPORTS_CALL_BUDGET_MONTHLY": "x"}, "SPORTS_CALL_BUDGET_MONTHLY"), ({"CLAIM_MAX_ATTEMPTS": "0"}, "CLAIM_MAX_ATTEMPTS"),
     ({"CLAIM_BACKOFF_SECONDS": "-1"}, "CLAIM_BACKOFF_SECONDS"), ({"NOTICE_MAX_ATTEMPTS": "0"}, "NOTICE_MAX_ATTEMPTS")],
)
def test_worker_config_errors_name_the_variable(override, name):
    with pytest.raises(ConfigError, match=name):
        load_settings({**BASE, **override}, env_file=None)


# --- the call budget ---

async def test_usage_meter_counts_by_month_and_stops_at_the_budget(repo, logs):
    now = [datetime(2026, 11, 30, 23, 0, tzinfo=timezone.utc)]
    meter = UsageMeter(repo, 2, logs.logger, lambda: now[0])
    assert [await meter.reserve() for _ in range(4)] == [True, True, False, False]
    assert await repo.provider_calls_in("2026-11") == 2  # never exceeded
    assert len(logs.events("rts.budget_exhausted")) == 1  # a clear error, once
    now[0] = datetime(2026, 12, 1, 0, 1, tzinfo=timezone.utc)
    assert await meter.reserve() is True and await repo.provider_calls_in("2026-12") == 1


async def test_the_realtime_provider_stops_calling_the_api_when_the_budget_is_used_up(repo, logs):
    api = FakeAPI()
    clock = Mono()

    async def no_sleep(_):
        pass

    meter = UsageMeter(repo, 1, logs.logger, lambda: NOW)
    p = RealtimeSportsProvider(
        "test-key", "America/Phoenix", logs.logger, now=lambda: NOW, client=httpx.AsyncClient(transport=httpx.MockTransport(api)),
        sleep=no_sleep, monotonic=clock, base_url="https://api.test/api/v1", meter=meter,
    )
    assert await p.get_status("rts:nfl:401873052") == "final"  # the one call in the budget
    clock.t += 1000  # the 60 s event cache has expired
    with pytest.raises(ProviderError, match="budget"):
        await p.get_status("rts:nfl:401873052")
    assert len(api.calls) == 1 and await repo.provider_calls_in("2026-11") == 1


# --- migration 0006 ---

SQL = (Path(__file__).resolve().parent.parent / "migrations" / "0006_worker.sql").read_text()
MIGRATIONS = sorted((Path(__file__).resolve().parent.parent / "migrations").glob("*.sql"))
CAT = """
select 'col', table_name || '.' || column_name || ':' || data_type || ':' || coalesce(column_default, '') from information_schema.columns
  where table_schema = current_schema() and table_name in ('bets', 'payout_tasks', 'provider_usage', 'bet_notices')
union all select 'con', conrelid::regclass::text || '.' || conname || ':' || pg_get_constraintdef(oid) from pg_constraint
  where connamespace = (select oid from pg_namespace where nspname = current_schema())
union all select 'rls', relname || ':' || relrowsecurity from pg_class where relname in ('payout_tasks', 'provider_usage')
  and relnamespace = (select oid from pg_namespace where nspname = current_schema())
order by 1, 2
"""


def test_migration_0006_is_rerunnable_and_widens_the_checks(pg_url):
    schema = f"mig6_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f'create schema "{schema}"')
        conn.execute(f'set search_path to "{schema}"')
        try:
            for f in MIGRATIONS:
                if f.name < "0006":
                    conn.execute(f.read_text())
            conn.execute(SQL)
            first = conn.execute(CAT).fetchall()
            conn.execute(SQL)
            assert conn.execute(CAT).fetchall() == first
            assert ("rls", "payout_tasks:true") in first and ("rls", "provider_usage:true") in first
            conn.execute(
                "insert into bets (proposer_handle, game_id, league, home_id, home_name, away_id, away_name, kickoff_utc, status, refund_reason) "
                "values ('h', 'g', 'nfl', 'a', 'A', 'b', 'B', now(), 'refunded', 'no_result')"
            )
            for kind in ("result", "refund_result", "payouts_delayed"):
                conn.execute("insert into bet_notices (bet_id, kind, idempotency_key) select id, %s, 'k' from bets", (kind,))
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("update bets set refund_reason = 'because'")
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("update bets set result_winner_side = 2")
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute("insert into payout_tasks (bet_id, wallet, kind, amount_base) select id, 'w', 'win', 1 from bets")
                conn.execute("insert into payout_tasks (bet_id, wallet, kind, amount_base) select id, 'w', 'win', 1 from bets")
        finally:
            conn.execute(f'drop schema "{schema}" cascade')
