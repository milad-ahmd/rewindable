"""Telling a human when a run starts waiting: on_waiting, webhooks and Slack."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rewindable import Engine, webhook
from rewindable.cli import main
from rewindable.notify import is_slack


def two_approvals(ctx):
    ctx.step("draft", lambda: "secret draft output")
    answers = ctx.parallel(
        [
            lambda c: bool(c.approve("Refund <b>€129</b> & close?", key="refund")),
            lambda c: c.wait_for("ticket", "Which ticket?"),
        ]
    )
    return answers


@pytest.fixture
def hook():
    """A local webhook that records every POST."""
    received: list[dict] = []
    status = {"code": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append({"body": json.loads(body), "type": self.headers["Content-Type"]})
            self.send_response(status["code"])
            self.end_headers()
            self.wfile.write(b"ok")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook", received, status
    server.shutdown()
    server.server_close()


def test_notifies_once_per_signal_across_resumes(tmp_path, hook):
    url, received, _ = hook
    engine = Engine(tmp_path / "runs.db", on_waiting=webhook(url))
    run = engine.run(two_approvals)
    assert run.status == "waiting"
    assert sorted(r["body"]["key"] for r in received) == ["refund", "ticket"]
    refund = next(r["body"] for r in received if r["body"]["key"] == "refund")
    assert refund == {
        "run_id": run.run_id,
        "workflow": refund["workflow"],
        "key": "refund",
        "prompt": "Refund <b>€129</b> & close?",
        "approval": True,
        "requested_at": refund["requested_at"],
        "ui_hint": f"rewindable approve {run.run_id} refund",
    }
    ticket = next(r["body"] for r in received if r["body"]["key"] == "ticket")
    assert ticket["approval"] is False and ticket["ui_hint"].startswith(f"rewindable signal {run.run_id} ticket")
    assert all("secret draft output" not in json.dumps(r["body"]) for r in received)

    engine.resume(run.run_id)  # still waiting on both: no new messages
    engine.approve(run.run_id, "refund")
    other = Engine(tmp_path / "runs.db", on_waiting=webhook(url))  # e.g. a second worker
    assert other.resume(run.run_id).status == "waiting"
    assert len(received) == 2
    other.signal(run.run_id, "ticket", 7)
    assert other.resume(run.run_id).output == [True, 7]
    assert len(received) == 2


def test_notification_failure_is_swallowed(tmp_path, hook, capsys):
    url, received, status = hook
    status["code"] = 500
    engine = Engine(tmp_path / "runs.db", on_waiting=webhook(url))
    run = engine.run(two_approvals)
    assert run.status == "waiting" and len(received) == 2
    assert "warning: could not notify" in capsys.readouterr().err

    def broken(waiting):
        raise RuntimeError("no network")

    engine.on_waiting = broken
    second = engine.run(two_approvals)
    assert second.status == "waiting"
    assert "no network" in capsys.readouterr().err

    unreachable = Engine(tmp_path / "runs.db", on_waiting=webhook("http://127.0.0.1:9/nothing", timeout=1))
    assert unreachable.run(two_approvals).status == "waiting"


def test_slack_shape(tmp_path, hook):
    url, received, _ = hook
    assert is_slack("https://hooks.slack.com/services/T0/B0/xyz") and not is_slack(url)
    engine = Engine(tmp_path / "runs.db", on_waiting=webhook(url, slack=True))
    run = engine.run(two_approvals)
    texts = {r["body"]["text"] for r in received}
    assert all(set(r["body"]) == {"text"} for r in received)
    [refund] = [t for t in texts if "waiting for approval" in t]
    assert run.run_id in refund
    assert "Refund &lt;b&gt;€129&lt;/b&gt; &amp; close?" in refund
    assert f"rewindable approve {run.run_id} refund" in refund


def test_timer_only_waits_do_not_notify(tmp_path):
    seen = []

    def nap(ctx):
        ctx.sleep(3600)

    engine = Engine(tmp_path / "runs.db", on_waiting=seen.append)
    assert engine.run(nap).status == "waiting" and seen == []


def test_waiting_dataclass(tmp_path):
    seen = []
    engine = Engine(tmp_path / "runs.db", on_waiting=seen.append)
    run = engine.run(two_approvals)
    by_key = {w.key: w for w in seen}
    assert by_key["refund"].approval and by_key["refund"].prompt.startswith("Refund")
    assert by_key["ticket"].run_id == run.run_id and by_key["ticket"].requested_at > 0
    assert engine.store.get_signal(run.run_id, "refund").notified_at is not None


def test_cli_notify_url_and_env(tmp_path, hook, monkeypatch, capsys):
    url, received, _ = hook
    monkeypatch.chdir(tmp_path)
    (tmp_path / "flow.py").write_text(
        "def gated(ctx):\n    return 'yes' if ctx.approve('Ship it?') else 'no'\n",
    )
    db = str(tmp_path / "runs.db")
    assert main(["--db", db, "start", "flow.py:gated", "--notify-url", url]) == 0
    assert [r["body"]["prompt"] for r in received] == ["Ship it?"]
    monkeypatch.setenv("REWINDABLE_NOTIFY_URL", url)
    assert main(["--db", db, "start", "flow.py:gated"]) == 0
    assert len(received) == 2
    capsys.readouterr()
