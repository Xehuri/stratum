"""Allow ``python -m stratum``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
