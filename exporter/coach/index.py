"""In-memory index of tool calls and compactions, read incrementally from the `part` table.

`part` holds every tool output and is slow to scan through a Docker bind mount on Windows,
so it is read once and afterwards only for parts newer than the last one seen, like the
exporter's skill scan. Only metadata is kept: the tool, its status, a class for its error
and a short hash of its arguments (to spot repeated calls). The error text and the
arguments themselves are discarded.
"""
import hashlib
import threading

from . import errors

TOOL_PARTS = """
select p.id, p.session_id, p.message_id, p.time_created,
       json_extract(p.data,'$.type'),
       json_extract(p.data,'$.tool'),
       json_extract(p.data,'$.state.status'),
       substr(json_extract(p.data,'$.state.error'), 1, 400),
       json_extract(p.data,'$.state.input'),
       json_extract(p.data,'$.state.input.name')
from part p
where json_extract(p.data,'$.type') in ('tool','compaction')
"""


class ToolCall:
    __slots__ = ("id", "session", "message", "time", "tool", "status", "error_class", "arg_key", "skill")

    def __init__(self, pid, session, message, time, tool, status, err, args, skill):
        self.id = pid
        self.session = session
        self.message = message
        self.time = time
        self.tool = tool or "?"
        self.status = status or "unknown"
        self.error_class = errors.classify(err) if self.status == "error" else None
        # arguments are only hashed, to recognise the same call repeated; they are not kept
        self.arg_key = hashlib.sha1(((tool or "") + "\0" + (args or "")).encode("utf-8", "replace")).hexdigest()[:12]
        self.skill = skill if tool == "skill" else None


class ToolIndex:
    def __init__(self):
        self.calls = {}         # part id -> ToolCall
        self.compactions = {}   # part id -> (session, message, time)
        self.pending = set()    # calls still running at the last read
        self.last_id = ""
        self.ready = False
        self.lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    def refresh(self, conn):
        """Read the parts added since the last call (all of them the first time)."""
        with self._refresh_lock:
            top = conn.execute("select max(id) from part").fetchone()[0] or ""
            with self.lock:
                last, pending = self.last_id, list(self.pending)
            rows = conn.execute(TOOL_PARTS + " and p.id > ? and p.id <= ?", (last, top)).fetchall()
            for pid in pending:
                rows += conn.execute(TOOL_PARTS + " and p.id = ?", (pid,)).fetchall()
            with self.lock:
                for pid, sess, msg, t, ptype, tool, status, err, args, skill in rows:
                    if ptype == "compaction":
                        self.compactions[pid] = (sess, msg, t)
                        continue
                    call = ToolCall(pid, sess, msg, t, tool, status, err, args, skill)
                    self.calls[pid] = call
                    if call.status in ("pending", "running"):
                        self.pending.add(pid)
                    else:
                        self.pending.discard(pid)
                self.last_id = top
                self.ready = True

    def snapshot(self):
        with self.lock:
            return list(self.calls.values()), list(self.compactions.values())


INDEX = ToolIndex()
