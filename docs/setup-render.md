# Render setup (and the honest limits of the free tier)

The repository contains a `render.yaml` Blueprint, so Render creates and updates
the service itself — you do not fill in a settings form field by field.

## 1. Deploy

1. Push this branch to your repo (or merge it to `main`).
2. <https://dashboard.render.com> → **New + → Blueprint** → pick the
   `auto-manager` repository → **Next**.
3. Render reads `render.yaml` and lists the env vars marked `sync: false`.
   Fill in:

   | Key | Value |
   | --- | --- |
   | `DATABASE_URL` | the Supabase **pooler** string from [setup-supabase.md](setup-supabase.md) |
   | `TELEGRAM_API_ID` | number from <https://my.telegram.org> → API development tools |
   | `TELEGRAM_API_HASH` | the hash on the same page |
   | `TELEGRAM_SESSION_STRING` | output of `python scripts/login.py` **on your own computer** |
   | `TELEGRAM_OWNER_USER_IDS` | your numeric Telegram user ID |
   | `TELEGRAM_MAIN_ADMIN_USER_ID` | your main account's numeric ID |
   | `CONTROL_TOKEN` | any long random string (see step 5) |

   Leave `APP_MODE` as `shadow` for the first deploy.
4. **Apply**. Build takes ~2 minutes.

Do not paste the session string, the code Telegram sent you, or your 2FA
password into a chat, an issue, or a commit. `scripts/login.py` exists so those
never leave your keyboard, and `scripts/check_secrets.py` (plus CI) fails the
build if one is committed anyway.

## 2. Check it

| URL | Expect |
| --- | --- |
| `/` | JSON with `"status": "alive"` |
| `/health` | `200`, `database: "up"`, `mode: "shadow"` |
| `/ready` | `200 {"ready": true}` |
| `/status` | queue counts, worker heartbeat, blocked features |

`/health` stays `200` even when Supabase is down — deliberate, because a `500`
here makes Render redeploy the service and it loses nothing by knowing the
process is alive. `/ready` is the endpoint that reports the real dependency
state, and it is the one to alert on.

## 3. Keep it awake (UptimeRobot)

1. <https://uptimerobot.com> → **Add New Monitor** → **HTTP(s)**.
2. URL: `https://<your-service>.onrender.com/health`, interval **5 minutes**.

Free web services spin down after ~15 minutes with no inbound traffic, so this
ping is what keeps the queue moving between posts. It reduces spin-downs; it
does not eliminate them.

## 4. The maths you should know before relying on this

Free tier is **750 instance-hours per workspace per month**, shared across all
free services.

```text
31-day month × 24 h = 744 h   →  fits, with about 6 hours to spare
```

So keeping one free service awake 24/7 fits inside the allowance **by a hair**,
and any build time, restart storm, or second free service eats the margin —
when the allowance runs out, Render suspends every free service in the workspace
until the 1st. Practical consequences:

* Keep exactly one free service here (no extra Render Postgres — Supabase is the
  store, and Render's free Postgres is deleted after 30 days anyway).
* Prefer `APP_MODE=shadow` while developing: fewer jobs, fewer restarts.
* If uploads must genuinely not pause, Starter compute ($7/month) is the fix —
  not more pinging. Everything in this repo except `render.yaml`'s `plan: free`
  is unchanged by that upgrade.

## 5. Your two emergency levers

```bash
# Stop taking new work (in-flight job finishes its current stage):
curl -X POST https://<service>.onrender.com/control/pause \
  -H "Authorization: Bearer $CONTROL_TOKEN" -H 'content-type: application/json' \
  -d '{"reason":"publishing to the wrong channel"}'

# Full stop: pause first, then exit. The pause flag lives in Postgres, so the
# service stays paused through Render's automatic restart.
curl -X POST https://<service>.onrender.com/control/shutdown \
  -H "Authorization: Bearer $CONTROL_TOKEN"
```

Generate the token locally:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Every `/control/*` route requires it, and they return `503` when it is unset —
fail-closed, so "I forgot to configure auth" can never mean "anyone can stop my
bot".

## 6. Updating

`autoDeployTrigger: commit` means every push redeploys. On a push mid-upload the
instance is replaced; the job keeps its stage and boot reconciliation reclaims
its lease. That is the designed path, not an edge case — but it is why long
uploads are checkpointed per stage rather than at the end.

## 7. Logs

Dashboard → **Logs**, or:

```bash
render logs --service auto-manager --tail
```

Lines worth grepping for: `reclaimed`, `blocked:`, `flood wait`, `database
unavailable`.

## Discovering the bot protocols (one switch)

The storage bot's menu flow and Channel Help's publishing handshake cannot be
read from documentation, and they cannot be probed from a laptop behind a network
that filters MTProto. So the service probes itself, once, from inside Render:

1. Set `PROBE_ON_BOOT` to `1` in Environment and **Save** (it redeploys).
2. Watch **Logs** for `running read-only protocol discovery once`.
3. The spare account's owner (`TELEGRAM_MAIN_ADMIN_USER_ID`) receives one message
   from yourself containing: each bot's opening text, every button with whether
   it is a callback or a URL, what the safe buttons lead to, and the bots'
   command lists.
4. Paste that message to whoever is building the handlers, then set
   `PROBE_ON_BOOT` back to `0`.

The same run is available on demand with `POST /control/probe` (bearer token), and
it is safe by construction: only `/start`-class text to the two bots and the
owner, no uploads, no forwards, no channel posts, no permission changes, and a
hard message budget. A guard in `app/probe.py` rejects anything else — including
anything a future edit of this file might try — and a test asserts that exactly
two functions in the module can send at all.

If `APP_MODE` is still `shadow`, the probe refuses to run and says so: discovery
is a real action on a real account and should be a deliberate one.

