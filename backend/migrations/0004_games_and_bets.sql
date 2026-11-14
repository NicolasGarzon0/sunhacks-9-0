-- 0004: game lookup + confirmation. Draft gains offered candidates; simulated game state; confirmed bets.
alter table drafts add column if not exists candidates jsonb,
                   add column if not exists confirm_misses smallint not null default 0;
alter table drafts drop constraint if exists drafts_pending_field_check,
  add constraint drafts_pending_field_check check (pending_field in ('team', 'date', 'confirm'));

create table if not exists sim_games (
  game_id     text primary key,
  league      text not null,
  home_id     text not null,
  home_name   text not null,
  away_id     text not null,
  away_name   text not null,
  kickoff_utc timestamptz not null,
  status      text not null check (status in ('scheduled', 'in_progress', 'final', 'postponed', 'cancelled')),
  home_score  int,
  away_score  int,
  official    boolean not null default false,
  updated_at  timestamptz not null default now()
);

create table if not exists bets (
  id              uuid primary key default gen_random_uuid(),
  proposer_handle text not null,
  game_id         text not null,
  league          text not null,
  home_id         text not null,
  home_name       text not null,
  away_id         text not null,
  away_name       text not null,
  kickoff_utc     timestamptz not null,
  status          text not null check (status in ('game_confirmed')),
  created_at      timestamptz not null default now()
);
-- One active confirmed bet per game per proposer (chunk 4 widens the active-status list).
create unique index if not exists bets_one_active_per_game on bets (proposer_handle, game_id)
  where status = 'game_confirmed';

alter table sim_games enable row level security;
alter table bets enable row level security;
