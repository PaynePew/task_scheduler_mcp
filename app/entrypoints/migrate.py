"""One-shot Alembic migration entrypoint.

Runs `alembic upgrade head`. No-ops until S02 lands the first migration.
"""

import subprocess
import sys


def main() -> None:
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        check=False,
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
