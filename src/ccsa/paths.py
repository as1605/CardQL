from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    repo_root: Path

    @property
    def local_dir(self) -> Path:
        return self.repo_root / ".local"

    @property
    def local_config_dir(self) -> Path:
        return self.local_dir / "config"

    @property
    def local_state_dir(self) -> Path:
        return self.local_dir / "state"

    @property
    def data_dir(self) -> Path:
        return self.repo_root / "data"

    @property
    def raw_pdfs_dir(self) -> Path:
        return self.data_dir / "raw-pdfs"

    @property
    def normalized_dir(self) -> Path:
        return self.data_dir / "normalized"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"


def detect_repo_root(start: Path | None = None) -> Path:
    """
    Resolve repo root by walking upwards until `.git` is found.

    Falls back to current working directory if `.git` can't be located.
    """
    p = (start or Path.cwd()).resolve()
    for candidate in [p, *p.parents]:
        if (candidate / ".git").exists():
            return candidate
    return p


def get_paths() -> Paths:
    return Paths(repo_root=detect_repo_root())
