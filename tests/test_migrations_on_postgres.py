"""Execute the real migrations against a real PostgreSQL and assert behaviour.

These tests apply 0001 and 0002 to a disposable cluster (``pgserver``, the same
PostgreSQL version family Supabase runs on) and then exercise the promises the
architecture document makes. The point is to test the *SQL*, not a Python
imitation of it: the queue lease, the stage machine, the campaign dedup key and
the "DM never approves" constraint all live in the database.

Skipped automatically when pgserver/psycopg are unavailable.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from conftest import config_row_count

import pytest

psycopg = pytest.importorskip("psycopg", reason="psycopg[binary] is a dev dependency")
pgserver = pytest.importorskip("pgserver", reason="pgserver bundles the test cluster")

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))

pytestmark = pytest.mark.integration

# Set by the `conn` fixture; needed by tests that build a Database.
pg_uri: str = ""


@pytest.fixture(scope="session")
def conn():
    # A temp dir, never the repo's .pgdata: `scripts/devdb.py` may be holding a
    # live local cluster there, and "delete" cleanup would destroy it mid-dev.
    import tempfile

    try:
        server = pgserver.get_server(
            Path(tempfile.gettempdir()) / "auto-manager-pg-tests", cleanup_mode="delete"
        )
    except Exception as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"cannot start bundled postgres: {exc}")
    global pg_uri
    pg_uri = server.get_uri()
    connection = psycopg.connect(pg_uri, autocommit=True)
    with connection.cursor() as cur:
        for role, attrs in (
            ("anon", "nologin"),
            ("authenticated", "nologin"),
            ("service_role", "nologin bypassrls"),
        ):
            cur.execute(
                f"select 1 from pg_roles where rolname = '{role}'"
            )
            if not cur.fetchone():
                cur.execute(f"create role {role} {attrs}")
    for path in MIGRATIONS:
        connection.execute(path.read_text(encoding="utf-8"))
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def clean_queue(conn):
    conn.execute("truncate table app.job_event, app.job restart identity")
    conn.execute("delete from app.join_campaign_contact")
    conn.execute("delete from app.join_campaign")
    conn.execute("delete from app.storage_link")
    conn.execute("delete from app.destination_post")
    conn.execute("update app.service_state set paused = false, paused_reason = null where id = 1")
    yield


@pytest.fixture(scope="session")
def seed(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "insert into app.series (title, normalized_title) values ('Bleach','bleach') "
            "on conflict (normalized_title) do update set title = excluded.title returning id"
        )
        series_id = cur.fetchone()[0]
        cur.execute(
            "insert into app.season (series_id, season_number, first_episode, last_episode) "
            "values (%s, 1, 1, 2) on conflict (series_id, season_number) do update "
            "set last_episode = excluded.last_episode returning id",
            (series_id,),
        )
        season_id = cur.fetchone()[0]
        cur.execute(
            "insert into app.episode (season_id, episode_number, canonical_key) "
            "values (%s, 1, 'bleach|s01|e01|hindi') on conflict (season_id, episode_number) "
            "do nothing returning id",
            (season_id,),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "select id from app.episode where season_id = %s and episode_number = 1", (season_id,)
            )
            episode_id = cur.fetchone()[0]
        else:
            episode_id = row[0]
        cur.execute(
            "insert into app.destination (series_id, telegram_channel_id, title) "
            "values (%s, -1001234, 'Bleach Anime in Hindi') "
            "on conflict (telegram_channel_id) do nothing returning id",
            (series_id,),
        )
        drow = cur.fetchone()
        if drow is None:
            cur.execute("select id from app.destination where telegram_channel_id = -1001234")
            destination_id = cur.fetchone()[0]
        else:
            destination_id = drow[0]
        cur.execute(
            "insert into app.source_channel (series_id, destination_id, telegram_channel_id, username, priority) "
            "values (%s, %s, -100999, '@ycanime_bleach', 1) on conflict (telegram_channel_id) do nothing",
            (series_id, destination_id),
        )
    return {
        "series_id": series_id,
        "season_id": season_id,
        "episode_id": episode_id,
        "destination_id": destination_id,
    }


@pytest.fixture
def episode(conn, seed) -> int:
    """A fresh episode per test.

    Variants, links and posts are unique per episode, so sharing one episode
    across tests would make a uniqueness assertion pass for the wrong reason.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select coalesce(max(episode_number), 0) + 1 from app.episode where season_id = %s",
            (seed["season_id"],),
        )
        number = cur.fetchone()[0]
        cur.execute(
            "insert into app.episode (season_id, episode_number, canonical_key) "
            "values (%s, %s, %s) returning id",
            (seed["season_id"], number, f"test|s01|e{number:03d}|hindi"),
        )
        return cur.fetchone()[0]


def val(conn, sql, *args):
    """Scalar query helper. Accepts val(c, sql, 1) and val(c, sql, (1,))."""
    if len(args) == 1 and isinstance(args[0], tuple):
        args = args[0]
    with conn.cursor() as cur:
        cur.execute(sql, args or None)
        row = cur.fetchone()
        return row[0] if row else None


def job_by_key(conn, key):
    with conn.cursor() as cur:
        cur.execute(
            "select to_jsonb(j) from app.job j where dedup_key = %s", (key,)
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------- apply / shape
def test_migrations_are_idempotent_enough_to_reapply(conn):
    """Re-running must not abort a deploy."""
    for path in MIGRATIONS:
        conn.execute(path.read_text(encoding="utf-8"))


def test_expected_tables_exist(conn):
    expected = {
        "config", "service_state", "series", "destination", "source_channel",
        "archive_channel", "season", "episode", "media_variant", "source_candidate",
        "thumbnail_review", "dupe_fingerprint", "processed_message", "storage_link",
        "destination_post", "job", "job_event", "join_request", "join_campaign",
        "join_campaign_contact", "reply_log", "audit_log",
    }
    found = {
        r[0]
        for r in conn.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'app' and table_type = 'BASE TABLE'"
        ).fetchall()
    }
    assert expected <= found, f"missing tables: {sorted(expected - found)}"


def test_rls_is_on_and_default_deny_for_api_roles(conn):
    rows = conn.execute(
        """
        select c.relname, c.relrowsecurity, c.relforcerowsecurity
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'app' and c.relkind = 'r'
        """
    ).fetchall()
    assert rows
    for name, enabled, forced in rows:
        assert enabled, f"{name} has no RLS"
        # FORCE would lock out our own worker, which connects as the table owner.
        assert not forced, f"{name} is FORCE'd and the app would be locked out"


def test_no_policies_exist_for_public_api_roles(conn):
    count = val(
        conn,
        "select count(*) from pg_policies where schemaname = 'app'",
    )
    assert count == 0, "default-deny is the intended state; a policy would open it"


def test_anon_role_cannot_read_anything(conn):
    with conn.cursor() as cur:
        cur.execute("set role anon")
        try:
            cur.execute("select count(*) from app.job")
            leaked = cur.fetchone()[0]
            raise AssertionError(f"anon could read app.job ({leaked} rows)")
        except psycopg.errors.InsufficientPrivilege:
            pass
        finally:
            cur.execute("reset role")


def test_api_schema_is_not_in_public_search_path(conn):
    """Nothing in `public`, so the PostgREST Data API cannot serve it."""
    count = val(
        conn,
        """
        select count(*) from information_schema.tables
        where table_schema = 'public' and table_name in
          ('job','episode','media_variant','join_campaign','storage_link')
        """,
    )
    assert count == 0


def test_config_seeds_present(conn):
    keys = {
        r[0]
        for r in conn.execute("select key from app.config").fetchall()
    }
    for required in (
        "branding.primary_handles",
        "quality.order",
        "thumbnail.strict_mode",
        "destination.channel_per_series",
        "bots.storage_username",
        "bots.channel_help_username",
        "campaign.mode",
    ):
        assert required in keys, f"{required} missing from default config"


def test_branding_defaults_match_the_agreement(conn):
    handles = val(conn, "select value from app.config where key = 'branding.primary_handles'")
    assert sorted(handles) == ["india_crunchyroll", "ycanime"]
    # 0004 replaces 0002's lowercase footer with the operator's own casing; the
    # allow-list itself is untouched, because matching is casefolded.
    footer = val(conn, "select value from app.config where key = 'branding.footer'")
    from app.captions import APPROVED_FOOTER

    assert footer == APPROVED_FOOTER
    order = val(conn, "select value from app.config where key = 'quality.order'")
    assert order == ["360p", "480p", "720p", "1080p", "2160p"]


# --------------------------------------------------------------- queue semantics
def test_enqueue_is_idempotent(conn, seed):
    first = val(conn, "select to_jsonb(app.enqueue_job('ingest_media', 'eq:1'))")
    assert first and first["status"] == "queued"
    again = val(conn, "select to_jsonb(app.enqueue_job('ingest_media', 'eq:1'))")
    assert again is None, "a re-scan must not create a second job for the same file"
    assert val(conn, "select count(*) from app.job where dedup_key = 'eq:1'") == 1


def test_claim_grants_an_exclusive_lease(conn):
    conn.execute("select app.enqueue_job('archive_media', 'lease:1')")
    claimed = val(conn, "select to_jsonb(app.claim_next_job('worker-a', 60))")
    assert claimed and claimed["status"] == "running"
    assert claimed["locked_by"] == "worker-a"
    assert claimed["attempts"] == 1
    other = val(conn, "select to_jsonb(app.claim_next_job('worker-b', 60))")
    assert other is None, "two processes claimed the same file; uploads would duplicate"


def test_claim_respects_the_kill_switch(conn):
    conn.execute("select app.enqueue_job('publish_post', 'pause:1')")
    conn.execute("select app.set_pause(true, 'operator kill switch')")
    assert val(conn, "select to_jsonb(app.claim_next_job('w', 60))") is None
    conn.execute("select app.set_pause(false)")
    assert val(conn, "select (to_jsonb(app.claim_next_job('w', 60)))->>'dedup_key'") == "pause:1"


def test_paused_reason_is_cleared_on_resume(conn):
    conn.execute("select app.set_pause(true, 'watermark review needed')")
    conn.execute("select app.set_pause(false)")
    assert val(conn, "select paused_reason from app.service_state") is None


def test_stage_checkpoint_rejects_skipping(conn):
    conn.execute("select app.enqueue_job('storage_upload', 'stage:1')")
    job_id = val(conn, "select id from app.job where dedup_key = 'stage:1'")
    conn.execute("select app.checkpoint_job(%s, 'thumbnail_checked')", (job_id,))
    with pytest.raises(psycopg.errors.RaiseException, match="invalid stage transition"):
        conn.execute("select app.checkpoint_job(%s, 'destination_posted')", (job_id,))
    assert val(conn, "select stage from app.job where id = %s", (job_id,)) == "thumbnail_checked"


def test_replaying_a_stage_is_allowed_for_retries(conn):
    conn.execute("select app.enqueue_job('link_verify', 'stage:2')")
    job_id = val(conn, "select id from app.job where dedup_key = 'stage:2'")
    conn.execute("select app.checkpoint_job(%s, 'thumbnail_checked')", (job_id,))
    conn.execute("select app.checkpoint_job(%s, 'thumbnail_checked')", (job_id,))
    assert val(conn, "select count(*) from app.job_event where job_id = %s", (job_id,)) == 2


def test_checkpoint_data_is_durable_for_resume(conn):
    conn.execute("select app.enqueue_job('archive_media', 'stage:3')")
    job_id = val(conn, "select id from app.job where dedup_key = 'stage:3'")
    conn.execute(
        "select app.checkpoint_job(%s, 'thumbnail_checked', '{\"archive_message_id\": 777}'::jsonb)",
        (job_id,),
    )
    stored = val(conn, "select data from app.job_event where job_id = %s order by id desc limit 1", (job_id,))
    assert stored["archive_message_id"] == 777, "resume would re-upload instead of continuing"


def test_expired_leases_are_reclaimed_and_stage_is_kept(conn):
    conn.execute("select app.enqueue_job('archive_media', 'lease:2')")
    job_id = val(conn, "select id from app.job where dedup_key = 'lease:2'")
    conn.execute("select app.claim_next_job('dead-worker', 60)")
    conn.execute("select app.checkpoint_job(%s, 'thumbnail_checked')", (job_id,))
    conn.execute("update app.job set locked_until = now() - interval '1 minute' where id = %s", (job_id,))
    assert val(conn, "select app.release_expired_locks()") == 1
    row = conn.execute(
        "select status, stage, locked_by from app.job where id = %s", (job_id,)
    ).fetchone()
    assert row == ("queued", "thumbnail_checked", None), (
        "the job must resume from its checkpoint, not restart from scratch"
    )


def test_retry_then_block_when_budget_spent(conn):
    conn.execute("select app.enqueue_job('publish_post', 'retry:1')")
    job_id = val(conn, "select id from app.job where dedup_key = 'retry:1'")
    conn.execute("update app.job set attempts = 1 where id = %s", (job_id,))
    conn.execute("select app.fail_job(%s, 'menu changed', 60)", (job_id,))
    assert val(conn, "select status from app.job where id = %s", (job_id,)) == "failed"
    when = val(conn, "select next_attempt_at > now() from app.job where id = %s", (job_id,))
    assert when, "a retry must wait, not hot-loop"
    conn.execute("update app.job set attempts = max_attempts where id = %s", (job_id,))
    conn.execute("select app.fail_job(%s, 'still broken', 60)", (job_id,))
    assert val(conn, "select status from app.job where id = %s", (job_id,)) == "blocked"
    assert val(conn, "select last_error from app.job where id = %s", (job_id,)) == "still broken"


def test_hold_and_review_episodes_are_not_claimed(conn, seed):
    conn.execute(
        "select app.enqueue_job('archive_media', 'hold:1', 'discovered', '{}'::jsonb, 100, %s)",
        (seed["episode_id"],),
    )
    conn.execute("update app.episode set status = 'hold' where id = %s", (seed["episode_id"],))
    assert val(conn, "select to_jsonb(app.claim_next_job('w', 60))") is None
    conn.execute("update app.episode set status = 'incomplete' where id = %s", (seed["episode_id"],))
    assert val(conn, "select (to_jsonb(app.claim_next_job('w', 60)))->>'dedup_key'") == "hold:1"


def test_running_job_without_a_lease_is_rejected(conn):
    conn.execute("select app.enqueue_job('edit_post', 'lease:3')")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("update app.job set status = 'running' where dedup_key = 'lease:3'")


# ------------------------------------------------------- manifest / dedup rules
def test_manifest_orders_by_quality_not_arrival(conn, seed, episode):
    """Arrival order never decides display order.

    Uploaded 1080p first, then 720p and 480p — the viewer must still be offered
    480p, 720p, 1080p in that order.
    """
    ep = episode
    for quality, rnk in (("1080p", 4), ("720p", 3), ("480p", 2)):
        conn.execute(
            "insert into app.media_variant (episode_id, quality, quality_rank) values (%s, %s, %s)",
            (ep, quality, rnk),
        )
    ordered = val(
        conn,
        "select jsonb_path_query_array(qualities, '$[*].quality') from app.v_episode_manifest where episode_id = %s",
        (ep,),
    )
    assert ordered == ["480p", "720p", "1080p"]


def test_duplicate_quality_is_rejected_case_insensitively(conn, seed, episode):
    conn.execute("insert into app.media_variant (episode_id, quality, quality_rank) values (%s, '1080p', 4)", (episode,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "insert into app.media_variant (episode_id, quality, quality_rank) values (%s, '1080P', 4)",
            (episode,),
        )


def test_episode_stays_open_for_later_qualities(conn, seed, episode):
    """'I only had 480p and 720p for episode 1' + 1080p found months later."""
    ep = episode
    conn.execute("insert into app.media_variant (episode_id, quality, quality_rank) values (%s,'480p',2),(%s,'720p',3)", (ep, ep))
    assert val(conn, "select status from app.episode where id = %s", (ep,)) == "incomplete"
    conn.execute("insert into app.media_variant (episode_id, quality, quality_rank) values (%s,'1080p',4)", (ep,))
    assert val(conn, "select count(*) from app.media_variant where episode_id = %s", (ep,)) == 3


def test_one_permanent_post_per_episode(conn, seed, episode):
    ep = episode
    conn.execute(
        "insert into app.destination_post (destination_id, kind, episode_id, message_id) values (%s,'episode',%s,501)",
        (seed["destination_id"], ep),
    )
    with pytest.raises(psycopg.errors.UniqueViolation, match="destination_post_episode_uq"):
        conn.execute(
            "insert into app.destination_post (destination_id, kind, episode_id, message_id) values (%s,'episode',%s,502)",
            (seed["destination_id"], ep),
        )
    # The 1080p update must therefore be an EDIT of that same message.
    conn.execute(
        "update app.destination_post set message_id = 501, edited_at = now() where episode_id = %s",
        (ep,),
    )
    assert val(conn, "select count(*) from app.destination_post where episode_id = %s", (ep,)) == 1


def test_season_sticker_post_is_also_unique(conn, seed):
    conn.execute(
        "insert into app.destination_post (destination_id, kind, season_id, message_id) values (%s,'season_sticker',%s,10)",
        (seed["destination_id"], seed["season_id"]),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "insert into app.destination_post (destination_id, kind, season_id, message_id) values (%s,'season_sticker',%s,11)",
            (seed["destination_id"], seed["season_id"]),
        )


def test_one_active_universal_link_per_episode(conn, seed, episode):
    conn.execute(
        "insert into app.storage_link (url, kind, episode_id) values ('https://s/1','universal',%s)",
        (episode,),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "insert into app.storage_link (url, kind, episode_id) values ('https://s/2','universal',%s)",
            (episode,),
        )
    # Superseding is allowed once the old one is deactivated, which is how the
    # "add 1080p to the batch" flow replaces a link without losing history.
    conn.execute("update app.storage_link set active = false, superseded_at = now() where url = 'https://s/1'")
    conn.execute(
        "insert into app.storage_link (url, kind, episode_id) values ('https://s/3','universal',%s)",
        (episode,),
    )
    assert val(conn, "select count(*) from app.storage_link where episode_id = %s", (episode,)) == 2


def test_url_scheme_is_validated(conn, seed, episode):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into app.storage_link (url, kind, episode_id) values ('file:///etc/passwd','single',%s)",
            (episode,),
        )


def test_multi_source_priority_ordering(conn):
    rows = conn.execute(
        "select username from app.source_channel where active order by priority"
    ).fetchall()
    assert rows, "seed source channel missing"


def test_processed_message_blocks_reingest(conn, seed):
    conn.execute(
        "insert into app.processed_message (source_channel_id, message_id) "
        "select id, 4321 from app.source_channel limit 1"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "insert into app.processed_message (source_channel_id, message_id) "
            "select id, 4321 from app.source_channel limit 1"
        )


def test_fingerprint_lookup_is_indexed_and_unique(conn, episode):
    conn.execute(
        "insert into app.media_variant (episode_id, quality, quality_rank, fingerprint) "
        "values (%s, '2160p', 5, 'fp-abc')",
        (episode,),
    )
    variant_id = conn.execute(
        "select id from app.media_variant where fingerprint = 'fp-abc'"
    ).fetchone()[0]
    conn.execute(
        "insert into app.dupe_fingerprint (fingerprint, variant_id) values ('fp-abc', %s)",
        (variant_id,),
    )
    assert val(conn, "select app.is_seen_fingerprint('fp-abc')") is True
    assert val(conn, "select app.is_seen_fingerprint('fp-new')") is False


# ------------------------------------------------------------- join-request rules
def test_messaging_never_approves_a_request(conn, seed):
    """The schema refuses to model the thing we promised not to do."""
    with pytest.raises(psycopg.errors.CheckViolation, match="campaign_never_approves"):
        conn.execute(
            "insert into app.join_campaign (destination_id, name, message_template, approve_after_send) "
            "values (%s, 'welcome', 'Hi', true)",
            (seed["destination_id"],),
        )
    conn.execute(
        "insert into app.join_campaign (destination_id, name, message_template, approve_after_send) "
        "values (%s, 'welcome', 'Hi', false)",
        (seed["destination_id"],),
    )
    assert val(conn, "select status from app.join_campaign order by id desc limit 1") == "draft"


def test_same_campaign_cannot_message_a_user_twice(conn, seed):
    conn.execute(
        "insert into app.join_campaign (destination_id, name, message_template, status) "
        "values (%s, 'w2', 'Hi', 'running') returning id",
        (seed["destination_id"],),
    )
    campaign_id = val(conn, "select max(id) from app.join_campaign")
    conn.execute(
        "insert into app.join_campaign_contact (campaign_id, telegram_user_id, status, sent_at) "
        "values (%s, 555, 'sent', now())",
        (campaign_id,),
    )
    allowed, reason = conn.execute(
        "select allowed, reason from app.contact_allowed(%s, 555)", (campaign_id,)
    ).fetchone()
    assert allowed is False
    assert reason == "duplicate_user"
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "insert into app.join_campaign_contact (campaign_id, telegram_user_id) values (%s, 555)",
            (campaign_id,),
        )


def test_hourly_rate_guard(conn, seed):
    conn.execute(
        "insert into app.join_campaign (destination_id, name, message_template, status, rate_per_hour) "
        "values (%s, 'w3', 'Hi', 'running', 2)",
        (seed["destination_id"],),
    )
    campaign_id = val(conn, "select max(id) from app.join_campaign")
    for user in (101, 102):
        conn.execute(
            "insert into app.join_campaign_contact (campaign_id, telegram_user_id, status, sent_at) "
            "values (%s, %s, 'sent', now())",
            (campaign_id, user),
        )
    allowed, reason = conn.execute(
        "select allowed, reason from app.contact_allowed(%s, 103)", (campaign_id,)
    ).fetchone()
    assert (allowed, reason) == (False, "hourly_rate_exhausted")


def test_flood_wait_pauses_a_user_not_the_queue(conn, seed):
    conn.execute(
        "insert into app.join_campaign (destination_id, name, message_template, status) "
        "values (%s, 'w4', 'Hi', 'running')",
        (seed["destination_id"],),
    )
    campaign_id = val(conn, "select max(id) from app.join_campaign")
    conn.execute(
        "insert into app.join_campaign_contact (campaign_id, telegram_user_id, status, flood_wait_until) "
        "values (%s, 201, 'queued', now() + interval '2 hours')",
        (campaign_id,),
    )
    allowed, reason = conn.execute(
        "select allowed, reason from app.contact_allowed(%s, 201)", (campaign_id,)
    ).fetchone()
    assert (allowed, reason) == (False, "flood_wait_active")


def test_campaign_progress_view_counts(conn, seed):
    conn.execute(
        "insert into app.join_campaign (destination_id, name, message_template, status, rate_per_hour) "
        "values (%s, 'w5', 'Hi', 'running', 20)",
        (seed["destination_id"],),
    )
    campaign_id = val(conn, "select max(id) from app.join_campaign")
    with conn.cursor() as cur:
        cur.execute(
            "insert into app.join_campaign_contact (campaign_id, telegram_user_id, status, sent_at) "
            "values (%s, 301, 'sent', now() - interval '5 minutes')",
            (campaign_id,),
        )
        cur.executemany(
            "insert into app.join_campaign_contact (campaign_id, telegram_user_id, status) values (%s, %s, %s)",
            [
                (campaign_id, 302, "failed"),
                (campaign_id, 303, "restricted"),
                (campaign_id, 304, "queued"),
            ],
        )
    row = conn.execute(
        "select sent, failed, restricted, pending, remaining_this_hour from app.v_campaign_progress where campaign_id = %s",
        (campaign_id,),
    ).fetchone()
    assert row == (1, 1, 1, 1, 19), "restricted sends must be counted, not hidden"


# ------------------------------------------------------------------ other views
# ------------------------------------------------- source declarations (0006)
def _declare_channel(conn, channel_id: int, **values: object) -> None:
    """Write the operator's channel-level statements, as /source would."""
    columns = [f"declared_{name}" for name in values]
    assignments = ", ".join(f"{name} = %s" for name in columns)
    with conn.cursor() as cur:
        cur.execute(
            f"update app.source_channel set {assignments}, declared_by = 'operator', declared_at = now()"
            " where id = %s",
            (*values.values(), channel_id),
        )


def test_a_bare_file_channel_parks_until_the_channel_is_declared(conn):
    """The scenario the operator described: a channel of mp4s captioned only ``episode 1``.

    Nothing in that text says which show it is or what language the audio is, so the first
    scan must park the file — the Hindi-in-scope rule and the destination-naming rule are both
    statements about the *show*, and inventing either one is a public, permanent mistake. One
    line per channel (``/source``) un-parks the whole backlog, because the re-scan is allowed
    to re-read a row that is still parked and never a row that has been decided.
    """
    import asyncio

    from app.db import Database
    from app.ingest import record_message

    channel_id, series_id = _ingest_channel(conn, "@yc_bare", -100700098, "Bare Shelf")
    db = Database(_ingest_settings())

    async def scenario():
        assert await db.connect(), await db.last_error

        async def scan(message_id: int, **extra):
            return await record_message(
                db,
                source_channel_id=channel_id,
                message_id=message_id,
                media_type="document",
                file_name="episode 1.mp4",
                raw_caption="episode 1",
                fingerprint=f"bare-{message_id}",
                **extra,
            )

        parked = await scan(8101)
        assert parked["disposition"] == "pending", parked
        assert "Hindi audio" in parked["reason"], parked
        # One signal, from the channel's own title: enough to park on, not enough to name a
        # destination channel after.
        assert parked["series_source"] == "channel_name" and parked["series_confirmed"] is False
        assert parked["episodes"] == [] and parked["variants"] == []

        _declare_channel(conn, channel_id, series="Bleach Bare", audio="hindi")
        filed = await scan(8101)
        assert filed["disposition"] == "accepted", filed
        assert filed["audio_source"] == "channel_declaration", filed
        assert filed["series_source"] == "channel_declaration" and filed["series_confirmed"] is True
        assert len(filed["episodes"]) == 1 and len(filed["variants"]) == 1, filed

        row = await db.fetchrow(
            "select language_tag, disposition, parsed->>'audio_source' as audio_source"
            "  from app.source_candidate where source_channel_id = $1 and message_id = 8101",
            channel_id,
        )
        assert row["disposition"] == "accepted" and row["audio_source"] == "channel_declaration", dict(row)
        # The language column and the dedup key are the same fact from the same source: an
        # episode whose key says hindi and whose column says nothing is a duplicate waiting
        # to be filed by the next source.
        assert row["language_tag"] == "hindi", dict(row)

        # And a decision, once made, is not re-litigated by a later declaration — which is
        # the price of making a rescan safe to run at any time.
        _declare_channel(conn, channel_id, audio="subbed_only")
        unchanged = await scan(8101)
        assert "already ingested and decided" in str(unchanged.get("skipped", "")), unchanged
        still = await db.fetchrow(
            "select disposition from app.source_candidate where source_channel_id = $1 and message_id = 8101",
            channel_id,
        )
        assert still["disposition"] == "accepted", dict(still)

        # A file nobody has decided yet *is* affected: the channel now says its files are
        # subbed-only, so this one is out of scope on the channel's own statement. That is
        # the whole design in two lines — declarations move the undecided, never the decided.
        other = await scan(8102)
        assert other["disposition"] == "rejected", other
        assert "subbed-only" in other["reason"] and other["audio_source"] == "channel_declaration", other
        return filed

    try:
        asyncio.run(scenario())
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "delete from app.source_candidate where source_channel_id = %s", (channel_id,)
            )
            cur.execute("delete from app.processed_message where source_channel_id = %s", (channel_id,))
            cur.execute(
                "delete from app.season where series_id = (select id from app.series where title = %s)",
                ("Bare Shelf",),
            )


def test_video_dimensions_supply_the_quality_when_no_label_exists(conn):
    """Telegram states a video's pixel height whether or not anyone captioned it, which is the
    one quality fact a shelf of bare files can give us without a download."""
    import asyncio

    from app.db import Database
    from app.ingest import record_message

    channel_id, _series_id = _ingest_channel(conn, "@yc_bare_hd", -100700099, "Bare HD")
    _declare_channel(conn, channel_id, series="Bare HD", audio="hindi")
    db = Database(_ingest_settings())

    async def scenario():
        assert await db.connect(), await db.last_error
        report = await record_message(
            db,
            source_channel_id=channel_id,
            message_id=8201,
            media_type="document",
            file_name="episode 4.mp4",
            raw_caption="episode 4",
            video_height=1080,
            video_width=1920,
            fingerprint="hd-4",
        )
        assert report["disposition"] == "accepted", report
        assert report["quality_source"] == "dimensions", report
        variant = await db.fetchrow(
            "select quality, quality_rank from app.media_variant where episode_id = $1 order by id",
            report["episodes"][0],
        )
        assert variant["quality"] == "1080p", dict(variant)
        # 1920 is the *width*; a scanner that handed over the long side of a vertical clip
        # would otherwise label a 1080 file 1440p forever.
        assert "quality_from_dimensions:1080" in str(report["flags"]), report
        assert variant["quality_rank"] == 4, dict(variant)

        # The label still outranks the pixels, and the disagreement is recorded rather than
        # resolved by whichever signal was read last.
        labelled = await record_message(
            db,
            source_channel_id=channel_id,
            message_id=8202,
            media_type="document",
            file_name="episode 5 720p.mp4",
            raw_caption="episode 5",
            video_height=1080,
            video_width=1920,
            fingerprint="hd-5",
        )
        assert "quality_label_disagrees_with_dimensions:1080p" in labelled["flags"], labelled
        assert labelled["quality_source"] == "caption", labelled
        return variant

    try:
        asyncio.run(scenario())
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from app.source_candidate where source_channel_id = %s", (channel_id,))
            cur.execute("delete from app.processed_message where source_channel_id = %s", (channel_id,))


def test_the_audio_declaration_has_a_global_off_switch(conn):
    """``ingest.accept_channel_audio_declaration`` exists so that "the channel's word
    licenses a file's language" can be paused while someone works out why a caption says
    Hindi on a subbed file — without editing twenty channel rows, and without losing the
    declarations themselves."""
    import asyncio

    from app.db import Database
    from app.ingest import record_message

    channel_id, _series_id = _ingest_channel(conn, "@yc_bare_knob", -100700100, "Bare Knob")
    _declare_channel(conn, channel_id, series="Bare Knob", audio="hindi")
    db = Database(_ingest_settings())
    key = "ingest.accept_channel_audio_declaration"
    original = _config_value(conn, key)

    async def scan(message_id: int):
        return await record_message(
            db,
            source_channel_id=channel_id,
            message_id=message_id,
            media_type="document",
            file_name="episode 9.mp4",
            raw_caption="episode 9",
            fingerprint=f"knob-{message_id}",
        )

    async def scenario():
        assert await db.connect(), await db.last_error
        # The accepted path first, with the knob on, so the only difference between the runs
        # below is the knob itself. One loop, one pool: a Database cannot be reused across
        # asyncio.run() calls, which is why the toggles happen in here.
        accepted = await scan(8300)
        assert accepted["disposition"] == "accepted", accepted
        with conn.cursor() as cur:
            cur.execute("update app.config set value = 'false'::jsonb where key = %s", (key,))
        blocked = await scan(8301)
        assert blocked["disposition"] == "pending", blocked
        assert blocked["audio_declaration_ignored"] is True, blocked
        assert blocked["audio_source"] == "none", blocked
        with conn.cursor() as cur:
            cur.execute("update app.config set value = 'true'::jsonb where key = %s", (key,))
        resumed = await scan(8302)
        assert resumed["disposition"] == "accepted", resumed
        assert resumed["audio_declaration_ignored"] is False, resumed

    try:
        asyncio.run(scenario())
    finally:
        with conn.cursor() as cur:
            cur.execute("update app.config set value = %s::jsonb where key = %s", (json.dumps(original), key))
        with conn.cursor() as cur:
            cur.execute("delete from app.source_candidate where source_channel_id = %s", (channel_id,))
            cur.execute("delete from app.processed_message where source_channel_id = %s", (channel_id,))


def test_a_declared_season_is_a_numbering_default_not_a_boundary(conn):
    """``/source ... season 2`` says "assume season 2 when the file says nothing".

    It must not open a season's sticker sequence or be recorded as a boundary: the season
    claim belongs to /declare, and the boundary claim belongs to a caption that stated a
    season. This is the same rule as the old ``season_hint``, asserted against the schema
    because the difference between a default and a claim is a week of silence at a boundary.
    """
    import asyncio

    from app.db import Database
    from app.ingest import record_message

    channel_id, _series_id = _ingest_channel(conn, "@yc_bare_s2", -100700101, "Bare S2")
    _declare_channel(conn, channel_id, series="Bare S2", audio="hindi", season=2)
    db = Database(_ingest_settings())

    async def scenario():
        assert await db.connect(), await db.last_error
        report = await record_message(
            db,
            source_channel_id=channel_id,
            message_id=8401,
            media_type="document",
            file_name="episode 3.mp4",
            raw_caption="episode 3",
            fingerprint="s2-3",
        )
        assert report["disposition"] == "accepted", report
        assert report["season"]["season"] == 2, report
        assert report["season_number"] == 2, report
        assert report["season"]["verdict"] in {"first", "continue"}, report
        season = await db.fetchrow(
            "select first_episode, last_episode, boundary_kind from app.season"
            "  where series_id = (select series_id from app.source_channel where id = $1)"
            "    and season_number = 2",
            channel_id,
        )
        assert season["boundary_kind"] is None, dict(season)
        assert (season["first_episode"], season["last_episode"]) == (None, None), dict(season)
        jobs = await db.fetch(
            "select count(*) as n from app.job where kind = 'season_sticker'"
            "  and payload->>'season' = '2'"
        )
        assert jobs[0]["n"] == 0, "a numbering default must not post a season opening sticker"
        return season

    try:
        asyncio.run(scenario())
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from app.source_candidate where source_channel_id = %s", (channel_id,))
            cur.execute("delete from app.processed_message where source_channel_id = %s", (channel_id,))


def test_season_coverage_needs_files_not_just_row_counts(conn):
    """A "Complete Season" post must never be published for an empty season.

    Coverage is only complete when every declared episode exists *and* has at
    least one archived variant.
    """
    with conn.cursor() as cur:
        cur.execute("insert into app.series (title, normalized_title) values ('Naruto','naruto') returning id")
        series_id = cur.fetchone()[0]
        cur.execute(
            "insert into app.season (series_id, season_number, first_episode, last_episode) "
            "values (%s, 1, 1, 2) returning id",
            (series_id,),
        )
        season_id = cur.fetchone()[0]
        episode_ids = []
        for number in (1, 2):
            cur.execute(
                "insert into app.episode (season_id, episode_number, canonical_key) values (%s, %s, %s) returning id",
                (season_id, number, f"naruto|s01|e{number:02d}|hindi"),
            )
            episode_ids.append(cur.fetchone()[0])

        def complete() -> bool:
            return cur.execute(
                "select season_complete from app.v_season_coverage where season_id = %s",
                (season_id,),
            ).fetchone()[0]

        assert complete() is False, "episodes with no archived files are not a complete season"
        cur.execute(
            "insert into app.media_variant (episode_id, quality, quality_rank) values (%s, '480p', 2)",
            (episode_ids[0],),
        )
        assert complete() is False, "half-imported season must not trigger a batch post"
        cur.execute(
            "insert into app.media_variant (episode_id, quality, quality_rank) values (%s, '720p', 3)",
            (episode_ids[1],),
        )
        # The phase 0005 added, and the whole point of it: the season row is dropped
        # back to "undeclared" with the same two episodes and the same files, and now it
        # must NOT read as finished. This is the pause bug — a source that stopped at 2 of
        # 26 is indistinguishable from a 2-episode season by observation alone.
        cur.execute(
            "update app.season set first_episode = null, last_episode = null, "
            "observed_first = 1, observed_last = 2 where id = %s",
            (season_id,),
        )
        assert complete() is False, "what the source delivered is not what the season is"
        cur.execute("update app.season set first_episode = 1, last_episode = 2 where id = %s", (season_id,))
        assert complete() is True, "2 of 2 declared episodes, each with a file"
        cur.execute("update app.season set last_episode = 3 where id = %s", (season_id,))
        assert complete() is False, "the owner raising the declared length reopens the season"
        cur.execute("delete from app.media_variant where episode_id = any(%s::bigint[])", (episode_ids,))
        cur.execute("delete from app.episode where season_id = %s", (season_id,))
        cur.execute("delete from app.season where id = %s", (season_id,))
        cur.execute("delete from app.series where id = %s", (series_id,))


def test_updated_at_trigger_moves(conn, seed):
    before = val(conn, "select updated_at from app.episode where id = %s", (seed["episode_id"],))
    conn.execute("update app.episode set status = 'review' where id = %s", (seed["episode_id"],))
    after = val(conn, "select updated_at from app.episode where id = %s", (seed["episode_id"],))
    assert after > before


def test_queue_health_view_counts_states(conn):
    conn.execute("select app.enqueue_job('link_health_check', 'qh:1')")
    conn.execute("select app.claim_next_job('w', 60)")
    row = conn.execute("select queued, running, blocked, expired_leases from app.v_queue_health").fetchone()
    assert row[0] + row[1] >= 1


def test_audit_log_records_decisions(conn):
    conn.execute(
        "insert into app.audit_log (actor_user_id, action, entity_type, entity_id, detail) "
        "values (7, 'watermark_approve', 'source_candidate', 1, '{\"note\":\"owner override\"}'::jsonb)"
    )
    detail = val(conn, "select detail from app.audit_log order by id desc limit 1")
    assert detail["note"] == "owner override"


def test_thumbnail_review_requires_a_candidate(conn):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "insert into app.thumbnail_review (candidate_id, detected_handles) values (999999, array['@thief'])"
        )


def test_watermark_review_lists_disallowed_handles(conn):
    with conn.cursor() as cur:
        cur.execute(
            "insert into app.source_candidate (source_channel_id, message_id, file_name, detected_handles, thumbnail_status) "
            "values ((select id from app.source_channel limit 1), 900, 'x.mkv', array['@leecher'], 'watermarked') returning id"
        )
        candidate_id = cur.fetchone()[0]
        cur.execute(
            "insert into app.thumbnail_review (candidate_id, detected_handles) values (%s, array['@leecher'])",
            (candidate_id,),
        )
        row = cur.execute(
            "select r.status, c.disposition from app.thumbnail_review r join app.source_candidate c on c.id = r.candidate_id where r.candidate_id = %s",
            (candidate_id,),
        ).fetchone()
    assert row == ("pending", "pending"), "a watermarked file must wait for the owner, not publish"


# ---------------------------------------------------------------- app/db.py layer
# The SQL being correct is not enough: the Python wrappers around these
# functions must unwrap the composite results the same way. A `row_to_json(j)
# as job` alias once passed every SQL test while the live service KeyErrors on
# "id" — so the wrappers are tested against the real database here.
def test_database_layer_roundtrip(conn):
    import asyncio

    from app.config import Settings
    from app.db import Database
    from app.stages import JobKind, JobStage

    settings = Settings(
        _env_file=None,
        database_url=pg_uri,
        db_ssl="disable",
        worker_enabled=False,
    )
    db = Database(settings)

    async def scenario():
        assert await db.connect(), "app/db.py could not reach the cluster the tests just used"
        key = "wrapper:roundtrip"
        try:
            assert await db.fetchrow("delete from app.job where dedup_key = $1", key) is None
            created = await db.enqueue(JobKind.RECONCILIATION.value, key, payload={"n": 1})
            assert created and "id" in created, f"enqueue returned {created!r}"
            assert created["payload"] == {"n": 1}, "jsonb must arrive decoded as a dict"
            assert await db.enqueue(JobKind.RECONCILIATION.value, key) is None

            claimed = await db.claim("wrapper-test")
            assert claimed and claimed["id"] == created["id"], f"claim returned {claimed!r}"
            assert claimed["status"] == "running"

            moved = await db.checkpoint(created["id"], JobStage.THUMBNAIL_CHECKED, {"archive_message_id": 5})
            assert moved["stage"] == "thumbnail_checked"
            with pytest.raises(Exception, match="invalid stage transition"):
                await db.checkpoint(created["id"], JobStage.COMPLETED)

            await db.complete(created["id"], {"ok": True})
            done = await db.fetchrow("select status, result from app.job where id = $1", created["id"])
            assert done["status"] == "succeeded" and done["result"] == {"ok": True}
            assert await db.claim("wrapper-test") is None
            return (await db.queue_health())["succeeded_1h"] >= 1
        finally:
            await db.close()

    assert asyncio.run(scenario())


def test_database_layer_reports_schema_state(conn):
    import asyncio

    from app.config import Settings
    from app.db import Database

    db = Database(Settings(_env_file=None, database_url=pg_uri, db_ssl="disable", worker_enabled=False))

    async def scenario():
        await db.connect()
        info = await db.describe()
        await db.close()
        return info

    info = asyncio.run(scenario())
    assert info["state"] == "up"
    assert info["schema"] == "ok", info
    assert "queue" in info
    assert "password" not in str(info)


def test_first_boot_installs_the_schema_into_an_empty_database(conn):
    """The exact path a Render deploy takes when DATABASE_URL points at a brand
    new Supabase project — on a database that has nothing in it."""
    import asyncio

    from app.config import Settings
    from app.db import Database

    dbname = "fresh_boot_check"
    with conn.cursor() as cur:
        # FORCE: the app's own pool may still be attached from a previous run,
        # and Postgres refuses to drop a database with an open session.
        cur.execute(f"drop database if exists {dbname} with (force)")
        cur.execute(f"create database {dbname}")
    # Swap only the database in the path: pg_uri carries the socket directory
    # in its query string, so naive string surgery would drop it.
    from urllib.parse import urlsplit, urlunsplit

    fresh_uri = urlunsplit(urlsplit(pg_uri)._replace(path="/" + dbname))

    settings = Settings(_env_file=None, database_url=fresh_uri, db_ssl="disable", worker_enabled=False)
    db = Database(settings)

    async def scenario():
        assert await db.connect()
        assert await db.schema_missing() is True, "empty database should look uninstalled"
        assert (await db.schema_ready())[0] is False
        assert await db.migrate() == "applied"
        assert await db.schema_missing() is False
        ready, detail = await db.schema_ready()
        assert ready, detail
        counts = await db.fetchrow(
            """
            select (select count(*) from information_schema.tables where table_schema='app') as rel,
                   (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
                     where n.nspname='app') as fn,
                   (select count(*) from app.config) as cfg
            """
        )
        # 0003 adds one table (app.telegram_session, which is what makes the
        # control bot's /login possible) and four operator-tunable settings; 0004
        # adds three more keys for the approved captions; 0005 adds two keys and no
        # relation at all — it only widens existing tables, which is what keeps every
        # file in this directory individually re-runnable.
        # The config total is derived from the migration files (see conftest), because a
        # typed number here went stale the same afternoon 0004 was written.
        expected_config = config_row_count()
        assert (counts["rel"], counts["fn"]) == (27, 14)
        assert counts["cfg"] == expected_config, (
            f"the database holds {counts['cfg']} config rows, the migrations seed {expected_config}"
        )
        # A second boot must be a no-op, never a re-run that fights live data.
        assert await db.schema_missing() is False
        # And the installer is itself replayable.
        assert await db.migrate() == "applied"
        await db.close()
        return counts

    try:
        asyncio.run(scenario())
    finally:
        with conn.cursor() as cur:
            cur.execute(f"drop database if exists {dbname} with (force)")


def test_thumbnail_screen_handler_rejects_parks_and_advances(conn, seed, episode):
    """Run the first real handler against the installed schema.

    SQL-level tests cannot catch this layer: it is the handler that decides what
    gets written, which review rows appear, and whether the ladder is allowed to
    move. Three cases in one job, because the interesting part is how they
    differ:

    * a foreign handle in the text -> rejected, review queued, **no** archive job
    * clean text but strict mode and no image analysis -> parked for review
    * same candidate with strict mode off -> passed, archive job queued
    """
    import asyncio

    from app.config import Settings
    from app.db import Database
    from app.handlers import Context, thumbnail_screen

    with conn.cursor() as cur:
        cur.execute("select id from app.source_channel where series_id = %s", (seed["series_id"],))
        channel_id = cur.fetchone()[0]

        cur.execute(
            """insert into app.source_candidate
                   (source_channel_id, message_id, media_idx, media_type, file_name, raw_caption)
               values (%s, 700101, 0, 'document', 'Bleach S01E99 720p Hindi.mkv',
                       'reposted by @some_leech_group')
               returning id""",
            (channel_id,),
        )
        foreign_candidate = cur.fetchone()[0]
        cur.execute(
            """insert into app.media_variant (episode_id, quality, quality_rank, source_candidate_id)
               values (%s, '720p', 3, %s) returning id""",
            (episode, foreign_candidate),
        )
        foreign_variant = cur.fetchone()[0]

        cur.execute(
            """insert into app.source_candidate
                   (source_channel_id, message_id, media_idx, media_type, file_name, raw_caption)
               values (%s, 700102, 0, 'document', 'Bleach S01E99 1080p Hindi.mkv', '@ycanime')
               returning id""",
            (channel_id,),
        )
        clean_candidate = cur.fetchone()[0]
        cur.execute(
            """insert into app.media_variant (episode_id, quality, quality_rank, source_candidate_id)
               values (%s, '1080p', 4, %s) returning id""",
            (episode, clean_candidate),
        )
        clean_variant = cur.fetchone()[0]

    settings = Settings(_env_file=None, database_url=pg_uri, db_ssl="disable", worker_enabled=False)
    db = Database(settings)

    async def scenario() -> dict:
        assert await db.connect(), await db.last_error
        ctx = Context(db=db, settings=settings)

        rejected = await thumbnail_screen({"candidate_id": foreign_candidate, "payload": {}}, ctx)
        parked = await thumbnail_screen({"candidate_id": clean_candidate, "payload": {}}, ctx)
        assert rejected["status"] == "watermarked" and rejected["disposition"] == "rejected"
        assert parked["status"] == "review_required" and parked["variants_parked"] == 1
        assert "no_clean_action" in parked and "owner review" in parked["no_clean_action"]

        row = await db.fetchrow(
            "select thumbnail_status, disposition, reason from app.source_candidate where id = $1",
            foreign_candidate,
        )
        assert row["thumbnail_status"] == "watermarked" and row["disposition"] == "rejected"
        assert "leech" in row["reason"]

        review = await db.fetchrow(
            "select status, detected_handles from app.thumbnail_review where candidate_id = $1",
            foreign_candidate,
        )
        assert review["status"] == "pending" and "some_leech_group" in review["detected_handles"]

        variant = await db.fetchrow(
            "select thumbnail_status, status from app.media_variant where id = $1", foreign_variant
        )
        assert variant["thumbnail_status"] == "watermarked" and variant["status"] == "review"

        # The rejected candidate must not have started the next stage.
        assert await db.fetchval("select count(*) from app.job where kind = 'archive_media'") == 0

        # Same handler, policy relaxed: caption-only evidence now counts, and the
        # ladder may move for the clean candidate only.
        await db.execute("update app.config set value = 'false' where key = 'thumbnail.strict_mode'")
        passed = await thumbnail_screen({"candidate_id": clean_candidate, "payload": {}}, ctx)
        assert passed["status"] == "clean" and passed["disposition"] == "accepted"
        assert passed["archive_jobs_queued"] == [clean_variant]

        job = await db.fetchrow(
            "select stage, episode_id, payload->>'candidate_id' as cand from app.job where dedup_key = $1",
            f"archive:{clean_variant}",
        )
        assert job["stage"] == "thumbnail_checked" and int(job["cand"]) == clean_candidate
        assert int(job["episode_id"]) == int(episode)

        # Re-screening the clean candidate again must not queue a second archive
        # job (dedup collapses it) — that is the whole point of the unique key.
        again = await thumbnail_screen({"candidate_id": clean_candidate, "payload": {}}, ctx)
        assert again["archive_jobs_queued"] == []
        assert await db.fetchval("select count(*) from app.job where kind = 'archive_media'") == 1

        await db.close()
        return {"rejected": rejected["reason"], "parked": parked["status"]}

    try:
        asyncio.run(scenario())
    finally:
        conn.execute("update app.config set value = 'true' where key = 'thumbnail.strict_mode'")
        for variant_id in (foreign_variant, clean_variant):
            conn.execute("delete from app.media_variant where id = %s", (variant_id,))
        for candidate_id in (foreign_candidate, clean_candidate):
            conn.execute("delete from app.source_candidate where id = %s", (candidate_id,))


def _ingest_channel(conn, username: str, telegram_id: int, title: str, *, link_series: bool = True) -> tuple[int, int]:
    """A source channel with its own series row, so tests do not share state."""
    with conn.cursor() as cur:
        cur.execute(
            "insert into app.series (title, normalized_title) values (%s, %s) "
            "on conflict (normalized_title) do update set title = excluded.title returning id",
            (title, f"ingest {username}"),
        )
        series_id = cur.fetchone()[0]
        cur.execute(
            "insert into app.source_channel (series_id, telegram_channel_id, username, title, priority, mode) "
            "values (%s, %s, %s, %s, 100, 'full') "
            "on conflict (telegram_channel_id) do update set title = excluded.title returning id",
            (series_id if link_series else None, telegram_id, username, title),
        )
        return cur.fetchone()[0], series_id


def test_ingest_writes_the_rows_the_ladder_reads(conn):
    """The ingest layer, against the installed schema.

    Each assertion is a promise the architecture document makes, checked where it
    can actually break: re-scanning must not double-queue, a second quality must
    widen the same episode row rather than creating a parallel episode, a repeated
    file must be recognised by fingerprint instead of filename, subbed-only must
    create nothing, and a batch archive must not fabricate one variant per
    episode.
    """
    import asyncio

    from app.config import Settings
    from app.db import Database
    from app.handlers import Context, ingest_media
    from app.ingest import record_message

    channel_id, _series_id = _ingest_channel(conn, "@yc_ingest", -100700001, "Bleach Ingest")
    settings = Settings(_env_file=None, database_url=pg_uri, db_ssl="disable", worker_enabled=False)
    db = Database(settings)

    async def scenario() -> dict:
        assert await db.connect(), await db.last_error

        first = await record_message(
            db,
            source_channel_id=channel_id,
            message_id=9001,
            media_type="document",
            file_name="Bleach Ingest S01E01 720p Hindi.mkv",
            raw_caption="t.me/ycanime",
            file_size_bytes=100,
            fingerprint="fp-720",
        )
        assert first["disposition"] == "accepted", first
        assert len(first["variants"]) == 1 and first["queued"] == "thumbnail_screen"

        # --- idempotent re-scan: same message -> no new rows, no new job
        again = await record_message(
            db,
            source_channel_id=channel_id,
            message_id=9001,
            media_type="document",
            file_name="Bleach Ingest S01E01 720p Hindi.mkv",
            raw_caption="t.me/ycanime",
            fingerprint="fp-720",
        )
        assert again["skipped"].startswith("already ingested")
        assert again["candidate_id"] == first["candidate_id"]
        jobs = await db.fetchval("select count(*) from app.job where candidate_id = $1", first["candidate_id"])
        assert int(jobs) == 1, "a re-scan must not double-queue the same file"

        episode = await db.fetchrow(
            "select id, episode_number, canonical_key, languages from app.episode where id = $1",
            first["episodes"][0],
        )
        assert int(episode["episode_number"]) == 1 and episode["languages"] == ["hindi"]

        # --- a second quality widens the same episode, it is not a new episode
        second = await record_message(
            db,
            source_channel_id=channel_id,
            message_id=9002,
            media_type="document",
            file_name="Bleach Ingest S01E01 1080p Hindi.mkv",
            fingerprint="fp-1080",
        )
        assert second["episodes"] == first["episodes"], "1080p of ep 1 is still ep 1"
        assert len(second["variants"]) == 1
        variants = await db.fetch(
            "select quality from app.media_variant where episode_id = $1 order by quality", episode["id"]
        )
        assert [v["quality"] for v in variants] == ["1080p", "720p"]

        # --- case-only differences must not look like a new quality
        same_quality = await record_message(
            db,
            source_channel_id=channel_id,
            message_id=9006,
            media_type="document",
            file_name="Bleach Ingest S01E01 1080P Hindi.mkv",
            fingerprint="fp-1080-upper",
        )
        assert same_quality["variants"] == [], "1080P is the 1080p variant that already exists"

        # --- duplicate media by fingerprint, not by filename
        dup = await record_message(
            db,
            source_channel_id=channel_id,
            message_id=9003,
            media_type="document",
            file_name="Re.Na.Med.Bleach.Ingest.S01E01.720p.Hindi.mkv",
            fingerprint="fp-720",
        )
        assert dup["disposition"] == "superseded" and dup["variants"] == []

        # --- subbed-only creates a rejected row and nothing else
        sub = await record_message(
            db,
            source_channel_id=channel_id,
            message_id=9004,
            media_type="document",
            file_name="Bleach Ingest S01E02 English Subtitle 1080p.mkv",
        )
        assert sub["disposition"] == "rejected" and "out of scope" in sub["reason"]
        assert sub.get("episodes", []) == []
        episodes_after = await db.fetchval(
            "select count(*) from app.episode where season_id = (select id from app.season where series_id = (select series_id from app.source_channel where id = $1) and season_number = 1)",
            channel_id,
        )
        assert int(episodes_after) == 1, "a rejected file must not open an episode row"

        # --- a batch records its episodes without inventing per-episode variants
        batch = await record_message(
            db,
            source_channel_id=channel_id,
            message_id=9005,
            media_type="document",
            file_name="Bleach Ingest S02 [01-03] Hindi.zip",
        )
        assert batch["file_kind"] == "batch" and batch["needs_batch_handling"] is True
        assert len(batch["episodes"]) == 3 and batch["variants"] == []
        season = await db.fetchrow(
            "select observed_first, observed_last, first_episode, last_episode, boundary_kind"
            "  from app.season where series_id = (select series_id from app.source_channel where id = $1)"
            "   and season_number = 2",
            channel_id,
        )
        # The span ingest recorded is what arrived, in the *observed* columns. The
        # declared pair stays empty: a filename saying [01-03] describes one archive,
        # not a season of three episodes.
        assert (season["observed_first"], season["observed_last"]) == (1, 3), dict(season)
        assert (season["first_episode"], season["last_episode"]) == (None, None), dict(season)
        assert season["boundary_kind"] == "declared", season

        # --- the series row is created from the file name when the channel has none
        loose_channel, loose_series = _ingest_channel(
            conn, "@loose_ingest", -100700002, "Unlinked Ingest", link_series=False
        )
        await db.execute("update app.source_channel set series_id = null where id = $1", loose_channel)
        loose = await record_message(
            db,
            source_channel_id=loose_channel,
            message_id=9100,
            media_type="document",
            file_name="Unlinked Ingest S01E07 720p Hindi.mkv",
        )
        assert loose["disposition"] == "accepted"
        created_series = await db.fetchrow(
            "select title, normalized_title from app.series where id = $1", loose["series_id"]
        )
        assert created_series["title"] == "Unlinked Ingest"

        # --- ignored channels are skipped before any parsing happens
        await db.execute("update app.source_channel set mode = 'ignore' where id = $1", loose_channel)
        ignored = await record_message(
            db, source_channel_id=loose_channel, message_id=9101, file_name="Unlinked Ingest S01E08 Hindi.mkv"
        )
        assert "ignore" in ignored["skipped"]
        await db.execute("update app.source_channel set mode = 'full' where id = $1", loose_channel)

        # --- an unconfigured channel says so instead of raising
        missing = await record_message(db, source_channel_id=99999999, message_id=1, file_name="x.mkv")
        assert "not configured" in missing["skipped"]

        # --- the job handler passes the payload through to the same function
        report = await ingest_media(
            {
                "payload": {
                    "source_channel_id": channel_id,
                    "message_id": 9200,
                    "media_type": "document",
                    "file_name": "Bleach Ingest S01E09 480p Hindi.mkv",
                }
            },
            Context(db=db, settings=settings),
        )
        assert report["disposition"] == "accepted" and len(report["variants"]) == 1

        try:
            await ingest_media({"payload": {}}, Context(db=db, settings=settings))
        except ValueError as exc:
            assert "source_channel_id" in str(exc)
        else:  # pragma: no cover - the guard must fire
            raise AssertionError("a payload without ids must fail loudly, not ingest nothing silently")

        # --- and screening a candidate that ingest queued moves the ladder on
        await db.execute("update app.config set value = 'false' where key = 'thumbnail.strict_mode'")
        from app.handlers import thumbnail_screen

        screened = await thumbnail_screen({"candidate_id": first["candidate_id"], "payload": {}}, Context(db=db, settings=settings))
        assert screened["status"] == "clean", screened
        assert screened["archive_jobs_queued"] == first["variants"]
        stages = await db.fetch(
            "select kind, stage from app.job where dedup_key = any($1) order by id",
            [f"screen:candidate:{first['candidate_id']}", f"archive:{first['variants'][0]}"],
        )
        assert [s["kind"] for s in stages] == ["thumbnail_screen", "archive_media"]
        await db.close()
        return {"episode_key": episode["canonical_key"], "stages": stages}

    try:
        info = asyncio.run(scenario())
    finally:
        conn.execute("update app.config set value = 'true' where key = 'thumbnail.strict_mode'")
        with conn.cursor() as cur:
            cur.execute("delete from app.job where dedup_key like 'screen:candidate%' or dedup_key like 'archive:%'")
            cur.execute(
                "delete from app.source_candidate where source_channel_id in "
                "(select id from app.source_channel where telegram_channel_id in (-100700001, -100700002))"
            )
            cur.execute("delete from app.processed_message where source_channel_id in (select id from app.source_channel where telegram_channel_id in (-100700001, -100700002))")
            cur.execute("delete from app.dupe_fingerprint")
            cur.execute(
                "delete from app.media_variant where episode_id in (select e.id from app.episode e "
                "join app.season s on s.id = e.season_id where s.series_id in "
                "(select id from app.series where normalized_title like 'ingest %'))"
            )
            cur.execute(
                "delete from app.episode where season_id in (select id from app.season where series_id in "
                "(select id from app.series where normalized_title like 'ingest %'))"
            )
            cur.execute("delete from app.season where series_id in (select id from app.series where normalized_title like 'ingest %')")
            cur.execute("delete from app.source_channel where telegram_channel_id in (-100700001, -100700002)")
            cur.execute("delete from app.series where normalized_title like 'ingest %'")
    assert info["episode_key"].count("|") >= 3


# ---------------------------------------------------------------------------
# 0003_control_bot.sql — the session store the control bot writes into
# ---------------------------------------------------------------------------


def _clear_sessions(conn) -> None:
    conn.execute("truncate table app.telegram_session")


def test_the_session_table_enforces_what_the_bot_cannot_assume(conn) -> None:
    """The database is the last line of defence for a value that *is* an account.

    Each constraint here mirrors a mistake the bot could make: an unauthorised
    caller writing a second live session, or a truncated string that would fail at
    connect time with an error nobody can read.
    """
    _clear_sessions(conn)
    filler = "1" + "A" * 300
    with conn.cursor() as cur:
        cur.execute(
            "insert into app.telegram_session (name, kind, session_string, active) "
            "values ('spare', 'user', %s, true)",
            (filler,),
        )
        # a second *active* user session would mean two workers posting the same
        # episode twice, so it is not merely untidy: it is refused
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "insert into app.telegram_session (name, kind, session_string, active) "
                "values ('other', 'user', %s, true)",
                (filler,),
            )
        # ...while an inactive spare may exist beside it, which is what /use swaps
        cur.execute(
            "insert into app.telegram_session (name, kind, session_string) values ('other', 'user', %s)",
            (filler,),
        )
        cur.execute("select count(*) from app.telegram_session")
        assert cur.fetchone()[0] == 2
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "insert into app.telegram_session (name, kind, session_string, active) values ('bot1', 'bot', %s, true)",
            (filler,),
        )
        cur.execute("select count(*) from app.telegram_session where kind = 'bot' and active")
        assert cur.fetchone()[0] == 1, "one live bot session must coexist with one live user session"
        # a truncated session string is worse than none: it connects, then fails
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "insert into app.telegram_session (name, kind, session_string) values ('tiny', 'user', '1AAA')"
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "insert into app.telegram_session (name, kind, session_string) values ('ghost', 'wizard', %s)",
                (filler,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "insert into app.telegram_session (name, kind, session_string) values ('Bad Name', 'user', %s)",
                (filler,),
            )
    _clear_sessions(conn)


def test_session_names_are_reused_not_multiplied(conn) -> None:
    """`/login spare` twice must update one row: two rows under one name would mean
    an ambiguous /use and an old credential the operator believes they replaced."""
    _clear_sessions(conn)
    filler = "1" + "B" * 300
    with conn.cursor() as cur:
        cur.execute(
            "insert into app.telegram_session (name, kind, session_string, note) "
            "values ('spare', 'user', %s, 'first')",
            (filler,),
        )
        cur.execute(
            "insert into app.telegram_session (name, kind, session_string, note) "
            "values ('spare', 'user', %s, 'second') "
            "on conflict (name) do update set session_string = excluded.session_string, "
            "note = coalesce(excluded.note, app.telegram_session.note), last_used_at = now()",
            (filler,),
        )
        cur.execute(
            "select count(*), max(note), bool_or(last_used_at >= created_at) from app.telegram_session"
        )
        count, note, fresh = cur.fetchone()
    assert (count, note) == (1, "second") and fresh is True
    _clear_sessions(conn)


def test_anon_and_authenticated_cannot_read_the_session_table(conn) -> None:
    """RLS with zero policies plus explicit grants: the roles the API surface uses
    must be refused, while service_role (the worker) is allowed."""
    _clear_sessions(conn)
    with conn.cursor() as cur:
        cur.execute(
            "insert into app.telegram_session (name, kind, session_string) values ('spare', 'user', %s)",
            ("1" + "C" * 300,),
        )
        for role in ("anon", "authenticated"):
            cur.execute(f"set role {role}")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("select session_string from app.telegram_session")
            cur.execute("reset role")
        cur.execute("set role service_role")
        cur.execute("select name from app.telegram_session")
        assert cur.fetchone()[0] == "spare"
        cur.execute("reset role")
    _clear_sessions(conn)


def test_the_operator_tunables_from_0003_are_seeded(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("select key, value::text from app.config where key like 'bot.%%' order by key")
        rows = dict(cur.fetchall())
    assert rows["bot.allow_login"] == "true"
    assert rows["bot.login_ttl_seconds"] == "600"
    assert rows["bot.delete_sensitive"] == "true"
    # A command list, not a boolean: which commands exist is code, which ones the
    # operator wants answered is configuration.
    assert '"login"' in rows["bot.enabled_commands"] and '"cancel"' in rows["bot.enabled_commands"]
def test_sessions_helpers_round_trip_against_real_postgres(conn) -> None:
    """store/list/activate/forget against the real table, through the real asyncpg
    wrapper — the SQL in app/sessions.py is otherwise only ever exercised by mocks.
    """
    import asyncio

    from app.config import Settings
    from app.db import Database
    from app import sessions

    _clear_sessions(conn)
    settings = Settings(_env_file=None, database_url=pg_uri, db_ssl="disable", worker_enabled=False)
    db = Database(settings)
    secret = "1" + "D" * 299

    async def scenario():
        assert await db.connect()
        try:
            described = await sessions.store(
                db, name="Spare", session_string=secret, account_id=4242, username="spare_account"
            )
            assert described["name"] == "spare", "names are normalised, not duplicated case-variants"
            assert "session_string" not in described, "the metadata view must not carry the secret"
            rows = await sessions.list_sessions(db)
            assert [row["name"] for row in rows] == ["spare"]
            assert rows[0]["length_chars"] == len(secret)
            assert await sessions.active_session_string(db) == secret
            assert sessions.masked(secret).endswith(f"chars>") and secret not in sessions.masked(secret)

            # An explicit activate=False keeps the live account live: a second
            # /login stores a credential without moving the pipeline onto it.
            await sessions.store(
                db, name="backup", session_string="1" + "E" * 250, note="second account", activate=False
            )
            assert await sessions.active_session_string(db) == secret
            by_name = {row["name"]: row["active"] for row in await sessions.list_sessions(db)}
            assert by_name == {"spare": True, "backup": False}
            # switching is a deliberate act, and it demotes the previous one
            assert await sessions.activate(db, "backup") is True
            assert await sessions.active_session_string(db) == "1" + "E" * 250
            assert await sessions.activate(db, "nobody") is False
            by_name = {row["name"]: row["active"] for row in await sessions.list_sessions(db)}
            assert by_name == {"spare": False, "backup": True}

            with pytest.raises(ValueError):
                await sessions.store(db, name="bad name", session_string=secret)
            with pytest.raises(ValueError):
                await sessions.store(db, name="spare", session_string="1AAA")

            assert await sessions.forget(db, "BACKUP") is True
            assert await sessions.forget(db, "backup") is False
            names = [row["name"] for row in await sessions.list_sessions(db)]
            assert names == ["spare"]
        finally:
            await db.close()

    asyncio.run(scenario())
    _clear_sessions(conn)


def test_control_bot_login_writes_one_usable_row(conn) -> None:
    """The whole login path against a real schema: reply text, stored row, and no
    secret anywhere in the chat log.
    """
    import asyncio

    from app.config import Settings
    from app.controlbot import ControlBot, LoginResult
    from app.db import Database
    from app.sessions import active_session_string

    _clear_sessions(conn)
    secret = "1" + "F" * 280
    settings = Settings(
        _env_file=None,
        app_name="auto-manager",
        database_url=pg_uri,
        db_ssl="disable",
        worker_enabled=False,
        telegram_owner_user_ids="7",
    )
    db = Database(settings)

    class Transport:
        async def send_code(self, phone):
            return "hash"

        async def sign_in(self, phone, code, code_hash, *, password=None):
            return LoginResult(session_string=secret, account_id=99, username="spare_account")

        async def discard(self, phone):
            return None

    class Api:
        def __init__(self):
            self.sent = []
            self.deleted = []

        async def get_updates(self, *, timeout=None):
            return []

        async def get_me(self):
            return {"id": 1, "username": "ctrl"}

        async def send(self, chat_id, text, *, reply_to=None, parse_mode=None):
            self.sent.append(text)
            return 5

        async def delete(self, chat_id, *message_ids):
            self.deleted.extend(mid for mid in message_ids if mid)
            return len(self.deleted)

        async def answer_callback(self, callback_id, text=""):
            return None

    from app.botapi import Update

    async def scenario():
        assert await db.connect()
        api = Api()
        try:
            bot = ControlBot(
                api=api, db=db, settings=settings, transport=Transport(), owner_ids=frozenset({7})
            )
            await bot.handle(Update(update_id=1, chat_id=7, from_id=7, from_username=None, text="/login spare +919876543210", message_id=11))
            replies = await bot.dispatch(
                Update(update_id=2, chat_id=7, from_id=7, from_username=None, text="482913", message_id=12)
            )
            assert replies and "stored as 'spare'" in replies[0].text
            assert all(secret not in text for text in api.sent)
            assert 12 in api.deleted, "the message that carried the code must be gone"
            assert await active_session_string(db) == secret
            with conn.cursor() as cur:
                cur.execute("select name, account_id, username, note, active from app.telegram_session")
                row = cur.fetchone()
            assert row[0] == "spare" and row[1] == 99 and row[2] == "spare_account"
            assert "control bot" in row[3] and row[4] is True
        finally:
            await db.close()

    asyncio.run(scenario())
    _clear_sessions(conn)


# ---------------------------------------------------------------------------
# 0004_approved_captions.sql — published text, so it is pinned in the database too
# ---------------------------------------------------------------------------


def _config_value(conn, key: str):
    with conn.cursor() as cur:
        cur.execute("select value from app.config where key = %s", (key,))
        row = cur.fetchone()
    if row is None:
        return None
    # psycopg3 already decodes jsonb, so a JSON string value arrives as a str and a
    # JSON array as a list. json.loads-ing it again would fail on any caption that
    # does not begin with a quote, which is most of them.
    return row[0]


def test_the_approved_captions_are_what_the_database_holds(conn) -> None:
    """The strings the operator dictated must be the strings a fresh deploy serves.

    A migration that inserts a *different* caption than ``app/captions.py`` renders
    would mean the database copy wins and nobody notices until a post looks wrong,
    so both are compared here on a real cluster.
    """
    from app.captions import APPROVED_TEMPLATES, BUTTON_ROWS, TOTAL_UNKNOWN

    for key, template in APPROVED_TEMPLATES.items():
        assert _config_value(conn, key) == template, f"{key} in the database is not the approved text"
    assert _config_value(conn, "caption.button_rows") == BUTTON_ROWS
    assert _config_value(conn, "caption.total_episodes_unknown") == TOTAL_UNKNOWN
    # the archive caption used to carry hashtags; the approved sample has none, and
    # adding them back would be a policy change, not a formatting one
    assert "#" in _config_value(conn, "templates.archive_caption"), "the Official tag is part of the sample"
    assert "#S01E01" not in _config_value(conn, "templates.archive_caption")


def test_reapplying_0004_never_overwrites_an_edited_caption(conn) -> None:
    """`ops/apply-all.sql` gets pasted more than once in a real setup.

    The guard has to cut both ways: an untouched placeholder is replaced on re-run,
    and a row the operator has since tuned is left exactly as they left it. The
    placeholder used to reset the row is copied out of 0002 rather than retyped,
    because "close enough" is precisely what this guard is testing.
    """
    import re

    from app.captions import APPROVED_TEMPLATES

    sql = (ROOT / "supabase" / "migrations/0004_approved_captions.sql").read_text(encoding="utf-8")
    seeded_sql = (ROOT / "supabase" / "migrations/0002_functions.sql").read_text(encoding="utf-8")
    key = "templates.episode_post"
    original = _config_value(conn, key)
    edited = original + "\n\nEDITED BY THE OPERATOR"
    try:
        conn.execute("update app.config set value = %s::jsonb where key = %s", (json.dumps(edited), key))
        conn.execute(sql)
        assert _config_value(conn, key) == edited, "a re-apply overwrote an edited caption"

        literal = re.search(
            r"\('" + re.escape(key) + r"',\s*\n?\s*('(?:[^']|\'\')*')", seeded_sql
        ).group(1)
        placeholder = literal[1:-1].replace("''", "'")
        conn.execute("update app.config set value = %s::jsonb where key = %s", (placeholder, key))
        assert _config_value(conn, key) != original, "the reset did not actually restore the placeholder"
        conn.execute(sql)
        assert _config_value(conn, key) == APPROVED_TEMPLATES[key], "the placeholder was not replaced"
    finally:
        conn.execute("update app.config set value = %s::jsonb where key = %s", (json.dumps(original), key))


def test_series_subtitle_column_exists_and_is_optional(conn) -> None:
    """Nothing may require the alternate title: most source channels never state one."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select is_nullable from information_schema.columns
             where table_schema = 'app' and table_name = 'series' and column_name = 'subtitle'
            """
        )
        row = cur.fetchone()
    assert row is not None, "app.series.subtitle was not added"
    assert row[0] == "YES"


# ------------------------------------------------------- season boundaries (0005)
def _ingest_settings():
    from app.config import Settings

    return Settings(_env_file=None, database_url=pg_uri, db_ssl="disable", worker_enabled=False)


def test_a_declared_season_boundary_files_into_the_new_season_and_queues_both_stickers(conn):
    """The operator's own scenario, end to end, against the real schema.

    Twelve episodes of season 1, then a caption that says S2 and restarts at 1. The
    promised behaviour is: season 2 opens, the *closing* sticker goes first, then the
    new season's sticker, then uploads continue. What must not happen is ep 1 of season 2
    landing on top of ep 1 of season 1 — that collision is what the season row and the
    canonical key exist to prevent, so it is asserted here rather than trusted.
    """
    import asyncio

    from app.db import Database
    from app.ingest import record_message

    channel_id, series_id = _ingest_channel(conn, "@yc_boundary", -100700097, "Boundary Show")
    db = Database(_ingest_settings())

    async def scenario():
        assert await db.connect(), await db.last_error
        for number in range(1, 13):
            report = await record_message(
                db,
                source_channel_id=channel_id,
                message_id=7000 + number,
                media_type="document",
                file_name=f"Boundary Show S01E{number:02d} 720p Hindi.mkv",
                raw_caption="t.me/ycanime",
                fingerprint=f"b-s1-{number}",
            )
            assert report["disposition"] == "accepted", report
        season_two = await record_message(
            db,
            source_channel_id=channel_id,
            message_id=7101,
            media_type="document",
            file_name="Boundary Show S02E01 720p Hindi.mkv",
            raw_caption="t.me/ycanime",
            fingerprint="b-s2-1",
        )
        assert season_two["disposition"] == "accepted", season_two
        assert season_two["season"]["verdict"] == "declared", season_two
        rows = await db.fetch(
            "select e.episode_number, s.season_number, e.canonical_key"
            "  from app.episode e join app.season s on s.id = e.season_id"
            " where s.series_id = $1 order by s.season_number, e.episode_number",
            series_id,
        )
        keys = [f"{r['season_number']}:{r['episode_number']}" for r in rows]
        assert keys == [f"1:{n}" for n in range(1, 13)] + ["2:1"], keys
        assert len({r["canonical_key"] for r in rows}) == len(rows), "season 2 ep 1 must not equal season 1 ep 1"
        jobs = await db.fetch(
            "select payload->>'kind' as kind, payload->>'season' as season, status"
            "  from app.job where kind = 'season_sticker' order by id"
        )
        return rows, jobs, season_two

    _rows, jobs, season_two = asyncio.run(scenario())
    assert [(job["kind"], job["season"]) for job in jobs] == [("closing", "1"), ("opening", "2")], jobs
    assert [job["status"] for job in jobs] == ["queued", "queued"], "both stickers before any new post"
    kinds = [step["kind"] for step in season_two["stickers"]]
    assert kinds == ["closing", "opening"], "the report must show the same order the jobs have"

    with conn.cursor() as cur:
        cur.execute(
            "select season_number, boundary_kind, observed_first, observed_last, "
            "       first_episode, last_episode from app.season"
            " where series_id = %s order by season_number",
            (series_id,),
        )
        rows = cur.fetchall()
    assert [(r[0], r[1]) for r in rows] == [(1, None), (2, "declared")], rows
    # Season 1's observed span must not have been inflated by season 2's episode 1:
    # the numbers went back to 1, and that is a new season, not a shorter one.
    assert rows[0][2] == 1 and rows[0][3] == 12, rows[0]
    assert rows[1][2] == 1 and rows[1][3] == 1, rows[1]
    # Neither season claims a length, because nothing was declared. The caption prints
    # TBA and the batch post stays held — that is the correct answer to "twelve episodes
    # arrived and then the source started over".
    assert rows[0][4] is None and rows[0][5] is None, rows[0]
    assert rows[1][4] is None and rows[1][5] is None, rows[1]
    with conn.cursor() as cur:
        cur.execute(
            "select season_complete from app.v_season_coverage where season_id = ("
            " select id from app.season where series_id = %s and season_number = 1)",
            (series_id,),
        )
        (complete,) = cur.fetchone()
    # Twelve of twelve filed, and the season is still not "complete": nobody said how
    # long it is. The closing sticker goes out because the *source* ended the season; the
    # batch post waits because completeness is a claim about the show, not a milestone.
    assert complete is False


def test_an_unlabelled_restart_is_held_and_files_nothing(conn):
    """Same numbering reset, no ``S2`` in the caption: hold, ask, write nothing public.

    The dangerous failure here is silent — season 2's episode 1 filed as a *duplicate of*
    season 1's episode 1 would add a variant to a published post and stretch season 1's
    span. So the assertion is that the episode count did not move at all.
    """
    import asyncio

    from app.db import Database
    from app.ingest import record_message

    channel_id, series_id = _ingest_channel(conn, "@yc_reset", -100700096, "Reset Show")
    db = Database(_ingest_settings())

    async def scenario():
        assert await db.connect(), await db.last_error
        for number in (1, 2, 3, 11, 12):
            await record_message(
                db,
                source_channel_id=channel_id,
                message_id=6000 + number,
                media_type="document",
                file_name=f"Reset Show Episode {number:02d} 720p Hindi.mkv",
                raw_caption="t.me/ycanime",
                fingerprint=f"r-{number}",
            )
        held = await record_message(
            db,
            source_channel_id=channel_id,
            message_id=6500,
            media_type="document",
            file_name="Reset Show Episode 01 720p Hindi.mkv",
            raw_caption="t.me/ycanime",
            fingerprint="r-restart",
        )
        count = await db.fetchval(
            "select count(*) from app.episode e join app.season s on s.id = e.season_id where s.series_id = $1",
            series_id,
        )
        variants = await db.fetchval(
            "select count(*) from app.media_variant v where v.episode_id in"
            " (select e.id from app.episode e join app.season s on s.id = e.season_id where s.series_id = $1)",
            series_id,
        )
        candidate = await db.fetchrow(
            "select disposition, reason from app.source_candidate where id = $1", held["candidate_id"]
        )
        stickers = await db.fetchval(
            "select count(*) from app.job where kind = 'season_sticker' and"
            " season_id in (select id from app.season where series_id = $1)",
            series_id,
        )
        return held, int(count), int(variants), candidate, int(stickers)

    held, episodes, variants, candidate, stickers = asyncio.run(scenario())
    assert held["season"]["verdict"] == "reset", held
    assert held.get("needs_review") is True and "confirmation" in held["held_reason"]
    assert episodes == 5 and variants == 5, "a held episode must create nothing at all"
    assert stickers == 0, "no farewell sticker on a season that may not have ended"
    assert candidate["disposition"] == "pending"
    assert "season boundary unconfirmed" in candidate["reason"]


def test_the_confirm_knob_off_turns_a_reset_into_an_inferred_season(conn):
    """``seasons.confirm_unlabelled_reset = false`` means "trust this channel's restarts".

    The season still opens and its sticker still goes out — that is what the operator
    asked for — but the row says *inferred*, so a season created from arithmetic can be
    told apart from one the source stated, forever and after the logs have rotated.
    """
    import asyncio

    from app.db import Database
    from app.ingest import record_message

    channel_id, series_id = _ingest_channel(conn, "@yc_trusted", -100700095, "Trusted Show")
    conn.execute("update app.config set value = 'false'::jsonb where key = 'seasons.confirm_unlabelled_reset'")
    db = Database(_ingest_settings())

    async def scenario():
        assert await db.connect(), await db.last_error
        for number in range(1, 13):
            await record_message(
                db,
                source_channel_id=channel_id,
                message_id=5000 + number,
                media_type="document",
                file_name=f"Trusted Show Episode {number:02d} 720p Hindi.mkv",
                raw_caption="t.me/ycanime",
                fingerprint=f"t-{number}",
            )
        report = await record_message(
            db,
            source_channel_id=channel_id,
            message_id=5101,
            media_type="document",
            file_name="Trusted Show Episode 01 720p Hindi.mkv",
            raw_caption="t.me/ycanime",
            fingerprint="t-restart",
        )
        jobs = await db.fetch(
            "select payload->>'kind' as kind from app.job where kind = 'season_sticker' order by id"
        )
        return report, [job["kind"] for job in jobs]

    try:
        report, kinds = asyncio.run(scenario())
        assert report["season"]["verdict"] == "declared" and report["disposition"] == "accepted", report
        assert "inferred" in report["season"]["reason"], report["season"]
        assert kinds == ["closing", "opening"], kinds
        with conn.cursor() as cur:
            cur.execute(
                "select season_number, boundary_kind, declared_by, first_episode, last_episode "
                "from app.season where series_id = %s order by season_number",
                (series_id,),
            )
            rows = cur.fetchall()
        assert [(r[0], r[1]) for r in rows] == [(1, None), (2, "inferred")], rows
        # Inferred is a provenance, not a declaration: no length is claimed either way.
        assert all(r[3] is None and r[4] is None for r in rows), rows
    finally:
        conn.execute("update app.config set value = 'true'::jsonb where key = 'seasons.confirm_unlabelled_reset'")


def test_season_completeness_needs_a_declaration_not_a_pause(conn, seed):
    """The bug 0005 kills, on the view the dashboard and the batch post both read.

    ``last_episode`` is filled from what the source delivered, and the old view computed
    ``season_complete`` from it. A season whose *observed* span happens to match its
    episode count was therefore "complete" — and a permanent "complete season" post goes
    out on the strength of the uploader stopping for the week.
    """
    with conn.cursor() as cur:
        cur.execute(
            "insert into app.series (title, normalized_title) values ('Paused Show', 'paused show') "
            "on conflict (normalized_title) do nothing"
        )
        cur.execute("select id from app.series where normalized_title = 'paused show'")
        paused_id = cur.fetchone()[0]
        # Exactly what ingest leaves behind: the observed span filled in, the declared
        # pair empty. Under 0001-0004 those two columns were the same pair, which is how
        # a pause became a completion.
        cur.execute(
            "insert into app.season (series_id, season_number, observed_first, observed_last) "
            "values (%s, 1, 1, 1) returning id",
            (paused_id,),
        )
        season_id = cur.fetchone()[0]
        cur.execute(
            "insert into app.episode (season_id, episode_number, canonical_key) values (%s, 1, %s)",
            (season_id, "paused|s01|e01|hindi"),
        )
        cur.execute(
            "select episodes, season_complete from app.v_season_coverage where season_id = %s", (season_id,)
        )
        episodes, complete = cur.fetchone()
        # One episode filed, and the *declared* span empty because ingest no longer fills
        # it: not complete. This is the whole fix in one row.
        assert episodes == 1 and complete is False, "an observed episode is not a declared season"
        episode_id = cur.execute(
            "select id from app.episode where season_id = %s", (season_id,)
        ).fetchone()[0]
        cur.execute(
            "insert into app.media_variant (episode_id, quality, quality_rank, status) values (%s, '720p', 3, 'published')",
            (episode_id,),
        )
        cur.execute("select season_complete from app.v_season_coverage where season_id = %s", (season_id,))
        assert cur.fetchone()[0] is False, "a file and an episode still do not declare a length"
        # The owner's /declare is the only thing that can flip it.
        cur.execute("update app.season set first_episode = 1, last_episode = 1 where id = %s", (season_id,))
        cur.execute("select season_complete from app.v_season_coverage where season_id = %s", (season_id,))
        assert cur.fetchone()[0] is True, "declared span, the episode, and a file: now it is complete"
        cur.execute("delete from app.media_variant where episode_id = %s", (episode_id,))
        cur.execute("delete from app.episode where season_id = %s", (season_id,))
        cur.execute("delete from app.season where id = %s", (season_id,))
        cur.execute("delete from app.series where id = %s", (paused_id,))


def test_in_place_captioning_changes_the_caption_not_the_gate_and_the_plan_is_the_proof(conn):
    """The second shape of the job, end to end, on the real schema.

    The operator's channel already holds the files and each message says ``episode 7``, and such
    a file is parked — "cannot determine whether the file carries Hindi audio" — because putting
    an unproven file in front of 30k members is the one thing this service will not do.

    In-place mode used to flip that gate. The reasoning was that captioning a file nobody re-posts
    publishes nothing, so the gate had nothing to guard; the operator corrected the reasoning, not
    just the wording: caption the file, then the store bot, then the link, then the post, and a
    channel of bare files is never a destination. So what this scenario proves is the narrower
    claim, and it is the one worth pinning in a test — the mode decides *which message a caption
    is written on* and changes no rule about the file itself.
    """
    import asyncio

    from app import inplace
    from app.controlbot import ControlBot
    from app.db import Database
    from app.ingest import record_message

    # Not `_ingest_channel`: its series row is named `ingest <handle>`, and an earlier test's
    # cleanup deletes every row matching `ingest %` — which quietly unlinks this channel's
    # series and makes the control bot's lookup answer "not configured". This test owns its
    # rows, including removing them first, so a leftover from a previous run cannot answer for
    # the one being made now.
    slug, tg = "inplace @yc_inplace", -100700097
    for statement in (
        "delete from app.destination_post where destination_id in"
        " (select id from app.destination where telegram_channel_id = %s)",
        "delete from app.destination where telegram_channel_id = %s",
        "delete from app.source_candidate where source_channel_id in"
        " (select id from app.source_channel where telegram_channel_id = %s)",
        "delete from app.processed_message where source_channel_id in"
        " (select id from app.source_channel where telegram_channel_id = %s)",
        "delete from app.source_channel where telegram_channel_id = %s",
        "delete from app.season where series_id in"
        " (select id from app.series where normalized_title = %s)",
        "delete from app.series where normalized_title = %s",
    ):
        conn.execute(statement, (tg if "channel_id = %s" in statement else slug,))
    with conn.cursor() as cur:
        cur.execute(
            "insert into app.series (title, normalized_title) values (%s, %s) returning id",
            ("Naruto Inplace", slug),
        )
        series_id = cur.fetchone()[0]
        cur.execute(
            "insert into app.source_channel"
            " (series_id, telegram_channel_id, username, title, priority, mode, we_are_admin)"
            " values (%s, %s, 'yc_inplace', 'Naruto Inplace', 100, 'full', true) returning id",
            (series_id, tg),
        )
        channel_id = cur.fetchone()[0]
    db = Database(_ingest_settings())

    class Api:
        async def send(self, chat_id, text, *, reply_to=None, parse_mode=None):
            return 1

        async def delete(self, chat_id, *message_ids):
            return 0

        async def get_updates(self, *, timeout=None):
            return []

        async def get_me(self):
            return {"id": 1, "username": "ctrl"}

        async def answer_callback(self, callback_id, text=""):
            return None

    def update(text: str, update_id: int):
        from app.botapi import Update

        return Update(
            update_id=update_id, chat_id=7, from_id=7, from_username=None, text=text, message_id=update_id
        )

    async def scenario() -> None:
        assert await db.connect(), await db.last_error
        bot = ControlBot(api=Api(), db=db, settings=_ingest_settings(), owner_ids=frozenset({7}))

        async def scan(message_id: int, episode: int, *, audio: str = "") -> dict:
            """One file message, as the pipeline sees it. ``audio`` is the file's own claim.

            The claim goes in the *file name*, deliberately. The caption has to stay a bare label
            so the overwrite rule still says "write it", and the audio has to be proven by the file
            rather than by the mode. The re-scan is the other half: the same message scanned twice
            updates one row instead of leaving two candidates side by side.
            """
            return await record_message(
                db,
                source_channel_id=channel_id,
                message_id=message_id,
                media_type="document",
                file_name=f"episode {episode}" + (f" [{audio} Audio]" if audio else "") + ".mp4",
                raw_caption=f"episode {episode}",
                video_height=720,
                fingerprint=f"inplace-{message_id}",
            )

        parked = await scan(8301, 1)
        assert parked["disposition"] == "pending", parked
        assert parked["audio_gate"] == "hindi-audio-required", parked

        # The command's own preview, before it changes anything: one message, one caption, and
        # the honest line that there is no second channel to compare against.
        replies = await bot.dispatch(update("/inplace @yc_inplace plan", 1))
        text = replies[0].text
        assert "plan only" in text and "1 caption" in text, text
        assert "no source channel to compare" in text, text
        assert parked["disposition"] == "pending", "plan must not have written anything"

        # Now the mode is recorded. Nothing about the file changed and nothing about the rules for
        # it changed either; the mode is about where the caption goes.
        replies = await bot.dispatch(update("/inplace @yc_inplace", 2))
        assert "in-place captioning is ON" in replies[0].text, replies[0].text
        role = conn.execute(
            "select publish_role from app.source_channel where id = %s", (channel_id,)
        ).fetchone()[0]
        assert role == "source_and_destination", role

        # The gate still stands, which is the point of this block: an in-place channel is not a
        # channel where the rules were lifted. Parked, same reason, nothing filed.
        blocked = await scan(8302, 2)
        assert blocked["disposition"] == "pending", blocked
        assert blocked["audio_gate"] == "hindi-audio-required", blocked
        assert "cannot determine whether the file carries Hindi audio" in blocked["reason"], blocked
        assert blocked["episodes"] == [] and blocked["variants"] == [], blocked
        assert "captioned_without_audio_claim" not in blocked["flags"], blocked

        # What clears the file is the file saying something, not the mode. Same message, second
        # scan, name now carrying the claim: accepted, one episode row, one variant — and the row
        # updated in place instead of a second candidate appearing next to it.
        filed = await scan(8302, 2, audio="Hindi")
        assert filed["disposition"] == "accepted", filed
        assert filed["candidate_id"] == blocked["candidate_id"], filed
        assert filed["audio_gate"] == "hindi-audio-required", filed
        # Where the claim came from is recorded as the file's own text, never as the mode.
        assert filed["audio_source"] in ("file_name", "caption"), filed
        # The kind it recorded is the file's own word ("dual_audio" for a name saying
        # `[Hindi Audio]`), which is the point: the claim travelled in through the text.
        assert "audio detected" in filed["reason"], filed
        # The series still comes from the channel's own title (one signal), which is the honest
        # reading: nothing here pretends the file named a show.
        assert filed["series_source"] == "channel_name", filed
        assert len(filed["episodes"]) == 1 and len(filed["variants"]) == 1, filed

        # The plan the publisher would act on, read straight from the rows.
        rows = await bot._inplace_rows({"id": channel_id}, None)
        assert len(rows) == 2, rows
        decisions = inplace.plan(rows)
        assert [decision.action for decision in decisions] == [inplace.Action.CAPTION] * 2, decisions
        caption = decisions[0].caption
        caption_by_episode = {decision.episode: decision.caption for decision in decisions}
        assert "\u274d 𝗘𝗽𝗶𝘀𝗼𝗱𝗲: 01" in caption, caption  # padded, like the approved box
        assert "〄 𝗔𝘂𝗱𝗶𝗼: Unknown" in caption and "◎ 𝗧𝗼𝘁𝗮𝗹 𝗘𝗽𝗶𝘀𝗼𝗱𝗲𝘀: TBA" in caption, caption
        assert "\u2750" not in caption and "http" not in caption, caption  # no buttons, no link
        assert decisions[0].previous_caption == "episode 1", decisions[0]

        # Writing it is a separate act; the row that records it makes a re-plan a no-op and
        # keeps the burned text, which is the only copy Telegram does not have.
        destination_id = conn.execute(
            "insert into app.destination (series_id, telegram_channel_id, title, publish_mode)"
            " values (%s, %s, %s, 'in_place_caption') returning id",
            (series_id, -100700097, "Naruto Inplace"),
        ).fetchone()[0]
        episode_id = conn.execute(
            "select e.id from app.episode e join app.season s on s.id = e.season_id"
            " where s.series_id = %s and s.season_number = 1 and e.episode_number = 2",
            (series_id,),
        ).fetchone()[0]

        # Writing the caption is the publisher's act, not this command's. What the row buys is
        # that a second pass is a no-op — the message carries the box, and the text it replaced
        # is kept here because Telegram keeps no copy of it.
        conn.execute(
            "insert into app.destination_post"
            " (destination_id, kind, episode_id, message_id, body, caption_previous, edits)"
            " values (%s, 'episode', %s, 8302, %s, %s, 1)",
            (destination_id, episode_id, caption_by_episode[2], "episode 2"),
        )
        conn.execute(
            "update app.source_candidate set raw_caption = %s"
            " where source_channel_id = %s and message_id = 8302",
            (caption_by_episode[2], channel_id),
        )
        rows = await bot._inplace_rows(
            {"id": channel_id}, {"id": destination_id, "series_id": series_id}
        )
        by_message = {decision.message_id: decision for decision in inplace.plan(rows)}
        assert by_message[8302].action == inplace.Action.SKIP, by_message[8302]
        assert by_message[8301].action == inplace.Action.CAPTION, by_message[8301]
        stored = conn.execute(
            "select caption_previous, body, edits from app.destination_post where message_id = 8302"
        ).fetchone()
        assert stored[0] == "episode 2" and stored[1] == caption_by_episode[2], stored
        assert stored[2] == 1, stored

        # Switching back is a write too, and it must not un-caption anything: the edited post
        # stays edited, because undoing a caption is a human decision about their own channel.
        replies = await bot.dispatch(update("/inplace @yc_inplace off", 3))
        assert "link route again" in replies[0].text, replies[0].text
        mode, role = conn.execute(
            "select d.publish_mode, s.publish_role from app.destination d, app.source_channel s"
            " where d.id = %s and s.id = %s",
            (destination_id, channel_id),
        ).fetchone()
        assert mode == "link_post" and role == "source", (mode, role)
        third = await scan(8303, 3)
        assert third["disposition"] == "pending" and third["audio_gate"] == "hindi-audio-required", third

        # And the same column, read as data from a real row, decides the other half: lose the
        # rights and the answer stops being "caption it here" and becomes "this is a source, and
        # the destination gets built". This is the assertion that keeps channel creation from
        # being skipped by the existence of an in-place mode.
        conn.execute("update app.source_channel set we_are_admin = false where id = %s", (channel_id,))
        # No destination row either — that is the case the sentence is about. With a destination
        # already present the same rights answer is "posts go there", and this reply would be wrong.
        conn.execute("delete from app.destination_post where destination_id = %s", (destination_id,))
        conn.execute("delete from app.destination where id = %s", (destination_id,))
        replies = await bot.dispatch(update("/inplace @yc_inplace", 4))
        refusal = replies[0].text
        assert "I did not switch this channel to in-place mode" in refusal, refusal
        assert "create_channel" in refusal and "ordinary member" in refusal, refusal
        assert conn.execute(
            "select publish_role from app.source_channel where id = %s", (channel_id,)
        ).fetchone()[0] == "source", "a refusal must not have flipped the role back on"
        # A count over the whole table would answer for other tests' channels too. This series is
        # the one this scenario owns, so this is the row a skipped-creation bug would leave missing.
        assert conn.execute(
            "select count(*) from app.destination d join app.series s on s.id = d.series_id"
            " where s.normalized_title = %s",
            (slug,),
        ).fetchone()[0] == 0, "a refusal creates nothing either, in the database or on Telegram"
        conn.execute("update app.source_channel set we_are_admin = true where id = %s", (channel_id,))

        conn.execute("delete from app.destination_post where destination_id = %s", (destination_id,))
        conn.execute("delete from app.destination where id = %s", (destination_id,))
        conn.execute("delete from app.source_candidate where source_channel_id = %s", (channel_id,))
        conn.execute("delete from app.processed_message where source_channel_id = %s", (channel_id,))

    asyncio.run(scenario())
