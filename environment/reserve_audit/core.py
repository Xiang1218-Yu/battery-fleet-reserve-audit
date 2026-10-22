"""Starting point for the battery-fleet reserve-audit repair task.

The executable contract is intentionally present, but the audit engine is not
implemented.  Participants must implement the complete input-validation,
telemetry, dispatch-feasibility, contingency, and atomic-reporting behavior
specified in instruction.md.
"""

import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.parse_args(argv)
    print("NOT_IMPLEMENTED: reserve audit implementation is incomplete", file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
