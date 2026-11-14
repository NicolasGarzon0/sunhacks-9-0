-- 0005: shared pool markets and persisted mock chain state.
alter table bets drop constraint if exists bets_status_check;
alter table bets add column if not exists side0_name text,
  add column if not exists side1_name text,
  add column if not exists chain_bet_id text,
  add column if not exists fee_bps int,
  add column if not exists treasury text,
  add column if not exists min_stake_base bigint,
  add column if not exists max_stake_base bigint,
  add column if not exists max_participants int,
  add column if not exists funding_closes_at timestamptz,
  add column if not exists settle_after timestamptz,
  add column if not exists opened_at timestamptz,
  add column if not exists locked_at timestamptz,
  add column if not exists settled_at timestamptz,
  add column if not exists winning_side smallint,
  add column if not exists total_pool_base bigint,
  add column if not exists fee_paid_base bigint,
  add column if not exists failure_reason text,
  add column if not exists refund_reason text,
  add column if not exists payouts_completed_at timestamptz;
update bets set status = 'failed', failure_reason = 'legacy row has no market' where status = 'game_confirmed';
alter table bets add constraint bets_status_check
  check (status in ('game_confirmed', 'open', 'locked', 'settled', 'refunded', 'failed'));
alter table bets add constraint bets_fee_bps_check check (fee_bps is null or fee_bps between 0 and 1000);
alter table bets add constraint bets_winning_side_check check (winning_side is null or winning_side in (0, 1));
alter table bets add constraint bets_refund_reason_check
  check (refund_reason is null or refund_reason in ('one_sided', 'too_few_participants', 'cancelled', 'tie'));
drop index if exists bets_one_active_per_game;
create unique index bets_one_active_per_game on bets (proposer_handle, game_id)
  where status in ('game_confirmed', 'open', 'locked');
create index bets_status_idx on bets (status);

create table if not exists bet_participants (
  bet_id uuid not null references bets(id) on delete cascade,
  wallet text not null,
  display_name text,
  created_at timestamptz not null default now(),
  primary key (bet_id, wallet)
);

create table if not exists bet_notices (
  bet_id uuid not null references bets(id) on delete cascade,
  kind text not null,
  idempotency_key text not null,
  status text not null check (status in ('pending', 'sent', 'failed')),
  attempts int not null default 0,
  last_error text,
  created_at timestamptz not null default now(),
  sent_at timestamptz,
  primary key (bet_id, kind)
);

create table if not exists mock_chain_bets (
  bet_id uuid primary key references bets(id) on delete cascade,
  state text not null check (state in ('open', 'locked', 'settled', 'refunded')),
  params jsonb not null,
  vault_base bigint not null default 0,
  fee_paid_base bigint not null default 0,
  distributable_base bigint not null default 0,
  winning_side smallint,
  treasury_balance_base bigint not null default 0
);

create table if not exists mock_chain_positions (
  bet_id uuid not null references mock_chain_bets(bet_id) on delete cascade,
  wallet text not null,
  side smallint not null check (side in (0, 1)),
  amount_base bigint not null check (amount_base > 0),
  claimed boolean not null default false,
  payout_base bigint not null default 0,
  closed boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (bet_id, wallet)
);

alter table bet_participants enable row level security;
alter table bet_notices enable row level security;
alter table mock_chain_bets enable row level security;
alter table mock_chain_positions enable row level security;
