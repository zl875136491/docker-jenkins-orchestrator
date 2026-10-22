from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import count
from threading import Barrier

import pytest

from orchestrator.models import AgentRole, AgentRuntimeConfig, BuildJob, BuildStatus, RouteCheckResult
from orchestrator.repository import InMemoryRepository, MongoRepository, RepositoryError


NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
_CHECK_IDS = count(1)


@pytest.fixture(params=["memory", "mongo"])
def repository(request):
    if request.param == "memory":
        repository = InMemoryRepository()
    else:
        mongomock = pytest.importorskip("mongomock")
        client = mongomock.MongoClient(tz_aware=True)
        repository = MongoRepository(client.route_check_tests, client=client)
    repository.ensure_indexes()
    yield repository
    repository.close()


def _role(role_key="static_build_agent", **fields):
    values = {
        "role_key": role_key,
        "prompt": "Inspect the required route",
        "output_schema": {"type": "object", "properties": {"passed": {"type": "boolean"}}},
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(fields)
    return AgentRole(**values)


def _check(check_id="check-1", **fields):
    values = {
        "id": next(_CHECK_IDS),
        "check_id": check_id,
        "app_id": "app-1",
        "commit_id": "a" * 40,
        "require_route": "/health",
        "static_agent_role_version": 1,
        "dynamic_agent_role_version": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(fields)
    return RouteCheckResult(**values)


def _parallel(work, count=12):
    barrier = Barrier(count)

    def start(index):
        barrier.wait(timeout=10)
        return work(index)

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(start, range(count)))


def test_role_initialization_is_insert_only_and_role_list_has_stable_order(repository):
    original = _role()
    assert repository.save_agent_role(original) == original
    assert repository.save_agent_role(_role(prompt="Do not overwrite", agent_role_version=9)) == original
    dynamic = _role("dynamic_debug_agent")
    repository.save_agent_role(dynamic)

    assert repository.get_agent_role("static_build_agent") == original
    assert repository.get_agent_role("missing") is None
    assert repository.list_agent_roles() == [dynamic, original]


def test_role_compare_and_swap_is_full_replacement_and_failure_returns_none(repository):
    original = _role(runtime_config=AgentRuntimeConfig(model="old-model"))
    repository.save_agent_role(original)
    replacement = _role(prompt="Changed role", output_schema={"type": "object"}, agent_role_version=2)

    assert repository.save_agent_role(replacement, expected_version=3) is None
    assert repository.get_agent_role(original.role_key) == original
    assert repository.save_agent_role(replacement, expected_version=1) == replacement
    assert repository.get_agent_role(original.role_key).runtime_config.model is None
    assert repository.get_agent_role(original.role_key).output_schema == {"type": "object"}
    assert repository.save_agent_role(original, expected_version=1) is None
    assert repository.save_agent_role(_role("dynamic_debug_agent"), expected_version=1) is None
    assert repository.get_agent_role("dynamic_debug_agent") is None


def test_concurrent_default_role_initializers_cannot_overwrite_each_other(repository):
    candidates = [_role(prompt=f"Initializer {index}") for index in range(12)]
    saved = _parallel(lambda index: repository.save_agent_role(candidates[index]))

    assert len({role.prompt for role in saved}) == 1
    assert repository.list_agent_roles() == [saved[0]]
    assert repository.get_agent_role("static_build_agent") == saved[0]


def test_concurrent_role_updates_have_only_one_cas_winner(repository):
    repository.save_agent_role(_role())
    saved = _parallel(lambda index: repository.save_agent_role(
        _role(prompt=f"CAS {index}", agent_role_version=2), expected_version=1,
    ))
    winners = [role for role in saved if role is not None]

    assert len(winners) == 1
    assert repository.get_agent_role("static_build_agent") == winners[0]


def test_role_reads_and_writes_are_copy_isolated(repository):
    original = _role()
    saved = repository.save_agent_role(original)
    original.output_schema["properties"]["passed"]["type"] = "string"
    saved.output_schema["properties"]["passed"]["type"] = "number"
    fetched = repository.get_agent_role(original.role_key)
    listed = repository.list_agent_roles()
    fetched.runtime_config.model = "mutated"
    listed[0].output_schema.clear()

    assert repository.get_agent_role(original.role_key) == _role()


def test_route_check_get_or_insert_preserves_the_winner(repository):
    original = _check(probe_payload={"body": {"values": [1]}})
    saved, created = repository.ensure_route_check(original)
    assert created is True
    assert saved == original
    competitor = _check("competitor", static_status="succeeded", static_check_passed=True)

    found, created = repository.ensure_route_check(competitor)

    assert created is False
    assert found == original
    assert repository.get_route_check(competitor.check_id) is None
    assert repository.find_route_check("app-1", "a" * 40, "/health", 1) == original
    assert repository.find_route_check("app-1", "a" * 40, "/health", 2) is None
    assert repository.get_route_check("missing") is None


def test_concurrent_route_check_insertions_have_one_persistent_identity(repository):
    candidates = [_check(f"candidate-{index}") for index in range(12)]
    saved = _parallel(lambda index: repository.ensure_route_check(candidates[index]))
    winners = [result for result, created in saved if created]

    assert len(winners) == 1
    assert all(result == winners[0] for result, _ in saved)
    assert repository.list_route_checks("app-1", "a" * 40) == winners


def test_save_route_check_updates_only_the_same_persistent_identity(repository):
    original = _check()
    repository.save_route_check(original)
    updated = original.model_copy(update={"static_status": "succeeded", "static_check_passed": True}, deep=True)

    assert repository.save_route_check(updated) == updated
    assert repository.save_route_check(_check("other", static_status="failed")) == updated
    assert repository.get_route_check("other") is None
    assert repository.list_route_checks(original.app_id, original.commit_id) == [updated]


@pytest.mark.parametrize("field,value", [
    ("app_id", "different-app"),
    ("commit_id", "b" * 40),
    ("require_route", "/different"),
    ("static_agent_role_version", 2),
    ("id", 999999),
])
def test_save_route_check_cannot_move_an_existing_id_to_another_identity(repository, field, value):
    original = _check()
    repository.save_route_check(original)

    with pytest.raises(ValueError, match="identity is immutable"):
        repository.save_route_check(original.model_copy(update={field: value}, deep=True))

    assert repository.get_route_check(original.check_id) == original


def test_ensure_route_check_cannot_reuse_check_id_or_numeric_primary_key(repository):
    original = _check()
    repository.ensure_route_check(original)

    with pytest.raises(ValueError, match="identity is immutable"):
        repository.ensure_route_check(original.model_copy(update={"require_route": "/other"}, deep=True))
    with pytest.raises(RepositoryError, match="Duplicate route check id"):
        repository.ensure_route_check(_check("different-check", id=original.id, require_route="/other"))

    assert repository.list_route_checks(original.app_id, original.commit_id) == [original]


def test_numeric_primary_key_matches_signed_bson_int64_range(repository):
    maximum = (1 << 63) - 1
    result = _check(id=maximum)

    saved, created = repository.ensure_route_check(result)

    assert created is True
    assert saved.id == maximum
    assert repository.get_route_check(result.check_id).id == maximum
    with pytest.raises(ValueError):
        _check("out-of-range", id=maximum + 1)


def test_save_route_check_numeric_primary_key_cannot_belong_to_another_check(repository):
    original = _check()
    repository.save_route_check(original)

    with pytest.raises(RepositoryError, match="Duplicate route check id"):
        repository.save_route_check(_check("other", id=original.id, require_route="/other"))

    assert repository.get_route_check(original.check_id) == original


def test_concurrent_legacy_saves_do_not_overwrite_another_check_for_the_same_key(repository):
    candidates = [_check(f"legacy-{index}") for index in range(12)]
    saved = _parallel(lambda index: repository.save_route_check(candidates[index]))

    assert len({result.check_id for result in saved}) == 1
    assert repository.list_route_checks("app-1", "a" * 40) == [saved[0]]


def test_route_check_cas_matches_generation_status_and_run_token(repository):
    original = _check()
    repository.ensure_route_check(original)
    fields = {"static_status": "running", "static_run_token": "worker-1", "probe_payload": {"body": {"x": [1]}}}
    expected = {"generation": 1, "static_status": "queued", "static_run_token": None}

    updated = repository.update_route_check(original.check_id, fields, expected)
    assert updated.static_status == "running"
    assert updated.static_run_token == "worker-1"
    assert fields["probe_payload"] == {"body": {"x": [1]}}
    assert expected == {"generation": 1, "static_status": "queued", "static_run_token": None}
    fields["probe_payload"]["body"]["x"].append(2)
    assert repository.get_route_check(original.check_id).probe_payload == {"body": {"x": [1]}}
    assert repository.update_route_check(original.check_id, {"static_status": "failed"}, expected) is None
    assert repository.update_route_check(original.check_id, {"static_status": "failed"}, {"generation": 2}) is None
    assert repository.update_route_check(original.check_id, {"static_status": "failed"}, {"static_run_token": "other"}) is None
    assert repository.update_route_check("missing", {"static_status": "running"}) is None
    assert repository.get_route_check(original.check_id).static_status == "running"


def test_concurrent_route_claims_have_one_cas_winner(repository):
    original = _check()
    repository.ensure_route_check(original)
    saved = _parallel(lambda index: repository.update_route_check(
        original.check_id,
        {"static_status": "running", "static_run_token": f"worker-{index}"},
        {"generation": 1, "static_status": "queued", "static_run_token": None},
    ))
    winners = [result for result in saved if result is not None]

    assert len(winners) == 1
    assert repository.get_route_check(original.check_id) == winners[0]


def test_old_worker_cannot_finish_a_new_generation(repository):
    original = _check(static_status="running", static_run_token="old-worker")
    repository.ensure_route_check(original)
    next_generation = repository.update_route_check(
        original.check_id,
        {"generation": 2, "static_status": "queued", "static_run_token": None},
        {"generation": 1, "static_run_token": "old-worker"},
    )

    assert next_generation.generation == 2
    assert repository.update_route_check(
        original.check_id,
        {"static_status": "succeeded", "static_check_passed": True},
        {"generation": 1, "static_run_token": "old-worker"},
    ) is None
    assert repository.get_route_check(original.check_id) == next_generation


@pytest.mark.parametrize("field,value", [
    ("check_id", "other"),
    ("id", 987654321),
    ("app_id", "other"),
    ("commit_id", "b" * 40),
    ("require_route", "/other"),
    ("static_agent_role_version", 2),
])
def test_route_check_updates_reject_identity_fields(repository, field, value):
    original = _check()
    repository.ensure_route_check(original)
    fields = {field: value}

    with pytest.raises(ValueError, match=f"{field} is immutable"):
        repository.update_route_check(original.check_id, fields)

    assert fields == {field: value}
    assert repository.get_route_check(original.check_id) == original


@pytest.mark.parametrize("fields,expected", [
    ({"probe_payload.body": {}}, None),
    ({"$set": {"static_status": "failed"}}, None),
    ({"unknown": "bad"}, None),
    ({"static_status": "running"}, {"$or": []}),
])
def test_route_check_updates_reject_query_operator_or_unknown_field_injection(repository, fields, expected):
    original = _check()
    repository.ensure_route_check(original)

    with pytest.raises(ValueError, match="Unknown route check"):
        repository.update_route_check(original.check_id, fields, expected)

    assert repository.get_route_check(original.check_id) == original


def test_cas_expectations_cannot_redirect_a_write_to_another_check(repository):
    first = _check()
    second = _check("second", require_route="/second")
    repository.ensure_route_check(first)
    repository.ensure_route_check(second)

    assert repository.update_route_check(first.check_id, {"static_status": "failed"}, {"check_id": second.check_id}) is None
    assert repository.get_route_check(first.check_id) == first
    assert repository.get_route_check(second.check_id) == second


@pytest.mark.parametrize("value", [{"$ne": 2}, [1, 2], (1, 2)])
def test_cas_expectations_reject_compound_values_to_keep_backend_semantics_equal(repository, value):
    original = _check()
    repository.ensure_route_check(original)

    with pytest.raises(ValueError, match="expectations must be scalar"):
        repository.update_route_check(original.check_id, {"static_status": "failed"}, {"generation": value})

    assert repository.get_route_check(original.check_id) == original


def test_exact_delete_preserves_other_routes_versions_commits_and_apps(repository):
    records = [
        _check(),
        _check("old-role-version", static_agent_role_version=2),
        _check("other-route", require_route="/other"),
        _check("other-commit", commit_id="b" * 40),
        _check("other-app", app_id="other-app"),
    ]
    for result in records:
        repository.ensure_route_check(result)

    assert repository.delete_route_check("app-1", "a" * 40, "/health", 1) is True
    assert repository.delete_route_check("app-1", "a" * 40, "/health", 1) is False
    assert repository.get_route_check(records[0].check_id) is None
    for result in records[1:]:
        assert repository.get_route_check(result.check_id) == result


def test_route_check_history_is_complete_filtered_and_stably_sorted(repository):
    records = [
        _check("older", require_route="/older", created_at=NOW - timedelta(hours=1)),
        _check("tie-a", require_route="/a"),
        _check("tie-z", require_route="/z"),
        _check("newest", require_route="/new", created_at=NOW + timedelta(hours=1)),
        _check("other-app", app_id="other-app"),
        _check("other-commit", commit_id="b" * 40),
    ]
    for index in range(25):
        records.append(_check(f"history-{index:02}", require_route=f"/history/{index}", created_at=NOW - timedelta(days=1)))
    for result in records:
        repository.ensure_route_check(result)

    expected = [records[3], records[2], records[1], records[0], *reversed(records[6:])]
    assert repository.list_route_checks("app-1", "a" * 40) == expected
    assert repository.list_route_checks("missing", "a" * 40) == []


def test_pending_recovery_returns_all_queued_and_running_checks_in_stable_order(repository):
    queued = _check("queued")
    running = _check("running", require_route="/running", static_status="running", updated_at=NOW - timedelta(hours=1))
    succeeded = _check("succeeded", require_route="/success", static_status="succeeded")
    failed = _check("failed", require_route="/failed", static_status="failed")
    other_app = _check("other-app", app_id="other-app", updated_at=NOW + timedelta(hours=1))
    for result in (queued, running, succeeded, failed, other_app):
        repository.ensure_route_check(result)

    assert repository.list_pending_route_checks() == [running, queued, other_app]


def test_route_check_reads_writes_and_cas_outputs_are_deeply_copy_isolated(repository):
    original = _check(diff_list=["diff"], probe_payload={"body": {"values": [1]}})
    saved, _ = repository.ensure_route_check(original)
    original.probe_payload["body"]["values"].append(2)
    saved.diff_list.append("mutated")
    saved.probe_payload["body"]["values"].append(3)
    fetched = repository.get_route_check(original.check_id)
    found = repository.find_route_check(original.app_id, original.commit_id, original.require_route, 1)
    listed = repository.list_route_checks(original.app_id, original.commit_id)[0]
    pending = repository.list_pending_route_checks()[0]
    for result in (fetched, found, listed, pending):
        result.probe_payload["body"]["values"].append(4)
    updated = repository.update_route_check(original.check_id, {"probe_result": {"attempts": [{"status": 200}]}})
    updated.probe_result["attempts"][0]["status"] = 500

    final = repository.get_route_check(original.check_id)
    assert final.diff_list == ["diff"]
    assert final.probe_payload == {"body": {"values": [1]}}
    assert final.probe_result == {"attempts": [{"status": 200}]}


def test_mongo_route_check_indexes_enforce_each_identity_and_support_recovery():
    mongomock = pytest.importorskip("mongomock")
    repository = MongoRepository(mongomock.MongoClient().index_tests)
    repository.ensure_indexes()
    repository.ensure_indexes()

    assert repository.agent_roles.index_information()["unique_agent_role"]["unique"] is True
    indexes = repository.route_checks.index_information()
    assert indexes["unique_route_check_id"]["key"] == [("check_id", 1)]
    assert indexes["unique_route_check_id"]["unique"] is True
    assert indexes["unique_route_check_numeric_id"]["key"] == [("id", 1)]
    assert indexes["unique_route_check_numeric_id"]["unique"] is True
    assert indexes["uniq_app_commit_route_version"]["unique"] is True
    assert indexes["uniq_app_commit_route_version"]["key"] == [
        ("app_id", 1), ("commit_id", 1), ("require_route", 1), ("static_agent_role_version", 1),
    ]
    assert indexes["pending_static_checks"]["key"] == [("static_status", 1), ("updated_at", 1)]


def test_pending_dynamic_builds_include_terminal_teardown_and_have_stable_order(repository):
    def build(build_id, **fields):
        return BuildJob(build_id=build_id, appid="app-1", git_ref="main", created_at=NOW,
                        updated_at=fields.pop("updated_at", NOW), **fields)

    records = [
        build("z-queued", dynamic_status="queued"),
        build("a-running", dynamic_status="running"),
        build("old-terminal-teardown", status=BuildStatus.FAILED, dynamic_status="failed", teardown_required=True,
              updated_at=NOW - timedelta(hours=1)),
        build("new-terminal-teardown", status=BuildStatus.FAILED, dynamic_status="failed", teardown_required=True,
              updated_at=NOW + timedelta(hours=1)),
        build("cancelled-queued", status=BuildStatus.CANCELLED, dynamic_status="queued"),
        build("plain-pending", dynamic_status="pending"),
        build("completed", status=BuildStatus.SUCCEEDED, dynamic_status="succeeded"),
        build("failed-without-teardown", status=BuildStatus.FAILED, dynamic_status="failed"),
    ]
    for record in records:
        repository.create_build(record)

    assert repository.list_pending_dynamic_builds() == [records[index] for index in (2, 1, 4, 0, 3)]


def test_pending_dynamic_builds_are_complete_and_copy_isolated(repository):
    for index in range(25):
        repository.create_build(BuildJob(
            build_id=f"recover-{index:02}", appid="app-1", git_ref="main", dynamic_status="running",
            probe_result={"attempts": [{"status": 200}]}, updated_at=NOW,
        ))

    pending = repository.list_pending_dynamic_builds()
    assert [build.build_id for build in pending] == [f"recover-{index:02}" for index in range(25)]
    pending[0].probe_result["attempts"][0]["status"] = 500
    assert repository.get_build("recover-00").probe_result == {"attempts": [{"status": 200}]}
