"""Command line: write the improvement report to standard output.

    docker compose exec exporter python /app/coach/cli.py report --window 30 > informe.md
    docker compose exec exporter python /app/coach/cli.py report --from 2026-10-01 --to 2026-10-31 --project mi-proyecto
"""
import argparse
import calendar
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import exporter  # noqa: E402  (the exporter's helpers: database, projects, commands)
from coach import render, report  # noqa: E402


def day_ms(text, end=False):
    t = calendar.timegm(time.strptime(text, "%Y-%m-%d")) * 1000
    return t + 86399999 if end else t


def main(argv=None):
    ap = argparse.ArgumentParser(prog="coach")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="informe de mejoras")
    r.add_argument("--window", type=int, default=30, help="días hacia atrás (0 = todo el historial)")
    r.add_argument("--from", dest="date_from", help="día inicial AAAA-MM-DD (UTC)")
    r.add_argument("--to", dest="date_to", help="día final AAAA-MM-DD (UTC)")
    r.add_argument("--project", action="append", help="proyecto (se puede repetir)")
    r.add_argument("--anon", action="store_true", help="sustituye los nombres de proyecto")
    r.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    now = int(time.time() * 1000)
    if args.date_from or args.date_to:
        since = day_ms(args.date_from) if args.date_from else 0
        until = day_ms(args.date_to, end=True) if args.date_to else now
        label = "del %s al %s" % (args.date_from or "inicio", args.date_to or "hoy")
    elif args.window > 0:
        since, until, label = now - args.window * 86400000, now, "últimos %d días" % args.window
    else:
        since, until, label = 0, now, "todo el historial"

    conn = exporter.connect()
    try:
        data = report.build(conn, exporter, since, until, label, set(args.project) if args.project else None, args.anon)
    finally:
        conn.close()
    out = render.render(data) if args.format == "md" else render.render_json(data)
    sys.stdout.buffer.write(out.encode("utf-8"))


if __name__ == "__main__":
    main()
