"""mcp_cli_enabled() default + set/get round-trip."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.services import user_settings as us  # noqa: E402


def main() -> int:
    failures = 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with mock.patch.object(us, "SETTINGS_DIR", tmp_dir), \
             mock.patch.object(us, "SETTINGS_PATH", tmp_dir / "settings.json"):

            # 1. default = False (no file)
            if us.mcp_cli_enabled() is not False:
                print("FAIL  default should be False")
                failures += 1
            else:
                print("OK    default False on missing file")

            # 2. set True persists
            us.set_mcp_cli_enabled(True)
            if us.mcp_cli_enabled() is not True:
                print("FAIL  set True not reflected by read")
                failures += 1
            else:
                print("OK    set True → mcp_cli_enabled True")

            blob = json.loads((tmp_dir / "settings.json").read_text())
            if blob.get("mcpCliEnabled") is not True:
                print(f"FAIL  on-disk key should be mcpCliEnabled=true, got {blob}")
                failures += 1
            else:
                print("OK    persisted as mcpCliEnabled=true")

            # 3. set False clears
            us.set_mcp_cli_enabled(False)
            if us.mcp_cli_enabled() is not False:
                print("FAIL  set False not reflected")
                failures += 1
            else:
                print("OK    set False → mcp_cli_enabled False")

            # 4. unrelated keys preserved
            blob = us.read()
            blob["provider"] = "anthropic"
            us.write(blob)
            us.set_mcp_cli_enabled(True)
            if us.read().get("provider") != "anthropic":
                print("FAIL  set_mcp_cli_enabled clobbered unrelated keys")
                failures += 1
            else:
                print("OK    other keys preserved across toggle")

    if failures:
        print(f"\n{failures} assertion(s) failed")
        return 1
    print("\nuser_settings mcp flag: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
