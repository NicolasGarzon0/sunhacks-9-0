-- 0005: pool markets. Extends bets (chunk 3), adds participants, durable proposer notices, and the mock chain.
-- Money columns are bigint USDC base units (6 decimals). Statuses are lowercase like the rest of the schema.
-- Safe to run more than once: every statement is guarded (if not exists / drop ... if exists).

alter table bets
  add column if not exists side0_name text,                 -- away team (side 0)
  add column if not exists side1_name text,                 -- home team (side 1)
  add column if not exists chain_bet_id text,
  add column if not exists fee_bps integer,
  add column if not exists treasury text,
  add column if not exists min_stake_base bigint,
  add column if not exists max_stake_base bigint,
  add column if not exists max_participants integer,
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

-- Chunk 3 confirmed games before markets existed have no market behind them; close them out.
alter table bets drop constraint if exists bets_status_check;
update bets set status = 'failed', failure_reason = 'legacy_no_market' where status = 'game_confirmed' and chain_bet_id is null and opened_at is null and funding_closes_at is null;
alter table bets add constraint bets_status_check
  check (status in ('game_confirmed', 'open', 'locked', 'settled', 'refunded', 'failed'));
alter table bets drop constraint if exists bets_fee_bps_check;
alter table bets add constraint bets_fee_bps_check check (fee_bps is null or (fee_bps >= 0 and fee_bps <= 1000));
alter table bets drop constraint if exists bets_winning_side_check;
alter table bets add constraint bets_winning_side_check check (winning_side is null or winning_side in (0, 1));
alter table bets drop constraint if exists bets_refund_reason_check;
alter table bets add constraint bets_refund_reason_check
  check (refund_reason is null or refund_reason in ('one_sided', 'too_few_participants', 'cancelled', 'tie'));

-- One active market per proposer + game (chunk 3 allowed only game_confirmed here).
drop index if exists bets_one_active_per_game;
create unique index bets_one_active_per_game on bets (proposer_handle, game_id)
  where status in ('game_confirmed', 'open', 'locked');
create index if not exists bets_status_idx on bets (status);

create table if not exists bet_participants (
  bet_id       uuid not null references bets (id) on delete cascade,
  wallet       text not null,
  display_name text,
  created_at   timestamptz not null default now(),
  primary key (bet_id, wallet)
);

-- Durable record of every text sent to the proposer about a market. The gateway's own idempotency is in memory
-- (lost on restart), so this is what guarantees "once".
create table if not exists bet_notices (
  bet_id          uuid not null references bets (id) on delete cascade,
  kind            text not null check (kind in ('link', 'failed', 'locked', 'refunded')),
  idempotency_key text not null,
  status          text not null default 'pending' check (status in ('pending', 'sent', 'failed')),
  attempts        integer not null default 0,
  last_error      text,
  created_at      timestamptz not null default now(),
  sent_at         timestamptz,
  primary key (bet_id, kind)
);

-- Mock chain (CHAIN_MODE=mock). Everything is prefixed mock_chain_ so the real client can drop it.
create table if not exists mock_chain_bets (
  bet_id                uuid primary key,
  state                 text not null check (state in ('open', 'locked', 'settled', 'refunded')),
  params                jsonb not null,
  vault_base            bigint not null default 0,
  fee_paid_base         bigint not null default 0,
  distributable_base    bigint not null default 0,
  winning_side          smallint check (winning_side is null or winning_side in (0, 1)),
  treasury_balance_base bigint not null default 0
);

create table if not exists mock_chain_positions (
  bet_id      uuid not null references mock_chain_bets (bet_id) on delete cascade,
  wallet      text not null,
  side        smallint not null check (side in (0, 1)),
  amount_base bigint not null check (amount_base > 0),
  claimed     boolean not null default false,
  payout_base bigint not null default 0,
  closed      boolean not null default false,
  created_at  timestamptz not null default now(),
  primary key (bet_id, wallet)
);

alter table bet_participants enable row level security;
alter table bet_notices enable row level security;
alter table mock_chain_bets enable row level security;
alter table mock_chain_positions enable row level security;


-- Reconcile: converge any earlier variant of these tables (created before this file was final) to this definition.
-- Every statement is guarded, so it is a no-op on a database that already matches.
alter table bet_notices alter column status set default 'pending';
alter table bet_notices drop constraint if exists bet_notices_kind_check;
alter table bet_notices add constraint bet_notices_kind_check check (kind in ('link', 'failed', 'locked', 'refunded'));
alter table mock_chain_bets drop constraint if exists mock_chain_bets_bet_id_fkey;
alter table mock_chain_bets drop constraint if exists mock_chain_bets_winning_side_check;
alter table mock_chain_bets add constraint mock_chain_bets_winning_side_check check (winning_side is null or winning_side in (0, 1));
alter table mock_chain_positions drop column if exists updated_at;
