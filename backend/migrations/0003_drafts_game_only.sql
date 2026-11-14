-- 0003: bet requests are game-only (team, opponent, date). Status is collecting|ready (expired drafts are
-- deleted), and only team or date can be pending.
delete from drafts where status = 'cancelled';
alter table drafts drop constraint if exists drafts_status_check,
  add constraint drafts_status_check check (status in ('collecting', 'ready'));
alter table drafts drop constraint if exists drafts_pending_field_check,
  add constraint drafts_pending_field_check check (pending_field in ('team', 'date'));
