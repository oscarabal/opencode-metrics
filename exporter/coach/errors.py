"""Reduce a tool's error message to a class.

The error text is tool output, not conversation. It is read only to pick a class and is
never stored or published.
"""
import re

# (class, pattern) in order; the first match wins
_RULES = [
    ("permission_rule", r"user has specified a rule which prevents"),
    ("user_rejected", r"user rejected permission|user dismissed|rejected by the user"),
    ("aborted", r"tool execution aborted|aborted"),
    ("patch_mismatch", r"apply_patch verification failed|failed to find expected lines|failed to apply patch|hunk"),
    ("old_string_missing", r"could not find oldstring|oldstring not found"),
    ("multiple_matches", r"found multiple matches|multiple matches for oldstring"),
    ("no_change", r"no changes to apply|oldstring and newstring are identical"),
    ("read_before_write", r"must read .* before|read the file before|has been modified since"),
    ("not_found", r"file not found|no such file or directory|not found:|does not exist|enoent|cannot find path"),
    ("schema_error", r"invalid arguments|schemaerror|invalid input|expected .* received"),
    ("output_too_large", r"exceeded \d+ bytes|too large|exceeds .*limit|output limit"),
    ("access_denied", r"access is denied|permission denied|eacces|eperm"),
    ("shell_error", r"cannot validate argument|is not recognized as|commandnotfoundexception|parameterbindingexception|exit code"),
    ("timeout", r"timed out|timeout"),
    ("network", r"econnrefused|enotfound|fetch failed|network|status code [45]\d\d|http [45]\d\d"),
    ("skill_missing", r"skill .* not found"),
]
_COMPILED = [(name, re.compile(pat, re.I | re.S)) for name, pat in _RULES]

# Plain-language label and what the user can change, by class
ADVICE = {
    "not_found": ("Rutas inexistentes",
                  "Añade a AGENTS.md una regla como «No adivines rutas: localiza los ficheros con glob o grep antes de leerlos». "
                  "Si se concentra en un proyecto, documenta su estructura de carpetas en el AGENTS.md del proyecto."),
    "patch_mismatch": ("El parche no coincide con el fichero",
                       "El modelo parchea con una versión desactualizada del fichero. Regla: «Vuelve a leer el fichero justo antes de "
                       "aplicar un parche». Si se concentra en un modelo, usa otro para editar o fija otro modelo en el agente que edita."),
    "old_string_missing": ("El texto a sustituir no está en el fichero",
                           "Regla: «Copia oldString literalmente de una lectura reciente del fichero, con la misma indentación»."),
    "multiple_matches": ("El texto a sustituir aparece varias veces",
                         "Regla: «Incluye en oldString contexto suficiente para que sea único»."),
    "no_change": ("Ediciones sin cambios", "El modelo intenta ediciones vacías; suele indicar que no ha verificado el resultado anterior."),
    "read_before_write": ("Escritura sin lectura previa", "Regla: «Lee siempre un fichero antes de modificarlo»."),
    "schema_error": ("Llamadas mal formadas",
                     "El modelo no respeta el esquema de la herramienta. Si se concentra en un modelo o un MCP, revisa su compatibilidad "
                     "o desactiva esa herramienta para ese modelo."),
    "output_too_large": ("Salida demasiado grande",
                         "Acota las búsquedas (rutas, patrones, exclusiones) o añade un .ignore para carpetas generadas."),
    "access_denied": ("Acceso denegado por el sistema", "Revisa desde qué carpeta se lanza OpenCode y excluye carpetas del sistema de las búsquedas."),
    "shell_error": ("Errores de shell", "Indica en AGENTS.md la shell disponible (p. ej. PowerShell en Windows) y sus comandos equivalentes."),
    "timeout": ("Tiempo agotado", "Divide las operaciones largas o aumenta el tiempo límite de la herramienta."),
    "network": ("Errores de red", "Revisa la conectividad o el servidor MCP afectado."),
    "skill_missing": ("Skill inexistente", "Una instrucción o comando menciona una skill que no está instalada: instálala o corrige la referencia."),
    "permission_rule": ("Bloqueado por una regla de permisos", ""),
    "user_rejected": ("Rechazado por el usuario", ""),
    "aborted": ("Interrumpido", ""),
    "other": ("Otros errores", "Revisa los casos en el detalle de las sesiones."),
}


def classify(error):
    text = error or ""
    for name, rx in _COMPILED:
        if rx.search(text):
            return name
    return "other"
