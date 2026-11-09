"""Static checks on deployment manifests: they parse, and RBAC stays read-only."""

from __future__ import annotations

import py_compile
from pathlib import Path
from typing import Any

import yaml

from tests.conftest import ROOT

DEPLOY = ROOT / "deploy"


def docs(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def test_all_manifests_parse() -> None:
    files = list(DEPLOY.rglob("*.yaml")) + list(DEPLOY.rglob("*.yml")) + [ROOT / "compose.yaml"]
    assert len(files) > 10
    for f in files:
        docs(f)


def test_rbac_is_read_only_and_excludes_secrets() -> None:
    roles = [d for d in docs(DEPLOY / "kubernetes" / "rbac.yaml") if d["kind"] == "ClusterRole"]
    assert roles
    for role in roles:
        for rule in role["rules"]:
            assert set(rule["verbs"]) <= {"get", "list", "watch"}, rule
            assert not {"secrets", "configmaps", "pods/log", "pods/exec", "*"} & set(
                rule["resources"]
            )


def test_agent_deployment_is_hardened() -> None:
    [deployment] = [
        d for d in docs(DEPLOY / "kubernetes" / "deployment.yaml") if d["kind"] == "Deployment"
    ]
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "triage-agent"
    assert pod["securityContext"]["runAsNonRoot"] is True
    ctx = pod["containers"][0]["securityContext"]
    assert ctx["readOnlyRootFilesystem"] is True
    assert ctx["allowPrivilegeEscalation"] is False


def test_kustomization_references_existing_files() -> None:
    kustomization = docs(DEPLOY / "kubernetes" / "kustomization.yaml")[0]
    for resource in kustomization["resources"]:
        assert (DEPLOY / "kubernetes" / resource).exists()


def test_demo_app_compiles(tmp_path: Path) -> None:
    py_compile.compile(
        str(DEPLOY / "demo-app" / "app.py"), cfile=str(tmp_path / "app.pyc"), doraise=True
    )
