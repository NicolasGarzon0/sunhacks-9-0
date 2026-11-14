-- 0002_drafts: one bet draft per proposer handle. RLS on, no policies (backend connects as the table owner).
create table if not exists drafts (
  handle              text primary key,
  fields              jsonb not null default '{}'::jsonb,
  status              text not null check (status in ('collecting', 'ready', 'cancelled')),
  pending_field       text check (pending_field in ('team', 'date', 'stake_usd', 'friend')),
  pending_replacement jsonb,
  updated_at          timestamptz not null default now()
);

alter table drafts enable row level security;
