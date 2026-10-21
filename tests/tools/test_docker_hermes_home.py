"""Docker sandbox HOME and the host Hermes directory are one path.

``$HOME/.hermes/scripts`` and the absolute Hermes ``scripts`` directory must be
the same bind. A second tree under ``/root/.hermes`` is not created. The legacy
layout (Hermes home not named ``.hermes``) keeps the ``/root/.hermes`` mirror.
"""

import json
import os

import pytest

from tools.environments import docker as docker_env
from tools.credential_files import (
    docker_visible_hermes_dir, docker_visible_home, get_hermes_tree_mounts,
)
import tools.terminal_tool as tt
from tests.tools.test_docker_environment import _make_dummy_env, _mock_subprocess_run, _run_args_from_calls


def _layout(tmp_path):
    home = tmp_path / "victor"
    hermes = home / ".hermes"
    scripts = hermes / "scripts"
    shared = hermes / "shared"
    scripts.mkdir(parents=True)
    shared.mkdir()
    (scripts / "now_in_zone.py").write_text("print('ok')\n", encoding="utf-8")
    (shared / "rhett_current_iana_tz").write_text("UTC\n", encoding="utf-8")
    skill = hermes / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    token = hermes / "token.json"
    token.write_text("{}\n", encoding="utf-8")
    return home, hermes, scripts, shared, token


def _env_names(run_args):
    return [run_args[i + 1] for i, flag in enumerate(run_args[:-1]) if flag == "-e"]


def _volume_specs(run_args):
    return [run_args[i + 1] for i, flag in enumerate(run_args[:-1]) if flag == "-v"]


@pytest.fixture
def identity_home(tmp_path, monkeypatch):
    home, hermes, scripts, shared, token = _layout(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setattr(
        "tools.credential_files.get_credential_file_mounts",
        lambda: [{"host_path": str(token), "container_path": "/root/.hermes/token.json"}],
    )
    return home, hermes, scripts, shared, token


def test_legacy_layout_keeps_root_mirror(tmp_path, monkeypatch):
    hermes = tmp_path / "hermes_test"
    (hermes / "scripts").mkdir(parents=True)
    (hermes / "skills" / "demo").mkdir(parents=True)
    ((hermes / "skills" / "demo") / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    assert docker_visible_hermes_dir() == "/root/.hermes"
    assert docker_visible_home() is None
    assert get_hermes_tree_mounts() == []

    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    calls = _mock_subprocess_run(monkeypatch)
    env = _make_dummy_env()
    run_args = _run_args_from_calls(calls)
    assert "HOME" not in _env_names(run_args)
    assert env._container_user_home is None
    assert any(spec.endswith(":/root/.hermes/skills:ro") for spec in _volume_specs(run_args))
    assert not any("/scripts:" in spec for spec in _volume_specs(run_args))


def test_scripts_and_shared_bind_at_home_hermes(identity_home, monkeypatch):
    home, hermes, scripts, shared, token = identity_home
    assert docker_visible_home() == str(home)
    assert docker_visible_hermes_dir() == str(hermes)
    mounts = {entry["container_path"]: entry["host_path"] for entry in get_hermes_tree_mounts()}
    assert mounts[str(scripts)] == str(scripts)
    assert mounts[str(shared)] == str(shared)

    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    calls = _mock_subprocess_run(monkeypatch)
    env = _make_dummy_env()
    run_args, kwargs = next(
        (cmd, kw) for cmd, kw in calls if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "run"
    )
    specs = _volume_specs(run_args)
    assert specs.count(f"{scripts}:{scripts}:ro") == 1
    assert specs.count(f"{shared}:{shared}:ro") == 1
    assert f"{token}:/root/.hermes/token.json:ro" in specs
    assert any(spec.endswith(":/root/.hermes/skills:ro") for spec in specs)
    assert not any("/root/.hermes/scripts" in spec or "/root/.hermes/shared" in spec for spec in specs)
    assert "HOME" in _env_names(run_args)
    assert kwargs["env"]["HOME"] == str(home)
    assert env._container_user_home == str(home)


def test_existing_scripts_volume_is_not_mounted_twice(identity_home, monkeypatch):
    _home, _hermes, scripts, shared, _token = identity_home
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    calls = _mock_subprocess_run(monkeypatch)
    _make_dummy_env(volumes=[f"{scripts}:{scripts}:ro", f"{shared}:{shared}:ro"])
    specs = _volume_specs(_run_args_from_calls(calls))
    assert specs.count(f"{scripts}:{scripts}:ro") == 1
    assert specs.count(f"{shared}:{shared}:ro") == 1


def test_container_home_changes_reuse_fingerprint():
    base = docker_env._reuse_environment_fingerprint(
        image="python:3.11", mount_args=["-v", "volume-a:/workspace"], hermes_home="/profiles/alpha",
    )
    assert base == docker_env._reuse_environment_fingerprint(
        image="python:3.11", mount_args=["-v", "volume-a:/workspace"], hermes_home="/profiles/alpha",
        container_home="",
    )
    assert base != docker_env._reuse_environment_fingerprint(
        image="python:3.11", mount_args=["-v", "volume-a:/workspace"], hermes_home="/profiles/alpha",
        container_home="/home/victor",
    )


def test_cwd_on_scripts_stays_on_the_hermes_path(identity_home, monkeypatch):
    _home, _hermes, scripts, shared, _token = identity_home
    scratch = shared / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", "[]")
    monkeypatch.setenv("TERMINAL_CWD", str(scripts))
    assert tt._resolve_config_cwd("docker", False) == (str(scripts), None)

    tt.record_session_cwd("hermes-home-scripts", str(scripts))
    try:
        assert tt._resolve_command_cwd(
            workdir=None, default_cwd="/root", session_key="hermes-home-scripts", env_type="docker",
        ) == str(scripts)
        assert tt._resolve_command_cwd(
            workdir=str(scripts / "now_in_zone.py"), default_cwd="/root",
            session_key="hermes-home-scripts", env_type="docker",
        ) == str(scripts / "now_in_zone.py")
    finally:
        tt.clear_session_cwd("hermes-home-scripts")


def test_scratch_cwd_follows_the_workspace_volume(identity_home, monkeypatch):
    _home, _hermes, _scripts, shared, _token = identity_home
    scratch = shared / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", json.dumps([f"{scratch}:/workspace"]))
    monkeypatch.setenv("TERMINAL_CWD", str(scratch))
    assert tt._resolve_config_cwd("docker", False) == ("/workspace", None)

    tt.record_session_cwd("hermes-home-scratch", str(scratch))
    try:
        assert tt._resolve_command_cwd(
            workdir=None, default_cwd="/root", session_key="hermes-home-scratch", env_type="docker",
        ) == "/workspace"
    finally:
        tt.clear_session_cwd("hermes-home-scratch")


def test_unrelated_host_cwd_is_still_discarded(identity_home, monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.setenv("TERMINAL_DOCKER_VOLUMES", "[]")
    tt.record_session_cwd("hermes-home-other", "/Users/me/dev/repo")
    try:
        assert tt._resolve_command_cwd(
            workdir=None, default_cwd="/workspace", session_key="hermes-home-other", env_type="docker",
        ) == "/workspace"
    finally:
        tt.clear_session_cwd("hermes-home-other")
