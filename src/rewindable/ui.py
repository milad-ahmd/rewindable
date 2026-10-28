"""`rewindable ui`: a local, dependency-free web view of the journal.

Browse runs, inspect every step (including full model conversations), answer
approvals, resume and rewind. It binds to 127.0.0.1 only; write requests must
carry an ``X-Rewindable`` header and a localhost ``Host``, which blocks other
websites from approving things through your browser.
"""

from __future__ import annotations

import html
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .errors import RewindableError, RunLocked, RunNotFound
from .runtime import Engine, load_workflow
from .store import Run, StepRecord, iter_tree

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def run_summary(engine: Engine, run: Run) -> dict:
    usage = engine.store.usage(run.id)
    return {
        "id": run.id,
        "workflow": run.workflow,
        "status": run.status,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "wake_at": run.wake_at,
        "parent_run_id": run.parent_run_id,
        "forked_at": run.forked_at,
        "cost_usd": usage["cost_usd"],
        "tokens": usage["input_tokens"] + usage["output_tokens"],
        "llm_calls": usage["llm_calls"],
        "pending": [s.key for s in engine.store.signals(run.id, pending_only=True)],
    }


def step_view(step: StepRecord) -> dict:
    return {
        "key": step.key,
        "name": step.name,
        "kind": step.kind,
        "status": step.status,
        "depth": step.depth,
        "duration": step.duration,
        "started_at": step.started_at,
        "attempts": step.attempts,
        "input_tokens": step.input_tokens,
        "output_tokens": step.output_tokens,
        "cost_usd": step.cost_usd,
        "output": step.output,
        "error": step.error,
    }


def run_detail(engine: Engine, run_id: str) -> dict:
    run = engine.store.get_run(run_id)
    return {
        **run_summary(engine, run),
        "input": run.input,
        "output": run.output,
        "error": run.error,
        "budget": run.budget,
        "steps": [step_view(s) for s in iter_tree(engine.store.steps(run.id))],
        "signals": [
            {"key": s.key, "prompt": s.prompt, "requested_at": s.requested_at}
            for s in engine.store.signals(run.id, pending_only=True)
        ],
        "children": [r.id for r in engine.store.list_runs(limit=500) if r.parent_run_id == run.id],
    }


class _Handler(BaseHTTPRequestHandler):
    engine: Engine
    page: bytes
    log_requests = False

    def log_message(self, format: str, *args: Any) -> None:
        if self.log_requests:
            super().log_message(format, *args)

    # -- plumbing -----------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in LOCAL_HOSTS

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    # -- routes -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
        try:
            if not parts:
                return self._send(200, self.page, "text/html; charset=utf-8")
            if parts == ["api", "runs"]:
                status = parse_qs(url.query).get("status", [None])[0]
                runs = self.engine.store.list_runs(status=status or None, limit=200)
                return self._json([run_summary(self.engine, r) for r in runs])
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                return self._json(run_detail(self.engine, parts[2]))
        except RunNotFound as exc:
            return self._error(HTTPStatus.NOT_FOUND, f"no run {exc}")
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if not self._host_ok() or self.headers.get("X-Rewindable") != "1":
            return self._error(HTTPStatus.FORBIDDEN, "missing X-Rewindable header or unexpected Host")
        parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]
        try:
            body = self._body()
            if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3] == "signals":
                run = self.engine.store.get_run(parts[2])
                payload = (
                    body["payload"]
                    if "payload" in body
                    else {
                        "approved": bool(body.get("approved")),
                        "note": str(body.get("note") or ""),
                    }
                )
                self.engine.signal(run.id, parts[4], payload)
                self._drive_later(run.id)
                return self._json({"ok": True, "run_id": run.id})
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "resume":
                run = self.engine.store.get_run(parts[2])
                self._drive_later(run.id)
                return self._json({"ok": True, "run_id": run.id})
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "rewind":
                child = self.engine.store.fork(parts[2], str(body["to"]))
                self._drive_later(child.id)
                return self._json({"ok": True, "run_id": child.id})
        except RunNotFound as exc:
            return self._error(HTTPStatus.NOT_FOUND, f"no run {exc}")
        except (KeyError, ValueError, RewindableError) as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def _drive_later(self, run_id: str) -> None:
        """Resume in the background so the page stays responsive; the poll shows progress."""
        engine = self.engine

        def drive() -> None:
            try:
                run = engine.store.get_run(run_id)
                engine._drive(run, load_workflow(run.workflow))
            except RunLocked:
                pass  # another process is already driving it
            except RewindableError as exc:  # e.g. the workflow can no longer be imported
                engine.store.set_status(run_id, "failed", error=str(exc))

        threading.Thread(target=drive, daemon=True).start()


def make_server(engine: Engine, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    page = resources.files("rewindable").joinpath("ui.html").read_bytes()
    page = page.replace(b"{{DB}}", html.escape(str(engine.store.path)).encode())
    handler = type("Handler", (_Handler,), {"engine": engine, "page": page})
    return ThreadingHTTPServer((host, port), handler)


def serve(engine: Engine, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    server = make_server(engine, host, port)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"rewindable ui on {url}  (journal: {engine.store.path})  Ctrl+C to stop")
    if open_browser:
        import webbrowser

        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
