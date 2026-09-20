from __future__ import annotations

import pytest

from scripts.prepare_test_environment import choose_jenkins_job


class FakeResponse:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"jobs": [{"name": name} for name in self._names]}


def test_prepare_environment_selects_only_the_real_delivery_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scripts.prepare_test_environment.requests.get",
        lambda *_args, **_kwargs: FakeResponse(
            ["live-e2e-evidence-1789368954", "orchestrator-real-delivery-20260920"]
        ),
    )

    assert choose_jenkins_job("http://jenkins.example", "user", "password", None) == (
        "apps-orchestrator/orchestrator-real-delivery-20260920"
    )


def test_prepare_environment_refuses_to_fall_back_to_a_fake_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scripts.prepare_test_environment.requests.get",
        lambda *_args, **_kwargs: FakeResponse(["live-e2e-evidence-1789368954"]),
    )

    with pytest.raises(SystemExit, match="real orchestrator delivery Jenkins job"):
        choose_jenkins_job("http://jenkins.example", "user", "password", None)
