"""Enter deployment credentials directly on the server without displaying them."""

import getpass
import os
import re
import tempfile
from pathlib import Path

KEYS = ("OPENPROJECT_CLIENT_ID", "OPENPROJECT_CLIENT_SECRET", "DISCORD_BOT_TOKEN")
VALUE = re.compile(r"^[^\s#'\"`$\\]+$")


def main():
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        raise SystemExit(f"Missing {env_path}")

    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    current = dict(line.rstrip("\n").partition("=")[::2] for line in lines if "=" in line)
    values = {}
    for key in KEYS:
        value = getpass.getpass(f"{key} (Enter to keep current): ")
        if not value:
            value = current.get(key, "")
        if not VALUE.fullmatch(value):
            raise SystemExit(f"Invalid {key}: use a non-empty value without shell syntax")
        values[key] = value

    seen = set()
    updated = []
    for line in lines:
        key = line.partition("=")[0]
        if key in values:
            if key in seen:
                raise SystemExit(f"Duplicate {key} in .env")
            updated.append(f"{key}={values[key]}\n")
            seen.add(key)
        else:
            updated.append(line)
    if seen != set(KEYS):
        raise SystemExit(f"Missing keys in .env: {', '.join(sorted(set(KEYS) - seen))}")

    fd, temp_name = tempfile.mkstemp(prefix=".env.", dir=env_path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.writelines(updated)
        os.replace(temp_name, env_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    print("Saved credentials to .env with mode 600.")


if __name__ == "__main__":
    main()
