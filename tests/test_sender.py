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


class ReadClient:
    """A Telegram that answers the three calls a waiting-list read makes, recording every request.

    The recorded requests are the point: the defect this file now guards was not a wrong row but a call
    that was never made, so no assertion about the returned rows could have caught it. `pages` is the list
    of importer pages the invite-link query hands back, one per call, so a paging test can make the second
    page differ from the first the way the real server does.
    """

    def __init__(
        self,
        *,
        exported: object | None = None,
        invites: tuple = (),
        pages: tuple = ((),),
        count: int | None = None,
        refuses: Exception | None = None,
        users: tuple = (),
    ) -> None:
        self.exported = exported
        self.invites = list(invites)
        self.pages = list(pages)
        self.count = count
        self.refuses = refuses
        self.users = users
        self.requests: list = []

    async def get_entity(self, peer):
        # The read resolves the id through the session (see `Sender._target`), and a fake that could not is
        # indistinguishable from a session that has never met the channel — which is its own honest answer,
        # so this one has met it.
        return peer

    async def get_input_entity(self, peer):
        return peer

    async def __call__(self, request):
        self.requests.append(request)
        if self.refuses is not None:
            raise self.refuses
        name = type(request).__name__
        if name == "GetFullChannelRequest":
            return SimpleNamespace(full_chat=SimpleNamespace(exported_invite=self.exported))
        if name == "GetExportedChatInvitesRequest":
            return SimpleNamespace(
                invites=[SimpleNamespace(invite=one) for one in self.invites],
            )
        if name == "GetChatInviteImportersRequest":
            page = self.pages[min(sum(1 for r in self.requests if type(r).__name__ == name) - 1, len(self.pages) - 1)]
            return SimpleNamespace(count=self.count, importers=list(page), users=list(self.users))
        raise AssertionError(f"the read sent a call this fake does not know: {name}")


def _read(client, *, peer="-1002575861262", **kwargs):
    from app.sender import Sender, WritePolicy

    # The same policy the control bot plans a campaign with: reads are not writes, so a plan-mode sender
    # still asks Telegram, and the raw id is passed through without an entity lookup.
    writer = Sender(client, policy=WritePolicy(mode="plan", max_writes=0))
    return asyncio.run(writer.pending_requests(peer, **kwargs))


def test_the_waiting_list_is_read_from_the_link_the_requests_sit_on() -> None:
    """**The regression.** `getChatInviteImporters` answers "who is waiting on *this link*", and a private
    channel's requests all sit on its primary `+AbCdEf` link. A query that names no link returns an empty
    page — a success, not an error — which is how an operator was told "0 request(s) are pending" while
    Telegram's own approval panel showed twenty. The fix is visible only in the outgoing request, so that
    is what this asserts.
    """
    page = tuple(
        {"user_id": 11 + n, "about": "request", "date": None, "approved_by": None} for n in range(3)
    )
    client = ReadClient(
        exported=SimpleNamespace(link="https://t.me/+AbCdEf", requested=20, revoked=False),
        pages=(page,),
        count=20,
    )

    result, rows = _read(client, limit=100)

    asked = [r for r in client.requests if type(r).__name__ == "GetChatInviteImportersRequest"]
    assert asked, client.requests
    assert [r.link for r in asked] == ["AbCdEf", "+AbCdEf", None], (
        "the hash is tried both ways and the linkless query last; omitting either half is the bug",
        [r.link for r in asked],
    )
    assert [r.requested for r in asked] == [True, True, True], asked
    assert len(rows) == 3, rows
    assert result.total == 20, "the operator is shown the queue's size, not the page's"
    assert "3 request(s) read of 20 waiting" in result.detail, result.detail


def test_a_public_channel_with_no_invite_link_is_still_asked_about() -> None:
    """The linkless query is the only shape that answers for a public `@name` channel, where requests do not
    belong to an exported link at all. Dropping it as "the broken call" would have emptied the other half of
    the feature, so it stays — last, and only when there is no link to ask about."""
    page = ({"user_id": 5, "about": None, "date": None, "approved_by": None},)
    client = ReadClient(exported=None, invites=(), pages=(page,), count=1)

    result, rows = _read(client)

    asked = [r for r in client.requests if type(r).__name__ == "GetChatInviteImportersRequest"]
    assert len(asked) == 1, asked
    assert getattr(asked[0], "link", "unset") is None, asked[0]
    assert len(rows) == 1 and result.total == 1, (rows, result)
    assert result.ok


def test_the_read_walks_past_people_the_caller_already_wrote_to() -> None:
    """A campaign that contacted the first hundred must not read them again, conclude nobody is new, and be
    called finished. The walk is `offset_date` plus `offset_user`, and `offset_user` has to be an
    `InputUser` — the bare id is refused by the server, which is the trap the paging used to fall into.
    """
    first = tuple(
        {"user_id": 1000 + n, "about": None, "date": None, "approved_by": None} for n in range(100)
    )
    second = ({"user_id": 2000, "about": None, "date": None, "approved_by": None},)
    client = ReadClient(
        exported=SimpleNamespace(link="https://t.me/+AbCdEf", requested=101, revoked=False),
        pages=(first, second),
        count=101,
        # The response's `users` array is the only place access hashes come from, and the next page cannot
        # be asked for without one: `tests/test_sender.py` has to carry it or the paging never runs.
        users=tuple(SimpleNamespace(id=one["user_id"], access_hash=77) for one in first + second),
    )

    result, rows = _read(client, limit=1, skip=tuple(one["user_id"] for one in first))

    asked = [
        r
        for r in client.requests
        if type(r).__name__ == "GetChatInviteImportersRequest" and getattr(r, "link", None)
    ]
    assert len(asked) >= 2, "the short first page must not have been taken as the end of the queue"
    follow_up = asked[1]
    assert follow_up.offset_user is not None and follow_up.offset_date is not None, follow_up
    assert type(follow_up.offset_user).__name__ == "InputUser", follow_up.offset_user
    assert follow_up.offset_user.user_id == 1099, follow_up.offset_user
    assert [row["user_id"] for row in rows] == [2000], rows
    assert result.total == 101, result


def test_a_shadow_deployment_still_reads_and_still_never_sends() -> None:
    """The second half of the same defect, and the half that no amount of link-scoping would have fixed.

    `Sender._gate` short-circuits every call in plan mode, so the campaign plan built its sentence from a
    request that was never made: zero rows, "0 request(s) are pending", `ok=True`. Shadow mode means nothing
    leaves the account, not nothing is asked, so a read has to run — and this test keeps both halves in one
    place, because the day someone "restores" the shadowing of reads is the day the queue goes back to being
    reported empty.
    """
    page = ({"user_id": 7, "about": None, "date": None, "approved_by": None},)
    client = ReadClient(
        exported=SimpleNamespace(link="https://t.me/+AbCdEf", requested=1, revoked=False),
        pages=(page,),
        count=1,
    )
    from app.sender import Sender, WritePolicy

    writer = Sender(client, policy=WritePolicy(mode="plan", max_writes=0))

    result, rows = asyncio.run(writer.pending_requests("-1002575861262"))
    assert result.ok and len(rows) == 1, (result, rows)
    asked = len(client.requests)
    assert asked >= 2, client.requests

    planned = asyncio.run(writer.send_text("-1002575861262", "hello"))
    assert planned.action == "planned", planned
    assert len(client.requests) == asked, "a shadow plan reached Telegram with a write after all"


def test_a_waiting_person_comes_back_addressable() -> None:
    """An id is not a peer: Telegram's rows name strangers by id, and the hash that addresses them rides in the response's `users`.

    A campaign that DMs by bare id works only while the session happens to remember the person, and the
    people in a pending-request queue are exactly the ones it does not. Every contact then fails with
    "Cannot find any entity" — twenty failures that look like privacy walls. So the read hands the hash on,
    and a writer that has one uses it.
    """
    page = ({"user_id": 41, "about": None, "date": None, "approved_by": None},)
    client = ReadClient(
        exported=SimpleNamespace(link="https://t.me/+AbCdEf", requested=1, revoked=False),
        pages=(page,),
        count=1,
        users=(SimpleNamespace(id=41, access_hash=911),),
    )

    _result, rows = _read(client)

    assert len(rows) == 1, rows
    peer = rows[0]["input_user"]
    assert type(peer).__name__ == "InputUser", peer
    assert (peer.user_id, peer.access_hash) == (41, 911), peer


def test_an_input_entity_is_not_asked_of_the_session_again() -> None:
    """`_target` resolves ids because a string id is a *username* to Telethon; an input entity needs nothing.

    The lookup is the fragile half, and for a peer that arrived with its own access hash it is also
    pointless: re-asking is where a campaign loses the person it was told to write to.
    """
    from telethon import types

    from app.sender import Sender, WritePolicy

    client = Client()
    writer = Sender(client, policy=WritePolicy(mode="live", allow_peers=(), max_writes=4))

    result = asyncio.run(writer.send_text(types.InputUser(user_id=41, access_hash=911), "hello"))

    assert result.ok, result
    assert not [c for c in client.calls if c[0] == "entity"], client.calls
    assert client.calls, "the message still has to be sent"


def test_a_channel_the_account_cannot_read_says_so_instead_of_being_empty() -> None:
    """Every failure the read can meet leaves the result not-ok with a reason; none of them may come back as
    `ok=True, rows=[]`, because that is the sentence the operator acts on."""
    for error in (RuntimeError("CHAT_ADMIN_REQUIRED"), ValueError("Could not find the input entity for PeerChannel")):
        client = ReadClient(exported=None, invites=(), pages=((),), refuses=error)
        result, rows = _read(client)
        assert not result.ok, (error, result)
        assert result.total == 0 and rows == [], (error, result, rows)
        assert "could not be read" in result.detail, result.detail


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
