"""Prometheus series for the dashboard's «Mejoras» row.

They follow the exporter's pattern: the day or week is a label and the whole history is
recomputed from the database, so the dashboard can pick any range. Unlike the exporter's
own series they are computed in the background thread and cached (see `refresh`).

- Tool calls and errors by day, tool, error class, model and project, and compactions and
  interrupted answers by day: to see whether a change (a rule in AGENTS.md, another model)
  lowers the errors afterwards.
- Cost and messages by week and agent, tool calls and errors, compactions and interrupted
  answers by week: the trends of the row.
- The number and impact of the report's findings for the dashboard's periods.

Until the tool index is ready nothing is published but `coach_index_ready` 0.
"""
import datetime
import threading
import time
from collections import Counter

from . import report
from .index import INDEX

WINDOWS = (7, 30, 90, 36500)   # the dashboard's period choices
SERIES_EVERY = 120             # seconds between two computations of the series
FINDINGS_EVERY = 600           # seconds between two computations of the findings

# Everything is computed in the exporter's background thread and cached here: a scrape only
# copies text, so it costs no more than before this module existed. On a Windows bind mount
# the extra database reads made scrapes pass Prometheus' 10 s timeout.
_state = {"series": [], "series_updated": 0, "by_window": {}, "findings_updated": 0}
_lock = threading.Lock()


def _day(ms):
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def _week(ms):
    d = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).date()
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _lab(**kw):
    return ",".join('%s="%s"' % (k, _esc(v)) for k, v in kw.items())


def refresh(conn, helpers, weeks=13):
    """Recompute the series and the findings when they are due. Called from the background thread."""
    if not INDEX.ready:
        return
    now = time.time()
    series_due = now - _state["series_updated"] >= SERIES_EVERY
    findings_due = now - _state["findings_updated"] >= FINDINGS_EVERY
    if not (series_due or findings_due):
        return
    loaded = report.load(conn, helpers, refresh=False)  # read the database once for both
    if series_due:
        series = _series(loaded, weeks)
        with _lock:
            _state.update(series=series, series_updated=time.time())
    if findings_due:
        ms = int(time.time() * 1000)
        by_window = {}
        for w in WINDOWS:
            data = report.build(conn, helpers, ms - w * 86400000, ms, "w%d" % w, loaded=loaded)
            by_window[w] = [(f["detector"], f["severity"], f.get("impact_usd") or 0.0) for f in data["findings"]]
        with _lock:
            _state.update(by_window=by_window, findings_updated=time.time())


def lines(*_ignored):
    """What a scrape publishes: the cached series and findings. Reads nothing from the database."""
    with _lock:
        series = list(_state["series"])
        by_window = dict(_state["by_window"])
        s_upd, f_upd = _state["series_updated"], _state["findings_updated"]
    out = list(series)
    add = out.append
    add("# TYPE coach_index_ready gauge")
    add("coach_index_ready %d" % (1 if INDEX.ready else 0))
    add("# TYPE coach_findings gauge")
    add("# TYPE coach_finding_impact_usd gauge")
    for w, items in by_window.items():
        count, impact = Counter(), Counter()
        for det, sev, usd in items:
            count[(det, sev)] += 1
            impact[det] += usd
        for (det, sev), n in count.items():
            add("coach_findings{%s} %d" % (_lab(window=w, detector=det, rule=report.RULES.get(det, det), severity=sev), n))
        for det, v in impact.items():
            add("coach_finding_impact_usd{%s} %s" % (_lab(window=w, detector=det, rule=report.RULES.get(det, det)), round(v, 4)))
    add("# TYPE coach_findings_updated_timestamp_seconds gauge")
    add("coach_findings_updated_timestamp_seconds %s" % round(f_upd))
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
