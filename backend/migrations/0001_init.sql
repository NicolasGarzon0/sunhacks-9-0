-- 0001_init: inbound dedup + demo roster. RLS on, no policies (backend connects as the table owner).
create table if not exists inbound_messages (
  message_id   text primary key,
  handle       text not null,
  received_at  timestamptz not null,
  processed_at timestamptz
);

create table if not exists roster (
  name         text primary key check (name = lower(name) and name <> ''),
  display_name text not null,
  handle       text not null unique check (handle !~ '\s' and handle <> '')
);

alter table inbound_messages enable row level security;
alter table roster enable row level security;
