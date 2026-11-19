"""HTML page with the detail of one OpenCode session. It only renders: the exporter reads the data."""
import html

PALETTE = {"input": "#5794F2", "cache_read": "#8AB8FF", "cache_write": "#2EC4B6", "output": "#F2994A"}
LABELS = {"input": "Entrada", "cache_read": "Caché leída", "cache_write": "Caché escrita", "output": "Salida"}

CSS = """
:root{--bg:#f6f6f3;--surface:#fff;--fg:#1d2127;--muted:#5d6672;--line:#dcdcd5;--free:#e4e4de;--accent:#5794F2}
@media (prefers-color-scheme:dark){:root{--bg:#14171b;--surface:#1c2025;--fg:#e8eaec;--muted:#9aa3ad;--line:#2d333a;--free:#2b3037}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif;padding:24px max(16px,env(safe-area-inset-left))}
.wrap{max-width:1000px;margin:0 auto}
a{color:var(--accent)}
h1{font-size:24px;margin:4px 0 6px;overflow-wrap:anywhere}
h2{font-size:15px;margin:28px 0 10px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.sub{color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:18px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:12px 14px}
.kpi b{display:block;font-size:22px}
.kpi span{color:var(--muted);font-size:13px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:14px 16px}
.ctxhead{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.bar{display:flex;height:16px;border-radius:4px;overflow:hidden;background:var(--free)}
.bar i{display:block;height:100%}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;font-size:13px;color:var(--muted)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--muted);font-weight:600;font-size:13px}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
svg{width:100%;height:auto;display:block}
svg text{fill:var(--muted);font-size:11px}
.note{color:var(--muted);font-size:13px;margin-top:28px}
"""


def e(v):
    return html.escape(str(v), quote=True)


def kilo(n):
    n = float(n)
    if n >= 1_000_000:
        return ("%.2f" % (n / 1_000_000)).rstrip("0").rstrip(".") + "M"
    if n >= 1000:
        return "%.1fk" % (n / 1000)
    return "%d" % n


def usd(v):
    return "$%.2f" % v if v >= 0.01 or v == 0 else "$%.3f" % v


def when(ms):
    """A moment as a <time> element that the page script shows in the reader's time zone."""
    if not ms:
        return "—"
    return '<time data-ms="%d"></time>' % ms


def duration(ms):
    s = int(ms / 1000)
    if s < 60:
        return "%d s" % s
    if s < 3600:
        return "%d min" % (s // 60)
    if s < 86400:
        return "%d h %d min" % (s // 3600, (s % 3600) // 60)
    return "%d d %d h" % (s // 86400, (s % 86400) // 3600)


def context_card(d):
    last = d["last"]
    if not last:
        return '<div class="card sub">Esta sesión no tiene mensajes del asistente con tokens.</div>'
    parts = {"input": last["input"], "cache_read": last["cache_read"], "cache_write": last["cache_write"], "output": last["output"]}
    used = sum(parts.values())
    limit = d["limit"]
    if limit:
        pct = used / limit * 100
        head = '<b>%.0f %% usado</b><span class="sub">%s / %s · quedan %s</span>' % (pct, kilo(used), kilo(limit), kilo(max(limit - used, 0)))
        scale = max(limit, used)
    else:
        head = '<b>%s tokens</b><span class="sub">límite del modelo desconocido</span>' % kilo(used)
        scale = used or 1
    segs = "".join('<i style="width:%.2f%%;background:%s" title="%s: %s"></i>' % (v / scale * 100, PALETTE[k], LABELS[k], kilo(v)) for k, v in parts.items() if v)
    legend = "".join('<span><i style="background:%s"></i>%s %s</span>' % (PALETTE[k], LABELS[k], kilo(v)) for k, v in parts.items())
    return ('<div class="card"><div class="ctxhead">%s</div><div class="bar">%s</div><div class="legend">%s'
            '<span><i style="background:var(--free)"></i>Libre</span></div>'
            '<div class="sub" style="margin-top:8px;font-size:13px">Modelo: %s (%s). Contexto del último mensaje respondido.</div></div>'
            % (head, segs, legend, e(last["model"]), e(last["provider"])))


def line_chart(points, title, fmt, color, marks=(), hline=None):
    """points: list of y values, one per message. marks: indexes where a compaction happened."""
    if len(points) < 2:
        return '<div class="card sub">%s: hacen falta al menos dos mensajes.</div>' % e(title)
    w, h, left, right, top, bottom = 760, 200, 56, 12, 14, 26
    top_value = max(max(points), hline or 0) * 1.1 or 1
    n = len(points)

    def x(i):
        return left + (w - left - right) * i / (n - 1)

    def y(v):
        return top + (h - top - bottom) * (1 - v / top_value)

    grid = ""
    for k in range(5):
        v = top_value * k / 4
        grid += '<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="currentColor" opacity=".12"/><text x="%d" y="%.1f" text-anchor="end">%s</text>' % (
            left, w - right, y(v), y(v), left - 6, y(v) + 4, fmt(v))
    path = " ".join("%s%.1f,%.1f" % ("M" if i == 0 else "L", x(i), y(v)) for i, v in enumerate(points))
    marked = "".join('<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="#E0B400" stroke-dasharray="3 3"/>' % (x(i), x(i), top, h - bottom) for i in marks if 0 <= i < n)
    limit_line = ""
    if hline:
        limit_line = '<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="#E24D42" stroke-dasharray="5 4"/><text x="%d" y="%.1f" text-anchor="end">límite %s</text>' % (
            left, w - right, y(hline), y(hline), w - right, y(hline) - 4, fmt(hline))
    axis = '<text x="%d" y="%d">mensaje 1</text><text x="%d" y="%d" text-anchor="end">mensaje %d</text>' % (left, h - 8, w - right, h - 8, n)
    note = ' · las líneas amarillas son compactaciones' if marks else ''
    return ('<div class="card"><div class="sub" style="font-size:13px;margin-bottom:6px">%s%s</div><svg viewBox="0 0 %d %d" role="img" aria-label="%s">%s%s%s'
            '<path d="%s" fill="none" stroke="%s" stroke-width="2"/>%s</svg></div>'
            % (e(title), note, w, h, e(title), grid, limit_line, marked, path, color, axis))


def table(headers, rows, numeric=()):
    if not rows:
        return '<div class="card sub">Sin datos.</div>'
    head = "".join('<th class="%s">%s</th>' % ("n" if i in numeric else "", e(hd)) for i, hd in enumerate(headers))
    body = "".join("<tr>%s</tr>" % "".join('<td class="%s">%s</td>' % ("n" if i in numeric else "", c) for i, c in enumerate(r)) for r in rows)
    return '<div class="card scroll"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (head, body)


def render(d, grafana_url):
    msgs = d["messages"]
    total_cost = sum(m["cost"] for m in msgs)
    total_tokens = sum(sum(m["tokens"].values()) for m in msgs)
    ctx_series = [sum(m["tokens"].values()) for m in msgs]
    cum, running = [], 0.0
    for m in msgs:
        running += m["cost"]
        cum.append(running)
    times = [m["t"] for m in msgs]
    marks = [sum(1 for t in times if t <= c) for c in d["compactions"]]

    by_model = {}
    for m in msgs:
        k = (m["model"], m["provider"], m["agent"])
        row = by_model.setdefault(k, [0, 0.0, 0])
        row[0] += 1
        row[1] += m["cost"]
        row[2] += sum(m["tokens"].values())
    model_rows = [[e(k[0]), e(k[1]), e(k[2]), str(v[0]), kilo(v[2]), usd(v[1])] for k, v in sorted(by_model.items(), key=lambda kv: -kv[1][1])]
    tool_rows = [[e(n), str(v[0]), str(v[1])] for n, v in sorted(d["tools"].items(), key=lambda kv: -kv[1][0])]
    skill_rows = [[e(n), "Comando" if k == "command" else "Skill", str(c)] for (k, n), c in sorted(d["skills"].items(), key=lambda kv: -kv[1])]
    child_rows = [['<a href="/session/%s">%s</a>' % (e(cid), e(title or cid)), usd(cost), when(upd)] for cid, title, cost, upd in d["children"]]

    span = (d["updated"] - d["created"]) if d["updated"] and d["created"] else 0
    meta = " · ".join(x for x in (e(d["project"]), "%d mensajes del asistente" % len(msgs), "duración %s" % duration(span)) if x)
    parent = ""
    if d["parent"]:
        parent = ' · <a href="/session/%s">sesión padre</a>' % e(d["parent"])
    diff = "+%d −%d en %d archivos" % (d["additions"], d["deletions"], d["files"]) if d["files"] else "sin cambios en archivos"

    parts = [
        "<!doctype html><html lang='es'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>%s · Sesión de OpenCode</title><style>%s</style></head><body><div class='wrap'>" % (e(d["title"] or d["id"]), CSS),
        "<div class='sub'><a href='%s'>← Dashboard</a> · <a href='/sessions'>Todas las sesiones</a>%s</div>" % (e(grafana_url), parent),
        "<h1>%s</h1><div class='sub'>%s</div>" % (e(d["title"] or d["id"]), meta),
        "<div class='sub'>Creada %s · actualizada %s</div>" % (when(d["created"]), when(d["updated"])),
        "<div class='kpis'>",
        "<div class='kpi'><b>%s</b><span>Coste estimado</span></div>" % usd(total_cost),
        "<div class='kpi'><b>%s</b><span>Tokens</span></div>" % kilo(total_tokens),
        "<div class='kpi'><b>%s</b><span>Cambios</span></div>" % e(diff),
        "<div class='kpi'><b>%d</b><span>Herramientas llamadas</span></div>" % sum(v[0] for v in d["tools"].values()),
        "<div class='kpi'><b>%d</b><span>Compactaciones</span></div>" % len(d["compactions"]),
        "</div>",
        "<h2>Contexto</h2>", context_card(d),
        "<h2>Evolución</h2>",
        line_chart(ctx_series, "Tamaño del contexto en cada mensaje (tokens)", kilo, PALETTE["input"], marks, d["limit"] if d["limit"] and d["limit"] <= max(ctx_series or [1]) * 3 else None),
        "<div style='height:12px'></div>",
        line_chart(cum, "Coste acumulado (USD)", usd, PALETTE["output"]),
        "<h2>Modelos y agentes</h2>", table(["Modelo", "Proveedor", "Agente", "Mensajes", "Tokens", "Coste"], model_rows, numeric=(3, 4, 5)),
        "<h2>Skills y comandos</h2>", table(["Nombre", "Tipo", "Veces"], skill_rows, numeric=(2,)),
        "<h2>Herramientas</h2>", table(["Herramienta", "Llamadas", "Errores"], tool_rows, numeric=(1, 2)),
    ]
    if child_rows:
        parts += ["<h2>Sesiones hijas (subagentes)</h2>", table(["Sesión", "Coste", "Actualizada"], child_rows, numeric=(1,))]
    parts += [
        "<p class='note'>Datos de la base de datos de OpenCode, leídos en solo lectura. Aquí no aparece el texto de la conversación. "
        "El coste es una estimación de OpenCode, no una factura.</p>",
        "<script>document.querySelectorAll('time[data-ms]').forEach(function(t){t.textContent=new Date(+t.dataset.ms).toLocaleString()})</script>",
        "</div></body></html>",
    ]
    return "".join(parts)


def not_found(grafana_url):
    return ("<!doctype html><html lang='es'><meta charset='utf-8'><title>Sesión no encontrada</title><style>%s</style>"
            "<body><div class='wrap'><h1>Sesión no encontrada</h1><p class='sub'>OpenCode no tiene una sesión con ese identificador.</p>"
            "<a href='%s'>← Dashboard</a></div></body></html>" % (CSS, e(grafana_url)))


LIST_CSS = """
.tools{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:14px 0}
input[type=search]{padding:7px 10px;border:1px solid var(--line);border-radius:5px;background:var(--surface);color:var(--fg);min-width:240px;font:inherit}
th.s{cursor:pointer;user-select:none}
th.s:hover{color:var(--fg)}
td a{overflow-wrap:anywhere}
.tag{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:1px 7px;margin-left:6px}
"""

LIST_JS = """
var q=document.getElementById('q'),tb=document.querySelector('tbody'),rows=[].slice.call(tb.rows),cnt=document.getElementById('cnt');
function filt(){var v=q.value.toLowerCase(),n=0;rows.forEach(function(r){var ok=r.textContent.toLowerCase().indexOf(v)>=0;r.style.display=ok?'':'none';if(ok)n++});cnt.textContent=n}
q.addEventListener('input',filt);
document.querySelectorAll('th.s').forEach(function(th,i){var asc=false;th.addEventListener('click',function(){asc=!asc;
 rows.sort(function(a,b){var x=a.cells[i].dataset.v||a.cells[i].textContent,y=b.cells[i].dataset.v||b.cells[i].textContent,nx=parseFloat(x),ny=parseFloat(y);
  var c=(!isNaN(nx)&&!isNaN(ny))?nx-ny:String(x).localeCompare(String(y));return asc?c:-c});
 rows.forEach(function(r){tb.appendChild(r)})})});
document.querySelectorAll('time[data-ms]').forEach(function(t){t.textContent=new Date(+t.dataset.ms).toLocaleString()});
"""


def render_list(rows, grafana_url, subagents, note, toggle_href):
    """rows: (id, title, parent, project, created, updated, messages, cost, tokens, agent)"""
    def cell(v, text):
        return '<td class="n" data-v="%s">%s</td>' % (e(v), text)

    body = []
    for sid, title, parent, project, created, updated, msgs, cost, tokens, agent in rows:
        tag = '<span class="tag">subagente</span>' if parent else ""
        body.append('<tr><td><a href="/session/%s">%s</a>%s</td><td>%s</td><td>%s</td>%s%s%s%s%s</tr>' % (
            e(sid), e(title or sid), tag, e(project), e(agent or "—"), cell(msgs, str(msgs)), cell(tokens, kilo(tokens)),
            cell(cost, usd(cost)), cell(created, when(created)), cell(updated, when(updated))))
    heads = ["Sesión", "Proyecto", "Agente", "Mensajes", "Tokens", "Coste", "Creada", "Actualizada"]
    th = "".join('<th class="s%s">%s</th>' % (" n" if i >= 3 else "", h) for i, h in enumerate(heads))
    toggle = "<a href='%s'>%s</a>" % (e(toggle_href), "Ocultar subagentes" if subagents else "Incluir subagentes")
    return ("<!doctype html><html lang='es'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Sesiones de OpenCode</title><style>%s%s</style></head><body><div class='wrap'>"
            "<div class='sub'><a href='%s'>← Dashboard</a></div><h1>Sesiones</h1>"
            "<div class='sub'>%s</div>"
            "<div class='tools'><input id='q' type='search' placeholder='Filtrar por título, proyecto o agente' aria-label='Filtrar'>"
            "<span class='sub'><b id='cnt'>%d</b> de %d sesiones</span><span class='sub'>%s</span></div>"
            "<div class='card scroll'><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>"
            "<p class='note'>Pulsa una cabecera para ordenar. El coste es una estimación de OpenCode, no una factura. Aquí no aparece el texto de la conversación.</p>"
            "<script>%s</script></div></body></html>"
            % (CSS, LIST_CSS, e(grafana_url), e(note), len(rows), len(rows), toggle, th, "".join(body), LIST_JS))
