"""Console dispatch: no arguments runs MCP; legacy commands remain setup helpers."""

import sys
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] == "server":
        from .server import main as server_main
        server_main()
        return
    if args[0] == "doctor":
        args[0] = "status"
    original = sys.argv
    try:
        sys.argv = [original[0]] + args
        from cli import main as legacy_main
        legacy_main()
    finally:
        sys.argv = original


if __name__ == "__main__":
    main()
