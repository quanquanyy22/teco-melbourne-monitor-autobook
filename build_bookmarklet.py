#!/usr/bin/env python3
"""Build a local bookmarklet from autofill_snippet.js and environment values.

The generated HTML contains applicant data and is intentionally ignored by git.
Set TECO_FNAME, TECO_LNAME, TECO_EMAIL, TECO_PHONE, TECO_VISA_GRANT_NO and
TECO_TRAVEL_DATE before running this helper.
"""

import html
import os
import re
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "autofill_snippet.js"
OUTPUT = ROOT / "autofill_bookmarklet.html"
FIELDS = {
    "FNAME": "TECO_FNAME",
    "LNAME": "TECO_LNAME",
    "EMAIL": "TECO_EMAIL",
    "Q3": "TECO_PHONE",
    "Q10": "TECO_VISA_GRANT_NO",
    "Q9": "TECO_TRAVEL_DATE",
    "Q12": "TECO_Q12",
}


def minify(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"^\s*//.*$", "", source, flags=re.M)
    return " ".join(line.strip() for line in source.splitlines() if line.strip())


def main() -> None:
    values = {key: os.environ.get(env, "") for key, env in FIELDS.items()}
    values["Q12"] = os.environ.get("TECO_Q12", "否")
    missing = [env for key, env in FIELDS.items() if key != "Q12" and not values[key]]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))

    javascript = SOURCE.read_text(encoding="utf-8")
    for key, value in values.items():
        marker = f"__{key}__"
        if marker not in javascript:
            raise SystemExit(f"Missing placeholder {marker} in autofill_snippet.js")
        javascript = javascript.replace(marker, value.replace("'", "\\'"))

    bookmarklet = "javascript:" + urllib.parse.quote(minify(javascript), safe="")
    rows = "".join(
        f"<tr><td><code>{html.escape(key)}</code></td><td>configured locally</td></tr>"
        for key in (*FIELDS, "Q8")
    )
    document = f"""<!doctype html>
<meta charset="utf-8">
<title>TECO form helper</title>
<h1>TECO form helper</h1>
<p>Drag the button to your bookmarks bar, then click it on the booking form.</p>
<p><a href="{bookmarklet}">Fill my form</a></p>
<p>This helper does not answer rotating anti-bot questions or submit the form.</p>
<table>{rows}</table>
"""
    OUTPUT.write_text(document, encoding="utf-8")
    OUTPUT.chmod(0o600)
    print(f"wrote {OUTPUT} (local-only; do not commit)")


if __name__ == "__main__":
    main()
