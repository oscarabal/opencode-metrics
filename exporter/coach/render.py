"""Markdown rendering of the report, written to be pasted into an AI chat."""
import json

AI_PROMPT = """Eres un revisor experto en el uso de agentes de programación (OpenCode). Este informe resume cómo los usa una persona: coste, modelos, agentes, herramientas, errores, contexto, skills y comandos. No contiene el texto de las conversaciones.

Tu tarea:

1. Revisa los hallazgos de la sección 2, que salen de reglas automáticas: confirma, matiza o descarta cada uno con los datos de las demás secciones.
2. Busca en las tablas otras mejoras que las reglas no cubran. En particular, cruza el modelo fijado de cada comando (sección 6) con la salud de ese modelo (sección 4: errores por herramienta y errores de respuesta), y el coste de cada agente dentro y fuera de comandos (sección 3) con los modelos que usa.
3. Entrega una lista priorizada por impacto. Para cada mejora indica: qué cambiar exactamente (una regla lista para pegar en AGENTS.md, el modelo que fijar en un agente o comando, una skill o comando que crear, dividir o retirar, un hábito al escribir prompts), por qué (cifras de este informe), el impacto esperado y cómo comprobar después que funcionó.

Reglas: usa solo las cifras del informe y no inventes otras; si la muestra es pequeña, dilo; si algo no se puede saber con estos datos, di qué dato faltaría. El coste es una estimación de OpenCode (coste equivalente), no una factura."""

LABELS = {
    "cost_usd": "Coste equivalente", "tokens": "Tokens", "cache_hit": "Acierto de caché", "sessions": "Sesiones",
    "turns": "Turnos", "assistant_messages": "Mensajes del asistente", "tool_calls": "Llamadas a herramientas",
    "tool_errors": "Errores de herramientas", "tool_error_rate": "Tasa de error de herramientas",
    "compactions": "Compactaciones", "aborted": "Respuestas interrumpidas",
}
PCT = {"cache_hit", "tool_error_rate", "cost_share", "error_rate", "rate", "subagent_cost_share"}


def usd(v):
    return "—" if v is None else "%.2f $" % v if abs(v) >= 0.01 or v == 0 else "%.4f $" % v


def usd4(v):
    return "—" if v is None else "%.4f $" % v


def tok(v):
    return "%.1f M" % (v / 1e6) if v >= 1e6 else "%.0f k" % (v / 1e3) if v >= 1e3 else "%d" % v


def pct(v):
    return "%.1f %%" % (100 * v)


def fmt(key, v):
    if v is None:
        return "—"
    if key in PCT:
        return pct(v)
    if key.endswith("usd") or key.startswith("cost"):
        return usd(v)
    if key.startswith("tokens"):
        return tok(v)
    return str(v)


def _rate(a, b):
    return a / b if b else 0.0


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")
    return "\n".join(out)


def cut(rows, n=10):
    return rows[:n], max(0, len(rows) - n)


def render(r):
    s = r["summary"]
    prev = r["previous"]
    t = r["tables"]
    L = []
    add = L.append
    add("---")
    add("informe: mejoras de uso de agentes")
    add("fuente: %s" % r["source"])
    add("generado: %s" % r["generated"])
    add("periodo: %s (%s a %s, UTC)" % (r["period"]["label"], r["period"]["from"], r["period"]["to"]))
    add("proyectos: %s" % (", ".join(r["projects"]) if isinstance(r["projects"], list) else r["projects"]))
    add("anonimizado: %s" % ("sí" if r["anonymized"] else "no"))
    add("formato: %d" % r["format_version"])
    add("---")
    add("")
    add("# Informe de uso de OpenCode")
    add("")
    add("## 0. Instrucciones para la IA")
    add("")
    add(AI_PROMPT)
    add("")

    add("## 1. Resumen")
    add("")
    rows = []
    for k, label in LABELS.items():
        cur = fmt(k, s[k])
        if prev:
            pv = prev[k]
            delta = ""
            if k in PCT and isinstance(pv, (int, float)):
                delta = " (%+.1f pp)" % (100 * (s[k] - pv))
            elif isinstance(pv, (int, float)) and pv:
                delta = " (%+.0f %%)" % (100 * (s[k] - pv) / pv)
            rows.append((label, cur, fmt(k, pv), delta.strip(" ()") if delta else "—"))
        else:
            rows.append((label, cur))
    add(table(("Indicador", "Periodo", "Periodo anterior", "Variación") if prev else ("Indicador", "Periodo"), rows))
    if not prev:
        add("")
        add("_Sin periodo anterior comparable._")
    add("")

    add("## 2. Hallazgos detectados")
    add("")
    if not r["findings"]:
        add("Las reglas automáticas no han encontrado nada que supere sus umbrales en este periodo.")
    for f in r["findings"]:
        add("### %s · %s · severidad %s" % (f["id"], f["title"], f["severity"]))
        add("")
        add("- **Evidencia:** %s" % f["evidence"])
        if f["recommendation"]:
            add("- **Recomendación:** %s" % f["recommendation"])
        imp = []
        if f.get("impact_usd"):
            imp.append("≈ %s" % usd(f["impact_usd"]))
        if f.get("impact_tokens"):
            imp.append("%s de tokens" % tok(f["impact_tokens"]))
        if f.get("impact_count"):
            imp.append("%d caso%s" % (f["impact_count"], "" if f["impact_count"] == 1 else "s"))
        if imp:
            add("- **Impacto en el periodo:** %s" % ", ".join(imp))
        if f["sessions"]:
            add("- **Sesiones de ejemplo:** %s" % ", ".join("`%s`" % x for x in f["sessions"]))
        add("")

    add("## 3. Coste por agente y modelo")
    add("")
    add(table(("Agente", "Mensajes", "Coste", "% coste", "Coste/mensaje", "Salida/mensaje", "Modelos principales",
               "Coste en comandos", "Coste fuera de comandos", "Modelos fuera de comandos"),
              [(a["agent"], a["messages"], usd(a["cost_usd"]), pct(a["cost_share"]), usd4(a["cost_per_message"]),
                tok(a["output_per_message"]), ", ".join(a["top_models"]), usd(a["cost_in_commands"]),
                usd(a["cost_outside_commands"]), ", ".join(a["top_models_outside_commands"]) or "—") for a in t["agents"]]))
    add("")
    rows, rest = cut(t["models"])
    add(table(("Proveedor/modelo", "Mensajes", "Coste", "% coste", "Tokens", "$ por M tokens", "Acierto de caché"),
              [("%s/%s" % (m["provider"], m["model"]), m["messages"], usd(m["cost_usd"]), pct(m["cost_share"]), tok(m["tokens"]),
                "%.2f" % m["usd_per_mtok"], pct(m["cache_hit"])) for m in rows]))
    if rest:
        add("")
        add("_Y %d modelos más con menos coste._" % rest)
    add("")
    if len(t["projects"]) > 1:
        add(table(("Proyecto", "Mensajes", "Coste"), [(p["project"], p["messages"], usd(p["cost_usd"])) for p in t["projects"][:TOP_PROJECTS]]))
        add("")

    add("## 4. Herramientas")
    add("")
    rows, rest = cut(t["tools"], 15)
    add(table(("Herramienta", "Llamadas", "Errores", "Tasa", "Error más frecuente"),
              [(x["tool"], x["calls"], x["errors"], pct(x["error_rate"]), x["main_error"] or "—") for x in rows]))
    if rest:
        add("")
        add("_Y %d herramientas más con menos llamadas._" % rest)
    add("")
    add("Errores por herramienta, clase y modelo (los %d grupos más frecuentes):" % min(15, len(t["tool_errors"])))
    add("")
    add(table(("Herramienta", "Clase de error", "Modelo", "Errores", "Llamadas de esa herramienta con ese modelo", "Tasa"),
              [(x["tool"], x["error_class"], x["model"], x["errors"], x["calls_tool_model"], pct(x["rate"])) for x in t["tool_errors"][:15]]))
    add("")
    health = sorted(t["model_health"].items(), key=lambda kv: -kv[1]["messages"])[:12]
    if health:
        add("Salud por modelo (los 12 con más mensajes; errores evitables, sin bloqueos por permisos ni interrupciones):")
        add("")
        add(table(("Modelo", "Mensajes", "Llamadas", "Errores de herramientas", "Tasa", "Herramienta que más falla", "Errores de respuesta"),
                  [(m, h["messages"], h["tool_calls"], h["tool_errors"], pct(_rate(h["tool_errors"], h["tool_calls"])),
                    ("%s (%d de %d)" % (h["tools"][0]["tool"], h["tools"][0]["errors"], h["tools"][0]["calls"])) if h["tools"] else "—",
                    ", ".join("%s %d" % kv for kv in sorted(h["message_errors"].items(), key=lambda x: -x[1])) or "—")
                   for m, h in health]))
        add("")
    tr = t["turns"]
    add("Llamadas por turno: mediana %d, p90 %d, p95 %d, máximo %d, en %d turnos con herramientas. "
        "Turnos con llamadas repetidas (mismos argumentos 3 o más veces): %d. Turnos con 3 o más errores seguidos: %d. "
        "Turnos de más de %d llamadas: %d." % (tr["p50"], tr["p90"], tr["p95"], tr["max"], tr["turns_with_tools"],
                                               len(tr["loops"]), len(tr["error_streaks"]), tr["long_limit"], len(tr["long_turns"])))
    add("")

    add("## 5. Contexto y caché")
    add("")
    c = t["context"]
    add("- Compactaciones: %d en %d sesiones; %d sesiones con dos o más. Coste de las compactaciones: %s." % (
        c["compactions"], c["sessions_with_compaction"], len(c["sessions_2plus"]), usd(c["compaction_cost_usd"])))
    if c["overflow_by_model"]:
        add("- Desbordamientos de contexto: %s." % ", ".join("`%s` %d" % (o["model"], o["errors"]) for o in c["overflow_by_model"]))
    add("- El acierto de caché por modelo está en la sección 3.")
    add("")

    add("## 6. Skills y comandos")
    add("")
    if t["skills"]:
        add(table(("Tipo", "Nombre", "Usos", "Errores", "Coste de sus turnos", "Coste por uso", "Tokens por uso", "Agente principal"),
                  [("skill" if x["kind"] == "skill" else "comando", x["name"], x["activations"], x["errors"], usd(x["cost_usd"]),
                    usd4(x["cost_per_activation"]), tok(x["tokens_per_activation"]), x["main_agent"] or "—") for x in t["skills"]]))
        add("")
        add("El coste y los tokens de una skill o un comando son los del turno completo que la cargó o lo ejecutó: es una cota superior. "
            "Con proveedores de coste 0, los tokens son la medida útil.")
        twice = sorted({x["name"] for x in t["skills"] if x["same_name_as_other_kind"]})
        if twice:
            add("")
            add("Aparecen como skill y como comando a la vez: %s. Si el comando carga la skill del mismo nombre, su coste se cuenta en las dos filas."
                % ", ".join("`%s`" % n for n in twice))
    else:
        add("No se usaron skills ni comandos propios en el periodo.")
    add("")
    cfg = r["config"]
    if cfg["commands"]:
        add("Comandos instalados (carpeta global):")
        add("")
        add(table(("Comando", "Modelo fijado", "Último cambio", "Usos en el periodo"),
                  [(x["name"], x["model"] or "—", x["changed"] or "—", x["uses"]) for x in cfg["commands"]]))
        add("")
    add("_%s_" % cfg["note"])
    add("")

    add("## 7. Interacción")
    add("")
    i = t["interaction"]
    add("- Respuestas interrumpidas: %d (%s); en %d se volvió a escribir en menos de dos minutos." % (
        i["aborted"], ", ".join("%s %d" % kv for kv in i["aborted_by_agent"].items()) or "—", i["aborted_quick_retry"]))
    add("- Mensajes del usuario por sesión principal: mediana %d, p90 %d." % (
        i["user_messages_per_session_p50"], i["user_messages_per_session_p90"]))
    add("- Subagentes: %d sesiones, %s del coste." % (i["subagent_sessions"], pct(i["subagent_cost_share"])))
    if i["message_errors"]:
        add("- Errores de mensaje: %s." % ", ".join("%s %d" % kv for kv in sorted(i["message_errors"].items(), key=lambda x: -x[1])))
    add("")

    add("## 8. Glosario y limitaciones")
    add("")
    add("- **Coste:** el que OpenCode estima para cada mensaje según el precio público del modelo. Con suscripciones (p. ej. GitHub Copilot) no es lo que se paga; sirve para comparar.")
    add("- **Turno:** todos los mensajes del asistente que responden a un mismo mensaje del usuario.")
    add("- **Impacto ≈ $:** en errores y repeticiones, número de llamadas por el coste medio por llamada del periodo; en el coste por agente, lo que sobra respecto al coste por mensaje del resto de agentes juntos; en los turnos largos, su coste total; en las variantes `-fast`, lo que habrían costado sus tokens al precio observado de la versión normal. Son órdenes de magnitud.")
    add("- **Clases de error:** se obtienen del mensaje de error de cada herramienta; el texto no se guarda ni aparece aquí.")
    add("- **Sesiones de ejemplo:** el detalle de cada una está en `http://localhost:9101/session/<id>`, solo en la máquina que ejecuta la pila.")
    add("- Fechas en UTC. Generado en %.1f s." % r["build_seconds"])
    add("")
    return "\n".join(L)


TOP_PROJECTS = 10


def render_json(r):
    return json.dumps(r, ensure_ascii=False, indent=2)
