"""Private evidence storage on a persistent volume (never served directly by nginx)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from sqlalchemy.orm import Session

from .config import get_settings
from .models import Evidence, Run


def root() -> Path:
    r = get_settings().evidence_dir
    r.mkdir(parents=True, exist_ok=True)
    return r.resolve()


def run_dir(run: Run) -> Path:
    d = root() / "projects" / str(run.project_id) / "runs" / str(run.id)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o750)
    return d


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def record(db: Session, run: Run, path: Path, kind: str, mime: str, step_id: int | None = None) -> Evidence:
    path = path.resolve()
    rel = path.relative_to(root())
    ev = Evidence(
        project_id=run.project_id,
        test_case_id=run.test_case_id,
        run_id=run.id,
        step_id=step_id,
        kind=kind,
        rel_path=str(rel),
        mime=mime,
        size=path.stat().st_size,
        sha256=sha256_file(path),
    )
    db.add(ev)
    db.flush()
    return ev


def resolve_evidence(rel_path: str) -> Path:
    base = root()
    p = (base / rel_path).resolve()
    if base not in p.parents:
        raise PermissionError("evidence path escapes storage root")
    return p
