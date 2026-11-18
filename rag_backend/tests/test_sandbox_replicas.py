"""calc-sandbox mit Repliken: agent._sandbox_post probiert alle Adressen von
SANDBOX_HOSTNAME mit frischer Verbindung, wiederholt aber nie nach dem Senden.
Kein echtes Netz: socket.getaddrinfo und der httpx-Client sind ersetzt."""
import os
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("SKIP_MIGRATIONS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("FERNET_KEY", "5yZSI5iZpTwBiP-USKxXRoGSALTjphSL_76iuVLnGrw=")
os.environ.setdefault("CLOUD_REDACTION_LLM_PASS", "false")

import httpx
import pytest

from rag_backend import agent

ADDR_A = "172.19.0.11"
ADDR_B = "172.19.0.12"
BUSY = "Sandbox ist ausgelastet, bitte gleich noch einmal versuchen."


def _fake_getaddrinfo(addrs, calls=None):
    def fake(host, port, *args, **kwargs):
        if calls is not None:
            calls.append((host, port))
        if host != "calc-sandbox":
            raise socket.gaierror("unbekannt")
        #--- Wie der echte Resolver: Eintraege koennen mehrfach vorkommen.
        out = []
        for a in addrs:
            out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)))
            out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)))
        return out
    return fake


@pytest.fixture
def sandbox_net(monkeypatch):
    """Ersetzt DNS und HTTP. handler(request) -> httpx.Response oder wirft.
    Liefert die Liste der tatsaechlich gesendeten Anfragen und der Clients."""
    state = SimpleNamespace(requests=[], clients=[], handler=None, dns_calls=[])
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo([ADDR_A, ADDR_B], state.dns_calls))
    monkeypatch.setattr(agent, "_SANDBOX_SEEN_IPS", frozenset())
    monkeypatch.delenv("SANDBOX_HOSTNAME", raising=False)
    monkeypatch.delenv("SANDBOX_WAIT_SECONDS", raising=False)

    def transport_handler(request: httpx.Request):
        state.requests.append(request)
        return state.handler(request)

    real_factory = agent._new_sandbox_client

    def factory(timeout):
        real = real_factory(timeout)
        state.clients.append(real)
        #--- Gleiche Einstellungen wie im Betrieb, nur die Transportschicht ersetzt.
        return httpx.AsyncClient(
            transport=httpx.MockTransport(transport_handler),
            timeout=real.timeout,
            headers=real.headers,
        )

    monkeypatch.setattr(agent, "_new_sandbox_client", factory)

    async def _noop():
        pass

    monkeypatch.setattr(agent, "_ensure_models_loaded", _noop)
    #--- Vertraulichkeitsmarke (egress_guard) ist hier nicht Gegenstand, siehe test_egress_guard.
    monkeypatch.setattr(agent, "_mark_sandbox_output", lambda _data: None)
    return state


def _host_of(request: httpx.Request) -> str:
    return request.url.host


@pytest.mark.asyncio
async def test_next_address_on_connect_error(sandbox_net):
    def handler(request):
        if _host_of(request) == ADDR_A:
            raise httpx.ConnectError("Connection refused", request=request)
        return httpx.Response(200, json={"result": "Ergebnis: 5"})

    sandbox_net.handler = handler
    resp = await agent._sandbox_post("/calculate", {"expression": "2+3"}, timeout=10.0)
    assert resp.status_code == 200
    assert [_host_of(r) for r in sandbox_net.requests] == [ADDR_A, ADDR_B]
    assert all(r.url.port == 8001 and r.url.path == "/calculate" for r in sandbox_net.requests)
    #--- Frische Verbindung je Anfrage, kein Keep-alive zu einem verbrauchten Container.
    assert all(r.headers.get("connection") == "close" for r in sandbox_net.requests)
    assert all(r.headers.get("host") == "calc-sandbox:8001" for r in sandbox_net.requests)
    assert sandbox_net.dns_calls and sandbox_net.dns_calls[0][0] == "calc-sandbox"
    #--- Die Adressen landen in der Sperrliste fuer main.block_sandbox_requests.
    assert {ADDR_A, ADDR_B} <= agent.sandbox_seen_ips()


def test_client_has_no_keepalive_and_short_connect_timeout():
    client = agent._new_sandbox_client(20.0)
    try:
        assert client.timeout.connect == agent._SANDBOX_CONNECT_TIMEOUT_SECONDS
        assert client.timeout.read == 20.0
        assert client.headers.get("connection") == "close"
        pool = client._transport._pool
        assert pool._max_keepalive_connections == 0
    finally:
        import asyncio
        asyncio.run(client.aclose())


@pytest.mark.asyncio
async def test_next_address_on_503_renewing(sandbox_net):
    def handler(request):
        if _host_of(request) == ADDR_A:
            return httpx.Response(503, json={"error": "Sandbox wird erneuert."})
        return httpx.Response(200, json={"result": "42\n", "files": []})

    sandbox_net.handler = handler
    out = await agent.execute_code.ainvoke({"code": "print(42)"})
    assert out == "42\n"
    assert [_host_of(r) for r in sandbox_net.requests] == [ADDR_A, ADDR_B]


@pytest.mark.asyncio
async def test_other_503_is_not_retried(sandbox_net):
    sandbox_net.handler = lambda request: httpx.Response(503, json={"error": "irgendwas anderes"})
    out = await agent.calculate.ainvoke({"expression": "1+1"})
    assert out.startswith("ERROR: Calculation failed: ")
    assert "503" in out
    assert len(sandbox_net.requests) == 1


@pytest.mark.asyncio
async def test_no_retry_after_timeout_once_sent(sandbox_net):
    def handler(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    sandbox_net.handler = handler
    out = await agent.execute_code.ainvoke({"code": "print(1)"})
    #--- Meldung wie bisher, und B wird NICHT probiert: der Code koennte auf A laufen.
    assert out == "ERROR: Execution failed (sandbox timeout)."
    assert [_host_of(r) for r in sandbox_net.requests] == [ADDR_A]


@pytest.mark.asyncio
async def test_no_retry_after_connection_lost_once_sent(sandbox_net):
    def handler(request):
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)

    sandbox_net.handler = handler
    ok_out, is_err = await agent._run_code_in_sandbox("print(1)")
    assert is_err is True
    assert "Server disconnected" in ok_out
    assert [_host_of(r) for r in sandbox_net.requests] == [ADDR_A]


@pytest.mark.asyncio
async def test_gives_up_after_wait_seconds_with_message(sandbox_net, monkeypatch):
    monkeypatch.setenv("SANDBOX_WAIT_SECONDS", "1.2")
    sandbox_net.handler = lambda request: httpx.Response(503, json={"error": "Sandbox wird erneuert."})

    t0 = time.monotonic()
    with pytest.raises(agent.SandboxBusyError) as exc:
        await agent._sandbox_post("/execute", {"code": "print(1)"}, timeout=20.0)
    elapsed = time.monotonic() - t0
    assert str(exc.value) == BUSY
    assert 1.0 <= elapsed < 5.0
    #--- Mehrere Runden ueber beide Adressen, mit Pause dazwischen.
    hosts = [_host_of(r) for r in sandbox_net.requests]
    assert hosts[:4] == [ADDR_A, ADDR_B, ADDR_A, ADDR_B]
    assert len(sandbox_net.dns_calls) >= 2

    out = await agent.execute_code.ainvoke({"code": "print(1)"})
    assert out == f"ERROR: Execution failed: {BUSY}"


@pytest.mark.asyncio
async def test_gives_up_when_name_not_resolvable(sandbox_net, monkeypatch):
    monkeypatch.setenv("SANDBOX_WAIT_SECONDS", "0.6")

    def no_dns(host, port, *a, **kw):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    sandbox_net.handler = lambda request: pytest.fail("ohne Adresse darf nichts gesendet werden")
    out = await agent.calculate.ainvoke({"expression": "1+1"})
    assert out == f"ERROR: Calculation failed: {BUSY}"
    assert sandbox_net.requests == []


@pytest.mark.asyncio
async def test_sandbox_hostname_from_env(sandbox_net, monkeypatch):
    calls = []
    monkeypatch.setenv("SANDBOX_HOSTNAME", "andere-sandbox")
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, *a, **kw: calls.append(host) or
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.9.9", port))])
    sandbox_net.handler = lambda request: httpx.Response(200, json={"result": "Ergebnis: 2"})
    assert await agent.calculate.ainvoke({"expression": "1+1"}) == "Ergebnis: 2"
    assert calls == ["andere-sandbox"]
    assert sandbox_net.requests[0].headers["host"] == "andere-sandbox:8001"


@pytest.mark.asyncio
async def test_calculate_uses_helper(monkeypatch):
    async def _noop():
        pass

    monkeypatch.setattr(agent, "_ensure_models_loaded", _noop)
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"result": "Ergebnis: 391"}
    helper = AsyncMock(return_value=resp)
    monkeypatch.setattr(agent, "_sandbox_post", helper)
    #--- Der gepoolte Client darf fuer die Sandbox nicht mehr benutzt werden.
    monkeypatch.setattr(agent, "_http_client_internal",
                        MagicMock(post=AsyncMock(side_effect=AssertionError("gepoolter Client benutzt"))))

    assert await agent.calculate.ainvoke({"expression": "17*23"}) == "Ergebnis: 391"
    helper.assert_awaited_once_with("/calculate", {"expression": "17*23"}, timeout=10.0)


@pytest.mark.asyncio
async def test_calculate_timeout_message_unchanged(monkeypatch):
    async def _noop():
        pass

    monkeypatch.setattr(agent, "_ensure_models_loaded", _noop)
    monkeypatch.setattr(agent, "_sandbox_post", AsyncMock(side_effect=httpx.ReadTimeout("t")))
    assert await agent.calculate.ainvoke({"expression": "1"}) == "ERROR: Calculation failed (sandbox timeout)."


@pytest.mark.asyncio
async def test_main_blocks_requests_from_seen_sandbox_ips(monkeypatch):
    """Eine Replik mit neuer IP, die der 30-s-Abgleich noch nicht kennt, ist trotzdem
    gesperrt, sobald das Backend dorthin Code geschickt hat."""
    import importlib
    backend_main = importlib.import_module("rag_backend.main")
    monkeypatch.setattr(backend_main, "_SANDBOX_IPS", frozenset())
    monkeypatch.setattr(agent, "_SANDBOX_SEEN_IPS", frozenset())

    async def call_next(_request):
        return "weiter"

    def req(host):
        return SimpleNamespace(client=SimpleNamespace(host=host), url=SimpleNamespace(path="/v1/dashboard/auth/state"))

    assert await backend_main.block_sandbox_requests(req("172.19.0.99"), call_next) == "weiter"
    agent._remember_sandbox_ips(["172.19.0.99"])
    blocked = await backend_main.block_sandbox_requests(req("172.19.0.99"), call_next)
    assert getattr(blocked, "status_code", None) == 403
    #--- Auch als IPv4-gemappte IPv6-Adresse.
    blocked6 = await backend_main.block_sandbox_requests(req("::ffff:172.19.0.99"), call_next)
    assert getattr(blocked6, "status_code", None) == 403
    assert await backend_main.block_sandbox_requests(req("172.19.0.10"), call_next) == "weiter"
