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
