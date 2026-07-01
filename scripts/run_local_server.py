from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT_LOG = ROOT / "uvicorn.restart.out.log"
ERR_LOG = ROOT / "uvicorn.restart.err.log"


def main() -> None:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    sys.stdout = OUT_LOG.open("a", buffering=1, encoding="utf-8")
    sys.stderr = ERR_LOG.open("a", buffering=1, encoding="utf-8")
    print("starting FlatWise local server", flush=True)

    try:
        import uvicorn

        uvicorn.run("app:app", host="127.0.0.1", port=8001, log_level="info")
    except BaseException:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
