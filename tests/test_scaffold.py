"""Scaffold smoke test — kept minimal on purpose.

Verifies the one thing every other test depends on: ccmem is importable
from the repo, not from a site-packages install that would shadow edits.
"""
from pathlib import Path
import sys

REPO = Path(__file__).parent.parent


def test_ccmem_importable_from_repo():
    sys.path.insert(0, str(REPO))
    import ccmem  # noqa: PLC0415
    pkg_path = Path(ccmem.__file__).resolve()
    assert pkg_path.is_relative_to(REPO), (
        f"ccmem loaded from {pkg_path}, not from {REPO}. "
        "A site-packages install is shadowing the dev copy."
    )
