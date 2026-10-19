from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
import errno
import fcntl
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile
from typing import Any, BinaryIO

from orchestrator.naming import external_namespace, validate_appid


class WorkspaceError(ValueError):
    pass


class WorkspaceLimitError(WorkspaceError):
    pass


class WorkspaceStore:
    def __init__(
        self, root: Path, *, host_root: Path | None = None,
        max_upload_bytes: int = 100 * 1024 * 1024,
        max_bytes: int = 1024 * 1024 * 1024, max_files: int = 10000,
    ) -> None:
        self.root = root.absolute()
        self.host_root = (host_root or self.root).absolute()
        self.max_upload_bytes = max_upload_bytes
        self.max_bytes = max_bytes
        self.max_files = max_files

    @classmethod
    def from_settings(cls, settings: Any) -> WorkspaceStore:
        return cls(
            settings.workspace_root, host_root=settings.workspace_host_root,
            max_upload_bytes=settings.workspace_max_upload_bytes,
            max_bytes=settings.workspace_max_bytes, max_files=settings.workspace_max_files,
        )

    @staticmethod
    def relative_path(value: str, *, allow_root: bool = False) -> Path:
        if not isinstance(value, str) or not value or "\\" in value or ":" in value:
            raise WorkspaceError("Workspace path must be a relative POSIX path")
        if any(ord(char) < 32 for char in value) or value.startswith(("/", "~")):
            raise WorkspaceError("Workspace path must be a relative POSIX path")
        path = PurePosixPath(value)
        if ".." in path.parts or ".git" in path.parts or len(value) > 1024:
            raise WorkspaceError("Workspace path traversal and .git paths are forbidden")
        if str(path) == "." and not allow_root:
            raise WorkspaceError("Workspace file path is required")
        return Path(*path.parts)

    @staticmethod
    def build_source(compose: Mapping[str, Any]) -> str:
        source = compose.get("x-orchestrator-build-source", "git")
        if source not in ("git", "workspace"):
            raise WorkspaceError("x-orchestrator-build-source must be git or workspace")
        return source

    def app_path(self, appid: str) -> Path:
        validate_appid(appid)
        return self.root / external_namespace(appid)

    def ensure_app(self, appid: str) -> Path:
        directory = self.app_path(appid) / "files"
        self._safe_path(self.root, directory.relative_to(self.root))
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @contextmanager
    def _lock(self, appid: str):
        self.ensure_app(appid)
        descriptor = os.open(self.app_path(appid) / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _safe_path(root: Path, relative: Path) -> Path:
        current = root
        if root.is_symlink():
            raise WorkspaceError("Workspace symbolic links are forbidden")
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise WorkspaceError("Workspace symbolic links are forbidden")
        return current

    def _files(self, root: Path) -> list[Path]:
        files = []
        for directory, directories, names in os.walk(root, followlinks=False):
            for name in directories + names:
                path = Path(directory) / name
                mode = path.lstat().st_mode
                if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                    raise WorkspaceError("Workspace contains a symbolic link or special file")
                if stat.S_ISREG(mode):
                    files.append(path)
        return sorted(files)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _describe(path: Path, relative: Path) -> dict[str, Any]:
        digest = WorkspaceStore._sha256(path)
        return {"path": relative.as_posix(), "size": path.stat().st_size, "sha256": digest}

    def list_files(self, appid: str) -> dict[str, Any]:
        with self._lock(appid):
            root = self.ensure_app(appid)
            files = [self._describe(path, path.relative_to(root)) for path in self._files(root)]
        return {"appid": appid, "files": files, "total_bytes": sum(item["size"] for item in files)}

    def delete_file(self, appid: str, filename: str) -> None:
        relative = self.relative_path(filename)
        with self._lock(appid):
            root = self.ensure_app(appid)
            target = self._safe_path(root, relative)
            if not target.exists():
                raise FileNotFoundError("Workspace file not found")
            if not target.is_file():
                raise WorkspaceError("Workspace deletion only supports individual files")
            target.unlink()
            parent = target.parent
            while parent != root:
                try:
                    parent.rmdir()
                except OSError as exc:
                    if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                        break
                    raise
                parent = parent.parent

    def upload(self, appid: str, uploads: Sequence[tuple[str, BinaryIO]]) -> dict[str, Any]:
        if not uploads:
            raise WorkspaceError("At least one file is required")
        paths = [self.relative_path(name) for name, _ in uploads]
        if len(set(paths)) != len(paths) or any(parent in paths for path in paths for parent in path.parents):
            raise WorkspaceError("Workspace upload paths overlap")
        if len(paths) > self.max_files:
            raise WorkspaceLimitError("Workspace file count limit exceeded")
        with self._lock(appid):
            root = self.ensure_app(appid)
            existing = {path.relative_to(root): path.stat().st_size for path in self._files(root)}
            with tempfile.TemporaryDirectory(prefix="upload-", dir=self.app_path(appid)) as temporary:
                staging = Path(temporary)
                total = 0
                for relative, (_, stream) in zip(paths, uploads):
                    target = self._safe_path(root, relative)
                    if target.is_dir() or any(parent.is_file() for parent in target.parents if parent != root):
                        raise WorkspaceError("Workspace upload conflicts with an existing file or directory")
                    staged = staging / relative
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    with staged.open("xb") as output:
                        while chunk := stream.read(1024 * 1024):
                            total += len(chunk)
                            if total > self.max_upload_bytes:
                                raise WorkspaceLimitError("Workspace upload byte limit exceeded")
                            output.write(chunk)
                    existing[relative] = staged.stat().st_size
                if sum(existing.values()) > self.max_bytes or len(existing) > self.max_files:
                    raise WorkspaceLimitError("Workspace storage quota exceeded")
                result = []
                for relative in paths:
                    target = self._safe_path(root, relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.chmod(staging / relative, 0o644)
                    os.replace(staging / relative, target)
                    result.append(self._describe(target, relative))
        return {"appid": appid, "files": result, "total_bytes": sum(existing.values())}

    def snapshot_path(self, appid: str, build_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", build_id):
            raise WorkspaceError("Workspace build identifier is invalid")
        return self.app_path(appid) / "snapshots" / build_id

    def snapshot(self, appid: str, build_id: str) -> dict[str, Any]:
        with self._lock(appid):
            source = self.ensure_app(appid)
            files = self._files(source)
            snapshot = self.snapshot_path(appid, build_id)
            self._safe_path(self.root, snapshot.relative_to(self.root))
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            if snapshot.exists():
                raise WorkspaceError("Workspace build snapshot already exists")
            with tempfile.TemporaryDirectory(prefix="snapshot-", dir=snapshot.parent) as temporary:
                staged = Path(temporary) / "snapshot"
                content = staged / "files"
                content.mkdir(parents=True)
                for path in files:
                    relative = path.relative_to(source)
                    target = content / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
                    target.chmod(0o644)
                archive = staged / "workspace.tar"
                with tarfile.open(archive, "w") as output:
                    for path in self._files(content):
                        output.add(path, arcname=path.relative_to(content).as_posix(), recursive=False)
                digest = self._sha256(archive)
                staged.rename(snapshot)
        return {"workspace_snapshot": build_id, "workspace_file_count": len(files), "workspace_archive_sha256": digest}

    def resolve_compose(self, appid: str, build_id: str, compose: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(compose))
        self.build_source(result)
        root = self.snapshot_path(appid, build_id) / "files"
        for definition in result.get("volumes", {}).values() if isinstance(result.get("volumes"), Mapping) else ():
            if isinstance(definition, Mapping) and isinstance(definition.get("name"), str):
                self._reject_managed_absolute(definition["name"])
        for service in result.get("services", {}).values():
            mounts = service.get("volumes")
            if not isinstance(mounts, list):
                continue
            resolved = []
            for mount in mounts:
                if isinstance(mount, str):
                    parts = mount.split(":")
                    if len(parts) in (2, 3):
                        source = parts[0]
                        if source.startswith("."):
                            parts[0] = self._resolve_source(root, source)
                            mount = ":".join(parts)
                        else:
                            self._reject_managed_absolute(source)
                elif isinstance(mount, Mapping):
                    mount = dict(mount)
                    source = mount.get("source")
                    if isinstance(source, str):
                        if mount.get("type") == "bind" and not source.startswith("/"):
                            mount["source"] = self._resolve_source(root, source)
                        else:
                            self._reject_managed_absolute(source)
                resolved.append(mount)
            service["volumes"] = resolved
        return result

    def resolve_build_compose(self, appid: str, build_id: str, compose: Mapping[str, Any]) -> dict[str, Any]:
        result = self.resolve_compose(appid, build_id, compose)
        allowed = self.host_root / (self.snapshot_path(appid, build_id) / "files").relative_to(self.root)
        for service in result.get("services", {}).values():
            mounts = []
            for mount in service.get("volumes", []):
                if isinstance(mount, str):
                    parts = mount.split(":")
                    if parts[0].startswith("/"):
                        if not Path(parts[0]).is_relative_to(allowed):
                            raise WorkspaceError("Build bind mounts must use relative workspace inputs")
                        mount = ":".join([parts[0], parts[1], "ro"])
                elif isinstance(mount, Mapping) and mount.get("type") == "bind":
                    if not Path(mount["source"]).is_relative_to(allowed):
                        raise WorkspaceError("Build bind mounts must use relative workspace inputs")
                    mount = {**mount, "read_only": True}
                mounts.append(mount)
            if "volumes" in service:
                service["volumes"] = mounts
        return result

    def delete_build_snapshot(self, appid: str, build_id: str, registered_path: str) -> None:
        path = self.snapshot_path(appid, build_id)
        if Path(registered_path) != path:
            raise WorkspaceError("Registered workspace path does not match build ownership")
        with self._lock(appid):
            checked = self._safe_path(self.root, path.relative_to(self.root))
            if checked.is_symlink():
                raise WorkspaceError("Symlink workspace roots cannot be deleted")
            if not checked.exists():
                return
            for parent, directories, filenames in os.walk(checked, followlinks=False):
                for name in directories + filenames:
                    candidate = Path(parent) / name
                    if candidate.is_symlink():
                        raise WorkspaceError("Symlink workspace contents cannot be deleted")
            shutil.rmtree(checked)

    def _reject_managed_absolute(self, source: str) -> None:
        if source.startswith("/") and Path(os.path.normpath(source)).is_relative_to(self.host_root):
            raise WorkspaceError("Use relative workspace mounts; direct managed workspace paths are forbidden")

    def _resolve_source(self, root: Path, source: str) -> str:
        relative = self.relative_path(source, allow_root=True)
        path = self._safe_path(root, relative)
        if not path.exists():
            raise WorkspaceError(f"Workspace mount source does not exist: {source}; upload it before creating the build")
        if not path.is_dir() and not path.is_file():
            raise WorkspaceError("Workspace mount source must be a file or directory")
        if path.is_dir() and not any(path.iterdir()):
            raise WorkspaceError(f"Workspace mount source is empty: {source}; upload files before creating the build")
        return str(self.host_root / path.relative_to(self.root))
