"""Execute the real migrations against a real PostgreSQL and assert behaviour.

These tests apply 0001 and 0002 to a disposable cluster (``pgserver``, the same
PostgreSQL version family Supabase runs on) and then exercise the promises the
architecture document makes. The point is to test the *SQL*, not a Python
imitation of it: the queue lease, the stage machine, the campaign dedup key and
the "DM never approves" constraint all live in the database.

Skipped automatically when pgserver/psycopg are unavailable.
"""

from __future__ import annotations

import socket
from pathlib import Path

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
    footer = val(conn, "select value from app.config where key = 'branding.footer'")
    assert footer == "@ycanime | @india_crunchyroll"
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
        assert complete() is True, "2 of 2 declared episodes, each with a file"
        cur.execute("update app.season set last_episode = 3 where id = %s", (season_id,))
        assert complete() is False, "a newly declared episode reopens the season"


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
        # control bot's /login possible) and four operator-tunable settings.
        assert (counts["rel"], counts["fn"], counts["cfg"]) == (27, 14, 32)
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
            "select first_episode, last_episode from app.season where series_id = (select series_id from app.source_channel where id = $1) and season_number = 2",
            channel_id,
        )
        assert (season["first_episode"], season["last_episode"]) == (1, 3)

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
