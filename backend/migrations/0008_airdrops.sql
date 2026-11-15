-- 0008: test USDC airdrop policy (chunk 3B). One row per airdrop, used for the per wallet lifetime cap,
-- the global daily cap and the per IP rate limit. Safe to run more than once.

create table if not exists airdrops (
  id          uuid primary key default gen_random_uuid(),
  wallet      text not null,
  amount_base bigint not null check (amount_base > 0),
  bet_id      uuid references bets (id) on delete set null,
  ip          text,
  tx_ref      text,
  created_at  timestamptz not null default now()
);
create index if not exists airdrops_wallet_idx on airdrops (wallet);
create index if not exists airdrops_created_idx on airdrops (created_at);

alter table airdrops enable row level security;
