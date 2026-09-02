"""``store()`` may only answer once the row can be read back.

This file exists because of one sentence: the first live login ended with the bot saying
"connected as @…, stored as 'spare'", while ``/sessions`` answered "no stored sessions". Both were
telling the truth about their own half — the insert was attempted, the read found nothing — and the
gap between them was invisible. "The statement ran" and "the account is usable" are different claims,
and only the second one deserves a reply in a chat.

So the rule tested here: :func:`app.sessions.store` reads the row it just wrote and raises if it is
not there, if it is not the size it should be, or if the write itself was refused.
"""

from __future__ import annotations

import asyncio

import pytest

from app.sessions import store

#: The shape a StringSession has, kept as three pieces so the secret scan in ``tests/test_secret_hygiene.py``
#: does not read this line as a session string that was committed on purpose.
SESSION = "1AAAAABcd" + "E" * 150 + "fg"


class FakeDb:
    """A connection that answers however the test needs it to.

    ``rows`` is what a read returns *after* the write: an empty list is the case that matters, because
    that is a table the app can write to and cannot see — a role, a policy or an unapplied migration.
    """

    def __init__(self, *, rows: list[dict] | None = None, write_error: Exception | None = None) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self.rows = [] if rows is None else rows
        self.write_error = write_error

    async def execute(self, sql: str, *args):
        self.statements.append((sql, args))
        if self.write_error is not None:
            raise self.write_error
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args):
        return [dict(row) for row in self.rows]


def _store(db, **kwargs):
    return asyncio.run(store(db, name=kwargs.pop("name", "spare"), session_string=SESSION, **kwargs))


def test_a_write_nobody_can_read_back_is_not_reported_as_stored() -> None:
    db = FakeDb(rows=[])

    with pytest.raises(LookupError) as exc:
        _store(db)

    assert "reads back empty" in str(exc.value)
    assert "row-level security" in str(exc.value), "the reply has to name what the operator can go and change"
    assert db.statements, "the insert really was attempted; it is the proof that is missing"


def test_a_row_of_the_wrong_size_is_not_trusted() -> None:
    """A session that came back short is a truncated write, and a truncated session fails later in a
      confusing way — so it is refused now, while there is still something to say about it."""
    db = FakeDb(rows=[{"name": "spare", "length_chars": len(SESSION) - 40, "active": True}])

    with pytest.raises(LookupError, match=r"came back as"):
        _store(db)


def test_a_store_that_reads_back_returns_the_row_it_showed() -> None:
    row = {"name": "spare", "kind": "user", "length_chars": len(SESSION), "active": True}
    db = FakeDb(rows=[row])

    assert _store(db) == row
    # demote the old active session, then write: two statements, in that order
    assert len(db.statements) == 2 and "update app.telegram_session set active = false" in db.statements[0][0]

    only_one = FakeDb(rows=[{**row, "active": False}])
    _store(only_one, activate=False)
    assert len(only_one.statements) == 1, "activating is a choice, and not activating must not touch the others"


def test_a_refused_write_is_passed_up_with_its_own_words() -> None:
    db = FakeDb(write_error=RuntimeError("execute failed: permission denied for table telegram_session"))

    with pytest.raises(RuntimeError, match="permission denied"):
        _store(db)


def test_an_unusable_name_or_string_never_reaches_the_database() -> None:
    """Checked first, because asking for a Telegram code and then failing to store it costs the account
    a rate-limit hit for nothing."""
    db = FakeDb()

    with pytest.raises(ValueError, match="session name"):
        _store(db, name="my spare account!")
    with pytest.raises(ValueError, match="too short"):
        asyncio.run(store(db, name="spare", session_string="1AAA"))

    assert db.statements == []
