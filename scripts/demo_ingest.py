#!/usr/bin/env python3
"""Put real work through the queue against a local cluster, for the preview.

    python scripts/demo_ingest.py

It creates one demo series with one source channel, feeds three source messages
through the same :func:`app.ingest.record_message` the live scanner will use, and
prints what the service decided. Nothing is sent to Telegram: this exists to show
the queue, the checkpoint ladder and the publish gate doing their jobs on data you
can read in one screen.

The three messages are chosen because each exercises a different rule:

* a Hindi 720p release          -> accepted, variant created, screening queued
* the same episode in 1080p     -> a second quality on the *same* episode row
* an English-subtitles-only file -> rejected as out of scope, row kept for review
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.ingest import record_message  # noqa: E402

DEMO_SERIES = "Demo Series (local preview)"
CHANNEL_ID = -10099900001


async def main() -> int:
    uri = os.environ.get("DATABASE_URL")
    if not uri:
        print("set DATABASE_URL (see scripts/devdb.py --print-url)", file=sys.stderr)
        return 2

    settings = Settings(_env_file=None, database_url=uri, db_ssl=os.environ.get("DB_SSL", "disable"))
    db = Database(settings)
    if not await db.connect():
        print(f"cannot connect: {db.last_error}", file=sys.stderr)
        return 1

    try:
        series_id = await db.fetchval(
            "insert into app.series (title, normalized_title) values ($1, $2) "
            "on conflict (normalized_title) do update set title = excluded.title returning id",
            DEMO_SERIES,
            "demo local preview",
        )
        channel = await db.fetchrow(
            "insert into app.source_channel (series_id, telegram_channel_id, username, title, priority, mode) "
            "values ($1, $2, '@demo_ycanime', $3, 100, 'full') "
            "on conflict (telegram_channel_id) do update set series_id = excluded.series_id returning id",
            series_id,
            CHANNEL_ID,
            DEMO_SERIES,
        )
        channel_id = channel["id"]

        messages = [
            {
                "message_id": 900_001,
                "media_type": "document",
                "file_name": "Demo.Series.S01E01.720p.Hindi.Dual.Audio.mkv",
                "raw_caption": "🎬 Episode 1\n@ycanime",
                "fingerprint": "demo-fp-720",
            },
            {
                "message_id": 900_002,
                "media_type": "document",
                "file_name": "Demo.Series.S01E01.1080p.Hindi.Dual.Audio.mkv",
                "raw_caption": "same episode, better quality, arrives later",
                "fingerprint": "demo-fp-1080",
            },
            {
                "message_id": 900_003,
                "media_type": "document",
                "file_name": "Demo.Series.S01E02.1080p.English.Subtitles.mkv",
                "raw_caption": "subbed only",
                "fingerprint": "demo-fp-subs",
            },
        ]

        print(f"series id={series_id}  source channel id={channel_id}\n")
        for payload in messages:
            report = await record_message(db, source_channel_id=channel_id, **payload)
            print(f"{payload['file_name']}")
            print(
                f"   -> {report.get('disposition') or 'skipped'}"
                f" | episodes={report.get('episodes', [])}"
                f" variants={report.get('variants', [])}"
                f" queued={report.get('queued')}"
                f"\n      {report.get('reason') or report.get('skipped')}"
            )

        print("\nmanifest (the view's own order is the display order, not arrival):")
        for row in await db.fetch(
            "select episode_number, episode_status, variant_count, qualities "
            "from app.v_episode_manifest where series_title = $1 order by episode_number",
            DEMO_SERIES,
        ):
            variants = row["qualities"] or []
            rendered = ", ".join(
                f"{v['quality']}/{v['thumbnail_status']}/{v['status']}" for v in variants
            ) or "(no variants yet)"
            print(f"   ep {row['episode_number']:>3}  {row['episode_status']:<11} {rendered}")

        print("\nqueue:")
        for row in await db.fetch(
            "select id, kind, stage, status, left(coalesce(last_error, ''), 60) as error from app.job order by id"
        ):
            print(f"   job {row['id']:>3} {row['kind']:<18} {row['stage']:<18} {row['status']:<10} {row['error']}")

        print("\nready state:")
        ready, detail = await db.schema_ready()
        print(f"   schema {'ok' if ready else 'problem'}: {detail}")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
