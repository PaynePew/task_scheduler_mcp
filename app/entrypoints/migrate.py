"""One-shot Alembic migration entrypoint.

Runs `alembic upgrade head` when `alembic.ini` is present. Until S02 lands
the first migration, exits 0 with a skip message so `docker compose up`
(default profile) stays green.
"""

import subprocess
import sys
from pathlib import Path


def main() -> None:
    if not Path("alembic.ini").is_file():
        print("migrate: no alembic.ini yet; skipping until S02 lands migrations.")
        sys.exit(0)

    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        check=False,
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
