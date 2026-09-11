from pathlib import Path
from typing import Any

import yaml


class TemplateError(ValueError):
    pass


class TemplateCatalog:
    def __init__(self, path: Path) -> None:
        with path.open(encoding="utf-8") as stream:
            document = yaml.safe_load(stream) or {}
        self.components: dict[str, dict[str, Any]] = document.get("components", {})

    def names(self) -> list[str]:
        return sorted(self.components)

    def compose(self, components: list[str], dependencies: dict[str, list[str]] | None = None) -> dict[str, Any]:
        if not components:
            raise TemplateError("At least one component is required")
        if len(set(components)) != len(components):
            raise TemplateError("Duplicate components are not allowed")
        unknown = sorted(set(components) - self.components.keys())
        if unknown:
            raise TemplateError(f"Unknown components: {', '.join(unknown)}")
        dependencies = dependencies or {}
        selected = set(components)
        for name, required in dependencies.items():
            if name not in selected or any(dep not in selected for dep in required):
                raise TemplateError("Dependencies must reference selected components")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise TemplateError("Component dependencies contain a cycle")
            if name in visited:
                return
            visiting.add(name)
            for dependency in dependencies.get(name, []):
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in components:
            visit(name)
        services = {
            name: {
                "image": self.components[name]["images"][0],
                "ports": [f"{self.components[name]['port']}:{self.components[name]['port']}"],
                **({"depends_on": dependencies[name]} if name in dependencies else {}),
            }
            for name in components
        }
        return {"version": "3.9", "services": services}
