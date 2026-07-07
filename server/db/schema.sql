-- Supabase schema for kublai-alpha.
-- Run in the Supabase SQL editor (or let scripts apply it over the direct
-- Postgres connection). Idempotent.

-- Shared post cache. Every scrape (user command or the daily auto-fetch)
-- lands here and is readable by ALL users, so the same group/keyword is not
-- re-fetched from Apify again and again. The cleanup poller purges rows a
-- week after they were posted (STALE_POST_DAYS), so this holds roughly the
-- last week of posts.
create table if not exists org_posts (
  post_id text primary key,
  url text,
  message text,
  message_rich text,
  timestamp bigint,
  author_id text,
  author_name text,
  author_url text,
  author_profile_picture_url text,
  type text check (type in ('seller', 'buyer')),
  -- Facebook group the post came from; null for regional keyword searches.
  group_id text,
  last_posted timestamptz not null,
  created_at timestamptz not null default now()
);

-- Older deployments predate the group_id column.
alter table org_posts add column if not exists group_id text;

-- The cleanup poller deletes by last_posted; keep that scan cheap.
create index if not exists org_posts_last_posted_idx on org_posts (last_posted);
-- Serving "posts of group X" from the shared cache.
create index if not exists org_posts_group_idx on org_posts (group_id);

create table if not exists saved_posts (
  id bigint generated always as identity primary key,
  agent_id text not null,
  post_id text not null,
  url text,
  message text,
  author_name text,
  saved_at timestamptz not null default now(),
  unique (agent_id, post_id)
);

create index if not exists saved_posts_agent_idx on saved_posts (agent_id);

-- Chats that opted in (/watch) to the daily new-post notifications.
create table if not exists subscribers (
  chat_id text primary key,
  created_at timestamptz not null default now()
);

-- Registered bot users. A row is created the first time someone talks to the
-- bot; phone_number is filled in when they share their Telegram contact
-- (that share is the auth step - Telegram guarantees the contact belongs to
-- the sender). `user` is a reserved word in Postgres, hence `users`.
create table if not exists users (
  user_id text primary key,             -- Telegram user id
  username text,                        -- Telegram @handle, if any
  full_name text,
  phone_number text,                    -- null until the user shares their contact
  mail text,                            -- email, for Google web accounts
  membership_type text not null default 'basic',
  bot_invited boolean not null default false,  -- true once the user has started/linked the bot
  -- Dashboard preferences (JSON). Groups and saved posts keep their own tables
  -- (user_groups / saved_posts); this only holds toggles like the watch digest.
  settings jsonb not null default '{"watch_enabled": true}'::jsonb,
  created_at timestamptz not null default now(),
  last_accessed timestamptz not null default now()
);

-- Older deployments predate these columns.
alter table users add column if not exists bot_invited boolean not null default false;
alter table users add column if not exists mail text;
alter table users add column if not exists settings jsonb not null
  default '{"watch_enabled": true}'::jsonb;

-- Plans. 'basic': up to 3 data requests per rolling 7 days, enforced by
-- counting the user's data_requests rows. 'essentials': up to 10 data
-- requests per rolling hour (request anytime, otherwise no weekly cap),
-- plus the automatic morning digest (07:00 Ulaanbaatar, once every 24h) sent
-- by the bot's auto-fetch loop to every essentials user.
update users set membership_type = 'essentials'
  where membership_type not in ('basic', 'essentials');
alter table users drop constraint if exists users_membership_check;
alter table users add constraint users_membership_check
  check (membership_type in ('basic', 'essentials'));

-- Every bot search (data request) lands here; the basic-plan quota is
-- "3 rows in the last 7 days".
create table if not exists data_requests (
  id bigint generated always as identity primary key,
  user_id text not null,
  requested_at timestamptz not null default now()
);

create index if not exists data_requests_user_time_idx
  on data_requests (user_id, requested_at);

-- Which web account this Telegram user belongs to (null until linked).
alter table users add column if not exists web_user_id text;
-- One web account maps to at most one Telegram account.
create unique index if not exists users_web_user_idx
  on users (web_user_id) where web_user_id is not null;

-- QPay payment orders. `id` doubles as the sender_invoice_no sent to QPay,
-- so its uniqueness (primary key) is what makes double-processing impossible.
-- Status transitions PENDING -> PAID exactly once, via an atomic
-- compare-and-set (update ... where status = 'PENDING').
create table if not exists payment_orders (
  id text primary key,                  -- uuid, also QPay sender_invoice_no
  user_id text not null,                -- web user (Supabase auth uid)
  qpay_invoice_id text not null,        -- QPay's id, used for /v2/payment/check
  amount numeric not null,
  currency text not null default 'MNT',
  status text not null default 'PENDING' check (status in ('PENDING', 'PAID')),
  qr_text text,                         -- so the QR survives a page refresh
  qr_image text,                        -- base64 PNG from QPay
  created_at timestamptz not null default now(),
  paid_at timestamptz
);

create index if not exists payment_orders_user_idx on payment_orders (user_id);

-- Facebook groups a user added via the "Add Group" button. Groups are stored
-- per user (each user manages their own list), but the POSTS fetched from
-- them go to the shared org_posts cache above.
create table if not exists user_groups (
  id bigint generated always as identity primary key,
  user_id text not null references users (user_id) on delete cascade,
  group_id text not null,
  group_url text,
  group_name text,
  added_at timestamptz not null default now(),
  unique (user_id, group_id)
);

create index if not exists user_groups_user_idx on user_groups (user_id);
create index if not exists user_groups_group_idx on user_groups (group_id);

-- Web account <-> Telegram linking.
-- The frontend asks the backend for a token; the backend verifies the
-- caller's Supabase JWT server-side and mints it (the client can't forge
-- someone else's uid). The user opens t.me/<bot>?start=<token>; the bot
-- consumes the token: short-lived, single-use (the `used` flag is flipped
-- atomically), so a leaked link can't bind an attacker's Telegram to the
-- victim's account.
create table if not exists telegram_link_tokens (
  token text primary key,
  user_id uuid not null references auth.users(id),
  created_at timestamptz not null default now(),
  expires_at timestamptz not null default now() + interval '10 minutes',
  used boolean not null default false
);

-- Superseded by telegram_link_tokens.
drop table if exists link_tokens;