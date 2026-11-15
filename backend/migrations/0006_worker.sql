-- 0006: market lifecycle worker. Result tracking on bets, payout tasks, provider call budget, notice retry fields.
-- Safe to run more than once: every statement is guarded (if not exists / drop ... if exists).

alter table bets
  add column if not exists result_away_score integer,
  add column if not exists result_home_score integer,
  add column if not exists result_winner_side smallint,
  add column if not exists result_first_seen_at timestamptz,
  add column if not exists final_confirmed_at timestamptz,
  add column if not exists last_polled_at timestamptz,
  add column if not exists provider_calls integer not null default 0;

alter table bets drop constraint if exists bets_result_winner_side_check;
alter table bets add constraint bets_result_winner_side_check check (result_winner_side is null or result_winner_side in (0, 1));

alter table bets drop constraint if exists bets_refund_reason_check;
alter table bets add constraint bets_refund_reason_check
  check (refund_reason is null or refund_reason in ('one_sided', 'too_few_participants', 'cancelled', 'tie', 'postponed', 'no_result'));

create table if not exists payout_tasks (
  id              uuid primary key default gen_random_uuid(),
  bet_id          uuid not null references bets (id) on delete cascade,
  wallet          text not null,
  kind            text not null check (kind in ('win', 'refund')),
  amount_base     bigint not null check (amount_base >= 0),
  status          text not null default 'pending' check (status in ('pending', 'done', 'failed')),
  attempts        integer not null default 0,
  next_attempt_at timestamptz,
  last_error      text,
  tx_ref          text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (bet_id, wallet)
);
create index if not exists payout_tasks_due_idx on payout_tasks (status, next_attempt_at);

create table if not exists provider_usage (
  month text primary key,
  calls integer not null default 0
);

alter table bet_notices
  add column if not exists next_attempt_at timestamptz,
  add column if not exists permanent_failure boolean not null default false;
alter table bet_notices drop constraint if exists bet_notices_kind_check;
alter table bet_notices add constraint bet_notices_kind_check
  check (kind in ('link', 'failed', 'locked', 'refunded', 'result', 'refund_result', 'payouts_delayed'));

alter table payout_tasks enable row level security;
alter table provider_usage enable row level security;
