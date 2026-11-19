"""Usage analysis for the improvement report.

`build()` reads OpenCode's database and the tool index and returns a plain dict: a summary
compared with the previous period, the tables an AI needs to judge how the agents are
being used, and the findings of the deterministic detectors. `/report.md`, `/report.json`,
the command line and, later, the MCP server all start from this dict.

Nothing here reads the text of the conversation. Tool error messages are reduced to a
class by the index and discarded there.
"""
import os
import re
import time
from collections import Counter, defaultdict

from .errors import ADVICE
from .index import INDEX

FORMAT_VERSION = 1
TOP = 10

MESSAGES = """
select m.id, m.session_id, m.time_created,
       json_extract(m.data,'$.role'),
       json_extract(m.data,'$.parentID'),
       coalesce(json_extract(m.data,'$.agent'),'unknown'),
       coalesce(json_extract(m.data,'$.providerID'),'unknown'),
       coalesce(json_extract(m.data,'$.modelID'),'unknown'),
       coalesce(json_extract(m.data,'$.cost'),0),
       coalesce(json_extract(m.data,'$.tokens.input'),0),
       coalesce(json_extract(m.data,'$.tokens.output'),0),
       coalesce(json_extract(m.data,'$.tokens.reasoning'),0),
       coalesce(json_extract(m.data,'$.tokens.cache.read'),0),
       coalesce(json_extract(m.data,'$.tokens.cache.write'),0),
       json_extract(m.data,'$.error.name'),
       coalesce(json_extract(m.data,'$.time.completed'), m.time_updated)
from message m
"""

SESSIONS = """
select s.id, s.parent_id, coalesce(p.worktree,''), s.time_created
from session s left join project p on p.id = s.project_id
"""

# Short names of the rules, for the dashboard
RULES = {
    "D1": "Errores de herramienta", "D2": "Coste por agente", "D3": "Bucles y exploración", "D4": "Contexto",
    "D5": "Permisos", "D6": "Caché", "D7": "Skills", "D8": "Modelo fijado en comandos", "D9": "Interrupciones",
    "D10": "Comandos sin uso", "D11": "Variantes -fast",
}

# Tools whose repetition with the same arguments is not a sign of a loop
NOT_LOOPS = {"todowrite", "todoread", "question"}


class Msg:
    __slots__ = ("id", "session", "time", "role", "parent", "agent", "provider", "model", "cost",
                 "inp", "out", "reasoning", "cache_read", "cache_write", "error", "done")

    def __init__(self, row):
        (self.id, self.session, self.time, self.role, self.parent, self.agent, self.provider, self.model,
         self.cost, self.inp, self.out, self.reasoning, self.cache_read, self.cache_write, self.error, self.done) = row

    @property
    def tokens(self):
        return self.inp + self.out + self.reasoning + self.cache_read + self.cache_write


def _pct(a, b):
    return (a / b) if b else 0.0


def _quantile(values, q):
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


class Period:
    """Everything the report needs for one time range and set of projects."""

    def __init__(self, msgs, sessions, calls, compactions, since, until, keep, commands=None):
        self.since, self.until = since, until
        self.commands = commands or {}  # user message id -> custom command name
        self.sessions = {sid: s for sid, s in sessions.items() if keep(s["project"])}
        inside = lambda t: since <= (t or 0) <= until
        self.msgs = [m for m in msgs if inside(m.time) and m.session in self.sessions]
        self.assistant = [m for m in self.msgs if m.role == "assistant"]
        self.user = [m for m in self.msgs if m.role == "user"]
        self.by_id = {m.id: m for m in msgs}
        self.calls = [c for c in calls if inside(c.time) and c.session in self.sessions]
        self.compactions = [(s, mid, t) for s, mid, t in compactions if inside(t) and s in self.sessions]
        self.turn_cost = defaultdict(float)
        self.turn_tokens = defaultdict(int)
        for m in self.assistant:
            if m.parent:
                self.turn_cost[m.parent] += m.cost
                self.turn_tokens[m.parent] += m.tokens

    def summary(self):
        a = self.assistant
        cost = sum(m.cost for m in a)
        inp = sum(m.inp for m in a)
        cr = sum(m.cache_read for m in a)
        cw = sum(m.cache_write for m in a)
        out = sum(m.out + m.reasoning for m in a)
        errors = sum(1 for c in self.calls if c.status == "error")
        main_sessions = [s for s in self.sessions.values() if not s["parent"] and self.since <= s["created"] <= self.until]
        return {
            "cost_usd": round(cost, 4),
            "tokens": inp + cr + cw + out,
            "tokens_input": inp, "tokens_cache_read": cr, "tokens_cache_write": cw, "tokens_output": out,
            "cache_hit": round(_pct(cr, inp + cr + cw), 4),
            "sessions": len(main_sessions),
            "turns": len({m.parent for m in a if m.parent}),
            "assistant_messages": len(a),
            "tool_calls": len(self.calls),
            "tool_errors": errors,
            "tool_error_rate": round(_pct(errors, len(self.calls)), 4),
            "compactions": len(self.compactions),
            "aborted": sum(1 for m in a if m.error == "MessageAbortedError"),
        }

    def model_of(self, call):
        m = self.by_id.get(call.message)
        return (m.model, m.agent, m.parent) if m else ("unknown", "unknown", None)


def load(conn, helpers, refresh=True):
    """Sessions, messages and the tool index. With refresh=False the index is used as it is,
    so a Prometheus scrape never waits for the first scan of `part`."""
    sessions = {}
    for sid, parent, wt, created in conn.execute(SESSIONS):
        sessions[sid] = {"parent": parent, "project": helpers.project_name(wt), "created": created}
    msgs = [Msg(r) for r in conn.execute(MESSAGES)]
    if refresh:
        INDEX.refresh(conn)
    calls, compactions = INDEX.snapshot()
    return msgs, sessions, calls, compactions


def commands_by_message(helpers, scan=True):
    """User message id -> custom command name, from the exporter's background scan."""
    if not hasattr(helpers, "_skills"):
        return {}
    if scan and not helpers._skills.get("done"):
        try:
            helpers.refresh_skill_calls()
        except Exception:
            pass
    with helpers._skills_lock:
        return {mid: name for mid, (_day, name, _wt) in helpers._skills["commands"].items()}


def build(conn, helpers, since, until, label, projects=None, anon=False, now=None, loaded=None):
    """The report as a dict. `since`/`until` in Unix milliseconds; `projects` a set of names or None."""
    started = time.time()
    msgs, sessions, calls, compactions = loaded or load(conn, helpers)
    commands = commands_by_message(helpers)
    keep = (lambda p: p in projects) if projects else (lambda p: True)
    # first activity of the chosen projects, so a project that started later has no empty previous period
    first = min((m.time for m in msgs if m.session in sessions and keep(sessions[m.session]["project"])), default=since)
    since = max(since, first - first % 86400000)
    span = until - since
    cur = Period(msgs, sessions, calls, compactions, since, until, keep, commands)
    prev = Period(msgs, sessions, calls, compactions, since - span, since - 1, keep, commands) if since - span >= first - 86400000 else None

    names = {}
    if anon:
        for i, p in enumerate(sorted({s["project"] for s in cur.sessions.values()}), 1):
            names[p] = "proyecto-%d" % i
    pname = lambda p: names.get(p, p)

    summary = cur.summary()
    avg_call_cost = _pct(summary["cost_usd"], summary["tool_calls"])
    ctx = {"cur": cur, "summary": summary, "avg_call_cost": avg_call_cost, "helpers": helpers, "pname": pname,
           "days": round(span / 86400000), "providers_ever": {m.provider for m in msgs if m.role == "assistant"}}

    tables = {
        "agents": agents_table(cur),
        "models": models_table(cur),
        "projects": projects_table(cur, pname),
        "tools": tools_table(cur),
        "tool_errors": tool_errors_table(cur),
        "turns": turns_stats(cur),
        "context": context_stats(cur),
        "skills": skills_table(cur),
        "interaction": interaction_stats(cur),
        "model_health": model_health(cur),
    }
    ctx["tables"] = tables
    ctx["config"] = config = config_inventory(helpers, tables["skills"])
    findings = []
    for detector in DETECTORS:
        findings.extend(detector(ctx))
    for f in findings:
        f["severity"] = severity(f, summary)
    order = {"alta": 0, "media": 1, "baja": 2}
    findings.sort(key=lambda f: (order[f["severity"]], -(f.get("impact_usd") or 0), -(f.get("impact_count") or 0)))
    for i, f in enumerate(findings, 1):
        f["id"] = "H%d" % i

    return {
        "format_version": FORMAT_VERSION,
        "source": "opencode",
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time())),
        "period": {"label": label, "from": time.strftime("%Y-%m-%d", time.gmtime(since / 1000)),
                   "to": time.strftime("%Y-%m-%d", time.gmtime(until / 1000)), "days": round(span / 86400000)},
        "projects": sorted(pname(p) for p in projects) if projects else "todos",
        "anonymized": anon,
        "summary": summary,
        "previous": prev.summary() if prev else None,
        "findings": findings,
        "tables": tables,
        "config": config,
        "build_seconds": round(time.time() - started, 2),
    }


# ----------------------------------------------------------------------------- tables

def agents_table(p):
    rows = defaultdict(lambda: {"messages": 0, "cost_usd": 0.0, "output": 0, "models": Counter(),
                                "cost_cmd": 0.0, "msgs_cmd": 0, "models_free": Counter()})
    for m in p.assistant:
        r = rows[m.agent]
        r["messages"] += 1
        r["cost_usd"] += m.cost
        r["output"] += m.out + m.reasoning
        r["models"][m.model] += m.cost
        if m.parent in p.commands:
            r["cost_cmd"] += m.cost
            r["msgs_cmd"] += 1
        else:
            r["models_free"][m.model] += m.cost
    total = sum(r["cost_usd"] for r in rows.values())
    out = []
    for agent, r in sorted(rows.items(), key=lambda x: -x[1]["cost_usd"]):
        out.append({"agent": agent, "messages": r["messages"], "cost_usd": round(r["cost_usd"], 4),
                    "cost_share": round(_pct(r["cost_usd"], total), 4),
                    "cost_per_message": round(_pct(r["cost_usd"], r["messages"]), 4),
                    "output_per_message": round(_pct(r["output"], r["messages"])),
                    "top_models": [m for m, _ in r["models"].most_common(3)],
                    "cost_in_commands": round(r["cost_cmd"], 4),
                    "cost_outside_commands": round(r["cost_usd"] - r["cost_cmd"], 4),
                    "messages_outside_commands": r["messages"] - r["msgs_cmd"],
                    "top_models_outside_commands": [m for m, _ in r["models_free"].most_common(3)]})
    return out


def models_table(p):
    rows = defaultdict(lambda: Counter())
    for m in p.assistant:
        r = rows[(m.provider, m.model)]
        r["messages"] += 1
        r["cost"] += m.cost
        r["inp"] += m.inp
        r["cr"] += m.cache_read
        r["cw"] += m.cache_write
        r["out"] += m.out + m.reasoning
    total = sum(r["cost"] for r in rows.values())
    out = []
    for (prov, model), r in sorted(rows.items(), key=lambda x: (-x[1]["cost"], -x[1]["messages"])):
        tok = r["inp"] + r["cr"] + r["cw"] + r["out"]
        out.append({"provider": prov, "model": model, "messages": r["messages"], "cost_usd": round(r["cost"], 4),
                    "cost_share": round(_pct(r["cost"], total), 4), "tokens": tok,
                    "usd_per_mtok": round(_pct(r["cost"], tok) * 1e6, 3),
                    "cache_hit": round(_pct(r["cr"], r["inp"] + r["cr"] + r["cw"]), 4),
                    "input_uncached": r["inp"]})
    return out


def projects_table(p, pname):
    rows = defaultdict(lambda: Counter())
    for m in p.assistant:
        r = rows[p.sessions[m.session]["project"]]
        r["messages"] += 1
        r["cost"] += m.cost
    return [{"project": pname(k), "messages": r["messages"], "cost_usd": round(r["cost"], 4)}
            for k, r in sorted(rows.items(), key=lambda x: -x[1]["cost"])]


def tools_table(p):
    rows = defaultdict(lambda: {"calls": 0, "errors": 0, "classes": Counter()})
    for c in p.calls:
        r = rows[c.tool]
        r["calls"] += 1
        if c.status == "error":
            r["errors"] += 1
            r["classes"][c.error_class] += 1
    return [{"tool": t, "calls": r["calls"], "errors": r["errors"], "error_rate": round(_pct(r["errors"], r["calls"]), 4),
             "main_error": r["classes"].most_common(1)[0][0] if r["classes"] else None}
            for t, r in sorted(rows.items(), key=lambda x: -x[1]["calls"])]


def tool_errors_table(p):
    """Errors by tool, class and model, with the calls of that tool and model for the rate."""
    calls = Counter()
    errs = Counter()
    sess = defaultdict(Counter)
    for c in p.calls:
        model = p.model_of(c)[0]
        calls[(c.tool, model)] += 1
        if c.status == "error":
            errs[(c.tool, c.error_class, model)] += 1
            sess[(c.tool, c.error_class, model)][c.session] += 1
    out = []
    for (tool, cls, model), n in errs.most_common():
        out.append({"tool": tool, "error_class": cls, "model": model, "errors": n,
                    "calls_tool_model": calls[(tool, model)], "rate": round(_pct(n, calls[(tool, model)]), 4),
                    "sessions": [s for s, _ in sess[(tool, cls, model)].most_common(5)]})
    return out


def _turns(p):
    turns = defaultdict(list)
    for c in sorted(p.calls, key=lambda c: c.id):
        parent = p.model_of(c)[2]
        if parent:
            turns[parent].append(c)
    return turns


def turns_stats(p):
    turns = _turns(p)
    sizes = [len(v) for v in turns.values()]
    loops, streaks = [], []
    for parent, cs in turns.items():
        keys = Counter(c.arg_key for c in cs if c.tool not in NOT_LOOPS)
        repeated = sum(n - 1 for n in keys.values() if n >= 3)
        if repeated:
            tool = Counter(c.tool for c in cs if keys[c.arg_key] >= 3).most_common(1)[0][0]
            loops.append({"turn": parent, "session": cs[0].session, "tool": tool, "repeated_calls": repeated})
        run = best = 0
        for c in cs:
            run = run + 1 if c.status == "error" else 0
            best = max(best, run)
        if best >= 3:
            streaks.append({"turn": parent, "session": cs[0].session, "consecutive_errors": best})
    p95 = _quantile(sizes, 0.95)
    long_limit = max(40, p95)
    long_turns = [{"turn": k, "session": v[0].session, "tool_calls": len(v), "cost_usd": p.turn_cost.get(k, 0),
                   "main_tool": Counter(c.tool for c in v).most_common(1)[0][0]}
                  for k, v in turns.items() if len(v) > long_limit]
    long_turns.sort(key=lambda x: -x["tool_calls"])
    return {"turns_with_tools": len(sizes), "p50": _quantile(sizes, 0.5), "p90": _quantile(sizes, 0.9),
            "p95": p95, "max": max(sizes) if sizes else 0, "long_limit": long_limit,
            "loops": sorted(loops, key=lambda x: -x["repeated_calls"]),
            "error_streaks": sorted(streaks, key=lambda x: -x["consecutive_errors"]),
            "long_turns": long_turns}


def context_stats(p):
    per_session = Counter(s for s, _, _ in p.compactions)
    overflow = Counter()
    overflow_sessions = defaultdict(set)
    for m in p.assistant:
        if m.error == "ContextOverflowError":
            overflow[m.model] += 1
            overflow_sessions[m.model].add(m.session)
    compaction_cost = sum(m.cost for m in p.assistant if m.agent == "compaction")
    return {"compactions": len(p.compactions), "sessions_with_compaction": len(per_session),
            "sessions_2plus": [{"session": s, "compactions": n} for s, n in per_session.most_common() if n >= 2],
            "compaction_cost_usd": round(compaction_cost, 4),
            "overflow_by_model": [{"model": k, "errors": n, "sessions": sorted(overflow_sessions[k])[:5]}
                                  for k, n in overflow.most_common()]}


def skills_table(p):
    rows = defaultdict(lambda: {"kind": "", "activations": 0, "errors": 0, "cost": 0.0, "tokens": 0, "agents": Counter()})
    turns_seen = defaultdict(set)
    for c in p.calls:
        if c.tool != "skill" or not c.skill or c.status in ("pending", "running"):
            continue
        r = rows[("skill", c.skill)]
        r["kind"] = "skill"
        r["activations"] += 1
        _model, agent, parent = p.model_of(c)
        r["agents"][agent] += 1
        if c.status == "error":
            r["errors"] += 1
        elif parent and parent not in turns_seen[("skill", c.skill)]:
            turns_seen[("skill", c.skill)].add(parent)
            r["cost"] += p.turn_cost.get(parent, 0)
            r["tokens"] += p.turn_tokens.get(parent, 0)
    turn_agents = defaultdict(Counter)
    for m in p.assistant:
        if m.parent in p.commands:
            turn_agents[m.parent][m.agent] += m.cost or 1e-9
    for mid, name in p.commands.items():
        m = p.by_id.get(mid)
        if not m or not (p.since <= m.time <= p.until) or m.session not in p.sessions:
            continue
        r = rows[("command", name)]
        r["kind"] = "command"
        r["activations"] += 1
        r["cost"] += p.turn_cost.get(mid, 0)
        r["tokens"] += p.turn_tokens.get(mid, 0)
        r["agents"].update(turn_agents.get(mid, {}))
    names = Counter(name for _kind, name in rows)
    out = []
    for (kind, name), r in sorted(rows.items(), key=lambda x: (-x[1]["cost"], -x[1]["tokens"])):
        out.append({"kind": kind, "name": name, "activations": r["activations"], "errors": r["errors"],
                    "cost_usd": round(r["cost"], 4), "cost_per_activation": round(_pct(r["cost"], r["activations"]), 4),
                    "tokens": r["tokens"], "tokens_per_activation": round(_pct(r["tokens"], r["activations"])),
                    "main_agent": r["agents"].most_common(1)[0][0] if r["agents"] else None,
                    "same_name_as_other_kind": names[name] > 1})
    return out


def model_health(p):
    """Per model: tool errors the user can act on (by tool) and message errors, for crossing with pinned models."""
    calls = defaultdict(Counter)
    errs = defaultdict(Counter)
    for c in p.calls:
        model = p.model_of(c)[0]
        calls[model][c.tool] += 1
        if c.status == "error" and c.error_class not in ("permission_rule", "user_rejected", "aborted"):
            errs[model][c.tool] += 1
    msgs = Counter(m.model for m in p.assistant)
    merr = defaultdict(Counter)
    for m in p.assistant:
        if m.error and m.error != "MessageAbortedError":
            merr[m.model][m.error] += 1
    out = {}
    for model in set(msgs) | set(calls):
        tools = []
        for tool, n in errs[model].most_common():
            tools.append({"tool": tool, "errors": n, "calls": calls[model][tool], "rate": round(_pct(n, calls[model][tool]), 4)})
        out[model] = {"messages": msgs[model], "tool_calls": sum(calls[model].values()),
                      "tool_errors": sum(errs[model].values()), "tools": tools,
                      "message_errors": dict(merr[model]), "message_error_rate": round(_pct(sum(merr[model].values()), msgs[model]), 4)}
    return out


def interaction_stats(p):
    aborted = [m for m in p.assistant if m.error == "MessageAbortedError"]
    user_by_session = defaultdict(list)
    for m in p.user:
        user_by_session[m.session].append(m.time)
    quick = []
    for m in aborted:
        nxt = [t for t in user_by_session.get(m.session, []) if t > (m.done or m.time)]
        if nxt and min(nxt) - (m.done or m.time) <= 120000:
            quick.append(m)
    per_session = [len(v) for s, v in user_by_session.items() if not p.sessions[s]["parent"]]
    sub_cost = sum(m.cost for m in p.assistant if p.sessions[m.session]["parent"])
    total = sum(m.cost for m in p.assistant)
    errors = Counter(m.error for m in p.assistant if m.error)
    return {"aborted": len(aborted), "aborted_by_agent": dict(Counter(m.agent for m in aborted)),
            "aborted_quick_retry": len(quick),
            "aborted_quick_retry_cost_usd": round(sum(p.turn_cost.get(m.parent, 0) for m in quick), 4),
            "aborted_quick_retry_sessions": sorted({m.session for m in quick})[:5],
            "user_messages_per_session_p50": _quantile(per_session, 0.5),
            "user_messages_per_session_p90": _quantile(per_session, 0.9),
            "subagent_sessions": sum(1 for s in p.sessions.values() if s["parent"] and p.since <= s["created"] <= p.until),
            "subagent_cost_share": round(_pct(sub_cost, total), 4),
            "message_errors": dict(errors)}


def config_inventory(helpers, skills):
    """Commands in the mounted commands folder, with their pinned model and use in the period."""
    used = {s["name"]: s["activations"] for s in skills if s["kind"] == "command"}
    commands = []
    folder = getattr(helpers, "COMMANDS_DIR", None)
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".md")) if folder else []
    except OSError:
        names = []
    for n in names:
        path = os.path.join(folder, n)
        model = None
        try:
            with open(path, encoding="utf-8") as f:
                head = f.read(2000)
            fm = re.match(r"^---\s*(.*?)\s*---", head, re.S)
            if fm:
                mm = re.search(r"^model:\s*(\S+)", fm.group(1), re.M)
                model = mm.group(1).strip("\"'") if mm else None
            changed = time.strftime("%Y-%m-%d", time.gmtime(os.path.getmtime(path)))
        except OSError:
            changed = None
        commands.append({"name": n[:-3], "model": model, "changed": changed, "uses": used.get(n[:-3], 0)})
    return {"commands": commands,
            "skills_used": sorted(s["name"] for s in skills if s["kind"] == "skill"),
            "note": "Solo se ven los comandos de la carpeta global montada; las skills instaladas aún no se inventarían."}


# ----------------------------------------------------------------------------- detectors

def severity(f, summary):
    cost = summary["cost_usd"]
    usd = f.get("impact_usd") or 0
    if cost > 0 and usd:
        share = usd / cost
        return "alta" if share >= 0.10 else "media" if share >= 0.02 else "baja"
    if f.get("impact_tokens"):
        return "media" if f["impact_tokens"] >= 50_000_000 else "baja"
    n = f.get("impact_count") or 0
    return "alta" if n >= 100 else "media" if n >= 20 else "baja"


def _finding(detector, title, evidence, recommendation, impact_usd=None, impact_count=None, impact_tokens=None,
             sessions=(), subject=""):
    return {"detector": detector, "subject": subject, "title": title, "evidence": evidence,
            "recommendation": recommendation, "impact_usd": round(impact_usd, 4) if impact_usd is not None else None,
            "impact_count": impact_count, "impact_tokens": impact_tokens, "sessions": list(sessions)[:5]}


def d1_tool_errors(ctx):
    """Tool errors that the user can prevent, grouped by tool and class."""
    rows = defaultdict(lambda: {"errors": 0, "models": Counter(), "sessions": Counter()})
    for r in ctx["tables"]["tool_errors"]:
        if r["error_class"] in ("permission_rule", "user_rejected", "aborted"):
            continue
        g = rows[(r["tool"], r["error_class"])]
        g["errors"] += r["errors"]
        g["models"][r["model"]] += r["errors"]
        for s in r["sessions"]:
            g["sessions"][s] += 1
    calls = {t["tool"]: t["calls"] for t in ctx["tables"]["tools"]}
    out = []
    for (tool, cls), g in rows.items():
        rate = _pct(g["errors"], calls.get(tool, 0))
        if g["errors"] < 10 and not (g["errors"] >= 5 and rate >= 0.05):
            continue
        model, n = g["models"].most_common(1)[0]
        label, advice = ADVICE.get(cls, ADVICE["other"])
        conc = ""
        if len(g["models"]) > 1 and n / g["errors"] >= 0.6:
            conc = " El %d %% de estos errores son de `%s`." % (round(100 * n / g["errors"]), model)
        elif len(g["models"]) == 1:
            conc = " Todos con `%s`." % model
        out.append(_finding(
            "D1", "`%s`: %s" % (tool, label.lower()),
            "%d errores de clase `%s` en %d llamadas a `%s` (%.1f %%).%s" % (g["errors"], cls, calls.get(tool, 0), tool, 100 * rate, conc),
            advice, impact_usd=g["errors"] * ctx["avg_call_cost"], impact_count=g["errors"],
            sessions=[s for s, _ in g["sessions"].most_common(5)], subject="%s/%s" % (tool, cls)))
    return out


def d2_agent_cost(ctx):
    agents = [a for a in ctx["tables"]["agents"] if a["agent"] != "compaction"]
    out = []
    if len(agents) < 2:
        return out
    for a in agents:
        others = [o for o in agents if o is not a]
        msgs = sum(o["messages"] for o in others)
        if a["messages"] < 20 or msgs < 20:
            continue
        ref = sum(o["cost_usd"] for o in others) / msgs  # cost per message of all the other agents together
        if a["cost_share"] >= 0.4 and ref > 0 and a["cost_per_message"] >= 2 * ref:
            saved = a["cost_usd"] - a["messages"] * ref
            out.append(_finding(
                "D2", "El agente `%s` concentra el coste" % a["agent"],
                "%d %% del coste del periodo (%.2f $) con %.4f $ por mensaje, %.1f veces el coste por mensaje del resto de agentes juntos (%.4f $). "
                "Modelos principales: %s. Fuera de comandos gasta %.2f $ en %d mensajes (modelos: %s); dentro de comandos, %.2f $." % (
                    round(100 * a["cost_share"]), a["cost_usd"], a["cost_per_message"], a["cost_per_message"] / ref, ref,
                    ", ".join("`%s`" % m for m in a["top_models"]), a["cost_outside_commands"], a["messages_outside_commands"],
                    ", ".join("`%s`" % m for m in a["top_models_outside_commands"]) or "—", a["cost_in_commands"]),
                ("Casi todo el gasto es uso directo del agente, no de comandos: fija un modelo más barato por defecto para `%s` "
                 "(`agent.%s.model` en opencode.jsonc) y cambia al caro solo cuando la tarea lo pida." % (a["agent"], a["agent"])
                 if a["cost_outside_commands"] >= 0.7 * a["cost_usd"] else
                 "Buena parte del gasto viene de comandos: revisa el modelo fijado en ellos (sección 6) antes que el del agente.")
                + " Comprueba también si el agente lee más contexto del necesario.",
                impact_usd=saved, subject=a["agent"]))
    return out


def d3_loops(ctx):
    t = ctx["tables"]["turns"]
    out = []
    loops = t["loops"]
    if len(loops) >= 3:
        repeated = sum(l["repeated_calls"] for l in loops)
        tools = Counter(l["tool"] for l in loops).most_common(3)
        out.append(_finding(
            "D3", "Llamadas repetidas con los mismos argumentos",
            "%d turnos repiten tres o más veces la misma llamada (%d llamadas de más). Herramientas: %s." % (
                len(loops), repeated, ", ".join("`%s` (%d)" % x for x in tools)),
            "Suele indicar que el agente no ve el resultado que espera y reintenta. Da la ruta o el criterio de éxito en el prompt, "
            "o añade a AGENTS.md: «Si una llamada no da el resultado esperado dos veces, para y explica qué ocurre».",
            impact_usd=repeated * ctx["avg_call_cost"], impact_count=repeated,
            sessions=[l["session"] for l in loops], subject="loops"))
    streaks = t["error_streaks"]
    if len(streaks) >= 3:
        out.append(_finding(
            "D3", "Rachas de errores seguidos",
            "%d turnos encadenan tres o más errores de herramienta seguidos (máximo %d)." % (len(streaks), streaks[0]["consecutive_errors"]),
            "El agente sigue intentándolo sin cambiar de estrategia. Revisa esas sesiones: suele faltar contexto (rutas, comandos de build) "
            "que se puede dejar fijo en el AGENTS.md del proyecto.",
            impact_count=sum(s["consecutive_errors"] for s in streaks), sessions=[s["session"] for s in streaks], subject="streaks"))
    longs = t["long_turns"]
    if len(longs) >= 3:
        tools = Counter(l["main_tool"] for l in longs).most_common(3)
        out.append(_finding(
            "D3", "Turnos con exploración muy larga",
            "%d turnos superan %d llamadas a herramientas (mediana %d, p95 %d, máximo %d). Herramienta dominante: %s." % (
                len(longs), t["long_limit"], t["p50"], t["p95"], t["max"], ", ".join("`%s` (%d)" % x for x in tools)),
            "Delegar la búsqueda en el subagente `explore` o indicar en el prompt los ficheros de partida reduce el contexto del agente principal. "
            "El impacto es el coste total de esos turnos, no lo que se ahorraría.",
            impact_usd=sum(l["cost_usd"] for l in longs), impact_count=len(longs),
            sessions=[l["session"] for l in longs], subject="long_turns"))
    return out


def d4_context(ctx):
    c = ctx["tables"]["context"]
    out = []
    many = c["sessions_2plus"]
    if many:
        out.append(_finding(
            "D4", "Sesiones que se compactan varias veces",
            "%d sesiones con dos o más compactaciones (máximo %d); %d compactaciones en el periodo con un coste de %.2f $." % (
                len(many), many[0]["compactions"], c["compactions"], c["compaction_cost_usd"]),
            "Cada compactación resume y pierde detalle. Parte las tareas largas en sesiones distintas, guarda el plan en un fichero "
            "y delega búsquedas en subagentes.",
            impact_usd=c["compaction_cost_usd"], impact_count=c["compactions"],
            sessions=[s["session"] for s in many], subject="compactions"))
    for o in c["overflow_by_model"]:
        if o["errors"] >= 2:
            out.append(_finding(
                "D4", "`%s` desborda su contexto" % o["model"],
                "%d errores de desbordamiento de contexto en %d sesiones." % (o["errors"], len(o["sessions"])),
                "Usa `%s` solo para tareas cortas o define su límite real (`CONTEXT_LIMITS`, y en la configuración del proveedor) "
                "para que OpenCode compacte a tiempo." % o["model"],
                impact_count=o["errors"], sessions=o["sessions"], subject="overflow/" + o["model"]))
    return out


def d5_permissions(ctx):
    p = ctx["cur"]
    by_agent = defaultdict(lambda: {"n": 0, "tools": Counter(), "sessions": Counter(), "classes": Counter()})
    for c in p.calls:
        if c.status == "error" and c.error_class in ("permission_rule", "user_rejected"):
            agent = p.model_of(c)[1]
            g = by_agent[agent]
            g["n"] += 1
            g["tools"][c.tool] += 1
            g["sessions"][c.session] += 1
            g["classes"][c.error_class] += 1
    out = []
    for agent, g in by_agent.items():
        if g["n"] < 3:
            continue
        out.append(_finding(
            "D5", "El agente `%s` intenta acciones que no tiene permitidas" % agent,
            "%d llamadas bloqueadas (por regla de permisos: %d; rechazadas a mano: %d). Herramientas: %s." % (
                g["n"], g["classes"]["permission_rule"], g["classes"]["user_rejected"],
                ", ".join("`%s` (%d)" % x for x in g["tools"].most_common(3))),
            "Si el agente no debe editar, díselo en sus instrucciones («no edites ficheros; describe los cambios») para que no gaste "
            "turnos intentándolo. Si debe hacerlo, lanza esas tareas con `build` o ajusta sus permisos.",
            impact_usd=g["n"] * ctx["avg_call_cost"], impact_count=g["n"],
            sessions=[s for s, _ in g["sessions"].most_common(5)], subject=agent))
    return out


def d6_cache(ctx):
    out = []
    for m in ctx["tables"]["models"]:
        if m["input_uncached"] >= 5_000_000 and m["cache_hit"] < 0.5:
            out.append(_finding(
                "D6", "Caché casi sin uso en `%s/%s`" % (m["provider"], m["model"]),
                "Acierto de caché del %.0f %% con %.1f M de tokens de entrada sin caché%s." % (
                    100 * m["cache_hit"], m["input_uncached"] / 1e6,
                    "; el coste registrado es 0, así que el impacto es de latencia y cuota" if m["cost_usd"] == 0 else ""),
                "Comprueba si el proveedor admite caché de prompts y si está activada en su configuración. Si la admite, busca "
                "lo que cambia al principio del prompt en cada mensaje (fechas, ficheros de instrucciones que se regeneran).",
                impact_tokens=m["input_uncached"], subject="%s/%s" % (m["provider"], m["model"])))
    return out


def d7_skills(ctx):
    out = []
    for s in ctx["tables"]["skills"]:
        if s["kind"] == "skill" and s["errors"] >= 1:
            out.append(_finding(
                "D7", "La skill `%s` falla al cargarse" % s["name"],
                "%d de %d cargas terminaron en error." % (s["errors"], s["activations"]),
                "Si no está instalada, instálala o quita la referencia de la instrucción o comando que la pide; si lo está, revisa su ruta y su frontmatter.",
                impact_count=s["errors"], subject=s["name"]))
    return out


def d9_aborts(ctx):
    i = ctx["tables"]["interaction"]
    if i["aborted_quick_retry"] < 5:
        return []
    return [_finding(
        "D9", "Respuestas interrumpidas y vueltas a pedir",
        "%d respuestas se interrumpieron y en menos de dos minutos se volvió a escribir al agente (%d interrupciones en total)." % (
            i["aborted_quick_retry"], i["aborted"]),
        "Suele pasar cuando el agente arranca en mala dirección. Incluye en el primer mensaje el objetivo, los ficheros de partida "
        "y cómo se comprueba que está bien; si se repite en un comando, añade esos datos a su plantilla.",
        impact_usd=i["aborted_quick_retry_cost_usd"], impact_count=i["aborted_quick_retry"],
        sessions=i["aborted_quick_retry_sessions"], subject="aborts")]


def _model_key(pinned):
    """'github-copilot/claude-sonnet-4.6' -> ('github-copilot', 'claude-sonnet-4.6')."""
    prov, _, model = (pinned or "").partition("/")
    return (prov, model) if model else ("", prov)


def d8_pinned_models(ctx):
    """Commands whose pinned model shows problems in this period."""
    health = ctx["tables"]["model_health"]
    out = []
    for c in ctx["config"]["commands"]:
        if not c["model"] or not c["uses"]:
            continue
        _prov, model = _model_key(c["model"])
        h = health.get(model) or health.get(model.replace(".", "-")) or health.get(model.replace("-", "."))
        if not h:
            continue
        problems = []
        for t in h["tools"]:
            if t["errors"] >= 5 and t["rate"] >= 0.10:
                problems.append("`%s` falla el %.0f %% (%d de %d)" % (t["tool"], 100 * t["rate"], t["errors"], t["calls"]))
        nerr = sum(h["message_errors"].values())
        if nerr >= 5 and h["message_error_rate"] >= 0.03:
            problems.append("%d respuestas con error (%s)" % (nerr, ", ".join("%s %d" % kv for kv in sorted(h["message_errors"].items(), key=lambda x: -x[1]))))
        if not problems:
            continue
        out.append(_finding(
            "D8", "El comando `%s` usa un modelo con problemas" % c["name"],
            "Modelo fijado `%s`, usado %d veces por este comando en el periodo. En todo el periodo, con ese modelo: %s." % (
                c["model"], c["uses"], "; ".join(problems)),
            "Cambia `model:` en `commands/%s.md` por un modelo más estable para lo que hace el comando (compara la salud por modelo "
            "en la sección 4) y vuelve a mirar este informe tras unos cuantos usos." % c["name"],
            impact_count=c["uses"], subject=c["name"]))
    return out


def d10_commands(ctx):
    """Installed commands that are never used, or whose pinned provider never appears in the history."""
    cmds = ctx["config"]["commands"]
    if not cmds:
        return []
    out = []
    ever = ctx["providers_ever"]
    unreachable = [c for c in cmds if c["model"] and _model_key(c["model"])[0] and _model_key(c["model"])[0] not in ever]
    if unreachable:
        out.append(_finding(
            "D10", "Comandos con un proveedor que nunca se ha usado",
            "%s. Ningún mensaje del historial usa esos proveedores, así que puede que no estén configurados." % "; ".join(
                "`%s` → `%s`" % (c["name"], c["model"]) for c in unreachable),
            "Ejecuta cada uno una vez para comprobarlo; si fallan, cambia su `model:` por uno de un proveedor que sí uses o retíralos.",
            impact_count=len(unreachable), subject="unreachable"))
    if ctx["days"] >= 30:
        unused = [c for c in cmds if not c["uses"] and c not in unreachable]
        if len(unused) >= 3:
            out.append(_finding(
                "D10", "Comandos instalados que no se usan",
                "%d de %d comandos no se han usado en %d días: %s." % (len(unused), len(cmds), ctx["days"], ", ".join("`%s`" % c["name"] for c in unused)),
                "Retira los que ya no necesites o muévelos a una carpeta de archivo: cada comando ocupa sitio en la lista y en el mantenimiento.",
                impact_count=len(unused), subject="unused"))
    return out


def d11_fast_variants(ctx):
    models = ctx["tables"]["models"]
    fast = [m for m in models if (m["model"].endswith("-fast") or "-fast-" in m["model"]) and m["cost_usd"] > 0]
    share = sum(m["cost_share"] for m in fast)
    if not fast or share < 0.10:
        return []
    by_name = {m["model"]: m for m in models}
    parts = []
    for m in fast:
        base = by_name.get(m["model"].replace("-fast", ""))
        cmp = " frente a %.2f $ de `%s`" % (base["usd_per_mtok"], base["model"]) if base else ""
        parts.append("`%s` %.2f $ (%.2f $ por M tokens%s)" % (m["model"], m["cost_usd"], m["usd_per_mtok"], cmp))
    # what the same tokens would have cost at the normal variant's observed rate, when there is one
    saved = 0.0
    for m in fast:
        base = by_name.get(m["model"].replace("-fast", ""))
        saved += max(0.0, m["cost_usd"] - m["tokens"] * base["usd_per_mtok"] / 1e6) if base else 0.0
    return [_finding(
        "D11", "Las variantes `-fast` pesan en el coste",
        "%.0f %% del coste del periodo: %s." % (100 * share, "; ".join(parts)),
        "Las variantes rápidas cuestan bastante más por token. Úsalas solo cuando la latencia importe y deja la versión normal como predeterminada.",
        impact_usd=saved or None, impact_count=sum(m["messages"] for m in fast), subject="fast")]


DETECTORS = [d1_tool_errors, d2_agent_cost, d3_loops, d4_context, d5_permissions, d6_cache, d7_skills, d8_pinned_models,
             d9_aborts, d10_commands, d11_fast_variants]
