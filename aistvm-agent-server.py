#!/usr/bin/env python3
# AISTVM agent server. Standard library only.
# Drives the coding agent headlessly and streams its activity to the app.
import hmac
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("AISTVM_AGENT_PORT", "8791"))
TOKEN = os.environ.get("AISTVM_AGENT_TOKEN", "")
AGENT_USER = os.environ.get("AISTVM_AGENT_USER", "")
WORKSPACE = os.environ.get("AISTVM_AGENT_WORKSPACE", "/tmp/aistvm-agent")
MODEL = os.environ.get("AISTVM_AGENT_MODEL", "opencode/deepseek-v4.1-flash-free")
OPENCODE_BIN = os.environ.get("AISTVM_OPENCODE_BIN", "opencode")
PROMPT_FILE = os.environ.get("AISTVM_AGENT_PROMPT", "")
TURN_TIMEOUT = float(os.environ.get("AISTVM_AGENT_TURN_TIMEOUT", "1800"))
EVENTS_TIMEOUT = 25.0
MAX_EVENTS = 2000
MAX_INPUT = 16000
WATCH_INTERVAL = 1.0

# Signals that the free allowance is used up and the provider now wants payment.
LIMIT_PATTERNS = [
    r"quota", r"rate limit", r"rate-limit", r"too many requests",
    r"insufficient", r"payment required", r"upgrade", r"billing",
    r"credits", r"usage limit", r"limit reached", r"402", r"429",
]

STATE_LOCK = threading.Lock()
EVENT_LOCK = threading.Lock()
EVENTS = []
CURSOR = 0
STATE = {
    "status": "idle",          # idle | running | limited | error
    "messages": 0,
    "limited": False,
    "detail": "",
}
HISTORY = []
WORKER = {"thread": None}


def _now():
    return time.time()


def _emit(kind, text, **extra):
    """Append one event and wake long-poll readers. Offsets are monotonic."""
    global CURSOR
    with EVENT_LOCK:
        CURSOR += 1
        event = {"offset": CURSOR, "at": _now(), "kind": kind, "text": text}
        event.update(extra)
        EVENTS.append(event)
        if len(EVENTS) > MAX_EVENTS:
            del EVENTS[: len(EVENTS) - MAX_EVENTS]
    return event


def _set_state(**changes):
    with STATE_LOCK:
        STATE.update(changes)


def _state():
    with STATE_LOCK:
        return dict(STATE)


def _looks_like_limit(text):
    low = text.lower()
    return any(re.search(pattern, low) for pattern in LIMIT_PATTERNS)


def workspace_root():
    return os.path.realpath(WORKSPACE)


def safe_path(relative):
    """Resolve a client path inside the workspace; reject anything outside."""
    root = workspace_root()
    candidate = (relative or "").strip()
    while candidate.startswith("/"):
        candidate = candidate[1:]
    target = os.path.realpath(os.path.join(root, candidate))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError("duong dan nam ngoai workspace")
    return target


def _owner_ids():
    if not AGENT_USER:
        return None
    try:
        import pwd

        record = pwd.getpwnam(AGENT_USER)
        return record.pw_uid, record.pw_gid
    except (ImportError, KeyError):
        return None


def _heal_entry(path, uid, gid):
    """Give an entry back to the session user with owner-only access bits."""
    if os.path.islink(path):
        return False
    try:
        info = os.lstat(path)
    except OSError:
        return False
    changed = False
    if info.st_uid != uid or info.st_gid != gid:
        try:
            os.chown(path, uid, gid)
            changed = True
        except OSError:
            pass
    wanted = info.st_mode | (0o700 if os.path.isdir(path) else 0o600)
    if wanted != info.st_mode:
        try:
            os.chmod(path, wanted)
            changed = True
        except OSError:
            pass
    return changed


def _apply_owner(target):
    ids = _owner_ids()
    if not ids:
        return
    uid, gid = ids
    root = workspace_root()
    current = target
    while True:
        _heal_entry(current, uid, gid)
        if current == root:
            break
        parent = os.path.dirname(current)
        if parent == current or not parent.startswith(root):
            break
        current = parent


def _snapshot():
    """Map of workspace-relative path -> (size, mtime) for change detection."""
    root = workspace_root()
    result = {}
    for base, directories, files in os.walk(root, followlinks=False):
        directories[:] = [d for d in directories if d != ".git"]
        for name in files:
            full = os.path.join(base, name)
            try:
                info = os.stat(full)
            except OSError:
                continue
            result[os.path.relpath(full, root)] = (int(info.st_size), int(info.st_mtime))
    return result


def _watch_workspace(stop_event, baseline):
    """Emit a file event whenever the agent creates or edits a file."""
    previous = dict(baseline)
    while not stop_event.is_set():
        stop_event.wait(WATCH_INTERVAL)
        try:
            current = _snapshot()
        except OSError:
            continue
        for path, meta in current.items():
            if path not in previous:
                _emit("file.create", path, path=path, size=meta[0])
            elif previous[path] != meta:
                _emit("file.change", path, path=path, size=meta[0])
        for path in previous:
            if path not in current:
                _emit("file.delete", path, path=path)
        previous = current


def _read_prompt():
    if PROMPT_FILE and os.path.isfile(PROMPT_FILE):
        try:
            with open(PROMPT_FILE, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return ""
    return ""


def _write_config():
    """Point the runtime at the system prompt, the free model and full permissions.

    The prompt is only advisory unless the runtime is told to read it, so the
    project config names the prompt file and allows the tools the agent needs
    (edit, shell, fetch). Without this the agent would ignore its instructions
    and stall on the first permission prompt.
    """
    if not _read_prompt():
        return False
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": MODEL,
        "instructions": [PROMPT_FILE],
        "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow"},
    }
    try:
        with open(os.path.join(WORKSPACE, "opencode.json"), "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
    except OSError:
        return False
    return True


def _agent_env():
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    env.setdefault("CI", "1")
    env.setdefault("HOME", WORKSPACE)
    # The workspace is the project the agent works in.
    env["AISTVM_WORKSPACE"] = WORKSPACE
    return env


def _run_turn(user_text):
    """Run one agent turn, streaming activity into the event log."""
    _set_state(status="running", detail="")
    _emit("info", "AISTVM Agent đang xử lý yêu cầu.")
    stop_event = threading.Event()
    baseline = {}
    try:
        baseline = _snapshot()
    except OSError:
        baseline = {}
    watcher = threading.Thread(
        target=_watch_workspace, args=(stop_event, baseline), daemon=True
    )
    watcher.start()

    command = [OPENCODE_BIN, "run", "--model", MODEL, user_text]
    output_tail = []
    limit_hit = False
    try:
        process = subprocess.Popen(
            command,
            cwd=WORKSPACE,
            env=_agent_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        stop_event.set()
        _emit("error", "Không tìm thấy công cụ AI trên máy. Hãy tạo lại máy AI.")
        _set_state(status="error", detail="missing-runtime")
        return
    except OSError as exc:
        stop_event.set()
        _emit("error", "Không khởi chạy được công cụ AI: " + str(exc))
        _set_state(status="error", detail="spawn-failed")
        return

    deadline = _now() + TURN_TIMEOUT
    try:
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            output_tail.append(line)
            del output_tail[:-40]
            if _looks_like_limit(line):
                limit_hit = True
                _emit("limit", line.strip()[:500])
            else:
                _emit("tool", line.strip()[:1000])
            if _now() > deadline:
                process.kill()
                _emit("error", "Lượt xử lý vượt quá thời gian cho phép.")
                break
        process.wait(timeout=30)
    except Exception as exc:  # noqa: BLE001 - keep the server alive
        _emit("error", "Lỗi khi chạy công cụ AI: " + str(exc))
        try:
            process.kill()
        except OSError:
            pass
    finally:
        stop_event.set()
        watcher.join(timeout=2)

    if limit_hit:
        _set_state(status="limited", limited=True, detail="free-limit")
        _emit(
            "limit",
            "Đã đạt giới hạn miễn phí của AISTVM Agent. Hãy xoá máy bot này và tạo máy bot mới để tiếp tục.",
        )
        return

    summary = "\n".join(output_tail[-20:]).strip()
    if summary:
        _emit("assistant", summary)
    _emit("done", "AISTVM Agent đã xử lý xong yêu cầu.")
    _set_state(status="idle", detail="")


def _worker(user_text):
    try:
        _run_turn(user_text)
    except Exception as exc:  # noqa: BLE001 - never kill the server thread
        _emit("error", "AISTVM Agent gặp lỗi không mong đợi: " + str(exc))
        _set_state(status="error", detail="unexpected")
    finally:
        WORKER["thread"] = None


def start_turn(user_text):
    with STATE_LOCK:
        if STATE["status"] == "running":
            return False, "AISTVM Agent đang xử lý lượt trước."
        if STATE["limited"]:
            return False, "Máy bot đã đạt giới hạn miễn phí."
    HISTORY.append({"role": "user", "text": user_text, "at": _now()})
    _emit("user", user_text)
    _set_state(messages=len(HISTORY))
    thread = threading.Thread(target=_worker, args=(user_text,), daemon=True)
    WORKER["thread"] = thread
    thread.start()
    return True, ""


def reset_conversation():
    with STATE_LOCK:
        if STATE["status"] == "running":
            return False, "Không thể làm mới khi AISTVM Agent đang xử lý."
    HISTORY.clear()
    with EVENT_LOCK:
        EVENTS.clear()
    global CURSOR
    with EVENT_LOCK:
        CURSOR = 0
    _set_state(messages=0, status="idle", limited=False, detail="")
    _emit("info", "Đã bắt đầu cuộc trò chuyện mới.")
    return True, ""


def list_files():
    root = workspace_root()
    entries = []
    for base, directories, files in os.walk(root, followlinks=False):
        directories[:] = [d for d in directories if d != ".git"]
        for name in files:
            full = os.path.join(base, name)
            try:
                info = os.stat(full)
            except OSError:
                continue
            entries.append(
                {
                    "path": os.path.relpath(full, root),
                    "size": int(info.st_size),
                    "mtime": int(info.st_mtime),
                }
            )
    entries.sort(key=lambda item: item["path"].lower())
    return {"entries": entries[:2000]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AISTVM-Agent/1.0"

    def log_message(self, *args):  # keep the log quiet; never print secrets
        pass

    def _authorized(self):
        if not TOKEN:
            return False
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip(), TOKEN)

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > MAX_INPUT * 4:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, {"ok": True})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        if path == "/agent/state":
            self._send(200, _state())
            return
        if path == "/agent/files":
            self._send(200, list_files())
            return
        if path == "/agent/events":
            offset = 0
            if "?" in self.path:
                query = self.path.split("?", 1)[1]
                for pair in query.split("&"):
                    if pair.startswith("offset="):
                        try:
                            offset = int(pair[7:])
                        except ValueError:
                            offset = 0
            self._stream_events(offset)
            return
        self._send(404, {"error": "not found"})

    def _stream_events(self, offset):
        deadline = _now() + EVENTS_TIMEOUT
        while True:
            with EVENT_LOCK:
                pending = [event for event in EVENTS if event["offset"] > offset]
                cursor = CURSOR
            if pending or _now() >= deadline:
                self._send(200, {"events": pending, "cursor": cursor})
                return
            time.sleep(0.25)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        if path == "/agent/chat":
            body = self._read_json()
            text = body.get("text") if isinstance(body, dict) else None
            if not isinstance(text, str) or not text.strip():
                self._send(400, {"error": "invalid text"})
                return
            if len(text) > MAX_INPUT:
                self._send(400, {"error": "text too long"})
                return
            accepted, reason = start_turn(text.strip())
            self._send(200 if accepted else 409, {"accepted": accepted, "reason": reason})
            return
        if path == "/agent/reset":
            accepted, reason = reset_conversation()
            self._send(200 if accepted else 409, {"accepted": accepted, "reason": reason})
            return
        self._send(404, {"error": "not found"})


def main():
    os.makedirs(WORKSPACE, exist_ok=True)
    _apply_owner(WORKSPACE)
    _write_config()
    _emit("info", "Máy AI đã sẵn sàng. Hãy gửi yêu cầu đầu tiên.")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("AISTVM agent server listening on port " + str(PORT), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
