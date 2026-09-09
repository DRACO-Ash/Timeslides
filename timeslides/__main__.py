"""`python -m timeslides` renders a report to a file, without a server.

Kept because the pipeline simulation and any local check needs a way to prove
the whole render path works, and because it is the closest thing to how the
original script was used.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from .audit import configure
from .config import load_settings
from .errors import TimeslidesError
from .groups import GroupStore
from .pipeline import RunSpec, build_report, demo_report, validate_modes, validate_sources
from .report.builder import write_report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m timeslides",
        description="Render the phase-offset waterfall to an HTML file.")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--modes", nargs="+", default=["REAL"])
    ap.add_argument("--sources", nargs="+", default=None)
    ap.add_argument("--invert", action="store_true")
    ap.add_argument("--group", action="append", default=[],
                    help="group name to render (repeatable); default is all live groups")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    settings = load_settings()
    configure(settings.log_level)
    end = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
    # Building the spec validates the modes and providers, so it belongs inside
    # the handler: a bad --modes value should print one line, not a traceback.
    try:
        spec = RunSpec(start=end - dt.timedelta(days=args.days), end=end,
                       modes=validate_modes(args.modes),
                       sources=validate_sources(args.sources),
                       invert=args.invert, classification=settings.classification)
        if settings.demo:
            html = demo_report(spec)
        else:
            from .udl import UDLClient
            store = GroupStore(settings.groups_file)
            store.seed_if_empty()
            groups = store.active()
            if args.group:
                wanted = {g.casefold() for g in args.group}
                groups = [g for g in groups if g["name"].casefold() in wanted]
                if not groups:
                    print("no saved group matched --group", file=sys.stderr)
                    return 2
            html = build_report(groups, UDLClient(settings), spec)
    except TimeslidesError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    out = write_report(html, args.out or f"phase_offset_{end:%Y%m%d}.html")
    print(f"Wrote {out} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
