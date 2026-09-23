from __future__ import annotations
import os
import shutil
from pathlib import Path


def resolve_home() -> str:
    """Return the resolved CCMEM_HOME directory (not yet created).

    Priority:
    1. CCMEM_HOME env var — always wins; lets tests and per-project overrides work.
    2. %LOCALAPPDATA%/ccmem on Windows — outside OneDrive, Dropbox, and iCloud sync
       boundaries by convention.
    3. $XDG_DATA_HOME/ccmem or ~/.local/share/ccmem on Linux/Mac — XDG standard,
       typically not cloud-synced.
    4. ~/.claude/ccmem — legacy fallback only; may sit inside a sync root on some
       setups and is documented as unsafe for SQLite WAL mode (see FACTS.md §11).
    """
    from_env = os.environ.get("CCMEM_HOME")
    if from_env:
        return from_env
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return os.path.join(local_app, "ccmem")
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            return os.path.join(xdg, "ccmem")
        return os.path.join(os.path.expanduser("~"), ".local", "share", "ccmem")
    return os.path.join(os.path.expanduser("~"), ".claude", "ccmem")


def sync_root_for(path: str) -> str | None:
    """Return the matching sync root if path sits inside a known cloud sync boundary.

    Checks OneDrive (env vars + common paths), Dropbox, iCloud, and Google Drive.
    Path-prefix matching — not exhaustive, but catches the common cases without
    requiring filesystem access or OS-specific APIs.
    """
    resolved = os.path.normpath(os.path.abspath(path))
    candidates: list[str] = []

    if os.name == "nt":
        for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            val = os.environ.get(var)
            if val:
                candidates.append(val)
        user_profile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        candidates.extend(
            os.path.join(user_profile, d) for d in ("OneDrive", "Dropbox")
        )

    home = os.path.expanduser("~")
    candidates.extend([
        os.path.join(home, "OneDrive"),
        os.path.join(home, "Dropbox"),
        os.path.join(home, "Library", "Mobile Documents"),
        os.path.join(home, "Library", "CloudStorage"),
        os.path.join(home, "Google Drive"),
        os.path.join(home, "GoogleDrive"),
    ])

    for root in candidates:
        norm = os.path.normpath(os.path.abspath(root))
        if resolved == norm or resolved.startswith(norm + os.sep):
            return root
    return None


_LEGACY_HOME = os.path.join(os.path.expanduser("~"), ".claude", "ccmem")


def maybe_migrate(new_home: str) -> str | None:
    """Move mem.db from ~/.claude/ccmem to new_home if the old path has data and
    the new path doesn't yet.

    Returns a human-readable message if migration ran, else None.
    Moves WAL/SHM sidecar files too so SQLite can open cleanly.
    """
    old_db = os.path.join(_LEGACY_HOME, "mem.db")
    new_db = os.path.join(new_home, "mem.db")

    if os.path.normpath(new_home) == os.path.normpath(_LEGACY_HOME):
        return None
    if not os.path.exists(old_db):
        return None
    if os.path.exists(new_db):
        return None

    Path(new_home).mkdir(parents=True, exist_ok=True)
    shutil.move(old_db, new_db)
    for ext in ("-wal", "-shm"):
        src = old_db + ext
        if os.path.exists(src):
            shutil.move(src, new_db + ext)
    return f"Migrated mem.db from {_LEGACY_HOME} to {new_home}"
