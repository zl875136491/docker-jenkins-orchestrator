from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import xml.etree.ElementTree as ET

import pytest
import requests

from scripts.jenkins_pipeline_gate import PipelineGateError, VALIDATION_SUCCESS, read_runtime_env, run_gate


SCRIPT = "pipeline { agent any; stages { stage('Test') { steps { echo 'ok' } } } }"
JOB = "apps-orchestrator/orchestrator-real-delivery-20260920"
BASE = "https://jenkins.example"
CONFIG = b'<flow-definition><description>preserve</description><definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition"><script>old</script><sandbox>true</sandbox></definition></flow-definition>'


@dataclass
class Response:
    status_code: int = 200
    text: str = ""
    content: bytes = b""
    data: Any = None

    def json(self):
        return self.data


class Session:
    def __init__(self, responses: list[Response]):
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self.responses, "unexpected extra request"
        return self.responses.pop(0)


def crumb():
    return Response(data={"crumbRequestField": "Jenkins-Crumb", "crumb": "private-crumb"})


def call(session, **kwargs):
    return run_gate(session, base_url=BASE, configured_job=JOB, expected_job=JOB, script=SCRIPT, **kwargs)


@pytest.mark.parametrize("body", ["No Jenkinsfile specified\n", "<html>login</html>", "Errors encountered validating Jenkinsfile:\nWorkflowScript: 105: unexpected char: '\\' TOKEN=do-not-print"])
def test_http_200_is_not_validation_success_and_never_updates_job(body):
    session = Session([crumb(), Response(text=body)])
    with pytest.raises(PipelineGateError, match="job was not updated") as exc:
        call(session, apply=True)
    assert "do-not-print" not in str(exc.value)
    assert "TOKEN=" not in str(exc.value)
    assert not any(url.endswith("config.xml") for _, url, _ in session.calls)


def test_validation_only_does_not_read_or_change_job_configuration():
    session = Session([crumb(), Response(text=VALIDATION_SUCCESS + "\n")])
    result = call(session)
    assert result["validated_by_jenkins"]
    assert not result["applied"]
    assert len(session.calls) == 2
    assert session.calls[1][2]["data"] == {"jenkinsfile": SCRIPT}


def test_apply_requires_validation_then_preserves_job_settings_and_reads_back():
    verified = ET.fromstring(CONFIG)
    verified.find("definition/script").text = SCRIPT
    session = Session([crumb(), Response(text=VALIDATION_SUCCESS), Response(content=CONFIG), Response(), Response(content=ET.tostring(verified))])
    result = call(session, apply=True)
    assert result["applied"] and result["readback_exact"] and result["sandbox_preserved"] and result["other_settings_preserved"]
    assert [method for method, _, _ in session.calls] == ["GET", "POST", "GET", "POST", "GET"]
    written = ET.fromstring(session.calls[3][2]["data"])
    assert written.findtext("description") == "preserve"
    assert written.findtext("definition/script") == SCRIPT
    assert written.findtext("definition/sandbox") == "true"
    assert session.calls[3][2]["headers"]["Jenkins-Crumb"] == "private-crumb"
    assert "private-crumb" not in str(result)


def test_readback_mismatch_is_not_reported_as_success():
    session = Session([crumb(), Response(text=VALIDATION_SUCCESS), Response(content=CONFIG), Response(), Response(content=CONFIG)])
    with pytest.raises(PipelineGateError, match="readback does not match"):
        call(session, apply=True)


def test_scm_job_is_not_overwritten():
    session = Session([crumb(), Response(text=VALIDATION_SUCCESS), Response(content=b'<flow-definition><definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition" /></flow-definition>')])
    with pytest.raises(PipelineGateError, match="inline Pipeline"):
        call(session, apply=True)
    assert len(session.calls) == 3


def test_wrong_job_is_rejected_before_network_access():
    session = Session([])
    with pytest.raises(PipelineGateError, match="expected test job"):
        run_gate(session, base_url=BASE, configured_job="production", expected_job=JOB, script=SCRIPT, apply=True)
    assert not session.calls


def test_matching_production_job_is_still_rejected_before_network_access():
    session = Session([])
    with pytest.raises(PipelineGateError, match="authorized test Jenkins job"):
        run_gate(session, base_url=BASE, configured_job="production", expected_job="production", script=SCRIPT, apply=True)
    assert not session.calls


def test_changed_non_pipeline_settings_are_not_reported_as_preserved():
    verified = ET.fromstring(CONFIG)
    verified.find("definition/script").text = SCRIPT
    verified.find("description").text = "unexpectedly changed"
    session = Session([crumb(), Response(text=VALIDATION_SUCCESS), Response(content=CONFIG), Response(), Response(content=ET.tostring(verified))])
    with pytest.raises(PipelineGateError, match="settings other than the Pipeline changed"):
        call(session, apply=True)


def test_configuration_layout_whitespace_does_not_fail_readback():
    verified = ET.fromstring(CONFIG)
    verified.find("definition/script").text = SCRIPT
    ET.indent(verified)
    session = Session([crumb(), Response(text=VALIDATION_SUCCESS), Response(content=CONFIG), Response(), Response(content=ET.tostring(verified))])
    assert call(session, apply=True)["other_settings_preserved"]


@pytest.mark.parametrize("url", ["https://user:private@jenkins.example", "file:///tmp/x", "https://jenkins.example?token=private"])
def test_invalid_base_urls_are_rejected_without_exposing_credentials(url):
    session = Session([])
    with pytest.raises(PipelineGateError, match="base URL is invalid") as exc:
        run_gate(session, base_url=url, configured_job=JOB, expected_job=JOB, script=SCRIPT)
    assert "private" not in str(exc.value)
    assert not session.calls


def test_transport_failure_is_redacted():
    class BrokenSession:
        def request(self, *args, **kwargs):
            raise requests.ConnectionError("https://user:private@jenkins.example")
    with pytest.raises(PipelineGateError, match="Jenkins request failed") as exc:
        call(BrokenSession())
    assert "private" not in str(exc.value)


def test_runtime_env_reader_does_not_evaluate_shell_and_decodes_json_quotes(tmp_path):
    env = tmp_path / "runtime.env"
    env.write_text('# comment\nUSER="a"\nPASSWORD="escaped\\\\slash"\nRAW=$(false)\n', encoding="utf-8")
    values = read_runtime_env(env)
    assert values == {"USER": "a", "PASSWORD": "escaped\\slash", "RAW": "$(false)"}


def test_apply_declares_workspace_parameters_before_first_build():
    script = SCRIPT + "\nstring(name: 'WORKSPACE_ARCHIVE_URL')\nstring(name: 'WORKSPACE_ARCHIVE_SHA256')\npassword(name: 'WORKSPACE_ARCHIVE_TOKEN')"

    class EchoSession(Session):
        def request(self, method, url, **kwargs):
            if method == "POST" and url.endswith("config.xml"):
                self.written = kwargs["data"]
            if method == "GET" and url.endswith("config.xml") and hasattr(self, "written"):
                return Response(content=self.written)
            return super().request(method, url, **kwargs)

    session = EchoSession([crumb(), Response(text=VALIDATION_SUCCESS), Response(content=CONFIG), Response()])
    result = run_gate(session, base_url=BASE, configured_job=JOB, expected_job=JOB, script=script, apply=True)
    written = ET.fromstring(session.written)
    definitions = written.find("properties/hudson.model.ParametersDefinitionProperty/parameterDefinitions")
    assert {item.findtext("name"): item.tag for item in definitions} == {
        "WORKSPACE_ARCHIVE_URL": "hudson.model.StringParameterDefinition",
        "WORKSPACE_ARCHIVE_SHA256": "hudson.model.StringParameterDefinition",
        "WORKSPACE_ARCHIVE_TOKEN": "hudson.model.PasswordParameterDefinition",
    }
    assert len(result["workspace_parameters_added"]) == 3


@pytest.mark.parametrize("parameter_name", ["BUILD_ID", "EXPECTED_COMMIT"])
def test_apply_declares_build_identity_parameters_before_first_build(parameter_name):
    script = SCRIPT + f"\nstring(name: '{parameter_name}')"

    class EchoSession(Session):
        def request(self, method, url, **kwargs):
            if method == "POST" and url.endswith("config.xml"):
                self.written = kwargs["data"]
            if method == "GET" and url.endswith("config.xml") and hasattr(self, "written"):
                return Response(content=self.written)
            return super().request(method, url, **kwargs)

    session = EchoSession([crumb(), Response(text=VALIDATION_SUCCESS), Response(content=CONFIG), Response()])
    result = run_gate(session, base_url=BASE, configured_job=JOB, expected_job=JOB, script=script, apply=True)
    written = ET.fromstring(session.written)
    definitions = written.find("properties/hudson.model.ParametersDefinitionProperty/parameterDefinitions")
    assert {item.findtext("name"): item.tag for item in definitions} == {
        parameter_name: "hudson.model.StringParameterDefinition",
    }
    assert result["pipeline_parameters_added"] == [parameter_name]
    assert "workspace_parameters_added" not in result


def test_apply_rejects_wrong_build_identity_parameter_type_before_job_write():
    config = ET.fromstring(CONFIG)
    definitions = ET.SubElement(ET.SubElement(ET.SubElement(config, "properties"), "hudson.model.ParametersDefinitionProperty"), "parameterDefinitions")
    parameter = ET.SubElement(definitions, "hudson.model.PasswordParameterDefinition")
    ET.SubElement(parameter, "name").text = "BUILD_ID"
    script = SCRIPT + "\nstring(name: 'BUILD_ID')"
    session = Session([crumb(), Response(text=VALIDATION_SUCCESS), Response(content=ET.tostring(config))])
    with pytest.raises(PipelineGateError, match="parameter type"):
        run_gate(session, base_url=BASE, configured_job=JOB, expected_job=JOB, script=script, apply=True)
    assert not any(method == "POST" and url.endswith("config.xml") for method, url, _ in session.calls)
