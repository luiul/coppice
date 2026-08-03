import re

from coppice.branch import MAX_BRANCH_LENGTH, normalize_branch, timestamp_branch


def test_normalize_branch_lowercases_and_dashes():
    assert normalize_branch("Fix The Thing") == "fix-the-thing"


def test_normalize_branch_collapses_slashes_and_underscores():
    assert normalize_branch("feature/my_cool_thing") == "feature-my-cool-thing"


def test_normalize_branch_strips_non_alnum_dash():
    assert normalize_branch("fix bug #123!!") == "fix-bug-123"


def test_normalize_branch_trims_leading_trailing_dashes():
    assert normalize_branch("  --already-dashed--  ") == "already-dashed"


def test_normalize_branch_caps_length_on_dash_boundary():
    description = "a very long description that definitely exceeds the forty character cap"
    result = normalize_branch(description)
    assert len(result) <= MAX_BRANCH_LENGTH
    assert not result.endswith("-")


def test_normalize_branch_falls_back_to_timestamp_when_empty():
    result = normalize_branch("!!! ### ---")
    assert re.match(r"^wip-\d{8}-\d{6}$", result)


def test_timestamp_branch_format():
    assert re.match(r"^wip-\d{8}-\d{6}$", timestamp_branch())
