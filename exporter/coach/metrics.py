"""Prometheus series for the dashboard's «Mejoras» row.

They follow the exporter's pattern: the day or week is a label and the whole history is
recomputed from the database, so the dashboard can pick any range. Unlike the exporter's
own series they are computed in the background thread and cached (see `refresh`).

- Tool calls and errors by day, tool, error class, model and project, and compactions and
  interrupted answers by day: to see whether a change (a rule in AGENTS.md, another model)
  lowers the errors afterwards.
- Cost and messages by week and agent, tool calls and errors, compactions and interrupted
  answers by week: the trends of the row.

The data read for the series (messages, sessions, tool calls, compactions) is also kept, so
the dashboard's findings panels build the report from it instead of reading the database
again (see `snapshot`). The findings themselves are no longer published as series: the
panels ask the exporter for them with the dashboard's period and projects.

Until the tool index is ready nothing is published but `coach_index_ready` 0.
"""
import datetime
import threading
import time
from collections import Counter

from . import report
from .index import INDEX

SERIES_EVERY = 120             # seconds between two readings of the database for the series

# Everything is computed in the exporter's background thread and cached here: a scrape only
# copies text, so it costs no more than before this module existed. On a Windows bind mount
# the extra database reads made scrapes pass Prometheus' 10 s timeout.
_state = {"series": [], "series_updated": 0, "loaded": None, "loaded_at": 0}
_lock = threading.Lock()
_wake = threading.Event()   # set by a findings panel that finds the data old, to read it again sooner


def _day(ms):
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def _week(ms):
    d = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).date()
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _lab(**kw):
    return ",".join('%s="%s"' % (k, _esc(v)) for k, v in kw.items())


def refresh(conn, helpers, weeks=13, force=False):
    """Read the database and recompute the series when due, or now with `force`. Called from the background thread."""
    if not INDEX.ready:
        return
    if not force and time.time() - _state["series_updated"] < SERIES_EVERY:
        return
    read_at = time.time()
    loaded = report.load(conn, helpers, refresh=False)  # the thread has just refreshed the index
    series = _series(loaded, weeks)
    with _lock:
        _state.update(series=series, series_updated=time.time(), loaded=loaded, loaded_at=read_at)


def snapshot():
    """The data of the last reading and when it started, or (None, 0) before the first one."""
    with _lock:
        return _state["loaded"], _state["loaded_at"]


def request_refresh():
    """Ask the background thread to read the database now instead of at its next turn."""
    _wake.set()


def wait(seconds):
    """Sleep the background thread until its next turn or a request. True if it was a request."""
    woken = _wake.wait(seconds)
    _wake.clear()
    return woken


def lines(*_ignored):
    """What a scrape publishes: the cached series. Reads nothing from the database."""
    with _lock:
        series = list(_state["series"])
        s_upd = _state["series_updated"]
    out = list(series)
    add = out.append
    add("# TYPE coach_index_ready gauge")
    add("coach_index_ready %d" % (1 if INDEX.ready else 0))
    add("# TYPE coach_series_updated_timestamp_seconds gauge")
    add("coach_series_updated_timestamp_seconds %s" % round(s_upd))
    return out


def _series(loaded, weeks):
    out = []
    add = out.append
    msgs, sessions, calls, compactions = loaded
    by_id = {m.id: m for m in msgs}
    project = lambda sid: (sessions.get(sid) or {}).get("project", "global")
    week_from = (time.time() - weeks * 7 * 86400) * 1000

    # messages: interrupted answers by day; cost and messages by week and agent
    aborted_day, aborted_week = Counter(), Counter()
    agent_cost, agent_msgs = Counter(), Counter()
    for m in msgs:
        if m.role != "assistant":
            continue
        proj = project(m.session)
        if m.error == "MessageAbortedError":
            aborted_day[(_day(m.time), m.agent, proj)] += 1
        if m.time >= week_from:
            wk = _week(m.time)
            agent_cost[(wk, m.agent, proj)] += m.cost
            agent_msgs[(wk, m.agent, proj)] += 1
            if m.error == "MessageAbortedError":
                aborted_week[(wk, proj)] += 1

    add("# TYPE opencode_day_aborted gauge")
    for (day, agent, proj), n in aborted_day.items():
        add("opencode_day_aborted{%s} %d" % (_lab(day=day, agent=agent, project=proj), n))
    add("# TYPE opencode_weekly_agent_cost_usd gauge")
    for (wk, agent, proj), v in agent_cost.items():
        add("opencode_weekly_agent_cost_usd{%s} %s" % (_lab(week=wk, agent=agent, project=proj), v))
    add("# TYPE opencode_weekly_agent_messages gauge")
    for (wk, agent, proj), n in agent_msgs.items():
        add("opencode_weekly_agent_messages{%s} %d" % (_lab(week=wk, agent=agent, project=proj), n))
    add("# TYPE opencode_weekly_aborted gauge")
    for (wk, proj), n in aborted_week.items():
        add("opencode_weekly_aborted{%s} %d" % (_lab(week=wk, project=proj), n))

    if True:  # only called once the index is ready
        tc_day, te_day, tc_week, te_week = Counter(), Counter(), Counter(), Counter()
        for c in calls:
            if c.status in ("pending", "running"):
                continue
            m = by_id.get(c.message)
            model = m.model if m else "unknown"
            proj = project(c.session)
            day = _day(c.time)
            tc_day[(day, c.tool, model, proj)] += 1
            if c.status == "error":
                te_day[(day, c.tool, c.error_class, model, proj)] += 1
            if c.time >= week_from:
                wk = _week(c.time)
                tc_week[(wk, c.tool, proj)] += 1
                if c.status == "error":
                    te_week[(wk, c.tool, c.error_class, proj)] += 1
        comp_day, comp_week = Counter(), Counter()
        for sess, _mid, t in compactions:
            comp_day[(_day(t), project(sess))] += 1
            if t >= week_from:
                comp_week[(_week(t), project(sess))] += 1

        add("# TYPE opencode_day_tool_calls gauge")
        for (day, tool, model, proj), n in tc_day.items():
            add("opencode_day_tool_calls{%s} %d" % (_lab(day=day, tool=tool, model=model, project=proj), n))
        add("# TYPE opencode_day_tool_errors gauge")
        for (day, tool, cls, model, proj), n in te_day.items():
            add("opencode_day_tool_errors{%s} %d" % (_lab(day=day, tool=tool, error_class=cls, model=model, project=proj), n))
        add("# TYPE opencode_day_compactions gauge")
        for (day, proj), n in comp_day.items():
            add("opencode_day_compactions{%s} %d" % (_lab(day=day, project=proj), n))
        add("# TYPE opencode_weekly_tool_calls gauge")
        for (wk, tool, proj), n in tc_week.items():
            add("opencode_weekly_tool_calls{%s} %d" % (_lab(week=wk, tool=tool, project=proj), n))
        add("# TYPE opencode_weekly_tool_errors gauge")
        for (wk, tool, cls, proj), n in te_week.items():
            add("opencode_weekly_tool_errors{%s} %d" % (_lab(week=wk, tool=tool, error_class=cls, project=proj), n))
        add("# TYPE opencode_weekly_compactions gauge")
        for (wk, proj), n in comp_week.items():
            add("opencode_weekly_compactions{%s} %d" % (_lab(week=wk, project=proj), n))

    return out
