"""The peer spelling a write is allowed to use, and what a refused write does to a job.

Two rules live at this boundary and both were learned from a failure that was invisible from the
outside, which is why they have their own file:

* Telethon resolves the **integer** ``-1001234567890`` and looks the **string** ``"-1001234567890"`` up
  as a username. This project stores channel ids in jsonb config rows and reads them from chat command
  arguments, where they are text — so the cast belongs here, and a writer must not have to remember it.
* A peer this session cannot see has to stop the job. Before, a row that aliased its channel id under
  another name produced an empty peer, the fake client ignored it, and the test passed while the real
  send would have gone nowhere.

The third test is the counterpart: a refusal raises, so ``/status`` shows a blocked job instead of a
green one.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.sender import RetryLater, Sender, WritePolicy, resolve_peer
from app.writers import FeatureNotImplemented, WriteBlocked, Writers


class Client:
    """A stand-in for one end of the write ladder, recording what it was addressed by."""

    def __init__(self, *, entity_error: Exception | None = None, message_id: int = 4100) -> None:
        self.calls: list[tuple] = []
        self.entity_error = entity_error
        self._message_id = message_id

    async def get_entity(self, peer):
        self.calls.append(("entity", peer))
        if self.entity_error is not None:
            raise self.entity_error
        return peer

    async def get_input_entity(self, peer):
        return peer

    async def send_message(self, peer, message, **kwargs):
        self.calls.append(("send", peer, message, kwargs))
        return SimpleNamespace(id=self._message_id, text=message, out=True, date=None)

    async def edit_message(self, peer, message_id, message, **kwargs):
        self.calls.append(("edit", peer, message_id, message, kwargs))
        return SimpleNamespace(id=message_id, text=message, out=False, date=None)

    async def forward_messages(self, peer, ids, **kwargs):
        self.calls.append(("forward", peer, ids, kwargs))
        return [SimpleNamespace(id=self._message_id + i, text=None, out=False, date=None) for i, _ in enumerate(ids)]

    async def iter_messages(self, peer, limit=None):
        for _ in ():  # the read-back tests use a reply list; this one only needs the resolution order
            yield None


def _sender(client, *, live: bool = True, peers: tuple = ()):
    """One Sender, with the policy a deployment would hand it: the peers named, and live or not."""
    settings = SimpleNamespace(outbound_enabled=live)
    return Sender(client, policy=WritePolicy.from_settings(settings, peers=list(peers)))


def test_resolve_peer_casts_only_a_number_and_leaves_a_handle_alone() -> None:
    assert resolve_peer("-1001234567890") == -1001234567890
    assert resolve_peer(" -1001234567890 ") == -1001234567890
    assert resolve_peer("1234567890") == 1234567890
    assert resolve_peer(-1001234567890) == -1001234567890
    # A username is not a number and must stay a string, or Telethon is asked to find an entity called 0
    assert resolve_peer("@Link_providerobot") == "@Link_providerobot"
    assert resolve_peer("Link_providerobot") == "Link_providerobot"
    # A row with no id in it is the common accident, and it must read as "nothing named", not as a
    # username called "None" — the sender refuses an empty peer, which is what the next test checks.
    assert resolve_peer(None) == ""
    assert resolve_peer("") == ""


def test_a_send_names_the_peer_in_the_form_telethon_resolves() -> None:
    client = Client()
    writer = _sender(client, peers=("-1001234567890",))
    result = asyncio.run(writer.send_text("-1001234567890", "S01 E01 is up"))

    assert result.ok, result.detail
    # the lookup and the send both get the int, not the text "-1001234567890"
    assert ("entity", resolve_peer("-1001234567890")) in client.calls, client.calls
    assert client.calls[-1][1] == -1001234567890, client.calls


def test_a_peer_the_session_cannot_see_blocks_the_write_with_a_sentence() -> None:
    client = Client(entity_error=ValueError("Cannot find any entity corresponding to -100999"))
    writer = _sender(client, peers=("-100999",))
    result = asyncio.run(writer.send_text("-100999", "hello"))

    assert not result.ok
    assert "this session cannot see" in result.detail, result.detail
    assert not [c for c in client.calls if c[0] == "send"], "an unresolvable peer still reached Telegram"


def test_shadow_mode_never_looks_the_peer_up() -> None:
    """A plan is made of text, not of network calls — that is the whole promise of ``APP_MODE=shadow``."""
    client = Client()
    writer = _sender(client, live=False, peers=("-100999",))
    result = asyncio.run(writer.send_text("-100999", "planned"))

    assert result.ok and result.action == "planned", result
    assert client.calls == [], client.calls


def test_a_refused_write_parks_the_job_instead_of_passing_it() -> None:
    """``_stop`` must not return a sentence the worker would read as success."""
    result = SimpleNamespace(retry_after=0, action="failed", detail="Telegram refused: CHAT_WRITE_FORBIDDEN")
    with pytest.raises(WriteBlocked) as exc:
        Writers._stop(result, what="post the episode link")
    assert "post the episode link" in str(exc.value)
    assert isinstance(exc.value, FeatureNotImplemented), "the worker parks this shape, not that one"


def test_a_flood_still_retries_rather_than_parking_forever() -> None:
    """A flood knows when it ends, so it must not become a blocked job an operator has to clear."""
    result = SimpleNamespace(retry_after=90, action="blocked", detail="wait 90s")
    with pytest.raises(RetryLater) as exc:
        Writers._stop(result, what="post the episode link")
    assert getattr(exc.value, "retry_after", 90) == 90
