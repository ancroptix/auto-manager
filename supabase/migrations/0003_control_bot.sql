-- ============================================================================
-- 0003_control_bot.sql — sessions the control bot can log in, and the settings
-- that make that safe.
--
-- Why a table at all: the operator is not going to run Python on a laptop to
-- produce a session string, and a session string must never be pasted into a
-- chat. So the service performs the login itself, over Telegram, and stores the
-- result here — behind RLS with zero policies, reachable only by the roles that
-- already hold the database password.
--
-- The string is stored in plain text on purpose. There is no second secret to
-- encrypt it with that would not itself live in this same deployment; pretending
-- otherwise would add a key-management problem while providing no real
-- protection. The controls that matter are: nothing here is exposed to the
-- public API roles, the value is never sent back over the bot, and revoking a
-- session is one command (/forget) plus Telegram's own "terminate all sessions".
-- ============================================================================

create table if not exists app.telegram_session (
  name            text primary key,
  kind            text not null default 'user',
  session_string  text not null,
  account_id      bigint,
  username        text,
  active          boolean not null default false,
  created_at      timestamptz not null default now(),
  last_used_at    timestamptz,
  note            text,
  constraint telegram_session_name_shape check (name ~ '^[a-z0-9][a-z0-9_-]{0,39}$'),
  constraint telegram_session_kind_check check (kind in ('user','bot')),
  -- A session string is ~350 chars of base64-ish text. Anything shorter is a
  -- paste mistake, and a truncated session fails later in a confusing way.
  constraint telegram_session_length check (length(session_string) between 64 and 4096)
);

-- Exactly one *active* session per kind: two live user sessions means two
-- workers claiming jobs and double-posting the same episode.
create unique index if not exists telegram_session_one_active_uq
  on app.telegram_session (kind) where active;
create index if not exists telegram_session_kind_ix on app.telegram_session (kind, active);

comment on column app.telegram_session.session_string is
  'MTProto StringSession. Account-equivalent: reading it is owning the account. Never selected into a bot reply or a log line.';

-- Same posture as every other table in this schema: RLS on, zero policies, so
-- the exposed API roles cannot read it even by guessing the name.
alter table app.telegram_session enable row level security;

do $grants$
declare
  rname text;
  roles text[];
begin
  select coalesce(array_agg(rolname), '{}'::text[]) into roles
    from pg_roles
   where rolname in ('anon','authenticated','service_role');

  foreach rname in array roles loop
    if rname = 'service_role' then
      execute 'grant select, insert, update, delete on table app.telegram_session to service_role';
    else
      execute format('revoke all on table app.telegram_session from %I', rname);
      execute format('revoke usage on schema app from %I', rname);
    end if;
  end loop;
end
$grants$;

insert into app.config (key, value, description) values
  ('bot.allow_login', 'true',
   'Let the control bot run the MTProto login flow (phone, code, 2FA) and store the resulting session here. Turn off once the account is connected.'),
  ('bot.login_ttl_seconds', '600',
   'How long a pending login may wait for its code before the attempt is discarded. Codes expire on Telegram''s side anyway; this stops a half-finished attempt lingering in memory.'),
  ('bot.delete_sensitive', 'true',
   'Delete the operator''s own messages containing the phone number, code and 2FA password after use. Best effort: the operator should delete them too.'),
  ('bot.enabled_commands',
   '["help","status","pause","resume","reconcile","probe","sessions","use","forget","login","code","password","cancel"]',
   'Commands the bot answers. Unknown names are ignored rather than echoed back.')
on conflict (key) do nothing;
