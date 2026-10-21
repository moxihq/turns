"""A fake OpenCode server, in process or as a stub `opencode` executable.

It speaks the parts of OpenCode 1.18.34's server the OpenCode host uses, with the
semantics measured on the real one: HTTP basic auth with user `opencode`; `POST /session`
mints the id; `prompt_async` keeps the caller's `msg_` id and answers 204; an unknown
session is a 404; `GET /session/status` is `{}` when idle and names a running session as
busy; `GET /session/:id/children` lists child sessions; the event stream (`GET /event`)
carries `server.connected`, `server.heartbeat`, `message.updated` (the user message, then
the assistant message parented to it, its final update sent twice), `session.status`,
`session.idle`, `session.error` and `session.compacted`. Every id here is synthetic.

As an executable (`install`), it answers `opencode serve ...` and `opencode --version`,
driven by `FAKE_OPENCODE_DIR` (records: `events.jsonl`, and `sessions.json`, kept across
restarts) and `FAKE_OPENCODE_AUTO` ("0" leaves turns running until a control call ends
them). A control route on the server itself, `POST /__fake/<op>`, drives it.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import random
import string
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.18.34 (fake)"
#: A `post_answers` entry that admits the prompt and closes the connection unanswered.
DROP_ADMITTED = ("drop", True)
#: One that closes the connection unanswered and admits nothing.
DROP_UNADMITTED = ("drop", False)


def mint(prefix: str) -> str:
    ms = int(time.time() * 1000)
    tail = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(14))
    return f"{prefix}_{(ms * 0x1000 + random.randint(1, 4000)) & 0xFFFFFFFFFFFF:012x}{tail}"


class FakeOpenCode:
    """One fake server. Thread-safe; every state change is one event or more."""

    def __init__(self, *, password="pw", heartbeat=10.0, auto=True, store=None,
                 finish_after=0.05):
        self.password = password
        self.heartbeat = heartbeat
        self.auto = auto
        self.finish_after = finish_after
        self.store = Path(store) if store else None
        self.lock = threading.RLock()
        self.sessions: dict = {}
        self.clients: list = []
        self.requests: list = []           # (method, path, body) of every request
        self.stalled = False
        self.post_answers: list = []       # (status, body) to answer the next posts with
        self.status_answers: list = []     # (status, raw body) for the next status reads
        self._stop = threading.Event()
        self.httpd = None
        self.url = None
        #: False: a prompt is admitted and runs, but no reply is ever written.
        self.reply_to_prompts = True
        if self.store and self.store.exists():
            # A session's status lives in the server's memory: a new server finds every
            # stored session idle.
            self.sessions = json.loads(self.store.read_text())
            for session in self.sessions.values():
                session.update(status="idle", running=None)

    # ------------------------------------------------------------- lifecycle
    def start(self) -> "FakeOpenCode":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.handle(self, "GET")

            def do_POST(self):
                fake.handle(self, "POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self._heartbeats, daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.close_streams()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    def _heartbeats(self):
        while not self._stop.wait(self.heartbeat):
            self.emit("server.heartbeat", {})

    def _save(self):
        if self.store:
            self.store.write_text(json.dumps(self.sessions))

    # ------------------------------------------------------------- events
    def emit(self, kind: str, props: dict) -> None:
        data = json.dumps({"id": mint("evt"), "type": kind, "properties": props})
        with self.lock:
            if self.stalled:
                return
            clients = list(self.clients)
        for c in clients:
            c.put(data)

    def close_streams(self) -> None:
        with self.lock:
            clients, self.clients = list(self.clients), []
        for c in clients:
            c.put(None)

    # ------------------------------------------------------------- the http side
    def authorized(self, handler) -> bool:
        if not self.password:
            return True
        want = base64.b64encode(f"opencode:{self.password}".encode()).decode()
        return handler.headers.get("authorization") == f"Basic {want}"

    def reply(self, handler, status: int, body=None, raw: str | None = None):
        data = (raw if raw is not None else
                ("" if body is None else json.dumps(body))).encode()
        handler.send_response(status)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def handle(self, handler, method: str):
        path = handler.path.split("?", 1)[0]
        length = int(handler.headers.get("content-length") or 0)
        raw = handler.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        with self.lock:
            self.requests.append((method, path, body))
        if path.startswith("/__fake/"):
            return self.reply(handler, 200, self.control(path[len("/__fake/"):], body or {}))
        if not self.authorized(handler):
            return self.reply(handler, 401)
        if method == "GET" and path == "/event":
            return self.serve_events(handler)
        if method == "GET" and path == "/global/health":
            return self.reply(handler, 200, {"healthy": True, "version": VERSION})
        if method == "GET" and path == "/session/status":
            with self.lock:
                if self.status_answers:
                    status, text = self.status_answers.pop(0)
                    return self.reply(handler, status, raw=text)
                busy = {sid: {"type": s["status"]} for sid, s in self.sessions.items()
                        if s["status"] != "idle"}
            return self.reply(handler, 200, busy)
        if method == "POST" and path == "/session":
            return self.reply(handler, 200, self.create_session((body or {}).get("title")))
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "session":
            sid = parts[1]
            with self.lock:
                session = self.sessions.get(sid)
            if session is None:
                return self.reply(handler, 404, {"name": "NotFoundError", "data": {
                    "message": f"Session not found: {sid}"}})
            if method == "GET" and parts[2:] == ["message"]:
                with self.lock:
                    return self.reply(handler, 200, list(session["messages"]))
            if method == "GET" and parts[2:] == ["children"]:
                with self.lock:
                    kids = [{"id": k, "parentID": sid} for k, s in self.sessions.items()
                            if s.get("parent") == sid]
                return self.reply(handler, 200, kids)
            if method == "POST" and parts[2:] == ["prompt_async"]:
                with self.lock:
                    answer = self.post_answers.pop(0) if self.post_answers else None
                mid = (body or {}).get("messageID")
                if answer is not None and answer[0] == "drop":
                    if answer[1]:
                        self.prompt(sid, mid, body)
                    handler.close_connection = True
                    handler.connection.close()
                    return None
                if answer is not None:
                    return self.reply(handler, answer[0], answer[1])
                if not mid or not str(mid).startswith("msg"):
                    return self.reply(handler, 400, {"name": "BadRequest", "data": {
                        "message": "Expected a string starting with \"msg\""}})
                self.prompt(sid, mid, body)
                return self.reply(handler, 204)
        return self.reply(handler, 404, {"name": "NotFoundError", "data": {
            "message": f"no route {method} {path}"}})

    def serve_events(self, handler):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("cache-control", "no-cache")
        handler.end_headers()
        try:
            handler.wfile.write(("data: " + json.dumps(
                {"id": mint("evt"), "type": "server.connected", "properties": {}})
                + "\n\n").encode())
            handler.wfile.flush()
            while True:
                item = q.get()
                if item is None:
                    break
                handler.wfile.write(f"data: {item}\n\n".encode())
                handler.wfile.flush()
        except OSError:
            pass
        finally:
            with self.lock:
                if q in self.clients:
                    self.clients.remove(q)
            handler.close_connection = True

    # ------------------------------------------------------------- the session side
    def create_session(self, title=None, parent=None) -> dict:
        sid = mint("ses")
        with self.lock:
            self.sessions[sid] = {"title": title or "New session", "messages": [],
                                  "status": "idle", "running": None, "parent": parent}
            self._save()
        info = {"id": sid, **({"parentID": parent} if parent else {})}
        self.emit("session.created", {"sessionID": sid, "info": info})
        return {"id": sid, "title": self.sessions[sid]["title"]}

    def _message(self, sid, info, text=""):
        with self.lock:
            msgs = self.sessions[sid]["messages"]
            for m in msgs:
                if m["info"]["id"] == info["id"]:
                    m["info"] = dict(info)
                    break
            else:
                msgs.append({"info": dict(info),
                             "parts": [{"type": "text", "text": text}] if text else []})
            self._save()
        self.emit("message.updated", {"sessionID": sid, "info": dict(info)})

    def set_status(self, sid, status):
        with self.lock:
            self.sessions[sid]["status"] = status
            self._save()
        self.emit("session.status", {"sessionID": sid, "status": {"type": status}})

    def prompt(self, sid, mid, body=None, *, text=None):
        """Admit a prompt: its user message, busy, and an assistant message parented to
        it. A running turn is steered: its reply ends at `finish=tool-calls`."""
        text = text if text is not None else " ".join(
            p.get("text", "") for p in (body or {}).get("parts") or [])
        now = int(time.time() * 1000)
        with self.lock:
            running = self.sessions[sid]["running"]
        self._message(sid, {"id": mid, "role": "user", "sessionID": sid,
                            "time": {"created": now}}, text)
        if running is not None:
            self._message(sid, dict(running, finish="tool-calls",
                                    time={"created": running["time"]["created"],
                                          "completed": now}))
        else:
            self.set_status(sid, "busy")
        if not self.reply_to_prompts:
            return
        assistant = {"id": mint("msg"), "role": "assistant", "sessionID": sid,
                     "parentID": mid, "time": {"created": int(time.time() * 1000)}}
        with self.lock:
            self.sessions[sid]["running"] = assistant
        self._message(sid, assistant)
        if self.auto:
            threading.Timer(self.finish_after, self.finish, args=(sid,)).start()

    def finish(self, sid, finish="stop", error=None, idle=True):
        """End the running turn: its assistant message completes (sent twice, as the
        real server does), then idle."""
        with self.lock:
            session = self.sessions[sid]
            running, session["running"] = session["running"], None
        if running is not None:
            done = dict(running, finish=finish,
                        time={"created": running["time"]["created"],
                              "completed": int(time.time() * 1000)})
            if error:
                done["error"] = error
            self._message(sid, done)
            self.emit("message.updated", {"sessionID": sid, "info": done})
        if idle:
            self.go_idle(sid)

    def go_idle(self, sid):
        self.set_status(sid, "idle")
        self.emit("session.idle", {"sessionID": sid})

    def abort(self, sid):
        """Esc: the running turn's assistant message ends with `MessageAbortedError`."""
        self.finish(sid, finish=None, error={"name": "MessageAbortedError"})

    def error(self, sid, message="boom"):
        self.emit("session.error", {"sessionID": sid, "error": {
            "name": "UnknownError", "data": {"message": message}}})
        self.finish(sid, finish=None, error={"name": "UnknownError"})

    def compacted(self, sid):
        self.emit("session.compacted", {"sessionID": sid})

    def type(self, sid, text="typed"):
        """A prompt of the person's own (a window's)."""
        self.prompt(sid, mint("msg"), text=text)

    # ------------------------------------------------------------- control
    def control(self, op: str, body: dict) -> dict:
        sid = body.get("session")
        if op == "finish":
            self.finish(sid, finish=body.get("finish", "stop"))
        elif op == "type":
            self.type(sid, body.get("text", "typed"))
        elif op == "sessions":
            with self.lock:
                return {"sessions": {k: {"status": v["status"],
                                         "messages": [m["info"] for m in v["messages"]]}
                                     for k, v in self.sessions.items()}}
        return {"ok": True}


# --------------------------------------------------------------------------- as a binary

def event(kind: str, **fields) -> None:
    value = os.environ.get("FAKE_OPENCODE_DIR")
    if not value:
        return
    directory = Path(value)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "events.jsonl").open("a") as f:
        f.write(json.dumps({"event": kind, "pid": os.getpid(), "t": time.time(),
                            **fields}) + "\n")


def serve(argv) -> int:
    config = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "{}")
    value = os.environ.get("FAKE_OPENCODE_DIR")
    fake = FakeOpenCode(password=os.environ.get("OPENCODE_SERVER_PASSWORD"),
                        auto=os.environ.get("FAKE_OPENCODE_AUTO", "1") != "0",
                        store=(Path(value) / "sessions.json") if value else None)
    fake.start()
    event("serve", argv=argv, url=fake.url, config=config, cwd=os.getcwd(),
          password=bool(os.environ.get("OPENCODE_SERVER_PASSWORD")))
    sys.stdout.write(f"timestamp=0 level=INFO message=\"opencode server listening on "
                     f"{fake.url}\"\n")
    sys.stdout.flush()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    return 0


def main(argv) -> int:
    if "--version" in argv:
        print(VERSION)
        return 0
    if argv[:1] == ["serve"]:
        return serve(argv)
    print(f"fake opencode: unknown arguments {argv}", file=sys.stderr)
    return 2


def install(directory: Path, name: str = "opencode") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).resolve()} \"$@\"\n")
    script.chmod(0o755)
    return script


def control(url: str, op: str, **body) -> dict:
    import urllib.request

    req = urllib.request.Request(f"{url}/__fake/{op}", data=json.dumps(body).encode(),
                                 method="POST", headers={"content-type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=5).read() or b"{}")


def events(directory: Path) -> list:
    path = directory / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
