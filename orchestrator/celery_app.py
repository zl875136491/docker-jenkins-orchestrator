from orchestrator.adapters import AdapterError


def create_celery_app(broker_url: str):
    try:
        from celery import Celery
    except ImportError as exc:
        raise AdapterError("Celery is required to create the worker application") from exc
    app = Celery("orchestrator", broker=broker_url)

    @app.task(name="orchestrator.build")
    def build_task(build_id: str, appid: str, git_ref: str) -> dict[str, str]:
        return {"build_id": build_id, "appid": appid, "git_ref": git_ref, "status": "accepted"}

    @app.task(name="orchestrator.sync_base_images")
    def sync_base_images(images: list[str]) -> dict[str, int]:
        return {"synced": len(images)}

    return app
