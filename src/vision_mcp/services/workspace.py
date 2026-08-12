"""Workspace management.

Each vision task gets an isolated working directory. When ``runtime.workdir``
is ``None`` (the default) a brand-new temporary directory is created per task
and cleaned up afterwards. When a project workdir is configured, task-specific
media is staged under ``<workdir>/.vision-mcp/<uuid>/`` and removed on cleanup.

User files are never deleted and never modified.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ConfigError


@dataclass
class Workspace:
    """A single task's working directory."""

    root: Path
    input_dir: Path
    output_dir: Path
    schema_path: Path
    _temporary: bool = False
    _created: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._created = True

    def stage_media(self, source: str) -> Path:
        """Copy/link a media source into the input dir under a safe name."""
        src = Path(source)
        target = self.input_dir / f"media-{len(list(self.input_dir.iterdir()))}{src.suffix}"
        if not src.exists():
            raise FileNotFoundError(f"media source not found: {source}")
        shutil.copy2(src, target)
        return target

    def cleanup(self) -> None:
        """Remove the workspace. Only removes directories this manager created."""
        if self._temporary and self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)


class WorkspaceManager:
    """Creates and reaps per-task workspaces.

    ``base`` is the configured ``runtime.workdir`` (may be ``None``).
    """

    def __init__(self, base: Optional[Path] = None) -> None:
        self.base = base.expanduser().resolve() if base else None

    def create(self) -> Workspace:
        if self.base is None:
            root = Path(tempfile.mkdtemp(prefix="vision-mcp-"))
            return Workspace(
                root=root,
                input_dir=root / "input",
                output_dir=root / "output",
                schema_path=root / "schema.json",
                _temporary=True,
            )
        if not self.base.is_dir():
            raise ConfigError(f"workdir is not a directory: {self.base}")
        task = self.base / ".vision-mcp" / str(uuid.uuid4())
        return Workspace(
            root=task,
            input_dir=task / "input",
            output_dir=task / "output",
            schema_path=task / "schema.json",
            _temporary=True,
        )

    def write_schema(self, workspace: Workspace, schema: dict) -> Path:
        import json

        workspace.schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        return workspace.schema_path