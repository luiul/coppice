"""Branch-name helpers: turning a free-text description into a git-safe branch name.

Used by `coppice new`'s interactive prompt: lowercase, dash-joined,
40-character cap on a word boundary.
"""

from __future__ import annotations

import re

MAX_BRANCH_LENGTH = 40


def normalize_branch(description: str) -> str:
    """Turn a free-text description into a git-safe branch name.

    Lowercases, collapses whitespace/underscores/slashes into dashes, strips
    anything that isn't alphanumeric or a dash, trims leading/trailing dashes,
    and caps the result at MAX_BRANCH_LENGTH characters, cutting on a dash
    boundary rather than mid-word. Returns an empty string if nothing usable survives.
    """
    slug = description.strip().lower()
    slug = re.sub(r"[\s/_]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = slug.strip("-")

    if len(slug) > MAX_BRANCH_LENGTH:
        slug = slug[:MAX_BRANCH_LENGTH]
        slug = slug.rsplit("-", 1)[0] if "-" in slug else slug
        slug = slug.strip("-")

    return slug
