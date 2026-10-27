"""Validate workflow boundaries and execute promotion against real local Git."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml
from pip._internal.req.req_file import parse_requirements
from test_deployment import git

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workflow():
    path = ROOT / ".github/workflows/ci.yml"
    assert path.is_file(), "Task 2 workflow is missing"
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def test_ci_has_read_only_default_and_one_stable_required_job(workflow):
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["on"]) == {"pull_request", "push", "workflow_dispatch"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    job = workflow["jobs"]["ci"]
    assert job["name"] == "ci"
    assert job["runs-on"] == "ubuntu-latest"
    actions = [step for step in job["steps"] if "uses" in step]
    assert actions[0]["uses"] == (
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
    )
    assert actions[0]["with"]["persist-credentials"] == "false"
    assert actions[1]["uses"] == (
        "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
    )
    assert actions[1]["with"]["python-version"] == "3.11"
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "--constraint requirements.lock" in commands
    assert "setuptools>=75" in commands and "wheel" in commands
    assert "playwright install --with-deps chromium" in commands
    assert "ruff check ." in commands
    assert "pytest -q -W error" in commands
    assert "permissions" not in job


def test_only_successful_main_push_can_promote(workflow):
    job = workflow["jobs"]["promote"]
    assert job["needs"] == "ci"
    assert job["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    )
    assert job["permissions"] == {"contents": "write"}
    assert job["concurrency"]["cancel-in-progress"] == "false"
    assert job["concurrency"]["group"] == "paiper-deploy-promotion"
    checkout = job["steps"][0]
    assert checkout["uses"] == (
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
    )
    assert checkout["with"]["fetch-depth"] == "0"


@pytest.mark.parametrize(
    "scenario",
    [
        "current",
        "stale",
        "diverged",
        "pending",
        "pending-exhausted",
        "pending-stale",
        "denied",
    ],
)
def test_promotion_shell_checks_live_main_and_never_forces(
    workflow, tmp_path, scenario
):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "tests@example.invalid")
    git(source, "config", "user.name", "Promotion tests")
    (source / "file").write_text("one")
    git(source, "add", ".")
    git(source, "commit", "-m", "first")
    first = git(source, "rev-parse", "HEAD")
    (source / "file").write_text("two")
    git(source, "commit", "-am", "second")
    tested = git(source, "rev-parse", "HEAD")
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(remote)],
        check=True,
        capture_output=True,
    )
    git(source, "remote", "add", "origin", str(remote))
    git(remote, "--git-dir", str(remote), "update-ref", "refs/heads/deploy", first)
    if scenario == "stale":
        git(remote, "--git-dir", str(remote), "update-ref", "refs/heads/main", first)
    if scenario == "diverged":
        git(source, "checkout", "-b", "other", first)
        (source / "other").write_text("unrelated")
        git(source, "add", ".")
        git(source, "commit", "-m", "diverged")
        divergent = git(source, "rev-parse", "HEAD")
        git(source, "push", "origin", "other:deploy")
    attempts = remote / "push-attempts"
    waits = tmp_path / "waits"
    env = {**os.environ, "GITHUB_SHA": tested}
    if scenario in ("pending", "pending-exhausted", "pending-stale", "denied"):
        hook = remote / "hooks/pre-receive"
        error = (
            'Required status check "ci" is in progress.'
            if scenario != "denied"
            else "Permission denied."
        )
        hook.write_text(
            "#!/bin/sh\n"
            "attempt=0\n"
            "if [ -f push-attempts ]; then attempt=$(cat push-attempts); fi\n"
            "attempt=$((attempt + 1))\n"
            "printf '%s\\n' \"$attempt\" > push-attempts\n"
            + (
                'if [ "$attempt" -gt 2 ]; then exit 0; fi\n'
                if scenario == "pending"
                else ""
            )
            + "echo 'error: GH006: Protected branch update failed for refs/heads/deploy.' >&2\n"
            + f"echo {shlex.quote(error)} >&2\nexit 1\n"
        )
        hook.chmod(0o755)
        helpers = tmp_path / "helpers"
        helpers.mkdir()
        sleeper = helpers / "sleep"
        sleeper.write_text(
            f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> {shlex.quote(str(waits))}\n"
            + (
                f"git --git-dir {shlex.quote(str(remote))} update-ref refs/heads/main {first}\n"
                if scenario == "pending-stale"
                else ""
            )
        )
        sleeper.chmod(0o755)
        env["PATH"] = str(helpers) + os.pathsep + os.environ["PATH"]
    commands = [
        step["run"] for step in workflow["jobs"]["promote"]["steps"] if "run" in step
    ]
    assert len(commands) == 1
    result = subprocess.run(
        ["bash", "-e", "-u", "-o", "pipefail", "-c", commands[0]],
        check=False,
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
    )
    actual = git(remote, "--git-dir", str(remote), "rev-parse", "deploy")
    if scenario in ("current", "pending"):
        assert result.returncode == 0, result.stderr
        assert actual == tested
        if scenario == "pending":
            assert attempts.read_text().strip() == "3"
            assert waits.read_text().splitlines() == ["15", "15"]
    elif scenario in ("stale", "pending-stale"):
        assert result.returncode == 0, result.stderr
        assert actual == first
        if scenario == "pending-stale":
            assert attempts.read_text().strip() == "1"
    elif scenario in ("pending-exhausted", "denied"):
        assert result.returncode != 0
        assert actual == first
        if scenario == "pending-exhausted":
            assert attempts.read_text().strip() == "21"
            assert waits.read_text().splitlines() == ["15"] * 20
        else:
            assert attempts.read_text().strip() == "1"
            assert not waits.exists()
    else:
        assert result.returncode != 0
        assert actual == divergent


def test_weekly_dependabot_without_automerge():
    path = ROOT / ".github/dependabot.yml"
    assert path.is_file(), "Task 2 dependabot is missing"
    data = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert data["version"] == "2"
    updates = data["updates"]
    assert {item["package-ecosystem"] for item in updates} == {"github-actions", "pip"}
    assert all(item["directory"] == "/" for item in updates)
    assert all(item["schedule"]["interval"] == "weekly" for item in updates)


def test_dependabot_visible_pins_are_effective_manager_constraints():
    # Dependabot's pip fetcher discovers .txt/.in files, not arbitrary .lock files.
    manifest = ROOT / "requirements.txt"
    assert manifest.is_file(), "Runtime pins need a Dependabot-discoverable manifest"
    effective = list(
        parse_requirements(
            str(ROOT / "requirements.lock"), session=None, constraint=True
        )
    )
    visible = list(parse_requirements(str(manifest), session=None, constraint=True))
    assert all(item.constraint for item in effective)
    assert visible and all("==" in item.requirement for item in visible)
    assert {item.requirement for item in effective} == {
        item.requirement for item in visible
    }
    assert all(str(manifest) in item.comes_from for item in effective)
