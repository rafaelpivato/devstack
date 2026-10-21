"""Allow explicit invocation via ``python -m devstack.cli``."""
from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
