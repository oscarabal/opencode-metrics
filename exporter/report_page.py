"""HTML page that shows the improvement report, with buttons to download or copy its Markdown.

The report is Markdown written for an AI chat (coach/render.py); this module turns the subset it
uses (front matter, headings, paragraphs, lists, tables, bold, italics and code) into HTML, so the
page needs no library and nothing from the internet.
"""
import json
import re

from session_page import CSS, e

REPORT_CSS = """
.bar-top{position:sticky;top:0;z-index:2;background:var(--bg);display:flex;gap:10px;flex-wrap:wrap;align-items:center;
 justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--line);margin-bottom:8px}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.btn{display:inline-block;padding:7px 14px;border-radius:5px;border:1px solid var(--line);background:var(--surface);color:var(--fg);
 font:inherit;font-size:14px;cursor:pointer;text-decoration:none}
.btn:hover{border-color:var(--accent)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.md h1{font-size:26px;margin:18px 0 6px}
.md h2{font-size:19px;margin:30px 0 10px;text-transform:none;letter-spacing:0;color:var(--fg);border-bottom:1px solid var(--line);padding-bottom:4px}
.md h3{font-size:16px;margin:22px 0 8px}
.md p,.md li{max-width:80ch}
.md ul,.md ol{padding-left:22px}
.md li{margin:3px 0}
.md code{font:13px/1.4 ui-monospace,Consolas,monospace;background:var(--free);padding:1px 5px;border-radius:3px;overflow-wrap:anywhere}
.md .card{margin:10px 0}
.md td{vertical-align:top}
.meta dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:0;font-size:14px}
.meta dt{color:var(--muted)}
.meta dd{margin:0}
#msg{color:var(--muted);font-size:13px}
"""

REPORT_JS = """
var md=JSON.parse(document.getElementById('md').textContent),name=%s,msg=document.getElementById('msg');
function say(t){msg.textContent=t;setTimeout(function(){msg.textContent=''},2500)}
document.getElementById('dl').addEventListener('click',function(ev){
 try{var u=URL.createObjectURL(new Blob([md],{type:'text/markdown;charset=utf-8'})),a=document.createElement('a');
  a.href=u;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(function(){URL.revokeObjectURL(u)},1000);ev.preventDefault()}
 catch(x){/* falls back to the link's own address, /report.md */}});
document.getElementById('cp').addEventListener('click',function(){
 if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(md).then(function(){say('Copiado.')},function(){say('No se pudo copiar.')});return}
 var t=document.createElement('textarea');t.value=md;document.body.appendChild(t);t.select();
 try{document.execCommand('copy');say('Copiado.')}catch(x){say('No se pudo copiar.')}t.remove()});
"""

_NUM = re.compile(r"[−+\-]?[\d.,]+\s?(\$|%|k|M|pp|s)?|—")
_SESSION = re.compile(r"ses_[A-Za-z0-9]+")


def _inline(text):
    """Escape and apply the inline marks: `code`, **bold** and _italics_. Session ids become links."""
    codes = []

    def keep(m):
        inner = m.group(1)
        html = "<code>%s</code>" % e(inner)
        if _SESSION.fullmatch(inner):
            html = '<a href="/session/%s" target="_blank" rel="noopener">%s</a>' % (e(inner), html)
        codes.append(html)
        return "\x00%d\x00" % (len(codes) - 1)

    text = re.sub(r"`([^`]+)`", keep, text)
    text = e(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w])_(.+?)_(?![\w])", r"<i>\1</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _cells(line):
    parts = re.split(r"(?<!\\)\|", line.strip().strip("|") if line.strip().endswith("|") else line.strip().lstrip("|"))
    return [p.strip().replace("\\|", "|") for p in parts]


def _table(lines):
    head = _cells(lines[0])
    body = [_cells(l) for l in lines[2:]] if len(lines) > 1 and re.fullmatch(r"[\s|:\-]+", lines[1]) else [_cells(l) for l in lines[1:]]
    num = [bool(body) and all(_NUM.fullmatch(r[i]) for r in body if i < len(r)) for i in range(len(head))]
    th = "".join('<th class="%s">%s</th>' % ("n" if num[i] else "", _inline(h)) for i, h in enumerate(head))
    rows = "".join("<tr>%s</tr>" % "".join('<td class="%s">%s</td>' % ("n" if i < len(num) and num[i] else "", _inline(c))
                                           for i, c in enumerate(r)) for r in body)
    return '<div class="card scroll"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (th, rows)


def to_html(md):
    """The report's Markdown as HTML, and its front matter as (key, value) pairs."""
    lines = md.split("\n")
    meta, i = [], 0
    if lines and lines[0].strip() == "---" and "---" in [l.strip() for l in lines[1:]]:
        end = [l.strip() for l in lines].index("---", 1)
        meta = [tuple(x.strip() for x in l.split(":", 1)) for l in lines[1:end] if ":" in l]
        i = end + 1
    out = []
    block = re.compile(r"(#{1,6} |\||[-*] |\d+\. )")
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        h = re.match(r"(#{1,6}) (.*)", line)
        if h:
            n = len(h.group(1))
            out.append("<h%d>%s</h%d>" % (n, _inline(h.group(2)), n))
            i += 1
        elif line.lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            out.append(_table(lines[i:j]))
            i = j
        elif re.match(r"\s*([-*]|\d+\.) ", line):
            ordered = bool(re.match(r"\s*\d+\. ", line))
            items = []
            while i < len(lines) and re.match(r"\s*([-*]|\d+\.) ", lines[i]):
                items.append(re.sub(r"\s*([-*]|\d+\.) ", "", lines[i], count=1))
                i += 1
                while i < len(lines) and lines[i].startswith("  ") and lines[i].strip() and not re.match(r"\s*([-*]|\d+\.) ", lines[i]):
                    items[-1] += " " + lines[i].strip()
                    i += 1
            tag = "ol" if ordered else "ul"
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % _inline(t) for t in items), tag))
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not block.match(lines[i].lstrip()):
                para.append(lines[i].strip())
                i += 1
            out.append("<p>%s</p>" % _inline(" ".join(para)))
    return "\n".join(out), meta


def render(md, filename, grafana_url, download_href, anon, anon_href):
    body, meta = to_html(md)
    labels = {"periodo": "Periodo", "proyectos": "Proyectos", "generado": "Generado", "anonimizado": "Anonimizado",
              "fuente": "Fuente", "informe": "Informe", "formato": "Formato"}
    dl = "".join("<dt>%s</dt><dd>%s</dd>" % (e(labels.get(k, k)), e(v)) for k, v in meta if k in ("periodo", "proyectos", "generado", "anonimizado"))
    toggle = "<a href='%s'>%s</a>" % (e(anon_href), "Mostrar nombres de proyecto" if anon else "Ocultar nombres de proyecto")
    return ("<!doctype html><html lang='es'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Informe de mejoras · OpenCode</title><style>%s%s</style></head><body><div class='wrap'>"
            "<div class='bar-top'><div class='sub'><a href='%s'>← Dashboard</a> · <a href='/sessions'>Todas las sesiones</a> · %s</div>"
            "<div class='actions'><span id='msg' aria-live='polite'></span>"
            "<button id='cp' class='btn' type='button'>Copiar Markdown</button>"
            "<a id='dl' class='btn primary' href='%s' download='%s'>Descargar informe (.md)</a></div></div>"
            "%s<div class='md'>%s</div>"
            "<p class='note'>El informe descargado es exactamente este texto en Markdown, pensado para pegarlo en un chat con una IA. "
            "No contiene texto de las conversaciones.</p>"
            "<script type='application/json' id='md'>%s</script><script>%s</script></div></body></html>"
            % (CSS, REPORT_CSS, e(grafana_url), toggle, e(download_href), e(filename),
               "<div class='card meta'><dl>%s</dl></div>" % dl if dl else "", body,
               json.dumps(md, ensure_ascii=False).replace("<", "\\u003c"), REPORT_JS % json.dumps(filename)))


def error(message, grafana_url):
    return ("<!doctype html><html lang='es'><meta charset='utf-8'><title>Informe de mejoras</title><style>%s</style>"
            "<body><div class='wrap'><h1>No se pudo generar el informe</h1><p class='sub'>%s</p>"
            "<a href='%s'>← Dashboard</a></div></body></html>" % (CSS, e(message), e(grafana_url)))
