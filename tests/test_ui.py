import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from rewindable import Engine
from rewindable.ui import make_server


def gated(ctx):
    ctx.step("draft", lambda: "draft")
    return "sent" if ctx.approve("Send the newsletter?") else "held"


@pytest.fixture
def ui(tmp_path):
    engine = Engine(tmp_path / "runs.db")
    server = make_server(engine, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield engine, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def request(url, *, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def wait_for_status(engine, run_id, status):
    deadline = time.time() + 5
    while engine.store.get_run(run_id).status != status and time.time() < deadline:
        time.sleep(0.05)
    return engine.store.get_run(run_id)


def test_page_and_read_api(ui):
    engine, base = ui
    run = engine.run(gated)
    status, page = request(base + "/")
    assert status == 200 and b"rewindable" in page and str(engine.store.path).encode() in page

    status, body = request(base + "/api/runs")
    [summary] = json.loads(body)
    assert summary["id"] == run.run_id and summary["pending"] == run.waiting_for

    status, body = request(f"{base}/api/runs/{run.run_id}")
    detail = json.loads(body)
    assert [s["name"] for s in detail["steps"]] == ["draft"]
    assert detail["signals"][0]["prompt"] == "Send the newsletter?"
    assert request(f"{base}/api/runs/run_missing")[0] == 404


def test_writes_need_the_csrf_header(ui):
    engine, base = ui
    run = engine.run(gated)
    url = f"{base}/api/runs/{run.run_id}/signals/{run.waiting_for[0]}"
    assert request(url, body={"approved": True})[0] == 403
    assert engine.store.get_run(run.run_id).status == "waiting"


def test_foreign_host_header_is_rejected(ui):
    _, base = ui
    assert request(base + "/api/runs", headers={"Host": "evil.example"})[0] == 403


def test_approve_from_the_ui_resumes_the_run(ui):
    engine, base = ui
    run = engine.run(gated)
    url = f"{base}/api/runs/{run.run_id}/signals/{run.waiting_for[0]}"
    status, _ = request(url, body={"approved": True, "note": "lgtm"}, headers={"X-Rewindable": "1"})
    assert status == 200
    done = wait_for_status(engine, run.run_id, "completed")
    assert done.output == "sent"


def test_rewind_from_the_ui(ui):
    engine, base = ui
    run = engine.run(gated)
    engine.approve(run.run_id, run.waiting_for[0], approved=False)
    engine.resume(run.run_id)
    status, body = request(f"{base}/api/runs/{run.run_id}/rewind", body={"to": "2"}, headers={"X-Rewindable": "1"})
    child_id = json.loads(body)["run_id"]
    assert status == 200 and child_id != run.run_id
    child = wait_for_status(engine, child_id, "waiting")  # asks again from step 2
    assert child.parent_run_id == run.run_id
