from orchestrator.models import BaseImage
from orchestrator.repository import InMemoryRepository


def test_base_image_storage_is_separate_from_user_apps() -> None:
    repository = InMemoryRepository()
    image = BaseImage(source_image="python:3.12", harbor_reference="boot-images/python")
    repository.save_base_image(image)
    assert repository.base_images["python:3.12"].harbor_reference == "boot-images/python"
    assert repository.apps == {}
