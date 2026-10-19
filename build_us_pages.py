from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "us_boll" / "data" / "latest.json"
TARGET = ROOT / "docs" / "us" / "index.html"
TEMPLATE = ROOT / "us_boll" / "templates" / "index.html"


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit(f"US snapshot missing: {SOURCE}")
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    template = TEMPLATE.read_text(encoding="utf-8")
    page = template.replace("__US_DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(page, encoding="utf-8")
    (TARGET.parent / ".nojekyll").write_text("", encoding="utf-8")
    print(f"US Pages built: {TARGET}")


if __name__ == "__main__":
    main()
