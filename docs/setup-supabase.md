# Supabase setup (about 10 minutes, mostly clicking)

Supabase stores **only metadata**: what exists, where it is archived, which link
belongs to which episode, and how far each job got. No video files ever pass
through it. That is why the free tier is enough.

## 1. Create the project

1. Go to <https://supabase.com/dashboard> → **New project**.
2. Name: `auto-manager`. Database password: generate one and **save it now** —
   it is not shown again.
3. Region: pick the one closest to your users (for India, `ap-south-1` Mumbai is
   usually the best choice).
4. Wait for provisioning (1–2 minutes).

## 2. Apply the schema

1. Left sidebar → **SQL Editor** → **New query**.
2. Open [`supabase/migrations/0001_init.sql`](../supabase/migrations/0001_init.sql)
   in a text editor, select **everything**, paste, press **Run**.
3. Repeat with
   [`supabase/migrations/0002_functions.sql`](../supabase/migrations/0002_functions.sql).
   **Order matters** — 0002 creates the triggers, queue functions and views that
   0001's tables depend on.
4. You should see `Success. No rows returned` twice. If you instead see an error
   naming a role such as `anon` or `service_role`, that is normal only for a
   self-hosted Postgres; on Supabase those roles always exist.

Sanity check — paste and Run:

```sql
select count(*) as tables_in_app_schema
from information_schema.tables where table_schema = 'app';
```

Expect **22**. Then:

```sql
select key from app.config order by random() limit 5;
```

Expect rows such as `branding.primary_handles`, `quality.order`. Those are the
templates and rules the operator can change later without a code deploy.

## 3. Get the connection string Render will use

1. **Project Settings → Database**.
2. Under **Connection pooling**, copy the **session** or **transaction** string:

   ```text
   postgresql://postgres.<project-ref>:<your-db-password>@aws-0-ap-south-1.pooler.supabase.com:6543/postgres
   ```

3. Use port **6543** (the pooler). The direct 5432 connection is not reachable
   from Render's free instances in many regions.

Two things the code already handles for you, so you do not have to:

* Port 6543 is transaction pooling, which breaks asyncpg's prepared-statement
  cache. The app detects `pooler.supabase.com`/`:6543` and disables it
  automatically.
* `DB_SSL=require` is set in `render.yaml`.

## 4. What NOT to put here

* No service keys, tokens, or session strings — the schema has nowhere for them
  and the free project is still world-readable at the network layer.
* No media files. Supabase free storage would fill up on one episode.
* Never `TRUNCATE app.job` while the service is running mid-upload; pause it
  first (`POST /control/pause`), or restart-safety will re-queue the file.

## 5. Confirm it is wired correctly

After the first Render deploy, open `https://<your-service>.onrender.com/ready`.

| Response | Meaning |
| --- | --- |
| `{"ready": true}` | Connected and migrations applied. Done. |
| `DATABASE_URL not configured` | The env var is missing on Render. |
| `database unreachable: ...` | Wrong string, or the DB password needs URL-encoding (`#`, `%`, `/` in a password must be escaped). |
| `schema: migrations_not_applied` | Step 2 did not run, or ran on a different project. |

## Optional: Supabase CLI instead of the SQL editor

```bash
supabase link --project-ref <project-ref>
supabase db push          # applies supabase/migrations/*.sql in filename order
```

Equivalent to pasting; use whichever you are comfortable with.
