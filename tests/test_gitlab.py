import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import subprocess
from threading import Thread

import pytest

from orchestrator.gitlab import (
    GitLabComposeAmbiguousError,
    GitLabComposeInvalidError,
    GitLabComposeNotFoundError,
    GitLabError,
    GitLabHttpAdapter,
    GitLabNotFoundError,
    GitLabProject,
    GitLabRepositoryCloneError,
    GitLabRepositoryRef,
    HttpResponse,
    UrllibHttpTransport,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def json_response(payload, status=200, headers=None):
    return HttpResponse(status_code=status, headers=headers or {}, body=json.dumps(payload).encode())


def create_git_repository(directory: Path, files: dict[str, str]) -> tuple[Path, str]:
    repository = directory / "source"
    repository.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", str(repository)],
        check=True,
    )
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Compose test"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "compose-test@example.invalid"],
        check=True,
    )
    for relative_path, contents in files.items():
        file_path = repository / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(contents, encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "--all"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "--quiet", "-m", "test commit"], check=True)
    commit_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repository, commit_sha


class LocalCloneAdapter(GitLabHttpAdapter):
    def __init__(self, repository: Path):
        super().__init__("https://gitlab.example", "private-token")
        self.repository = repository
        self.git_commands: list[list[str]] = []
        self.cloned_repository: Path | None = None
        self.is_shallow_clone: bool | None = None
        self.checkout_sha: str | None = None
        self.remote_refs: list[str] = []
        self.remote_main_sha: str | None = None
        self.tags: list[str] = []
        self.all_commit_shas: list[str] = []

    def _clone_url(self, ref: GitLabRepositoryRef) -> str:
        # Use Git's transport instead of its local hardlink optimization so a
        # regression to --depth cannot be silently ignored by these tests.
        return self.repository.as_uri()

    def _run_git(self, command, environment):
        self.git_commands.append(command)
        return super()._run_git(command, environment)

    def _find_compose_path(self, repository_path: Path) -> str:
        self.cloned_repository = repository_path
        self.is_shallow_clone = subprocess.run(
            ["git", "-C", str(repository_path), "rev-parse", "--is-shallow-repository"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() == "true"
        self.checkout_sha = subprocess.run(
            ["git", "-C", str(repository_path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.remote_refs = subprocess.run(
            ["git", "-C", str(repository_path), "for-each-ref", "--format=%(refname)", "refs/remotes/origin"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.remote_main_sha = subprocess.run(
            ["git", "-C", str(repository_path), "rev-parse", "refs/remotes/origin/main"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.tags = subprocess.run(
            ["git", "-C", str(repository_path), "tag", "--list"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.all_commit_shas = subprocess.run(
            ["git", "-C", str(repository_path), "rev-list", "--all"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        return super()._find_compose_path(repository_path)


def local_repository_adapter(repository: Path) -> LocalCloneAdapter:
    return LocalCloneAdapter(repository)


def local_repository_ref(commit_sha: str) -> GitLabRepositoryRef:
    return GitLabRepositoryRef(
        project=GitLabProject(
            project_id=44,
            path_with_namespace="platform/orders",
            web_url="https://gitlab.example/platform/orders",
        ),
        git_ref="main",
        commit_sha=commit_sha,
        repository_url="git@gitlab.example:platform/orders.git",
    )


def test_validate_repository_ref_resolves_project_and_slash_ref() -> None:
    transport = FakeTransport(
        [
            json_response(
                {
                    "id": 44,
                    "path_with_namespace": "platform/orders",
                    "web_url": "https://gitlab.example/platform/orders",
                }
            ),
            json_response({"id": "f" * 40}),
        ]
    )
    adapter = GitLabHttpAdapter("https://gitlab.example", "private-token", transport=transport)

    resolved = adapter.validate_repository_ref("git@gitlab.example:platform/orders.git", "release/2026.09")

    assert resolved.project.project_id == 44
    assert resolved.project.path_with_namespace == "platform/orders"
    assert resolved.git_ref == "release/2026.09"
    assert resolved.commit_sha == "f" * 40
    assert transport.calls[0]["url"] == "https://gitlab.example/api/v4/projects/platform%2Forders"
    assert transport.calls[1]["url"] == "https://gitlab.example/api/v4/projects/44/repository/commits/release%2F2026.09"
    assert transport.calls[0]["headers"]["PRIVATE-TOKEN"] == "private-token"
    assert "private-token" not in repr(adapter)
    assert adapter._clone_url(resolved) == "https://gitlab.example/platform/orders.git"


def test_clone_uses_token_header_and_https_url_instead_of_ssh_credentials(tmp_path: Path, monkeypatch) -> None:
    token = "test-token-never-put-in-url"
    adapter = GitLabHttpAdapter("https://gitlab.example", token)
    captured: dict[str, object] = {}
    unsafe_environment = {
        "GIT_DIR": "/outside/repository.git",
        "GIT_COMMON_DIR": "/outside/common.git",
        "GIT_WORK_TREE": "/outside/worktree",
        "GIT_INDEX_FILE": "/outside/index",
        "GIT_OBJECT_DIRECTORY": "/outside/objects",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/outside/alternate-objects",
        "GIT_SHALLOW_FILE": "/outside/shallow",
        "GIT_NAMESPACE": "outside",
        "GIT_CONFIG_PARAMETERS": "'remote.origin.promisor=true'",
    }
    for name, value in unsafe_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/runtime/ca-certificates.crt")

    class StopAfterCapture(Exception):
        pass

    def capture(command, environment):
        captured["command"] = command
        captured["environment"] = dict(environment)
        raise StopAfterCapture

    adapter._run_git = capture
    with pytest.raises(StopAfterCapture):
        adapter._clone_repository(
            local_repository_ref("a" * 40),
            tmp_path / "repository",
        )

    command = captured["command"]
    environment = captured["environment"]
    assert isinstance(command, list)
    assert command[-2] == "https://gitlab.example/platform/orders.git"
    assert token not in " ".join(command)
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert not unsafe_environment.keys() & environment.keys()
    assert environment["HTTPS_PROXY"] == "http://proxy.example:3128"
    assert environment["SSL_CERT_FILE"] == "/runtime/ca-certificates.crt"
    assert environment["GIT_CONFIG_COUNT"] == "2"
    assert environment["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert environment["GIT_CONFIG_VALUE_0"] == ""
    assert environment["GIT_CONFIG_KEY_1"] == "http.https://gitlab.example/.extraheader"
    auth_header = environment["GIT_CONFIG_VALUE_1"].removeprefix("Authorization: Basic ")
    assert base64.b64decode(auth_header).decode() == f"oauth2:{token}"


@pytest.mark.parametrize(
    "web_url",
    [
        "http://gitlab.example/platform/orders",
        "https://gitlab.example:8443/platform/orders",
        "https://gitlab.example:80/platform/orders",
    ],
)
def test_clone_rejects_metadata_origin_changes_before_git_runs(
    tmp_path: Path, monkeypatch, web_url: str
) -> None:
    adapter = GitLabHttpAdapter("https://gitlab.example", "private-token")
    reference = GitLabRepositoryRef(
        project=GitLabProject(
            project_id=44,
            path_with_namespace="platform/orders",
            web_url=web_url,
        ),
        git_ref="main",
        commit_sha="a" * 40,
        repository_url="https://gitlab.example/platform/orders.git",
    )

    def unexpected_git(*_args, **_kwargs):
        pytest.fail("Git must not run for a project URL that changes the configured origin")

    monkeypatch.setattr(adapter, "_run_git", unexpected_git)

    with pytest.raises(GitLabError, match="invalid project URL"):
        adapter._clone_repository(reference, tmp_path / "repository")


def test_clone_url_accepts_the_equivalent_explicit_https_port() -> None:
    adapter = GitLabHttpAdapter("https://gitlab.example", "private-token")
    reference = GitLabRepositoryRef(
        project=GitLabProject(
            project_id=44,
            path_with_namespace="platform/orders",
            web_url="https://gitlab.example:443/platform/orders",
        ),
        git_ref="main",
        commit_sha="a" * 40,
        repository_url="https://gitlab.example/platform/orders.git",
    )

    assert adapter._clone_url(reference) == "https://gitlab.example:443/platform/orders.git"


def test_validate_repository_ref_has_safe_not_found_and_transport_errors() -> None:
    token = "token-that-must-not-appear"
    missing = GitLabHttpAdapter(
        "https://gitlab.example",
        token,
        transport=FakeTransport([HttpResponse(status_code=404)]),
    )
    with pytest.raises(GitLabNotFoundError) as missing_error:
        missing.validate_repository_ref("https://gitlab.example/team/api.git", "main")
    assert token not in str(missing_error.value)

    failed = GitLabHttpAdapter(
        "https://gitlab.example",
        token,
        transport=FakeTransport([RuntimeError(f"network failure using {token}")]),
    )
    with pytest.raises(GitLabError) as transport_error:
        failed.validate_repository_ref("https://gitlab.example/team/api.git", "main")
    assert str(transport_error.value) == "GitLab request failed"
    assert token not in str(transport_error.value)


@pytest.mark.parametrize(
    "commit_sha",
    ["", "HEAD", "main", "--help", "a" * 39, "g" * 40, "a" * 41, "a" * 65, "a" * 40 + "\n"],
)
def test_discover_compose_rejects_invalid_commit_sha_before_cloning(commit_sha: str, monkeypatch) -> None:
    adapter = GitLabHttpAdapter("https://gitlab.example", "private-token")

    def unexpected_clone(*_args, **_kwargs):
        pytest.fail("The repository must not be cloned for an invalid commit SHA")

    monkeypatch.setattr(adapter, "_clone_repository", unexpected_clone)

    with pytest.raises(GitLabError, match="full commit SHA"):
        adapter.discover_compose(local_repository_ref(commit_sha))


def test_discover_compose_performs_full_clone_and_prefers_root_compose_file(tmp_path: Path) -> None:
    repository, commit_sha = create_git_repository(
        tmp_path,
        {
            "nested/docker-compose.yml": "services: {nested: {build: {context: ., dockerfile: Dockerfile}, image: example/nested:1}}\n",
            "compose.yaml": "services:\n  web:\n    build: {context: ., dockerfile: Dockerfile}\n    image: nginx:1.27\n",
            "docker-compose.yml": "services: {legacy: {build: {context: ., dockerfile: Dockerfile}, image: example/legacy:1}}\n",
        },
    )
    adapter = local_repository_adapter(repository)

    document = adapter.discover_compose(local_repository_ref(commit_sha))

    assert document.path == "compose.yaml"
    assert document.commit_sha == commit_sha
    assert document.compose == {"services": {"web": {"build": {"context": ".", "dockerfile": "Dockerfile"}, "image": "nginx:1.27"}}}
    clone_command = adapter.git_commands[0]
    assert clone_command[:5] == ["git", "clone", "--quiet", "--no-checkout", "--"]
    assert not {"--depth", "--single-branch", "--filter"}.intersection(clone_command)
    assert adapter.is_shallow_clone is False


def test_discover_compose_uses_legacy_root_filename_precedence(tmp_path: Path) -> None:
    repository, commit_sha = create_git_repository(
        tmp_path,
        {
            "docker-compose.yaml": "services: {yaml: {build: {context: ., dockerfile: Dockerfile}, image: example/yaml:1}}\n",
            "docker-compose.yml": "services: {yml: {build: {context: ., dockerfile: Dockerfile}, image: example/yml:1}}\n",
        },
    )

    document = local_repository_adapter(repository).discover_compose(local_repository_ref(commit_sha))

    assert document.path == "docker-compose.yml"
    assert document.compose == {"services": {"yml": {"build": {"context": ".", "dockerfile": "Dockerfile"}, "image": "example/yml:1"}}}


def test_discover_compose_finds_unique_nested_candidate_in_full_clone(tmp_path: Path) -> None:
    files = {f"docs/file-{index:03}.txt": "not compose\n" for index in range(130)}
    files["services/api/compose.yml"] = "services: {api: {build: {context: ., dockerfile: Dockerfile}, image: example/api:1}}\n"
    repository, commit_sha = create_git_repository(tmp_path, files)

    document = local_repository_adapter(repository).discover_compose(local_repository_ref(commit_sha))

    assert document.path == "services/api/compose.yml"
    assert document.compose["services"]["api"]["image"] == "example/api:1"


def test_discover_compose_pins_search_to_resolved_commit_sha(tmp_path: Path) -> None:
    repository, original_sha = create_git_repository(
        tmp_path,
        {"compose.yaml": "services: {web: {build: {context: ., dockerfile: Dockerfile}, image: example/old:1}}\n"},
    )
    (repository / "compose.yaml").write_text(
        "services: {web: {build: {context: ., dockerfile: Dockerfile}, image: example/new:1}}\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repository), "add", "compose.yaml"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "--quiet", "-m", "new head"], check=True)
    latest_main_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    subprocess.run(["git", "-C", str(repository), "switch", "--quiet", "-c", "feature"], check=True)
    (repository / "feature.txt").write_text("feature branch\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "feature.txt"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "--quiet", "-m", "feature commit"], check=True)
    feature_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repository), "switch", "--quiet", "main"], check=True)
    subprocess.run(["git", "-C", str(repository), "tag", "v1.0", original_sha], check=True)
    subprocess.run(["git", "-C", str(repository), "tag", "v2.0", latest_main_sha], check=True)

    adapter = local_repository_adapter(repository)
    document = adapter.discover_compose(local_repository_ref(original_sha))

    assert document.commit_sha == original_sha
    assert document.compose["services"]["web"]["image"] == "example/old:1"
    assert adapter.checkout_sha == original_sha
    assert adapter.is_shallow_clone is False
    assert set(adapter.remote_refs) == {
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/feature",
        "refs/remotes/origin/main",
    }
    assert adapter.remote_main_sha == latest_main_sha
    assert set(adapter.tags) == {"v1.0", "v2.0"}
    assert set(adapter.all_commit_shas) == {original_sha, latest_main_sha, feature_sha}


def test_discover_compose_rejects_shallow_clone_before_scanning(tmp_path: Path, monkeypatch) -> None:
    repository, commit_sha = create_git_repository(
        tmp_path,
        {"compose.yaml": "services: {web: {build: {context: ., dockerfile: Dockerfile}, image: example/web:1}}\n"},
    )
    adapter = local_repository_adapter(repository)
    run_git = adapter._run_git

    def create_shallow_clone(command, environment):
        if command[:2] == ["git", "clone"]:
            command = [*command[:4], "--depth=1", *command[4:]]
        return run_git(command, environment)

    monkeypatch.setattr(adapter, "_run_git", create_shallow_clone)

    with pytest.raises(GitLabRepositoryCloneError, match="full clone"):
        adapter.discover_compose(local_repository_ref(commit_sha))

    assert adapter.cloned_repository is None
    assert adapter.checkout_sha is None


@pytest.mark.parametrize(
    ("config_key", "config_value"),
    [
        ("extensions.partialClone", "origin"),
        ("extensions.partialClone", ""),
        ("remote.origin.partialclonefilter", "blob:none"),
        ("remote.origin.partialclonefilter", ""),
        ("remote.origin.promisor", "true"),
        ("remote.origin.promisor", "YES"),
        ("remote.origin.promisor", "on"),
        ("remote.origin.promisor", "1"),
    ],
)
def test_discover_compose_rejects_partial_clone_before_scanning(
    tmp_path: Path, monkeypatch, config_key: str, config_value: str
) -> None:
    repository, commit_sha = create_git_repository(
        tmp_path,
        {"compose.yaml": "services: {web: {build: {context: ., dockerfile: Dockerfile}, image: example/web:1}}\n"},
    )
    adapter = local_repository_adapter(repository)
    run_git = adapter._run_git

    def mark_partial_clone(command, environment):
        result = run_git(command, environment)
        if command[:2] == ["git", "clone"]:
            subprocess.run(
                ["git", "-C", command[-1], "config", "--local", config_key, config_value],
                check=True,
            )
        return result

    monkeypatch.setattr(adapter, "_run_git", mark_partial_clone)

    with pytest.raises(GitLabRepositoryCloneError, match="full clone"):
        adapter.discover_compose(local_repository_ref(commit_sha))

    assert adapter.cloned_repository is None
    assert adapter.checkout_sha is None


@pytest.mark.parametrize("config_value", ["false", "no", "off", "0"])
def test_discover_compose_allows_disabled_promisor_config(
    tmp_path: Path, monkeypatch, config_value: str
) -> None:
    repository, commit_sha = create_git_repository(
        tmp_path,
        {"compose.yaml": "services: {web: {build: {context: ., dockerfile: Dockerfile}, image: example/web:1}}\n"},
    )
    adapter = local_repository_adapter(repository)
    run_git = adapter._run_git

    def disable_promisor(command, environment):
        result = run_git(command, environment)
        if command[:2] == ["git", "clone"]:
            subprocess.run(
                ["git", "-C", command[-1], "config", "--local", "remote.origin.promisor", config_value],
                check=True,
            )
        return result

    monkeypatch.setattr(adapter, "_run_git", disable_promisor)

    document = adapter.discover_compose(local_repository_ref(commit_sha))

    assert document.path == "compose.yaml"
    assert adapter.is_shallow_clone is False
    assert adapter.checkout_sha == commit_sha


def test_discover_compose_rechecks_full_clone_after_fallback_fetch(tmp_path: Path, monkeypatch) -> None:
    repository, commit_sha = create_git_repository(
        tmp_path,
        {"compose.yaml": "services: {web: {build: {context: ., dockerfile: Dockerfile}, image: example/web:1}}\n"},
    )
    adapter = local_repository_adapter(repository)
    run_git = adapter._run_git
    run_subprocess = subprocess.run

    def report_missing_commit(command, *args, **kwargs):
        if "cat-file" in command:
            return subprocess.CompletedProcess(command, returncode=1)
        return run_subprocess(command, *args, **kwargs)

    def mark_fetched_repository_partial(command, environment):
        result = run_git(command, environment)
        if "fetch" in command:
            run_subprocess(
                ["git", "-C", command[2], "config", "--local", "remote.origin.promisor", "true"],
                check=True,
            )
        return result

    monkeypatch.setattr(subprocess, "run", report_missing_commit)
    monkeypatch.setattr(adapter, "_run_git", mark_fetched_repository_partial)

    with pytest.raises(GitLabRepositoryCloneError, match="full clone"):
        adapter.discover_compose(local_repository_ref(commit_sha))

    assert sum("fetch" in command for command in adapter.git_commands) == 1
    assert sum("--is-shallow-repository" in command for command in adapter.git_commands) == 2
    assert adapter.cloned_repository is None
    assert adapter.checkout_sha is None


def test_discover_compose_rejects_ambiguous_missing_and_invalid_documents(tmp_path: Path) -> None:
    ambiguous_repository, ambiguous_sha = create_git_repository(
        tmp_path / "ambiguous",
        {
            "a/compose.yaml": "services: {a: {image: example/a:1}}\n",
            "b/compose.yaml": "services: {b: {image: example/b:1}}\n",
        },
    )
    with pytest.raises(GitLabComposeAmbiguousError):
        local_repository_adapter(ambiguous_repository).discover_compose(local_repository_ref(ambiguous_sha))

    missing_repository, missing_sha = create_git_repository(tmp_path / "missing", {"README.md": "no compose\n"})
    with pytest.raises(GitLabComposeNotFoundError):
        local_repository_adapter(missing_repository).discover_compose(local_repository_ref(missing_sha))

    invalid_repository, invalid_sha = create_git_repository(
        tmp_path / "invalid",
        {"compose.yaml": "services: []\n"},
    )
    with pytest.raises(GitLabComposeInvalidError):
        local_repository_adapter(invalid_repository).discover_compose(local_repository_ref(invalid_sha))

    malformed_repository, malformed_sha = create_git_repository(
        tmp_path / "malformed",
        {"docker-compose.yaml": "services: [broken\n"},
    )
    with pytest.raises(GitLabComposeInvalidError, match="invalid YAML"):
        local_repository_adapter(malformed_repository).discover_compose(local_repository_ref(malformed_sha))


def test_discover_compose_reports_clone_failures_without_leaking_details(tmp_path: Path) -> None:
    adapter = local_repository_adapter(tmp_path / "does-not-exist")
    missing_ref = local_repository_ref("f" * 40)

    with pytest.raises(GitLabRepositoryCloneError, match="Unable to clone GitLab repository") as clone_error:
        adapter.discover_compose(missing_ref)
    assert "private-token" not in str(clone_error.value)
    assert str(tmp_path) not in str(clone_error.value)


def test_run_git_reports_missing_executable_without_leaking_details(tmp_path: Path, monkeypatch) -> None:
    token = "token-that-must-not-appear"
    repository_url = f"https://oauth2:{token}@gitlab.example/private/project.git"
    command = ["git", "clone", "--", repository_url, str(tmp_path / "repository")]
    adapter = GitLabHttpAdapter("https://gitlab.example", token)

    def missing_git(*_args, **_kwargs):
        raise FileNotFoundError(f"missing executable for {' '.join(command)}")

    monkeypatch.setattr(subprocess, "run", missing_git)

    with pytest.raises(GitLabRepositoryCloneError, match="Git is required") as clone_error:
        adapter._run_git(command, {})

    message = str(clone_error.value)
    assert token not in message
    assert repository_url not in message
    assert " ".join(command) not in message
    assert str(tmp_path) not in message


def test_run_git_reports_timeout_without_leaking_details(tmp_path: Path, monkeypatch) -> None:
    token = "token-that-must-not-appear"
    repository_url = f"https://oauth2:{token}@gitlab.example/private/project.git"
    command = ["git", "clone", "--", repository_url, str(tmp_path / "repository")]
    adapter = GitLabHttpAdapter("https://gitlab.example", token)

    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(command, timeout=1, output=token, stderr=repository_url)

    monkeypatch.setattr(subprocess, "run", timed_out)

    with pytest.raises(GitLabRepositoryCloneError, match="timed out") as clone_error:
        adapter._run_git(command, {})

    message = str(clone_error.value)
    assert token not in message
    assert repository_url not in message
    assert " ".join(command) not in message
    assert str(tmp_path) not in message


def test_validate_repository_ref_rejects_other_gitlab_hosts_before_request() -> None:
    transport = FakeTransport([])
    adapter = GitLabHttpAdapter("https://gitlab.example", "private-token", transport=transport)

    with pytest.raises(GitLabError, match="host does not match"):
        adapter.validate_repository_ref("https://other-gitlab.example/team/api.git", "main")

    assert transport.calls == []


def test_urllib_transport_reuses_set_cookie_for_follow_up_requests() -> None:
    seen_cookie: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            self.send_response(200)
            if self.path == "/crumb":
                self.send_header("Set-Cookie", "JSESSIONID=test-session; Path=/")
                body = b"crumb"
            else:
                seen_cookie.append(self.headers.get("Cookie"))
                body = b"build"
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = UrllibHttpTransport(timeout=2)
        base = f"http://127.0.0.1:{server.server_port}"
        first = transport.request("GET", f"{base}/crumb", headers={})
        second = transport.request("GET", f"{base}/build", headers={})
        assert first.status_code == second.status_code == 200
        assert seen_cookie == ["JSESSIONID=test-session"]
    finally:
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.parametrize("status", [200, 403])
def test_urllib_transport_bounds_success_and_error_response_bodies(status):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"x" * 16384)

        def log_message(self, *_args):
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = UrllibHttpTransport(timeout=2)
        response = transport.request("GET", f"http://127.0.0.1:{server.server_port}/console", headers={}, max_bytes=256)
        assert response.status_code == status
        assert response.body == b"x" * 256
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
