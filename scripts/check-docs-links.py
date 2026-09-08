#!/usr/bin/env python3
"""Every link and image in the docs site resolves to a file that exists.

Docsify fetches markdown at runtime, so a link to a page that is not there is
a "404 — Not found" in the reader's browser and nothing at all in CI. This is
the check that turns that into a failed build.

What it checks, and only this:

* every relative link and image target in a docs page exists on disk;
* every page reachable from the sidebar exists;
* no page is unreachable, walking from the sidebar and the home page through
  the links between pages — a page nothing links to is one nobody reads and
  nobody notices going stale.

External links are not fetched. A link checker that hits the network fails on
somebody else's outage, and a red build that is not about this repository is
one people learn to ignore.

    python3 scripts/check-docs-links.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"

#: `[text](target)` and `![alt](target)`.
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)")

#: Pages the site has on purpose without a sidebar entry of their own.
NOT_IN_SIDEBAR = {
    "README.md",  # the home page, reached by the site root
    "_sidebar.md",
    "_navbar.md",
}


def pages() -> list[Path]:
    return sorted(p for p in DOCS.rglob("*.md") if ".git" not in p.parts)


def targets(page: Path) -> list[str]:
    return LINK.findall(page.read_text(encoding="utf-8"))


def resolve(page: Path, target: str) -> Path | None:
    """Where a link points, or None when it is not ours to check."""
    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target or target.startswith(("http://", "https://", "mailto:", "data:")):
        return None
    # Docsify resolves a leading slash against the site root, which is `docs/`.
    if target.startswith("/"):
        return DOCS / target.lstrip("/")
    return (page.parent / target).resolve()


def reachable() -> set[Path]:
    """Every page you can get to from the sidebar or the home page.

    A walk rather than a sidebar lookup: the design notes are reached through
    one page that lists them, which is a perfectly good way to be reachable.
    """
    seen: set[Path] = set()
    queue = [DOCS / "_sidebar.md", DOCS / "_navbar.md", DOCS / "README.md"]
    while queue:
        page = queue.pop()
        if not page.exists():
            continue
        for target in targets(page):
            resolved = resolve(page, target)
            if resolved is None or resolved.suffix != ".md" or resolved in seen:
                continue
            seen.add(resolved)
            queue.append(resolved)
    return seen


def main() -> int:
    broken: list[str] = []

    for page in pages():
        for target in targets(page):
            resolved = resolve(page, target)
            if resolved is None:
                continue
            if not resolved.exists():
                rel = page.relative_to(DOCS)
                broken.append(f"{rel}: {target}")

    linked = reachable()
    orphans = [
        p.relative_to(DOCS)
        for p in pages()
        if p not in linked and p.name not in NOT_IN_SIDEBAR
    ]

    if broken:
        print(f"{len(broken)} broken link(s):")
        for item in broken:
            print(f"  {item}")
    if orphans:
        print(f"\n{len(orphans)} page(s) nothing links to:")
        for item in orphans:
            print(f"  {item}")
        print("\nAdd them to docs/_sidebar.md, or link them from a page that is there.")

    if broken or orphans:
        return 1

    print(f"docs: {len(pages())} pages, every link resolves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
