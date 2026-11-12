'''Offline-Tests fuer den sicherheitskritischsten Pfad der Action-Engine:
BaseTool.run() (Audit -> Veto -> Confirmation-Gate -> Ausfuehrung). Keine DB -- alle Seams
(write_audit_log, confirmation_store) gemonkeypatcht.
Muster wie tests/test_cloud_agents.py; pytest.ini setzt asyncio_mode=auto.
'''
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
import pytest
from pydantic import BaseModel

from rag_backend.action_engine.base_tool import BaseTool, ToolClassification, ToolOutput


class _NoInput(BaseModel):
    pass


class _DummyTool(BaseTool):
    name = "dummy_tool"
    description = "test tool"

    def __init__(self, classification=ToolClassification.READ, veto=None):
        self._cls = classification
        self._veto_result = veto
        self.executed = False

    def get_input_schema(self):
        return _NoInput

    def _classify(self, params):
        return self._cls

    def _veto(self, params, detected):
        return self._veto_result

    async def _execute(self, params):
        self.executed = True
        return ToolOutput(success=True, data="done")


@pytest.fixture
def audit_calls(monkeypatch):
    calls = []
    import rag_backend.action_engine.audit as audit_mod
    monkeypatch.setattr(audit_mod, "write_audit_log", lambda **kw: calls.append(kw))
    return calls


def _patch_confirmation(monkeypatch, resolved_status):
    # resolved_status: ConfirmationStatus | None (None = Timeout/kein Ergebnis)
    import rag_backend.action_engine.confirmation as conf_mod

    class _Req:
        id = "req-test-1"

    def _create(**kwargs):
        return _Req()

    async def _wait(_id):
        if resolved_status is None:
            return None

        class _Resolved:
            status = resolved_status

        return _Resolved()

    monkeypatch.setattr(conf_mod.confirmation_store, "create", _create)
    monkeypatch.setattr(conf_mod.confirmation_store, "wait_for_resolution", _wait)
    return conf_mod


# ---- Confirmation-Gate ------------------------------------------------------

async def test_read_runs_without_confirmation(monkeypatch, audit_calls):
    tool = _DummyTool(ToolClassification.READ)
    out = await tool.run({})
    assert out.success is True and tool.executed is True
    assert audit_calls and audit_calls[-1]["status"] == "ok"
    assert audit_calls[-1]["tool_class"] == "read"


async def test_write_blocked_without_channel(monkeypatch, audit_calls):
    # OHNE Chat-Karte darf ein WRITE NIE ausgefuehrt werden (fail-closed).
    tool = _DummyTool(ToolClassification.WRITE)
    out = await tool.run({})
    assert out.success is False and tool.executed is False
    assert "card in the Argus chat" in (out.error or "")
    assert audit_calls[-1]["status"] == "confirmation_no_channel"


async def test_destructive_blocked_without_channel(monkeypatch, audit_calls):
    tool = _DummyTool(ToolClassification.DESTRUCTIVE)
    out = await tool.run({})
    assert out.success is False and tool.executed is False
    assert audit_calls[-1]["status"] == "confirmation_no_channel"


async def test_veto_blocks_before_confirmation(monkeypatch, audit_calls):
    # Veto greift VOR dem Confirmation-Gate -- auch mit offener Chat-Karte.
    _set_task()
    tool = _DummyTool(ToolClassification.WRITE, veto=("blocked", "vetoed"))
    out = await tool.run({})
    assert out.success is False and tool.executed is False
    assert out.error == "vetoed"
    assert audit_calls[-1]["status"] == "blocked"


async def test_confirmation_approved_executes(monkeypatch, audit_calls):
    _set_task()
    conf_mod = _patch_confirmation(monkeypatch, None)
    _patch_confirmation(monkeypatch, conf_mod.ConfirmationStatus.APPROVED)
    tool = _DummyTool(ToolClassification.WRITE)
    out = await tool.run({}, user_id=7)
    assert out.success is True and tool.executed is True
    assert audit_calls[-1]["status"] == "ok"


async def test_confirmation_rejected_blocks(monkeypatch, audit_calls):
    _set_task()
    conf_mod = _patch_confirmation(monkeypatch, None)
    _patch_confirmation(monkeypatch, conf_mod.ConfirmationStatus.REJECTED)
    tool = _DummyTool(ToolClassification.DESTRUCTIVE)
    out = await tool.run({}, user_id=7)
    assert out.success is False and tool.executed is False
    assert audit_calls[-1]["status"] == "confirmation_rejected"


async def test_confirmation_timeout_blocks(monkeypatch, audit_calls):
    _set_task()
    _patch_confirmation(monkeypatch, None)  # wait_for_resolution -> None = Timeout
    tool = _DummyTool(ToolClassification.WRITE)
    out = await tool.run({}, user_id=7)
    assert out.success is False and tool.executed is False
    assert audit_calls[-1]["status"] == "confirmation_timeout"


def test_confirmation_is_bound_to_requester():
    '''Ueber HTTP darf nur der Urheber freigeben. Vorher genuegte irgendein gueltiges
    Token -- auch der Service-Token aus der .env.'''
    import uuid
    from rag_backend.action_engine.confirmation import ConfirmationStore

    store = ConfirmationStore()
    req = store.create(
        tool_name="dummy_tool", tool_class="write", parameters={},
        impact_description="test", requested_by=7,
    )
    assert store.approve(req.id, actor=9) is None            # fremder Nutzer
    assert store.list_pending(actor=9) == []                 # sieht die ID nicht einmal
    assert any(r.id == req.id for r in store.list_pending(actor=7))
    approved = store.approve(req.id, actor=7)
    assert approved is not None and approved.status.value == "approved"

    # Einen Kanal ohne Konto gibt es nicht mehr: ohne Akteur keine Freigabe.
    req2 = store.create(
        tool_name="dummy_tool", tool_class="write", parameters={},
        impact_description="test", requested_by=7,
    )
    assert store.approve(req2.id, actor=None) is None
    assert store.list_pending(actor=None) == []

    # Ohne bekannten Urheber ist niemand zustaendig (fail-closed).
    req3 = store.create(
        tool_name="dummy_tool", tool_class="write", parameters={},
        impact_description="test",
    )
    assert store.approve(req3.id, actor=0) is None
    assert store.approve(req3.id, actor=None) is None


# ---- Aufgaben-Freigabe (Auto-Modus) -----------------------------------------
# Eine Aufgabe fragt EINMAL; loeschende Aktionen bleiben ausgenommen.

@pytest.fixture(autouse=True)
def _reset_context_vars():
    # ContextVars ueberleben sonst den Test und faerben den naechsten ein.
    import rag_backend.action_engine.base_tool as bt
    token_task = bt.CURRENT_TASK.set(None)
    token_user = bt.CURRENT_USER_ID.set(None)
    yield
    bt.CURRENT_TASK.reset(token_task)
    bt.CURRENT_USER_ID.reset(token_user)


@pytest.fixture
def task_store(tmp_path, monkeypatch):
    # Store auf ein frisches Verzeichnis umbiegen -- /tmp waere zwischen Tests geteilt.
    import rag_backend.action_engine.confirmation as conf_mod
    monkeypatch.setattr(conf_mod, "STATE_FILE", str(tmp_path / "confirmations.json"))
    monkeypatch.setattr(conf_mod, "TASKS_FILE", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(conf_mod, "LOCK_FILE", str(tmp_path / "conf.lock"))
    monkeypatch.setattr(conf_mod.FileLock, "__init__",
                        lambda self, path=str(tmp_path / "conf.lock"): (
                            setattr(self, "path", path), setattr(self, "fd", None))[0])
    return conf_mod


def _set_task(task_id="task-1", channel="dashboard", text="installiere LibreOffice"):
    import rag_backend.action_engine.base_tool as bt
    bt.CURRENT_TASK.set({"id": task_id, "text": text, "channel": channel})


async def test_task_approval_second_write_skips_gate(monkeypatch, audit_calls, task_store):
    created = []

    class _Req:
        id = "req-1"
        task_id = "task-1"
        task_scope = True
        tool_class = "write"
        tool_name = "dummy_tool"
        task_description = "installiere LibreOffice"
        requested_by = 0

    def _create(**kwargs):
        created.append(kwargs)
        return _Req()

    async def _wait(_id):
        class _R:
            status = task_store.ConfirmationStatus.APPROVED
        # Freigabe wirkt aufgabenweit -- genau das schreibt _mark_task fest.
        task_store.confirmation_store._mark_task(_Req(), "approved")
        return _R()

    monkeypatch.setattr(task_store.confirmation_store, "create", _create)
    monkeypatch.setattr(task_store.confirmation_store, "wait_for_resolution", _wait)

    _set_task()
    first = _DummyTool(ToolClassification.WRITE)
    assert (await first.run({}, user_id=0)).success is True
    assert first.executed is True
    assert len(created) == 1, "erste schreibende Aktion muss fragen"

    # Zweiter Schritt derselben Aufgabe: KEINE neue Anfrage.
    second = _DummyTool(ToolClassification.WRITE)
    assert (await second.run({}, user_id=0)).success is True
    assert second.executed is True
    assert len(created) == 1, "zweite Aktion derselben Aufgabe darf nicht erneut fragen"
    assert any(c["status"] == "task_approved_skip" for c in audit_calls)


async def test_tool_without_task_scope_asks_every_time(monkeypatch, audit_calls, task_store):
    # Ein Werkzeug kann die Aufgaben-Freigabe abwaehlen (task_scoped_approval = False):
    # dann fragt jeder schreibende Aufruf einzeln, auch wenn die Aufgabe schon
    # freigegeben ist.

    _set_task()

    class _Approved:
        id = "req-0"
        task_id = "task-1"
        task_scope = True
        tool_class = "write"
        tool_name = "dummy_tool"
        task_description = ""
        requested_by = 0
    task_store.confirmation_store._mark_task(_Approved(), "approved")

    created = []

    def _create(**kwargs):
        created.append(kwargs)
        class _R:
            id = f"req-{len(created)}"
        return _R()

    async def _wait(_id):
        class _R:
            status = task_store.ConfirmationStatus.APPROVED
        return _R()

    monkeypatch.setattr(task_store.confirmation_store, "create", _create)
    monkeypatch.setattr(task_store.confirmation_store, "wait_for_resolution", _wait)

    for _ in range(2):
        tool = _DummyTool(ToolClassification.WRITE)
        tool.task_scoped_approval = False
        assert (await tool.run({}, user_id=0)).success is True
    assert len(created) == 2, "jeder schreibende Aufruf muss einzeln fragen"
    assert all(c["task_scope"] is False for c in created)
    assert not any(c["status"] == "task_approved_skip" for c in audit_calls)


async def test_destructive_gated_despite_task_approval(monkeypatch, audit_calls, task_store):
    _set_task()
    # Aufgabe ist bereits freigegeben ...
    class _Approved:
        id = "req-0"
        task_id = "task-1"
        task_scope = True
        tool_class = "write"
        tool_name = "dummy_tool"
        task_description = ""
        requested_by = 0
    task_store.confirmation_store._mark_task(_Approved(), "approved")

    created = []

    def _create(**kwargs):
        created.append(kwargs)
        class _R:
            id = "req-del"
        return _R()

    async def _wait(_id):
        class _R:
            status = task_store.ConfirmationStatus.APPROVED
        return _R()

    monkeypatch.setattr(task_store.confirmation_store, "create", _create)
    monkeypatch.setattr(task_store.confirmation_store, "wait_for_resolution", _wait)

    tool = _DummyTool(ToolClassification.DESTRUCTIVE)
    assert (await tool.run({}, user_id=0)).success is True
    # ... trotzdem wurde gefragt, und zwar OHNE Aufgaben-Bindung.
    assert len(created) == 1, "loeschende Aktion muss trotz Aufgaben-Freigabe fragen"
    assert created[0]["task_scope"] is False


async def test_task_approval_expires(monkeypatch, task_store):
    _set_task()
    class _Req:
        id = "req-1"
        task_id = "task-1"
        task_scope = True
        tool_class = "write"
        tool_name = "dummy_tool"
        task_description = ""
        requested_by = 0
    store = task_store.confirmation_store
    store._mark_task(_Req(), "approved")
    assert store.task_status("task-1") == "approved"
    # Zeitdeckel abgelaufen -> Freigabe ist weg, es wird wieder gefragt.
    monkeypatch.setattr(store, "_task_ttl", -1)
    store._mark_task(_Req(), "approved")
    assert store.task_status("task-1") is None


async def test_task_approval_bound_to_actor(task_store):
    import time
    _set_task()
    class _Req:
        id = "req-1"
        task_id = "task-konto"
        task_scope = True
        tool_class = "write"
        tool_name = "dummy_tool"
        task_description = "Aufgabe fuer Konto 3"
        requested_by = 3

    store = task_store.confirmation_store
    store._mark_task(_Req(), "approved")

    # Freigabe von Konto 3 gilt fuer Konto 3:
    assert store.task_status("task-konto", actor=3) == "approved"
    # Freigabe von Konto 3 gilt NICHT fuer Konto 4 mit derselben task_id:
    assert store.task_status("task-konto", actor=4) is None
    # Ohne actor bleibt das alte Verhalten erhalten:
    assert store.task_status("task-konto") == "approved"

    # Eintraege ohne gespeichertes Konto (alte Datei) gelten bei gesetztem actor als nicht vorhanden:
    from rag_backend.action_engine.confirmation import _load_tasks, _save_tasks
    tasks = _load_tasks()
    tasks["task-alt"] = {
        "status": "approved",
        "tool": "dummy_tool",
        "expires_at": time.time() + 300,
    }
    _save_tasks(tasks)
    assert store.task_status("task-alt") == "approved"
    assert store.task_status("task-alt", actor=3) is None


async def test_task_reject_blocks_followup_writes(monkeypatch, audit_calls, task_store):
    _set_task()
    class _Req:
        id = "req-1"
        task_id = "task-1"
        task_scope = True
        tool_class = "write"
        tool_name = "dummy_tool"
        task_description = ""
        requested_by = 0
    task_store.confirmation_store._mark_task(_Req(), "rejected")

    created = []
    monkeypatch.setattr(task_store.confirmation_store, "create",
                        lambda **kw: created.append(kw))

    tool = _DummyTool(ToolClassification.WRITE)
    out = await tool.run({}, user_id=0)
    assert out.success is False and tool.executed is False
    assert not created, "nach Ablehnung darf keine neue Anfrage entstehen"
    assert "rejected this task" in (out.error or "")
    assert audit_calls[-1]["status"] == "confirmation_task_rejected"


async def test_chat_channel_allows_confirmation(monkeypatch, audit_calls, task_store):
    # Mit offener Chat-Karte und bekanntem Konto ist die Freigabe moeglich.
    _set_task(channel="dashboard")
    _patch_confirmation(monkeypatch, task_store.ConfirmationStatus.APPROVED)
    tool = _DummyTool(ToolClassification.WRITE)
    out = await tool.run({}, user_id=0)
    assert out.success is True and tool.executed is True
    assert not any(c["status"] == "confirmation_no_channel" for c in audit_calls)


async def test_other_channel_is_blocked(monkeypatch, audit_calls, task_store):
    # Ohne Chat-Karte (Kanal ungleich dashboard) kann niemand freigeben -- fail-closed.
    _set_task(channel="other")
    tool = _DummyTool(ToolClassification.WRITE)
    out = await tool.run({}, user_id=0)
    assert out.success is False and tool.executed is False
    assert audit_calls[-1]["status"] == "confirmation_no_channel"


async def test_chat_without_account_is_blocked(monkeypatch, audit_calls, task_store):
    # Chat-Karte, aber kein Konto: freigeben duerfte nur der Urheber, den es nicht gibt.
    _set_task(channel="dashboard")
    created = []
    monkeypatch.setattr(task_store.confirmation_store, "create", lambda **kw: created.append(kw))
    tool = _DummyTool(ToolClassification.WRITE)
    out = await tool.run({}, user_id=None)
    assert out.success is False and tool.executed is False
    assert not created
    assert audit_calls[-1]["status"] == "confirmation_no_channel"


def test_resolution_writes_audit(monkeypatch, audit_calls, task_store):
    # Jede Entscheidung landet im Audit.
    store = task_store.ConfirmationStore()
    req = store.create(tool_name="dummy_tool", tool_class="write", parameters={},
                       impact_description="test", requested_by=0,
                       task_id="task-9", task_scope=True, task_description="test")
    store.approve(req.id, actor=0, via="chat")
    entry = [c for c in audit_calls if c.get("tool") == "confirmation"]
    assert entry, "Freigabe muss einen Audit-Eintrag erzeugen"
    assert entry[-1]["status"] == "approved"
    assert '"via": "chat"' in entry[-1]["details"]
