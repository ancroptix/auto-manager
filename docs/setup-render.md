# Render setup (and the honest limits of the free tier)

There are two ways to create this service and both are supported. **Manual Web
Service** is what this project actually runs on today; the Blueprint in
`render.yaml` is the same configuration in a file, and becomes the better option
once the branch is merged to `main` because it keeps env-var *names* (never values)
and the start command in sync automatically.

## 1a. Deploy — manual Web Service (current)

1. Push this branch to your repo.
2. <https://dashboard.render.com> → **New +** → **Web Service** → pick the
   `auto-manager` repository → **Connect**.
3. Fill the form exactly like this:

   | Field | Value |
   | --- | --- |
   | Name | `auto-manager` |
   | Region | **Oregon (us-oregon)** — reliably has free instances; Singapore is ~80 ms closer to the Tokyo database if your account offers free there |
   | Branch | `main` once merged, otherwise the branch you are testing |
   | Root directory | *(blank — the repo root)* |
   | Runtime | **Python 3** |
   | Build command | `pip install -r requirements.txt` |
   | Start command | `uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers` |
   | Instance type | **Free** |
   | Health check path | `/health` |

   `--workers 1` is not a preference: two processes means two queue loops. The
   database lease still keeps them correct, but one process is what the design
   assumes. `${PORT}` must stay as written — Render assigns the port and the app
   refuses to bind a hardcoded one.
4. Scroll to **Environment** and add these (Advanced → Add Environment Variable):

   | Key | Value |
   | --- | --- |
   | `APP_MODE` | `shadow` for the first deploy |
   | `DATABASE_URL` | the Supabase **pooler** string from [setup-supabase.md](setup-supabase.md) |
   | `DB_SSL` | `require` |
   | `CONTROL_TOKEN` | any long random string (see step 5) |
   | `TELEGRAM_API_ID` | number from <https://my.telegram.org> → API development tools |
   | `TELEGRAM_API_HASH` | the hash on the same page |
   | `TELEGRAM_BOT_TOKEN` | the token from **@BotFather** — see [control-bot.md](control-bot.md) |
   | `TELEGRAM_OWNER_USER_IDS` | your numeric Telegram user ID (from @userinfobot) |
   | `TELEGRAM_MAIN_ADMIN_USER_ID` | your main account's numeric ID |
   | `TELEGRAM_SESSION_SOURCE` | `both` |
   | `BOT_ALLOW_LOGIN` | `true` while setting up |
   | `PROBE_ON_BOOT` | `0` (flip to `1` once, later — see the last section) |

   The app reads 33 settings (`python -c "from app.config import Settings; print('\n'.join(Settings.model_fields))"` lists them). Only these matter on day one:

   | You need it for | These keys |
   | --- | --- |
   | booting at all | nothing — `shadow` starts with zero configuration and does no harm |
   | persisting anything | `DATABASE_URL` (+ `DB_SSL=require`) |
   | the HTTP kill switch | `CONTROL_TOKEN` |
   | sending to Telegram (`APP_MODE=live`) | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_OWNER_USER_IDS`, a session (`TELEGRAM_SESSION_STRING` is the optional way to provide one; `/login` is the normal way), `DATABASE_URL` |
   | the control bot answering you | `TELEGRAM_BOT_TOKEN` **and** an owner id — with no owner list it refuses to start |
   | never needed | `PORT` (Render sets it), `LOG_LEVEL`, the 11 `WORKER_*`/queue/lease tunables, `DB_POOL_*`, `DB_SEARCH_PATH`, `RECONCILE_ON_BOOT`, `MIGRATE_ON_BOOT`, `CAMPAIGN_RATE_PER_HOUR`, `BOT_ENABLED` |

   The live-mode list is enforced, not documented: `APP_MODE=live` refuses to boot
   without those five and prints the exact missing names, so "I forgot one" shows up
   as a red deploy instead of a service that quietly does nothing.

   `TELEGRAM_SESSION_STRING` is **not** in that list on purpose. You do not need it:
   `/login` on the control bot puts the session in the database instead. Leave the
   variable absent — an env value always wins over a stored one, so a stale copy
   pasted here would silently override the live session.
5. **Create Web Service**. Build takes ~2 minutes.

Do not paste the session string, the code Telegram sent you, or your 2FA password
into a chat with a person, an issue, or a commit. The control bot is the one place
a code may be typed — it deletes those messages, and `scripts/check_secrets.py`
(plus CI) fails the build if a credential is committed anyway. `scripts/login.py`
remains as the offline fallback for when Telegram's side of the login needs a
different network path.

## 1b. Deploy — Blueprint (after merging)

<https://dashboard.render.com> → **New +** → **Blueprint** → pick the repository →
**Next** → Render reads `render.yaml` and prompts only for the vars marked
`sync: false` (the secrets). Values in the file are applied as-is, which is why
`APP_MODE=shadow` is committed: the first deploy must not be able to message
anyone. Press **Apply**.

## 2. Check it, then connect Telegram

Before the table below, one log line decides everything else: the service prints
`DATABASE_URL is NOT set…` when the variable never got **Saved**, and
`control bot: enabled for N owner id(s)` when the bot came up. If a step below
misbehaves, read those lines first — every failure mode in this project has been a
variable that was typed into the wrong place.


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

Only the Blueprint path auto-deploys: `autoDeployTrigger: commit` means every push redeploys. On a push mid-upload the
instance is replaced; the job keeps its stage and boot reconciliation reclaims
its lease. That is the designed path, not an edge case — but it is why long
uploads are checkpointed per stage rather than at the end.

## 7a. Driving it from Telegram instead of curl

[control-bot.md](control-bot.md) is the operator's guide: creating the bot with
@BotFather, `/login` (which is how the user session gets connected), `/status`,
`/pause`, `/probe`, and how to close the login door afterwards. Everything it can
do is also in the `/control/*` routes above — the bot is a phone-sized interface to
the same switches, not a second set of permissions.

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
it is safe by construction: only `/start`-class text to the bots and the
owner, no uploads, no forwards, no channel posts, no permission changes, and a
hard message budget. A guard in `app/probe.py` rejects anything else — including
anything a future edit of this file might try — and a test asserts that exactly
two functions in the module can send at all.

If `APP_MODE` is still `shadow`, the probe refuses to run and says so: discovery
is a real action on a real account and should be a deliberate one.

