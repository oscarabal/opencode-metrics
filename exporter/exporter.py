"""Prometheus exporter for OpenCode usage.

Reads OpenCode's SQLite database (read-only) and publishes cost and token
totals. Only the `message`, `session` and `project` tables are queried. The
context limit of each model comes from OpenCode's models.dev cache.
"""
import calendar
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import report_page
import session_page
from coach import index as coach_index, metrics as coach_metrics, render as coach_render, report as coach_report

DB = os.environ.get("OPENCODE_DB", "/data/opencode.db")
MODELS = os.environ.get("OPENCODE_MODELS", "/cache/models.json")
COMMANDS_DIR = os.environ.get("OPENCODE_COMMANDS_DIR", "/commands")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3001/d/opencode-usage")
PORT = int(os.environ.get("PORT", "9101"))
WEEKS = int(os.environ.get("WEEKS", "13"))
CONTEXT_SESSIONS = int(os.environ.get("CONTEXT_SESSIONS", "5"))
# "provider/model=tokens,..." for models the cache does not know (custom providers, Copilot aliases)
CONTEXT_LIMITS = os.environ.get("CONTEXT_LIMITS", "")

TOKEN_TYPES = {
    "input": "$.tokens.input",
    "output": "$.tokens.output",
    "reasoning": "$.tokens.reasoning",
    "cache_read": "$.tokens.cache.read",
    "cache_write": "$.tokens.cache.write",
}

_SUMS = ", ".join(
    f"coalesce(sum(json_extract(m.data,'{p}')),0)" for p in TOKEN_TYPES.values()
)
_TOTAL = "(" + " + ".join(
    f"coalesce(sum(json_extract(m.data,'{p}')),0)" for p in TOKEN_TYPES.values()
) + ")"

# worktree "/" is OpenCode's "no project" placeholder
TOTALS = f"""
select coalesce(json_extract(m.data,'$.providerID'),'unknown'),
       coalesce(json_extract(m.data,'$.modelID'),'unknown'),
       coalesce(json_extract(m.data,'$.agent'),'unknown'),
       coalesce(p.worktree,''),
       count(*),
       coalesce(sum(json_extract(m.data,'$.cost')),0),
       {_SUMS}
from message m
join session s on s.id = m.session_id
left join project p on p.id = s.project_id
where json_extract(m.data,'$.role') = 'assistant'
group by 1,2,3,4
"""

# One row per UTC day and label combination, over all history. The dashboard
# picks a date range from these; days are UTC.
PER_DAY = f"""
select date(m.time_created/1000,'unixepoch'),
       coalesce(json_extract(m.data,'$.providerID'),'unknown'),
       coalesce(json_extract(m.data,'$.modelID'),'unknown'),
       coalesce(json_extract(m.data,'$.agent'),'unknown'),
       coalesce(p.worktree,''),
       count(*),
       coalesce(sum(json_extract(m.data,'$.cost')),0),
       {_SUMS}
from message m
join session s on s.id = m.session_id
left join project p on p.id = s.project_id
where json_extract(m.data,'$.role') = 'assistant'
group by 1,2,3,4,5
"""

SESSIONS_PER_DAY = """
select date(s.time_created/1000,'unixepoch'), coalesce(p.worktree,''), count(*)
from session s left join project p on p.id = s.project_id
where s.parent_id is null
group by 1,2
"""

# Monday of the ISO week, so the dashboard can label weeks by a plain date
WEEKLY = f"""
select date(m.time_created/1000,'unixepoch','weekday 0','-6 days'),
       coalesce(p.worktree,''),
       coalesce(sum(json_extract(m.data,'$.cost')),0),
       {_TOTAL}
from message m
join session s on s.id = m.session_id
left join project p on p.id = s.project_id
where json_extract(m.data,'$.role') = 'assistant'
  and m.time_created >= ?
group by 1,2
"""


_limits = {"mtime": None, "data": {}}


def model_limits():
    """Context limits from the models.dev cache, reloaded only when the file changes."""
    try:
        mtime = os.path.getmtime(MODELS)
    except OSError:
        return {}
    if _limits["mtime"] != mtime:
        with open(MODELS, encoding="utf-8") as f:
            raw = json.load(f)
        data = {}
        for prov, pdata in raw.items():
            for mid, mdata in (pdata.get("models") or {}).items():
                ctx = (mdata.get("limit") or {}).get("context")
                if ctx:
                    data[(prov, mid)] = int(ctx)
        _limits.update(mtime=mtime, data=data)
    return _limits["data"]


def context_limit(provider, model):
    overrides = {}
    for item in filter(None, (x.strip() for x in CONTEXT_LIMITS.split(","))):
        key, _, val = item.partition("=")
        if val.strip().isdigit():
            overrides[key.strip()] = int(val)
    limits = model_limits()
    # Copilot spells versions with dots and models.dev with hyphens, or the other way round
    for mid in (model, model.replace(".", "-"), model.replace("-", ".")):
        key = provider + "/" + mid
        if key in overrides:
            return overrides[key]
        if (provider, mid) in limits:
            return limits[(provider, mid)]
    return None


QUOTA_DIR = os.environ.get("OPENCODE_QUOTA_DIR", "/cache/quota-provider-state")
# The opencode-quota plugin names providers differently from OpenCode's own provider ids
QUOTA_PROVIDER_ALIASES = {"copilot": "github-copilot"}
_SCALE = {"": 1, "k": 1_000, "m": 1_000_000}


def _number(text, suffix):
    return float(text.replace(",", "")) * _SCALE[suffix.lower()]


def parse_amounts(right):
    """'22/7000' or '1.5k/5k' -> (22.0, 7000.0); None when the plugin shows something else."""
    m = re.match(r"^\s*([\d.,]+)\s*([kKmM]?)\s*/\s*([\d.,]+)\s*([kKmM]?)\s*$", right or "")
    if not m:
        return None
    try:
        return _number(m.group(1), m.group(2)), _number(m.group(3), m.group(4))
    except ValueError:
        return None


def read_quotas():
    """Quota of each provider, from the state files the opencode-quota plugin caches.

    A provider that reports several quotas is reduced to the one with the least left.
    """
    quotas, errors = {}, {}
    if not os.path.isdir(QUOTA_DIR):
        return quotas, errors
    for name in sorted(os.listdir(QUOTA_DIR)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(QUOTA_DIR, name), encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            continue
        provider = QUOTA_PROVIDER_ALIASES.get(state.get("providerId", ""), state.get("providerId", ""))
        if not provider:
            continue
        updated = (state.get("timestamp") or 0) / 1000
        result = state.get("result") or {}
        for entry in result.get("entries") or []:
            remaining = entry.get("percentRemaining")
            if remaining is None:
                continue
            current = quotas.get(provider)
            if current is None or remaining < current["remaining"]:
                reset = entry.get("resetTimeIso")
                try:
                    reset_ts = calendar.timegm(time.strptime(reset[:19], "%Y-%m-%dT%H:%M:%S")) if reset else None
                except ValueError:
                    reset_ts = None
                quotas[provider] = {"remaining": remaining, "amounts": parse_amounts(entry.get("right")),
                                    "reset": reset_ts, "updated": updated}
        for err in result.get("errors") or []:
            if provider not in quotas:
                errors[provider] = str(err.get("message") or "error")
    return quotas, errors


def human(n):
    if n >= 1_000_000:
        return ("%.2f" % (n / 1_000_000)).rstrip("0").rstrip(".") + "M"
    return "%dk" % round(n / 1000)


SKILL_CALLS = """
select p.id,
       date(p.time_created/1000,'unixepoch'),
       json_extract(p.data,'$.state.input.name'),
       json_extract(p.data,'$.state.status'),
       json_extract(m.data,'$.parentID'),
       coalesce(pr.worktree,''),
       coalesce(json_extract(m.data,'$.providerID'),'unknown'),
       coalesce(json_extract(m.data,'$.modelID'),'unknown'),
       coalesce(json_extract(m.data,'$.agent'),'unknown')
from part p
join message m on m.id = p.message_id
join session s on s.id = p.session_id
left join project pr on pr.id = s.project_id
where json_extract(p.data,'$.type') = 'tool' and json_extract(p.data,'$.tool') = 'skill'
"""

# A turn is every assistant message that answers the same user message
TURN_COST = """
select json_extract(data,'$.parentID'),
       coalesce(json_extract(data,'$.providerID'),'unknown'),
       coalesce(json_extract(data,'$.modelID'),'unknown'),
       coalesce(json_extract(data,'$.agent'),'unknown'),
       coalesce(sum(json_extract(data,'$.cost')),0)
from message
where json_extract(data,'$.role') = 'assistant'
group by 1,2,3,4
"""


# A custom command leaves no mark in the database: OpenCode stores its template, already
# expanded, as the text of the user message. It is recognised by that template's opening.
USER_TEXTS = """
select p.id,
       date(p.time_created/1000,'unixepoch'),
       substr(json_extract(p.data,'$.text'), 1, ?),
       p.message_id,
       coalesce(pr.worktree,'')
from part p
join message m on m.id = p.message_id
join session s on s.id = p.session_id
left join project pr on pr.id = s.project_id
where json_extract(m.data,'$.role') = 'user' and json_extract(p.data,'$.type') = 'text'
  and p.id > ? and p.id <= ?
"""

_commands = {"stamp": None, "templates": {}}


def command_templates():
    """The opening of each custom command's template, up to its first $ARGUMENTS."""
    try:
        names = [n for n in os.listdir(COMMANDS_DIR) if n.endswith(".md")]
        stamp = tuple(sorted((n, os.path.getmtime(os.path.join(COMMANDS_DIR, n))) for n in names))
    except OSError:
        return {}
    if _commands["stamp"] != stamp:
        found = {}
        for n in names:
            try:
                with open(os.path.join(COMMANDS_DIR, n), encoding="utf-8") as f:
                    body = re.sub(r"^---.*?---\s*", "", f.read(), count=1, flags=re.S).strip()
            except OSError:
                continue
            cut = body.find("$ARGUMENTS")
            opening = (body[:cut] if cut > 0 else body).strip()
            if len(opening) >= 10:
                found[n[:-3]] = opening
        _commands.update(stamp=stamp, templates=found)
    return _commands["templates"]


def match_command(text, templates):
    """The command whose template opens this text; the longest opening wins, a tie matches none."""
    text = text.lstrip()
    hits = sorted(((len(o), n) for n, o in templates.items() if text.startswith(o)), reverse=True)
    if not hits or (len(hits) > 1 and hits[0][0] == hits[1][0]):
        return None
    return hits[0][1]


# Finding skill calls means reading the `part` table, which holds every tool output and is
# slow to scan through a Docker bind mount. It is scanned once in the background and then
# only for parts newer than the last one seen, so a scrape never waits for it.
_skills = {"calls": {}, "pending": set(), "commands": {}, "last_id": "", "done": False}
_skills_lock = threading.Lock()


def refresh_skill_calls():
    c = connect()
    try:
        top = c.execute("select max(id) from part").fetchone()[0] or ""
        with _skills_lock:
            last, pending = _skills["last_id"], list(_skills["pending"])
        rows = c.execute(SKILL_CALLS + " and p.id > ? and p.id <= ?", (last, top)).fetchall()
        for pid in pending:  # calls that were still running the last time
            rows += c.execute(SKILL_CALLS + " and p.id = ?", (pid,)).fetchall()
        templates = command_templates()
        texts = []
        if templates:
            width = max(len(o) for o in templates.values()) + 1
            texts = c.execute(USER_TEXTS, (width, last, top)).fetchall()
    finally:
        c.close()
    with _skills_lock:
        for pid, day, text, message_id, wt in texts:
            name = match_command(text or "", templates)
            if name and message_id not in _skills["commands"]:
                _skills["commands"][message_id] = (day, name, wt)
        for pid, day, name, status, parent, wt, prov, model, agent in rows:
            if not name:
                continue
            _skills["calls"][pid] = (day, name, status, parent, wt, prov, model, agent)
            if status in ("pending", "running"):
                _skills["pending"].add(pid)
            else:
                _skills["pending"].discard(pid)
        _skills["last_id"], _skills["done"] = top, True


def skill_loop():
    woken = False
    while True:
        try:
            refresh_skill_calls()
        except Exception:  # the next round retries; scrapes keep serving what is cached
            pass
        try:  # keep the improvement report's tool index warm, so /report.md answers quickly
            c = connect()
            try:
                coach_index.INDEX.refresh(c)
                # series for the «Mejoras» row and the data its findings panels build the report from,
                # cached so neither a scrape nor a panel reads the database
                coach_metrics.refresh(c, sys.modules[__name__], WEEKS, force=woken)
            finally:
                c.close()
        except Exception:
            pass
        woken = coach_metrics.wait(30)


def skill_usage(c):
    """Activations and cost per skill and per custom command, in total and by model and agent.

    The cost of a skill or command cannot be isolated, so it is the cost of the whole turn
    (every assistant message answering one user message) that loaded or ran it. When one
    turn loads several skills the cost is split equally among them. The by-model figures
    split that same cost by the model and agent that answered; an activation is credited to
    the model and agent of the message that loaded the skill, or, for a command, to the one
    that spent most in the turn it launched.
    """
    turns = {}  # user message -> {(provider, model, agent): cost}
    for parent, prov, model, agent, cost in c.execute(TURN_COST):
        turns.setdefault(parent, {})[(prov, model, agent)] = cost
    turn_cost = {parent: sum(parts.values()) for parent, parts in turns.items()}
    with _skills_lock:
        calls = list(_skills["calls"].values())
        commands = list(_skills["commands"].items())
    activations, cost, acts_by, cost_by = {}, {}, {}, {}
    pending_turns = {}
    for day, name, status, parent, wt, prov, model, agent in calls:
        if status in ("pending", "running"):
            continue
        proj = project_name(wt)
        result = "ok" if status == "completed" else "error"
        key = (day, "skill", name, proj, result)
        activations[key] = activations.get(key, 0) + 1
        dkey = (day, "skill", name, proj, prov, model, agent, result)
        acts_by[dkey] = acts_by.get(dkey, 0) + 1
        if result == "ok" and parent:
            pending_turns.setdefault(parent, (day, proj, set()))[2].add(name)
    for parent, (day, proj, names) in pending_turns.items():
        share = turn_cost.get(parent, 0) / len(names)
        for name in names:
            key = (day, "skill", name, proj)
            cost[key] = cost.get(key, 0) + share
            for (prov, model, agent), part in turns.get(parent, {}).items():
                dkey = (day, "skill", name, proj, prov, model, agent)
                cost_by[dkey] = cost_by.get(dkey, 0) + part / len(names)
    for message_id, (day, name, wt) in commands:
        proj = project_name(wt)
        key = (day, "command", name, proj, "ok")
        activations[key] = activations.get(key, 0) + 1
        ckey = (day, "command", name, proj)
        cost[ckey] = cost.get(ckey, 0) + turn_cost.get(message_id, 0)
        parts = turns.get(message_id, {})
        main = max(parts, key=parts.get) if parts else ("unknown", "unknown", "unknown")
        dkey = (day, "command", name, proj) + main + ("ok",)
        acts_by[dkey] = acts_by.get(dkey, 0) + 1
        for (prov, model, agent), part in parts.items():
            dkey = (day, "command", name, proj, prov, model, agent)
            cost_by[dkey] = cost_by.get(dkey, 0) + part
    return activations, cost, acts_by, cost_by


def project_name(worktree):
    name = os.path.basename(worktree.replace("\\", "/").rstrip("/"))
    return name or "global"


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def connect():
    # mode=ro needs the -shm/-wal files; immutable=1 is the fallback when the
    # database sits on a read-only mount without them (it ignores the WAL).
    for suffix in ("?mode=ro", "?immutable=1"):
        try:
            c = sqlite3.connect("file:" + DB + suffix, uri=True, timeout=5)
            c.execute("select 1 from message limit 1")
            return c
        except sqlite3.Error:
            continue
    raise sqlite3.OperationalError("cannot open " + DB)


RECENT_SESSIONS = """
select s.id, s.title, coalesce(p.worktree,'')
from session s left join project p on p.id = s.project_id
where s.parent_id is null
order by s.time_updated desc
limit ?
"""

LAST_ASSISTANT = """
select json_extract(data,'$.providerID'), json_extract(data,'$.modelID'),
       coalesce(json_extract(data,'$.tokens.input'),0),
       coalesce(json_extract(data,'$.tokens.output'),0) + coalesce(json_extract(data,'$.tokens.reasoning'),0),
       coalesce(json_extract(data,'$.tokens.cache.read'),0),
       coalesce(json_extract(data,'$.tokens.cache.write'),0)
from message
where session_id = ? and json_extract(data,'$.role') = 'assistant'
order by time_created desc
limit 8
"""


def context_usage(c):
    """Context size of the most recent sessions: tokens of their last answered message."""
    rows = []
    for sid, title, wt in c.execute(RECENT_SESSIONS, (CONTEXT_SESSIONS * 4,)):
        for prov, model, inp, out, cr, cw in c.execute(LAST_ASSISTANT, (sid,)):
            if inp + out + cr + cw > 0:  # skip the aborted or in-flight message that carries no tokens
                rows.append((sid, title or "", wt, prov or "unknown", model or "unknown", inp, out, cr, cw))
                break
        if len(rows) == CONTEXT_SESSIONS:
            break
    return rows


def collect():
    out = []
    add = out.append
    started = time.time()
    c = connect()

    def labels(lab):
        return 'provider="%s",model="%s",agent="%s",project="%s"' % tuple(esc(x) for x in lab)

    try:
        # Cumulative totals, kept for anyone querying the counters directly
        cost, msgs, tok = {}, {}, {}
        for prov, model, agent, wt, n, cst, *toks in c.execute(TOTALS):
            lab = (prov, model, agent, project_name(wt))
            cost[lab] = cost.get(lab, 0) + cst
            msgs[lab] = msgs.get(lab, 0) + n
            for t, v in zip(TOKEN_TYPES, toks):
                tok[lab + (t,)] = tok.get(lab + (t,), 0) + v
        add("# TYPE opencode_cost_usd_total counter")
        for lab, v in cost.items():
            add("opencode_cost_usd_total{%s} %s" % (labels(lab), v))
        add("# TYPE opencode_tokens_total counter")
        for lab, v in tok.items():
            add('opencode_tokens_total{%s,type="%s"} %s' % (labels(lab[:4]), lab[4], v))
        add("# TYPE opencode_messages_total counter")
        for lab, v in msgs.items():
            add("opencode_messages_total{%s} %s" % (labels(lab), v))

        # Per day, for the dashboard's date range
        day_cost, day_msgs, day_tok, days = {}, {}, {}, set()
        for day, prov, model, agent, wt, n, cst, *toks in c.execute(PER_DAY):
            lab = (day, prov, model, agent, project_name(wt))
            days.add(day)
            day_cost[lab] = day_cost.get(lab, 0) + cst
            day_msgs[lab] = day_msgs.get(lab, 0) + n
            for t, v in zip(TOKEN_TYPES, toks):
                day_tok[lab + (t,)] = day_tok.get(lab + (t,), 0) + v
        day_sess = {}
        for day, wt, n in c.execute(SESSIONS_PER_DAY):
            days.add(day)
            day_sess[(day, project_name(wt))] = day_sess.get((day, project_name(wt)), 0) + n

        def dl(lab):
            return 'day="%s",%s' % (lab[0], labels(lab[1:5]))

        add("# TYPE opencode_day_cost_usd gauge")
        for lab, v in day_cost.items():
            add("opencode_day_cost_usd{%s} %s" % (dl(lab), v))
        add("# TYPE opencode_day_messages gauge")
        for lab, v in day_msgs.items():
            add("opencode_day_messages{%s} %s" % (dl(lab), v))
        add("# TYPE opencode_day_tokens gauge")
        for lab, v in day_tok.items():
            add('opencode_day_tokens{%s,type="%s"} %s' % (dl(lab[:5]), lab[5], v))
        add("# TYPE opencode_day_sessions gauge")
        for (day, proj), v in day_sess.items():
            add('opencode_day_sessions{day="%s",project="%s"} %s' % (day, esc(proj), v))
        # Start of each UTC day as a sample value: PromQL cannot compare label
        # strings, so the dashboard filters days by comparing this value.
        add("# TYPE opencode_day_start_seconds gauge")
        for day in sorted(days):
            y, mo, d = (int(x) for x in day.split("-"))
            add('opencode_day_start_seconds{day="%s"} %s' % (day, calendar.timegm((y, mo, d, 0, 0, 0))))

        # Skills: activations and the cost of the turns that loaded them
        skill_acts, skill_cost, skill_acts_by, skill_cost_by = skill_usage(c)
        add("# TYPE opencode_day_skill_activations gauge")
        for (day, kind, name, proj, result), n in skill_acts.items():
            add('opencode_day_skill_activations{day="%s",kind="%s",skill="%s",project="%s",result="%s"} %s' % (day, kind, esc(name), esc(proj), result, n))
        add("# TYPE opencode_skill_scan_complete gauge")
        add("opencode_skill_scan_complete %s" % (1 if _skills["done"] else 0))
        add("# TYPE opencode_day_skill_cost_usd gauge")
        for (day, kind, name, proj), v in skill_cost.items():
            add('opencode_day_skill_cost_usd{day="%s",kind="%s",skill="%s",project="%s"} %s' % (day, kind, esc(name), esc(proj), v))
        add("# TYPE opencode_day_skill_detail_activations gauge")
        for (day, kind, name, proj, prov, model, agent, result), n in skill_acts_by.items():
            add('opencode_day_skill_detail_activations{day="%s",kind="%s",skill="%s",project="%s",provider="%s",model="%s",agent="%s",result="%s"} %s'
                % (day, kind, esc(name), esc(proj), esc(prov), esc(model), esc(agent), result, n))
        add("# TYPE opencode_day_skill_detail_cost_usd gauge")
        for (day, kind, name, proj, prov, model, agent), v in skill_cost_by.items():
            add('opencode_day_skill_detail_cost_usd{day="%s",kind="%s",skill="%s",project="%s",provider="%s",model="%s",agent="%s"} %s'
                % (day, kind, esc(name), esc(proj), esc(prov), esc(model), esc(agent), v))

        # Improvement series: tool errors, compactions, interruptions, agents by week, findings
        out.extend(coach_metrics.lines())

        # Weekly trend per project, last WEEKS weeks
        add("# TYPE opencode_weekly_cost_usd gauge")
        add("# TYPE opencode_weekly_tokens gauge")
        since = int((time.time() - WEEKS * 7 * 86400) * 1000)
        wk_cost, wk_tok = {}, {}
        for week, wt, cst, total in c.execute(WEEKLY, (since,)):
            key = (week, project_name(wt))
            wk_cost[key] = wk_cost.get(key, 0) + cst
            wk_tok[key] = wk_tok.get(key, 0) + total
        for (week, proj), v in wk_cost.items():
            add('opencode_weekly_cost_usd{week="%s",project="%s"} %s' % (week, esc(proj), v))
            add('opencode_weekly_tokens{week="%s",project="%s"} %s' % (week, esc(proj), wk_tok[(week, proj)]))

        # Context window of the most recent sessions
        add("# TYPE opencode_context_tokens gauge")
        add("# TYPE opencode_context_limit_tokens gauge")
        for sid, title, wt, prov, model, inp, outp, cr, cw in context_usage(c):
            limit = context_limit(prov, model)
            name = (title[:48] + "…") if len(title) > 48 else title
            shown = "%s · %s (%s, %s)" % (project_name(wt), name or sid[:12], model, human(limit) if limit else "límite desconocido")
            lab = 'session="%s",label="%s",project="%s",provider="%s",model="%s"' % (
                esc(sid), esc(shown), esc(project_name(wt)), esc(prov), esc(model))
            for t, v in (("input", inp), ("cache_read", cr), ("cache_write", cw), ("output", outp)):
                add('opencode_context_tokens{%s,type="%s"} %s' % (lab, t, v))
            if limit:
                add('opencode_context_limit_tokens{%s} %s' % (lab, limit))
    finally:
        c.close()

    # Quota per provider, from the opencode-quota plugin's cache
    quotas, qerrors = read_quotas()
    add("# TYPE opencode_quota_remaining_ratio gauge")
    add("# TYPE opencode_quota_used gauge")
    add("# TYPE opencode_quota_limit gauge")
    add("# TYPE opencode_quota_reset_timestamp_seconds gauge")
    add("# TYPE opencode_quota_updated_timestamp_seconds gauge")
    for prov, q in quotas.items():
        lab = 'provider="%s"' % esc(prov)
        add("opencode_quota_remaining_ratio{%s} %s" % (lab, q["remaining"] / 100))
        if q["amounts"]:
            add("opencode_quota_used{%s} %s" % (lab, q["amounts"][0]))
            add("opencode_quota_limit{%s} %s" % (lab, q["amounts"][1]))
        if q["reset"]:
            add("opencode_quota_reset_timestamp_seconds{%s} %s" % (lab, q["reset"]))
        add("opencode_quota_updated_timestamp_seconds{%s} %s" % (lab, q["updated"]))
    add("# TYPE opencode_quota_error gauge")
    for prov, msg in qerrors.items():
        add('opencode_quota_error{provider="%s",message="%s"} 1' % (esc(prov), esc(msg)))

    add("# TYPE opencode_exporter_scrape_seconds gauge")
    add("opencode_exporter_scrape_seconds %s" % round(time.time() - started, 3))
    return "\n".join(out) + "\n"


_TOKENS = ", ".join(
    "coalesce(json_extract(data,'%s'),0)" % p for p in ("$.tokens.input", "$.tokens.output", "$.tokens.reasoning", "$.tokens.cache.read", "$.tokens.cache.write")
)


def session_detail(sid):
    """Everything the detail page shows about one session, or None when it does not exist."""
    c = connect()
    try:
        row = c.execute(
            "select s.id, s.title, s.parent_id, coalesce(p.worktree,''), s.summary_additions, s.summary_deletions, s.summary_files, "
            "s.time_created, s.time_updated from session s left join project p on p.id = s.project_id where s.id = ?", (sid,)).fetchone()
        if not row:
            return None
        msgs = []
        for t, prov, model, agent, cst, inp, outp, rsn, cr, cw in c.execute(
                "select time_created, json_extract(data,'$.providerID'), json_extract(data,'$.modelID'), json_extract(data,'$.agent'), "
                "coalesce(json_extract(data,'$.cost'),0), " + _TOKENS + " from message "
                "where session_id = ? and json_extract(data,'$.role') = 'assistant' order by time_created, id", (sid,)):
            tokens = {"input": inp, "output": outp + rsn, "cache_read": cr, "cache_write": cw}
            if sum(tokens.values()) > 0:  # skip the aborted or in-flight message that carries no tokens
                msgs.append({"t": t, "provider": prov or "unknown", "model": model or "unknown", "agent": agent or "unknown", "cost": cst, "tokens": tokens})
        tools, skills = {}, {}
        for tool, status, name in c.execute(
                "select json_extract(data,'$.tool'), json_extract(data,'$.state.status'), json_extract(data,'$.state.input.name') "
                "from part where session_id = ? and json_extract(data,'$.type') = 'tool'", (sid,)):
            entry = tools.setdefault(tool or "?", [0, 0])
            entry[0] += 1
            entry[1] += 1 if status == "error" else 0
            if tool == "skill" and name and status == "completed":
                skills[("skill", name)] = skills.get(("skill", name), 0) + 1
        templates = command_templates()
        if templates:
            width = max(len(o) for o in templates.values()) + 1
            seen = set()
            for text, mid in c.execute(
                    "select substr(json_extract(p.data,'$.text'), 1, ?), p.message_id from part p join message m on m.id = p.message_id "
                    "where p.session_id = ? and json_extract(m.data,'$.role') = 'user' and json_extract(p.data,'$.type') = 'text'", (width, sid)):
                name = match_command(text or "", templates)
                if name and mid not in seen:
                    seen.add(mid)
                    skills[("command", name)] = skills.get(("command", name), 0) + 1
        compactions = [t for (t,) in c.execute(
            "select m.time_created from part p join message m on m.id = p.message_id "
            "where p.session_id = ? and json_extract(p.data,'$.type') = 'compaction' order by m.time_created", (sid,))]
        children = []
        for cid, title, upd in c.execute("select id, title, time_updated from session where parent_id = ? order by time_updated desc", (sid,)).fetchall():
            cost = c.execute("select coalesce(sum(json_extract(data,'$.cost')),0) from message where session_id = ? and json_extract(data,'$.role') = 'assistant'", (cid,)).fetchone()[0]
            children.append((cid, title, cost, upd))
    finally:
        c.close()
    last = None
    limit = None
    if msgs:
        m = msgs[-1]
        last = dict(m["tokens"], model=m["model"], provider=m["provider"])
        limit = context_limit(m["provider"], m["model"])
    return {"id": row[0], "title": row[1], "parent": row[2], "project": project_name(row[3]), "additions": row[4] or 0,
            "deletions": row[5] or 0, "files": row[6] or 0, "created": row[7], "updated": row[8], "messages": msgs,
            "tools": tools, "skills": skills, "compactions": compactions, "children": children, "last": last, "limit": limit}


def sessions_list(since_ms, until_ms, projects, subagents):
    """Sessions created in the range, newest first, with their messages, tokens and cost."""
    c = connect()
    try:
        agg = {sid: (n, cost, tok) for sid, n, cost, tok in c.execute(
            "select session_id, count(*), coalesce(sum(json_extract(data,'$.cost')),0), " + " + ".join(
                "coalesce(json_extract(data,'%s'),0)" % p for p in ("$.tokens.input", "$.tokens.output", "$.tokens.reasoning", "$.tokens.cache.read", "$.tokens.cache.write")) +
            " from message where json_extract(data,'$.role') = 'assistant' group by session_id")}
        rows = []
        for sid, title, parent, wt, created, updated, agent in c.execute(
                "select s.id, s.title, s.parent_id, coalesce(p.worktree,''), s.time_created, s.time_updated, s.agent "
                "from session s left join project p on p.id = s.project_id where s.time_created between ? and ? order by s.time_updated desc",
                (since_ms, until_ms)):
            if parent and not subagents:
                continue
            proj = project_name(wt)
            if projects and proj not in projects:
                continue
            n, cost, tok = agg.get(sid, (0, 0, 0))
            rows.append((sid, title, parent, proj, created, updated, n, cost, tok, agent))
    finally:
        c.close()
    return rows


def list_range(query):
    """The [from, to] milliseconds that the dashboard's period selects: days back, or two UTC days."""
    def num(key, default):
        try:
            return int(query.get(key, [default])[0])
        except ValueError:
            return default
    window = num("window", 36500)
    now = int(time.time() * 1000)
    if window > 0:
        return now - window * 86400 * 1000, now, ("de los últimos %d días" % window) if window < 36500 else "de todo el historial"
    start, end = num("from", 0) * 1000, (num("to", 0) + 86399) * 1000
    return start, end, "del %s al %s (UTC)" % (time.strftime("%Y-%m-%d", time.gmtime(start / 1000)), time.strftime("%Y-%m-%d", time.gmtime((end - 86399000) / 1000)))


REPORT_CACHE_SECONDS = 120
# Older than this, the data a panel's report is built from is served anyway and read again in the background
SNAPSHOT_FRESH_SECONDS = 30
# Reports already built for the «Mejoras» panels, which ask several at a time per dashboard load
_REPORTS, _REPORTS_LOCK, _BUILD_LOCK = {}, threading.Lock(), threading.Lock()


def report_args(query):
    """Period, label, projects and anonymisation of a report request (window or from/to, project, anon)."""
    since, until, label = list_range(query)
    label = label.replace("de los ", "").replace("de todo el historial", "todo el historial")
    raw = ",".join(query.get("project", []))
    projects = {p for p in re.split(r"[,|]", raw) if p and p not in (".*", "All", "$__all")}
    return since, until, label, projects, query.get("anon", ["0"])[0] == "1"


def build_report(since, until, label, projects, anon, loaded=None):
    c = connect()
    try:
        return coach_report.build(c, sys.modules[__name__], since, until, label, projects or None, anon, loaded=loaded)
    finally:
        c.close()


def report_for(query, fresh=False):
    """The improvement report for a query string.

    `fresh` (the downloaded report) reads the database now. Otherwise (the dashboard's panels) the
    report is built from the data the background thread last read, at most about two minutes old,
    which takes a fraction of a second instead of seconds; if that data is older than
    SNAPSHOT_FRESH_SECONDS the thread is asked to read it again for the next request.
    """
    since, until, label, projects, anon = report_args(query)
    loaded, read_at = (None, 0) if fresh else coach_metrics.snapshot()
    if loaded is None:  # the downloaded report, or the thread has not finished its first reading
        return build_report(since, until, label, projects, anon)
    if time.time() - read_at > SNAPSHOT_FRESH_SECONDS:
        coach_metrics.request_refresh()
    key = (since // 60000, until // 60000, tuple(sorted(projects)), anon, read_at)
    with _BUILD_LOCK:
        now = time.time()
        with _REPORTS_LOCK:
            hit = _REPORTS.get(key)
            if hit and now - hit[0] < REPORT_CACHE_SECONDS:
                return hit[1]
        data = build_report(since, until, label, projects, anon, loaded)
        with _REPORTS_LOCK:
            _REPORTS[key] = (now, data)
            for k in [k for k, (t, _d) in _REPORTS.items() if now - t >= REPORT_CACHE_SECONDS]:
                del _REPORTS[k]
    return data


def _plain(text):
    """Finding text without Markdown marks, for Grafana tables."""
    return re.sub(r"[`*]", "", text or "")


def coach_rows(name, r):
    """Rows of one part of the report as a JSON array, for Grafana's Infinity datasource. None if unknown."""
    findings = r["findings"]
    rule = lambda d: "%s %s" % (d, coach_report.RULES.get(d, ""))
    if name == "summary":
        sev = Counter(f["severity"] for f in findings)
        return [{"hallazgos": len(findings), "alta": sev["alta"], "media": sev["media"], "baja": sev["baja"],
                 "impacto_usd": round(sum(f.get("impact_usd") or 0 for f in findings), 2),
                 "desde": r["period"]["from"], "hasta": r["period"]["to"]}]
    if name == "findings":
        return [{"id": f["id"], "severidad": f["severity"], "regla": rule(f["detector"]), "hallazgo": _plain(f["title"]),
                 "impacto_usd": f.get("impact_usd"), "casos": f.get("impact_count"),
                 "evidencia": _plain(f["evidence"]), "recomendacion": _plain(f["recommendation"])} for f in findings]
    if name == "rules":
        rows = {}
        for f in findings:
            row = rows.setdefault(f["detector"], {"regla": rule(f["detector"]), "hallazgos": 0, "alta": 0, "impacto_usd": 0.0})
            row["hallazgos"] += 1
            row["alta"] += f["severity"] == "alta"
            row["impacto_usd"] = round(row["impacto_usd"] + (f.get("impact_usd") or 0), 2)
        return sorted(rows.values(), key=lambda x: (-x["impacto_usd"], -x["hallazgos"]))
    return None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.rstrip("/") == "/sessions":
            try:
                query = urllib.parse.parse_qs(parsed.query)
                since, until, label = list_range(query)
                raw = ",".join(query.get("project", []))
                projects = {p for p in re.split(r"[,|]", raw) if p and p not in (".*", "All", "$__all")}
                subagents = query.get("subagents", ["0"])[0] == "1"
                rows = sessions_list(since, until, projects, subagents)
                note = "Sesiones creadas %s%s%s." % (label, ", proyecto: " + ", ".join(sorted(projects)) if projects else "", ", con subagentes" if subagents else "")
                other = {k: v for k, v in query.items() if k != "subagents"}
                other["subagents"] = ["0" if subagents else "1"]
                body = session_page.render_list(rows, GRAFANA_URL, subagents, note, "?" + urllib.parse.urlencode(other, doseq=True)).encode()
                self.send_response(200)
            except Exception as ex:
                body = ("Error: %s" % ex).encode()
                self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path.rstrip("/") == "/report":
            self.report_view(parsed)
            return
        if parsed.path.rstrip("/") in ("/report.md", "/report.json"):
            self.report(parsed)
            return
        if parsed.path.startswith("/coach/"):
            try:
                rows = coach_rows(parsed.path[len("/coach/"):].strip("/"), report_for(urllib.parse.parse_qs(parsed.query)))
                status, body = (404, "[]") if rows is None else (200, json.dumps(rows, ensure_ascii=False))
            except Exception as e:
                status, body = 500, json.dumps({"error": str(e)})
            body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        path = self.path.split("?")[0]
        if path.startswith("/session/"):
            sid = path[len("/session/"):].strip("/")
            try:
                data = session_detail(sid) if re.fullmatch(r"[A-Za-z0-9_]+", sid) else None
                body = (session_page.render(data, GRAFANA_URL) if data else session_page.not_found(GRAFANA_URL)).encode()
                self.send_response(200 if data else 404)
            except Exception as e:
                body = ("Error: %s" % e).encode()
                self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            body = collect().encode()
            self.send_response(200)
        except Exception as e:  # report the failure to Prometheus instead of an empty 200
            body = ("# exporter error: %s\n" % e).encode()
            self.send_response(500)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def report(self, parsed):
        """The improvement report for the dashboard's period and projects, to download and share with an AI."""
        as_json = parsed.path.rstrip("/").endswith(".json")
        try:
            data = report_for(urllib.parse.parse_qs(parsed.query), fresh=True)
            if as_json:
                body, ctype = coach_render.render_json(data).encode(), "application/json; charset=utf-8"
            else:
                body, ctype = coach_render.render(data).encode(), "text/markdown; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Disposition", 'attachment; filename="opencode-informe-%s.%s"' % (
                time.strftime("%Y-%m-%d"), "json" if as_json else "md"))
        except Exception as e:
            body, ctype = ("Error: %s" % e).encode(), "text/plain; charset=utf-8"
            self.send_response(500)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def report_view(self, parsed):
        """The improvement report as a web page, opened from the dashboard, with a button to download its Markdown."""
        query = urllib.parse.parse_qs(parsed.query)
        anon = query.get("anon", ["0"])[0] == "1"
        other = {k: v for k, v in query.items() if k != "anon"}
        if not anon:
            other["anon"] = ["1"]
        anon_href = "?" + urllib.parse.urlencode(other, doseq=True)
        try:
            md = coach_render.render(report_for(query, fresh=True))
            filename = "opencode-informe-%s.md" % time.strftime("%Y-%m-%d")
            download = "/report.md" + ("?" + parsed.query if parsed.query else "")
            body = report_page.render(md, filename, GRAFANA_URL, download, anon, anon_href).encode()
            self.send_response(200)
        except Exception as ex:
            body = report_page.error("Error: %s" % ex, GRAFANA_URL).encode()
            self.send_response(500)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=skill_loop, daemon=True).start()
    # Threaded, so a session page or a report does not wait behind a scrape, and the other way round
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
