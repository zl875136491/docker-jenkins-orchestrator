from __future__ import annotations

from functools import lru_cache

from orchestrator.config import Settings, get_settings


class WorkerRuntime:
    """Runtime hook filled by the pipeline implementation; keeps task imports safe."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def start_build(self, build_id: str, task_id: str | None) -> dict[str, str]:
        raise RuntimeError("Build pipeline is not configured")

    def poll_build(self, build_id: str, attempt: int, task_id: str | None) -> dict[str, str]:
        raise RuntimeError("Build pipeline is not configured")

    def sync_base_images(self, task_id: str | None) -> dict[str, int]:
        raise RuntimeError("Base-image synchronizer is not configured")


@lru_cache
def get_worker_runtime() -> WorkerRuntime:
    return WorkerRuntime(get_settings())
