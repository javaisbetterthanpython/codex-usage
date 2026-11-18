"""Command-line entry point for codex-usage."""

import argparse
import json
import sys

from . import __version__
from .api import usage
from .auth import resolve_plan
from .report import render_limits, render_plain, render_short, warning_reached


def build_parser():
    p = argparse.ArgumentParser(
        prog='codex-usage',
        description="Show Codex plan rate limits, including the 5-hour window when available.",
    )
    p.add_argument('--json', action='store_const', dest='format', const='json',
                   help='print the raw usage JSON (alias for --format json)')
    p.add_argument('--warn', type=int, nargs='?', const=70, metavar='N',
                   help='exit 1 when any usage window is at or above N%% (default 70)')
    p.add_argument('--warn-7d', type=int, nargs='?', const=70, metavar='N',
                   help='exit 1 when the main 7-day window is at or above N%% (default 70)')
    p.add_argument('--all', action='store_true',
                   help='add per-limit details and the account summary (plain format)')
    p.add_argument('--format', choices=('plain', 'json', 'short'), default='plain',
                   help='output format (default: plain)')
    p.add_argument('-v', '--verbose', action='store_true',
                   help='log credential and usage sources and the plan to stderr')
    p.add_argument('-V', '--version', action='version', version='codex-usage %s' % __version__)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        out = usage(verbose=args.verbose)
        if args.verbose:
            plan = out['raw'].get('plan_type')
            if not isinstance(plan, str) or not plan:
                plan = resolve_plan()
            if plan:
                print('plan: ' + plan, file=sys.stderr)
        if args.format == 'json':
            json.dump(out['raw'], sys.stdout, indent=1)
            print()
        elif args.format == 'short':
            print(render_short(out))
        else:
            print(render_plain(out))
            if args.all:
                details = render_limits(out)
                if details:
                    print(details)
    except (RuntimeError, OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
        print('codex-usage: ' + str(exc), file=sys.stderr)
        return 1
    if args.warn is not None and warning_reached(out, args.warn):
        return 1
    if args.warn_7d is not None and warning_reached(out, args.warn_7d, weekly_only=True):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
