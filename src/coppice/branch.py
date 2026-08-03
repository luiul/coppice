"""Branch-name helpers: turning a free-text description into a git-safe branch name.

Ported from the normalization logic in dotfiles' `wtx` (`zsh/.zsh_config/funcs_wt.zsh`,
`_wtx_new`), which `coppice` is meant to eventually replace. See
https://github.com/luiul/dotfiles/issues/6.
"""

from __future__ import annotations

import re
from datetime import datetime

MAX_BRANCH_LENGTH = 40


def timestamp_branch() -> str:
    """Fallback branch name for when no usable description is given."""
    return f"wip-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def normalize_branch(description: str) -> str:
    """Turn a free-text description into a git-safe branch name.

    Lowercases, collapses whitespace/underscores/slashes into dashes, strips
    anything that isn't alphanumeric or a dash, trims leading/trailing dashes,
    and caps the result at MAX_BRANCH_LENGTH characters, cutting on a dash
    boundary rather than mid-word. Falls back to a timestamp branch if
    nothing usable survives.
    """
    slug = description.strip().lower()
    slug = re.sub(r"[\s/_]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = slug.strip("-")

    if len(slug) > MAX_BRANCH_LENGTH:
        slug = slug[:MAX_BRANCH_LENGTH]
        slug = slug.rsplit("-", 1)[0] if "-" in slug else slug
        slug = slug.strip("-")

    return slug or timestamp_branch()
