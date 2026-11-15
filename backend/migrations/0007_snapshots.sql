-- 0007: chain parity (chunk 3A). Position snapshots, rent reclaim tasks, close_bet tracking, mock chain close flag.
-- Safe to run more than once: every statement is guarded (if not exists / drop ... if exists).

-- A copy of every position taken when funding closes. On chain a position account disappears once it is claimed or
-- closed, so links and views read side, stake, payout and status from here, not from the chain.
create table if not exists position_snapshots (
  bet_id      uuid not null references bets (id) on delete cascade,
  wallet      text not null,
  side        smallint not null check (side in (0, 1)),
  amount_base bigint not null check (amount_base > 0),
  payout_base bigint check (payout_base is null or payout_base >= 0),
  status      text not null default 'held' check (status in ('held', 'paid', 'refunded', 'closed_loser')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (bet_id, wallet)
);

-- close_loser tasks (return the rent of losing positions) live next to the claim tasks.
alter table payout_tasks drop constraint if exists payout_tasks_kind_check;
alter table payout_tasks add constraint payout_tasks_kind_check check (kind in ('win', 'refund', 'close_loser'));

-- close_bet: once per market after the link expired, with retries.
alter table bets
  add column if not exists chain_closed_at timestamptz,
  add column if not exists close_bet_attempts integer not null default 0,
  add column if not exists close_bet_next_attempt_at timestamptz,
  add column if not exists close_bet_last_error text;

-- Mock chain: a closed bet behaves like a closed Bet account (reads return NOT_FOUND).
alter table mock_chain_bets add column if not exists closed boolean not null default false;

alter table position_snapshots enable row level security;
