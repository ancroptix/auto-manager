"""Logging in with the versions this project actually installs.

The first live ``/login`` the operator ran (2026-08-29) failed with ``TypeError`` before a request
was even sent: ``app/mtproto_login.py`` passed ``force=False`` to ``send_code_request``, an argument
Telethon removed, and the client it built was never connected — which would have been the *next*
failure, ``ConnectionError: Cannot send requests while disconnected``.

Two kinds of test follow from that. One drives the login flow against a stand-in Telethon and
asserts the calls it makes. The other reads this project's own source and binds every Telethon
keyword argument to the signature of the installed Telethon, so a future upgrade that renames or
drops an argument fails here instead of in front of a private chat.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest

from app import mtproto_login

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeSession:
    """What Telethon's ``StringSession`` owes the caller: a non-empty string to store."""

    def as_string(self) -> str:
        return "1.2.3.4-session-string"


class FakeSentCode:
    def __init__(self, phone_code_hash: str = "abcd1234") -> None:
        self.phone_code_hash = phone_code_hash


class FakeUser:
    id = 777
    username = "spare_account"


class FakeClient:
    """Enough of ``TelegramClient`` to see what the login module asks for."""

    instances: list["FakeClient"] = []
    connect_error: Exception | None = None
    code_error: Exception | None = None
    sign_in_error: Exception | None = None
    password_needed: bool = False

    def __init__(self, session, api_id, api_hash, **kwargs) -> None:  # noqa: D107
        self.build_session = session
        self.api_id = api_id
        self.api_hash = api_hash
        self.build_kwargs = kwargs
        self.calls: list[tuple] = []
        self.connected = False
        # A StringSession stand-in: the login module reads one string off the client at the end, and
        # an empty one would make a passing test mean nothing.
        self._string_session = FakeSession()
        FakeClient.instances.append(self)

    @property
    def session(self) -> FakeSession:
        """The attribute ``app/mtproto_login.py`` reads the session string off of."""
        return self._string_session

    async def connect(self):
        self.calls.append(("connect",))
        if FakeClient.connect_error is not None:
            raise FakeClient.connect_error
        self.connected = True
        return True

    async def disconnect(self):
        self.calls.append(("disconnect",))
        self.connected = False

    async def send_code_request(self, phone=None, **kwargs):
        self.calls.append(("send_code_request", phone, tuple(sorted(kwargs))))
        if FakeClient.code_error is not None:
            raise FakeClient.code_error
        return FakeSentCode()

    async def sign_in(self, phone=None, code=None, **kwargs):
        self.calls.append(("sign_in", phone, code, tuple(sorted(kwargs))))
        if FakeClient.password_needed:
            raise SessionPasswordNeededStub()
        if FakeClient.sign_in_error is not None:
            raise FakeClient.sign_in_error
        return FakeUser()

    async def get_me(self):
        self.calls.append(("get_me",))
        return FakeUser()

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def kwargs_of(self, name: str) -> tuple:
        """The keyword names of the *last* call by that name.

        "Last" matters for ``sign_in``: a two-factor account makes that call twice, and the second
        one — the password — is the one worth asserting on.
        """
        found = ()
        for call in self.calls:
            if call[0] == name:
                found = call[-1]
        return found


class SessionPasswordNeededStub(Exception):
    """Named like Telethon's error, because the module matches on the class name."""


@pytest.fixture(autouse=True)
def _clean_fake(monkeypatch):
    FakeClient.instances = []
    for attribute in ("connect_error", "code_error", "sign_in_error", "password_needed"):
        setattr(FakeClient, attribute, None if attribute != "password_needed" else False)
    monkeypatch.setattr("telethon.TelegramClient", FakeClient)
    yield


def _login(**overrides) -> mtproto_login.MTProtoLogin:
    params = {"api_id": 1234, "api_hash": "hash"}
    params.update(overrides)
    return mtproto_login.MTProtoLogin(**params)


@pytest.mark.asyncio
async def test_a_code_request_connects_first_and_sends_only_the_phone() -> None:
    """The two bugs at once: no connection, and an argument Telethon no longer accepts."""
    login = _login()
    code_hash = await login.send_code("+919876543210")

    client = FakeClient.instances[0]
    assert client.names() == ["connect", "send_code_request"], "connect must come before any request"
    assert client.connected is True
    assert client.kwargs_of("send_code_request") == (), (
        "send_code_request(phone) takes no keyword arguments in Telethon 1.44; a stale one raises "
        "TypeError before Telegram is ever contacted"
    )
    assert code_hash == "abcd1234"


@pytest.mark.asyncio
async def test_a_failed_code_request_leaves_no_connection_behind() -> None:
    """A half-finished login must not keep a live socket, in a container that will not close it."""
    FakeClient.code_error = RuntimeError("nope")
    login = _login()
    with pytest.raises(RuntimeError, match=r"login step failed \(RuntimeError\)"):
        await login.send_code("+919876543210")
    client = FakeClient.instances[0]
    assert client.names() == ["connect", "send_code_request", "disconnect"], client.calls
    assert login._clients == {}  # noqa: SLF001 - nothing to reuse, because nothing was started


@pytest.mark.asyncio
async def test_the_operator_gets_a_sentence_and_not_a_traceback() -> None:
    """The reply text is the whole product here: it says what to do about Telegram's own failures."""
    FakeClient.code_error = type("ConnectionError", (Exception,), {})("cannot reach DC")
    login = _login()
    with pytest.raises(RuntimeError) as exc:
        await login.send_code("+919876543210")
    assert "could not reach Telegram" in str(exc.value)

    class FloodWaitError(Exception):
        seconds = 340

    FakeClient.code_error = FloodWaitError("A wait of 340 seconds is required")
    with pytest.raises(RuntimeError) as exc:
        await login.send_code("+919876543210")
    text = str(exc.value)
    assert "rate-limiting" in text and "340" in text, text
    assert "A wait of" not in text, "Telegram's raw text is not repeated; only its number is"


@pytest.mark.asyncio
async def test_sign_in_reuses_the_connected_client_and_closes_it_after() -> None:
    login = _login()
    await login.send_code("+919876543210")
    client = FakeClient.instances[0]
    assert len(FakeClient.instances) == 1, "a second client for one login means a second auth attempt"

    result = await login.sign_in("+919876543210", "12345", "abcd1234")
    assert client.names() == [
        "connect",
        "send_code_request",
        "sign_in",
        "get_me",
        "disconnect",
    ], client.calls
    assert client.kwargs_of("sign_in") == ("phone_code_hash",), (
        "the hash is what binds this code to the request we made; a sign_in without it is a guess"
    )
    assert result.account_id == 777 and result.username == "spare_account"
    assert result.session_string, "the string is the whole point of the command"
    assert login._clients == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_two_factor_keeps_the_attempt_alive_instead_of_starting_over() -> None:
    """A password prompt must not cost the account a second code.

    Telethon raises ``SessionPasswordNeededError`` from ``sign_in``; the connection that produced it
    is the one the password belongs to, so the module holds it and asks for the password instead.
    """
    from app.controlbot import NeedsPassword

    login = _login()
    await login.send_code("+919876543210")
    client = FakeClient.instances[0]
    FakeClient.password_needed = True

    with pytest.raises(NeedsPassword):
        await login.sign_in("+919876543210", "12345", "abcd1234")
    assert client.connected is True, "closing here would force a new code request"
    assert login._clients.get("+919876543210") is client  # noqa: SLF001

    FakeClient.password_needed = False
    done = await login.sign_in("+919876543210", "", None, password="hunter2")
    assert done.account_id == 777
    assert client.kwargs_of("sign_in") == ("password",), "the password step sends only the password"
    assert client.connected is False, "and the connection is closed once the string exists"


@pytest.mark.asyncio
async def test_an_abandoned_attempt_is_disconnected() -> None:
    login = _login()
    await login.send_code("+919876543210")
    client = FakeClient.instances[0]
    await login.discard("+919876543210")
    assert ("disconnect",) in client.calls
    await login.discard("+910000000000")  # an unknown phone must not raise


# --- the drift guard -------------------------------------------------------------------------------

TELETHON_CALLERS = ("client", "c", "writer", "reader")


def _telethon_calls_in(source: str) -> list[tuple[int, str, set[str]]]:
    """``(line, method, keyword names)`` for every method call on a Telethon-shaped name.

    Two spellings count: a local called ``client``/``c``/``writer``/``reader``, and the injected
    attribute ``self.client`` — the sender holds the session that way, and that is where the verbs
    with the most arguments live.
    """
    out: list[tuple[int, str, set[str]]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        named = isinstance(owner, ast.Name) and owner.id in TELETHON_CALLERS
        injected = isinstance(owner, ast.Attribute) and owner.attr == "client"
        if not (named or injected):
            continue
        keywords = {k.arg for k in node.keywords if k.arg}
        out.append((node.lineno, node.func.attr, keywords))
    return out


def test_every_telethon_keyword_argument_this_project_passes_still_exists() -> None:
    """An argument the installed Telethon does not accept is a crash, not a warning.

    This is the guard for exactly the failure that broke the first live login. The check is
    deliberately loose about *our own* wrappers (`sender.Sender.send_text` and friends) — only names
    that Telethon's client really has are bound, and a call site that passes a keyword Telethon
    dropped is reported with the file and line to fix.
    """
    pytest.importorskip("telethon")
    from telethon import TelegramClient

    offenders: list[str] = []
    checked = 0
    for path in sorted((ROOT / "app").rglob("*.py")):
        for line, name, keywords in _telethon_calls_in(path.read_text(encoding="utf-8")):
            method = getattr(TelegramClient, name, None)
            if method is None or not callable(method):
                continue  # our own wrapper object, under the same local name
            signature = inspect.signature(method)
            if any(p.kind is p.VAR_KEYWORD for p in signature.parameters.values()):
                continue
            allowed = set(signature.parameters)
            unknown = keywords - allowed
            checked += 1
            if unknown:
                offenders.append(f"{path.name}:{line} {name}({', '.join(sorted(unknown))})")
    assert checked >= 6, f"the scan found only {checked} telethon call sites — it has stopped working"
    assert not offenders, "Telethon does not accept these: " + "; ".join(offenders)


def test_every_raw_request_this_project_builds_matches_the_installed_layer() -> None:
    """Same idea for the typed requests: ``functions.*`` names arguments too, and they drift as well."""
    pytest.importorskip("telethon")
    from telethon import functions, types

    namespaces = {"functions": functions, "types": types}
    offenders: list[str] = []
    checked = 0
    pattern = re.compile(r"(functions\.[A-Za-z.]+|types\.[A-Za-z.]+)\(")
    for path in sorted((ROOT / "app").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in pattern.finditer(source):
            dotted = match.group(1)
            try:
                cls = eval(dotted, dict(namespaces))  # noqa: S307 - names come from this repo's source
            except Exception:  # noqa: BLE001
                continue
            tail = source[match.end() : source.find(")", match.end()) + 1]
            keywords = set(re.findall(r"(\w+)\s*=", tail))
            if not keywords:
                continue
            allowed = set(inspect.signature(cls.__init__).parameters) - {"self"}
            checked += 1
            unknown = keywords - allowed
            if unknown:
                offenders.append(f"{path.name} {dotted}({', '.join(sorted(unknown))})")
    assert checked >= 1, "the request scan found nothing to check; it is not reading the source"
    assert not offenders, "these MTProto requests were built with arguments they do not take: " + "; ".join(offenders)
