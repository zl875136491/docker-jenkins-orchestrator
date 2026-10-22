"""Explicit route-check prerequisites for tests of other build boundaries."""

from collections.abc import Callable

from orchestrator.container import ApplicationContainer
from orchestrator.gitlab import GitLabProject, GitLabRepositoryRef
from orchestrator.models import BuildJob, BuildStatus, RouteCheckResult, UserAppUpdate
from orchestrator.route_checks import STATIC_ROLE


TEST_COMMIT_SHA = "a" * 40


class FullShaGitLab:
    """Resolve a test repository ref without contacting a real Git server."""

    def __init__(self, commit_sha: str = TEST_COMMIT_SHA) -> None:
        self.commit_sha = commit_sha
        self.calls: list[tuple[str, str]] = []

    def validate_repository_ref(self, repository_url: str, git_ref: str) -> GitLabRepositoryRef:
        self.calls.append((repository_url, git_ref))
        return GitLabRepositoryRef(GitLabProject(1, "tests/project"), git_ref, self.commit_sha, repository_url)


def prepare_static_pass(
    container: ApplicationContainer,
    appid: str,
    *,
    commit_sha: str = TEST_COMMIT_SHA,
    require_route: str = "/health",
) -> RouteCheckResult:
    """Create a real four-dimensional cache entry, then explicitly approve it.

    The static-agent implementation has dedicated tests. These callers exercise
    workspace, deployment, or resource boundaries with its persisted output as
    a prerequisite; neither the build gate nor production services are mocked.
    """

    container.applications.gitlab = FullShaGitLab(commit_sha)
    app = container.applications.get_record(appid)
    container.applications.update_app(appid, UserAppUpdate(
        repository_url=app.repository_url or f"https://git.example/{appid}.git",
        require_route=require_route,
    ))
    app = container.applications.get_record(appid)
    check = container.route_checks.create_or_reuse(app, commit_sha).result
    checked = container.repository.update_route_check(check.check_id, {
        "static_status": "succeeded",
        "static_check_passed": True,
        "probe_payload": {
            "protocol": "http", "method": "GET", "path": require_route,
            "query_params": {}, "body": None, "assertions": {"expected_status_list": [200]},
        },
        "static_report_text": "Explicit passing prerequisite for this boundary test",
    })
    assert checked is not None
    assert (checked.app_id, checked.commit_id, checked.require_route, checked.static_agent_role_version) == (
        appid, commit_sha, require_route, container.route_checks.roles.get(STATIC_ROLE).agent_role_version,
    )
    return checked


class PassingDynamicChecks:
    """Explicit successful dynamic boundary for unrelated delivery tests."""

    def __init__(self, container: ApplicationContainer) -> None:
        self.container = container
        self.resources = container.build_resources
        self.calls: list[str] = []

    def execute(self, build_id: str, *, guard: Callable[[], None]) -> BuildJob:
        guard()
        build = self.container.builds.get_build(build_id)
        assert build.status is BuildStatus.DEPLOYING
        assert build.service_ids
        self.calls.append(build_id)
        updated = self.container.repository.update_build(build_id, {
            "dynamic_status": "succeeded", "dynamic_check_passed": True,
            "probe_result": {"final": {"passed": True, "status_code": 200}},
        })
        assert updated is not None
        return updated
