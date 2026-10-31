import json
import sqlite3

import pytest

from rewindable import Agent, ApprovalCodeError, Engine, ScriptedModel, call, tool
from rewindable import approval_code as ac
from rewindable.cli import main


@pytest.fixture
def engine(tmp_path):
    return Engine(tmp_path / "runs.db")


def gated(ctx):
    return "sent" if ctx.approve("Send the newsletter?") else "held"


def test_code_shape_and_hash_only_storage(engine):
    code = engine.set_approval_code()
    assert len(code) == 23 and code.count("-") == 3 and set(code.replace("-", "")) <= set(ac.ALPHABET)
    stored = engine.store.get_setting(ac.SETTING)
    assert stored == ac.digest(code) and code.replace("-", "") not in stored
    assert ac.matches(stored, code.lower().replace("-", " "))  # typed by hand: case and separators don't matter


def test_no_code_set_changes_nothing(engine):
    run = engine.run(gated)
    engine.approve(run.run_id, run.waiting_for[0])
    assert engine.resume(run.run_id).output == "sent"


def test_approving_needs_the_code_and_rejecting_does_not(engine):
    code = engine.set_approval_code()
    run = engine.run(gated)
    key = run.waiting_for[0]
    with pytest.raises(ApprovalCodeError, match="needs the approval code"):
        engine.approve(run.run_id, key)
    with pytest.raises(ApprovalCodeError, match="wrong"):
        engine.approve(run.run_id, key, code="AAAAA-AAAAA-AAAAA-AAAAA")
    assert engine.store.get_signal(run.run_id, key).pending

    engine.approve(run.run_id, key, code=code)
    assert engine.resume(run.run_id).output == "sent"

    other = engine.run(gated)
    engine.approve(other.run_id, other.waiting_for[0], approved=False)
    assert engine.resume(other.run_id).output == "held"


def test_signal_cannot_approve_around_the_code(engine):
    engine.set_approval_code()
    run = engine.run(gated)
    key = run.waiting_for[0]
    for payload in ({"approved": True}, True, "yes"):
        with pytest.raises(ApprovalCodeError):
            engine.signal(run.run_id, key, payload)
    # Delivered before the run asks: no record says it is an approval yet, but it looks like one.
    with pytest.raises(ApprovalCodeError):
        engine.signal(run.run_id, "approve-9", {"approved": True})


def test_plain_signals_still_flow_without_the_code(engine):
    engine.set_approval_code()

    def flow(ctx):
        return ctx.wait_for("payment", "waiting for the webhook")

    run = engine.run(flow)
    engine.signal(run.run_id, "payment", {"paid": True})
    assert engine.resume(run.run_id, flow).output == {"paid": True}


def test_an_approval_written_past_the_check_reads_as_a_rejection(engine):
    engine.set_approval_code()
    run = engine.run(gated)
    key = run.waiting_for[0]
    engine.store.deliver_signal(run.run_id, key, {"approved": True})  # straight into the journal
    result = engine.resume(run.run_id)
    assert result.output == "held"
    [step] = [s for s in engine.store.steps(run.run_id) if s.kind == "signal"]
    assert step.output == {"approved": False, "note": "approved without the approval code"}
    assert engine.resume(run.run_id).output == "held"  # and replays stay that way


def test_a_damaged_setting_refuses_every_approval(engine):
    code = engine.set_approval_code()
    run = engine.run(gated)
    for broken in ("", "sha256:", "plain-text-code", code):
        engine.store.set_setting(ac.SETTING, broken)
        with pytest.raises(ApprovalCodeError):
            engine.approve(run.run_id, run.waiting_for[0], code=code)


def test_replacing_or_clearing_needs_the_current_code(engine):
    first = engine.set_approval_code()
    with pytest.raises(ApprovalCodeError):
        engine.set_approval_code()
    with pytest.raises(ApprovalCodeError):
        engine.clear_approval_code("AAAAA-AAAAA-AAAAA-AAAAA")
    second = engine.set_approval_code(current=first)
    assert second != first
    engine.clear_approval_code(second)
    assert not engine.approval_code_required


def test_agent_cannot_approve_its_own_gated_tool(engine):
    sent = []

    @tool(requires_approval=True)
    def send_email(to: str) -> str:
        """Send an email."""
        sent.append(to)
        return "sent"

    def respond(request):
        results = [
            b
            for m in request["messages"]
            if isinstance(m["content"], list)
            for b in m["content"]
            if b.get("type") == "tool_result"
        ]
        return call("send_email", to="all@example.com") if not results else results[0]["content"]

    agent = Agent("mailer", ScriptedModel(respond), tools=[send_email])

    def flow(ctx):
        return agent.run(ctx, "email everyone").text

    code = engine.set_approval_code()
    first = engine.run(flow)
    with pytest.raises(ApprovalCodeError):
        engine.approve(first.run_id, first.waiting_for[0])  # what an agent with a shell tool would try
    engine.approve(first.run_id, first.waiting_for[0], code=code)
    assert engine.resume(first.run_id, flow).output == "sent" and sent == ["all@example.com"]


def test_older_journals_get_the_new_columns(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE signals (run_id TEXT NOT NULL, key TEXT NOT NULL, prompt TEXT, payload TEXT,"
        " requested_at REAL, delivered_at REAL, consumed_at REAL, PRIMARY KEY (run_id, key))"
    )
    conn.execute("INSERT INTO signals (run_id, key, payload, delivered_at) VALUES ('r', 'k', 'true', 1)")
    conn.commit()
    conn.close()
    signal = Engine(db).store.get_signal("r", "k")
    assert signal.approval is False and signal.approved_with_code is False


# -- CLI ----------------------------------------------------------------------


def test_cli_approve_asks_for_the_code(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "runs.db")
    engine = Engine(db)
    code = engine.set_approval_code()
    run = engine.run(gated)
    asked = []
    monkeypatch.setattr("getpass.getpass", lambda prompt: asked.append(prompt) or "WRONG")
    assert main(["--db", db, "approve", run.run_id, run.waiting_for[0], "--no-resume"]) == 1
    assert "wrong approval code" in capsys.readouterr().err and asked == ["Approval code: "]

    monkeypatch.setattr("getpass.getpass", lambda prompt: code)
    assert main(["--db", db, "approve", run.run_id, run.waiting_for[0]]) == 0
    assert "completed" in capsys.readouterr().out

    other = engine.run(gated)
    monkeypatch.setattr("getpass.getpass", lambda prompt: pytest.fail("rejecting must not ask"))
    assert main(["--db", db, "approve", other.run_id, other.waiting_for[0], "--reject"]) == 0


def test_cli_set_refuses_without_a_terminal(tmp_path, capsys):
    db = str(tmp_path / "runs.db")
    assert main(["--db", db, "approval-code", "set"]) == 2  # pytest's stdin is not a TTY
    assert "interactive terminal" in capsys.readouterr().err
    assert not Engine(db).approval_code_required
    assert main(["--db", db, "approval-code", "status"]) == 0
    assert "No approval code" in capsys.readouterr().out


def test_cli_set_shows_the_code_once(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "runs.db")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert main(["--db", db, "approval-code", "set"]) == 0
    out = capsys.readouterr().out
    [code] = [w for w in out.split() if w.count("-") == 3 and len(w) == 23]
    stored = Engine(db).store.get_setting(ac.SETTING)
    assert ac.matches(stored, code)

    monkeypatch.setattr("getpass.getpass", lambda prompt: code)
    assert main(["--db", db, "approval-code", "clear"]) == 0
    assert not Engine(db).approval_code_required


def test_ui_reports_whether_the_code_is_needed(engine):
    from rewindable.ui import run_detail

    run = engine.run(gated)
    assert run_detail(engine, run.run_id)["approval_code"] is False
    engine.set_approval_code()
    assert json.loads(json.dumps(run_detail(engine, run.run_id)))["approval_code"] is True
