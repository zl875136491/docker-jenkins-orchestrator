"""Validate the whole Pipeline with Jenkins before updating the test job.

The default operation only lints. ``--apply`` additionally replaces the inline
script in the explicitly expected test job and verifies an exact readback.
Credentials come from the existing runtime dotenv file, never from arguments
or output. HTTP 200 alone is NOT proof that Jenkins accepted a Pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote, urlsplit
import xml.etree.ElementTree as ET

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_JOB = "apps-orchestrator/orchestrator-real-delivery-20260920"
VALIDATION_SUCCESS = "Jenkinsfile successfully validated."


class PipelineGateError(RuntimeError):
    """An operation failed with a message that contains no request/secret data."""


def read_runtime_env(path: Path) -> dict[str, str]:
    """Read the existing JSON-quoted dotenv without evaluating shell syntax."""

    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                raise PipelineGateError("Runtime dotenv contains an invalid quoted value") from None
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        if not isinstance(value, str):
            raise PipelineGateError("Runtime dotenv contains a non-string value")
        values[key.strip()] = value
    return values


def _request(session: Any, method: str, url: str, **kwargs: Any) -> Any:
    try:
        response = session.request(method, url, timeout=30, allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise PipelineGateError("Jenkins request failed; no job update has been verified") from None
    if response.status_code != 200:
        raise PipelineGateError(f"Jenkins {method} returned HTTP {response.status_code}")
    return response


def _configuration_signature(node: ET.Element, path: tuple[str, ...] = ()) -> tuple[Any, ...]:
    """Compare all settings except the script, ignoring XML layout whitespace."""

    path = (*path, node.tag)
    text = node.text or ""
    if path[-2:] == ("definition", "script") or not text.strip():
        text = ""
    return (
        node.tag,
        tuple(sorted(node.attrib.items())),
        text,
        tuple(_configuration_signature(child, path) for child in node),
    )


def run_gate(
    session: Any,
    *,
    base_url: str,
    configured_job: str,
    expected_job: str,
    script: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Fail closed on lint/config mismatches; write only after successful lint."""

    if expected_job != DEFAULT_TEST_JOB:
        raise PipelineGateError("Only the authorized test Jenkins job is allowed")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise PipelineGateError("Jenkins base URL is invalid")
    if not expected_job or configured_job != expected_job:
        raise PipelineGateError("Configured Jenkins job does not match the expected test job")
    if any(part in {"", ".", ".."} for part in expected_job.split("/")):
        raise PipelineGateError("Expected Jenkins job name is invalid")
    if not script.strip():
        raise PipelineGateError("Pipeline script is empty")
    base_url = base_url.rstrip("/")

    response = _request(session, "GET", base_url + "/crumbIssuer/api/json")
    try:
        crumb = response.json()
        field, value = crumb["crumbRequestField"], crumb["crumb"]
    except (ValueError, TypeError, KeyError):
        raise PipelineGateError("Jenkins crumb response is invalid") from None
    if not isinstance(field, str) or not field or not isinstance(value, str) or not value:
        raise PipelineGateError("Jenkins crumb response is incomplete")
    headers = {field: value}

    response = _request(
        session,
        "POST",
        base_url + "/pipeline-model-converter/validate",
        data={"jenkinsfile": script},
        headers=headers,
    )
    if response.text.strip() != VALIDATION_SUCCESS:
        location = re.search(r"WorkflowScript:\s*(\d+):", response.text)
        detail = f" at Groovy line {location.group(1)}" if location else ""
        # Never expose Jenkins' body: it may echo source or credentials.
        raise PipelineGateError(f"Jenkins rejected the Pipeline{detail}; job was not updated")

    evidence: dict[str, Any] = {
        "validated_by_jenkins": True,
        "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        "target_job": expected_job,
        "applied": False,
    }
    if not apply:
        return evidence

    job_path = "/job/" + "/job/".join(quote(part, safe="") for part in expected_job.split("/"))
    config_url = base_url + job_path + "/config.xml"
    response = _request(session, "GET", config_url)
    try:
        config = ET.fromstring(response.content)
    except ET.ParseError:
        raise PipelineGateError("Jenkins job configuration is not valid XML") from None
    definition = config.find("definition")
    if definition is None or definition.get("class") != "org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition":
        raise PipelineGateError("Expected an inline Pipeline job; refusing to replace another definition")
    script_node, sandbox_node = definition.find("script"), definition.find("sandbox")
    if script_node is None or sandbox_node is None:
        raise PipelineGateError("Inline Pipeline script or sandbox setting is missing")
    sandbox = sandbox_node.text
    script_node.text = script
    pipeline_parameters = {
        "BUILD_ID": "hudson.model.StringParameterDefinition",
        "EXPECTED_COMMIT": "hudson.model.StringParameterDefinition",
        "WORKSPACE_ARCHIVE_URL": "hudson.model.StringParameterDefinition",
        "WORKSPACE_ARCHIVE_SHA256": "hudson.model.StringParameterDefinition",
        "WORKSPACE_ARCHIVE_TOKEN": "hudson.model.PasswordParameterDefinition",
        "CHECKOUT_REQUIRED": "hudson.model.StringParameterDefinition",
    }
    added_parameters = []
    for name, kind in pipeline_parameters.items():
        if f"name: '{name}'" not in script:
            continue
        properties = config.find("properties")
        if properties is None:
            properties = ET.SubElement(config, "properties")
        parameter_property = properties.find("hudson.model.ParametersDefinitionProperty")
        if parameter_property is None:
            parameter_property = ET.SubElement(properties, "hudson.model.ParametersDefinitionProperty")
        definitions = parameter_property.find("parameterDefinitions")
        if definitions is None:
            definitions = ET.SubElement(parameter_property, "parameterDefinitions")
        existing = next((item for item in definitions if item.findtext("name") == name), None)
        if existing is not None:
            if existing.tag != kind:
                raise PipelineGateError("Pipeline parameter type does not match the Pipeline contract")
            continue
        parameter = ET.SubElement(definitions, kind)
        ET.SubElement(parameter, "name").text = name
        ET.SubElement(parameter, "description")
        ET.SubElement(parameter, "defaultValue").text = "true" if name == "CHECKOUT_REQUIRED" else None
        if kind == "hudson.model.StringParameterDefinition":
            ET.SubElement(parameter, "trim").text = "false"
        added_parameters.append(name)
    payload = ET.tostring(config, encoding="utf-8", xml_declaration=True)
    _request(session, "POST", config_url, data=payload, headers={**headers, "Content-Type": "application/xml"})
    readback = _request(session, "GET", config_url)
    try:
        verified = ET.fromstring(readback.content)
    except ET.ParseError:
        raise PipelineGateError("Jenkins readback is not valid XML; update verification failed") from None
    if verified.findtext("definition/script") != script:
        raise PipelineGateError("Jenkins script readback does not match; update verification failed")
    if verified.findtext("definition/sandbox") != sandbox:
        raise PipelineGateError("Jenkins sandbox setting changed; update verification failed")
    if _configuration_signature(verified) != _configuration_signature(config):
        raise PipelineGateError("Jenkins settings other than the Pipeline changed; update verification failed")
    evidence.update(applied=True, readback_exact=True, sandbox_preserved=True, other_settings_preserved=True)
    if added_parameters:
        evidence["pipeline_parameters_added"] = added_parameters
        workspace_parameters_added = [name for name in added_parameters if name.startswith("WORKSPACE_") or name == "CHECKOUT_REQUIRED"]
        if workspace_parameters_added:
            evidence["workspace_parameters_added"] = workspace_parameters_added
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path("/opt/orchestrator-test/.env"))
    parser.add_argument("--pipeline", type=Path, default=ROOT / "jenkins" / "orchestrator-build.groovy")
    parser.add_argument("--apply", action="store_true", help="Update the expected test job after Jenkins accepts the script")
    options = parser.parse_args()
    try:
        settings = read_runtime_env(options.env_file)
        required = ("ORCHESTRATOR_JENKINS_URL", "ORCHESTRATOR_JENKINS_USER", "ORCHESTRATOR_JENKINS_PASSWORD", "ORCHESTRATOR_JENKINS_JOB_NAME")
        if any(not settings.get(key) for key in required):
            raise PipelineGateError("Required Jenkins settings are missing")
        script = options.pipeline.read_text(encoding="utf-8")
        with requests.Session() as session:
            session.auth = (settings["ORCHESTRATOR_JENKINS_USER"], settings["ORCHESTRATOR_JENKINS_PASSWORD"])
            evidence = run_gate(
                session,
                base_url=settings["ORCHESTRATOR_JENKINS_URL"],
                configured_job=settings["ORCHESTRATOR_JENKINS_JOB_NAME"],
                expected_job=DEFAULT_TEST_JOB,
                script=script,
                apply=options.apply,
            )
    except OSError:
        print(json.dumps({"passed": False, "error": "Cannot read the runtime settings or Pipeline file"}))
        return 1
    except PipelineGateError as exc:
        print(json.dumps({"passed": False, "error": str(exc)}))
        return 1
    print(json.dumps({"passed": True, **evidence}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
