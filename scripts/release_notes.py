"""Print one version's CHANGELOG entry, shaped for a GitHub release.

    python scripts/release_notes.py v0.4.1 > notes.md

GitHub renders a newline inside a paragraph as a line break, so the CHANGELOG's
80-column wrapping would come out ragged on the release page; paragraphs and
list items are joined back into single lines here. Exits non-zero when the
version has no entry, which is how the release workflow insists the CHANGELOG
was written before the tag.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = "https://github.com/mdanifo/gmailscan"


def entry(changelog: str, version: str) -> str:
    """The body under ``## v<version> (<date>)``, up to the next release heading."""
    match = re.search(
        rf"^## v{re.escape(version)} \(.*?\)\n(.*?)(?=^## v|\Z)", changelog, re.S | re.M
    )
    if not match:
        raise SystemExit(f"CHANGELOG.md has no entry for v{version}; write one before tagging.")
    return unwrap(match.group(1).strip("\n"))


def unwrap(text: str) -> str:
    """Join wrapped lines back into their paragraph or list item."""
    blocks = []
    for block in text.split("\n\n"):
        lines: list[str] = []
        for line in block.split("\n"):
            new_item = line.startswith("- ")
            continues = (
                bool(lines)
                and not new_item
                and (line.startswith("  ") or not lines[-1].startswith("- "))
            )
            if continues:
                lines[-1] += " " + line.strip()
            else:
                lines.append(line)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def notes(changelog: str, version: str) -> str:
    version = version.removeprefix("v")
    wheel = f"{REPO}/releases/download/v{version}/gmailscan-{version}-py3-none-any.whl"
    return "\n".join(
        [
            f"Pin the wheel (no git needed in an image): `gmailscan @ {wheel}`",
            f"Or the tag: `gmailscan @ git+{REPO}@v{version}`",
            "",
            entry(changelog, version),
            "",
            f"Full history: [CHANGELOG.md]({REPO}/blob/v{version}/CHANGELOG.md)",
        ]
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    changelog = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
    print(notes(changelog.read_text(encoding="utf-8"), argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
