"""Regression tests for the Docker terminal network toggle.

Ported from NanoClaw PR #2713's opt-in egress lockdown idea. Hermes already
has DockerEnvironment(network=False), but the terminal config path did not
expose it, so operators could not request networkless Docker execution from
config.yaml.
"""

import tools.terminal_tool as terminal_tool
from tools.environments import docker as docker_env


def test_terminal_env_config_reads_docker_network_toggle(monkeypatch):
    monkeypatch.setenv("TERMINAL_DOCKER_NETWORK", "false")

    config = terminal_tool._get_env_config()

    assert config["docker_network"] is False


def test_sibling_container_config_sites_carry_docker_network():
    """Every container_config dict that carries docker_run_as_host_user must
    also carry docker_network — otherwise that code path silently falls back
    to networked containers while the terminal path honors the lockdown
    (the probe/exec asymmetry reported on issue #46358).
    """
    import ast
    import inspect

    import tools.code_execution_tool as code_execution_tool
    import tools.file_tools as file_tools

    for module in (terminal_tool, file_tools, code_execution_tool):
        tree = ast.parse(inspect.getsource(module))
        sites = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if "docker_run_as_host_user" in keys:
                sites += 1
                assert "docker_network" in keys, (
                    f"{module.__name__} builds a container_config with "
                    f"docker_run_as_host_user but without docker_network "
                    f"(line {node.lineno})"
                )
        assert sites >= 1, f"expected at least one container_config site in {module.__name__}"


# Stand-in for the real creation-config fingerprint so these tests don't depend
# on whatever volume set the host happens to produce.
_CURRENT_FP = "cfg-current"


def _reuse_guard_harness(
    monkeypatch,
    *,
    existing_mode: str,
    network: bool,
    existing_fp: str | None = _CURRENT_FP,
):
    """Drive DockerEnvironment through the cross-process reuse path with a
    fake existing container whose NetworkMode is *existing_mode* and whose
    creation-config label is *existing_fp* (``None`` = label absent).

    Returns the list of docker commands issued.
    """
    commands = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)

        class Result:
            returncode = 0
            stderr = ""
            stdout = ""

        if len(cmd) > 1 and cmd[1] == "ps":
            # Matches the egress-aware reuse probe: with egress off the
            # format string is ID\tState\tEgressLabel and docker renders a
            # missing label as "<no value>".
            Result.stdout = "existing-container-id\trunning\t<no value>\n"
        elif len(cmd) > 1 and cmd[1] == "inspect":
            fmt = cmd[cmd.index("--format") + 1] if "--format" in cmd else ""
            if "Config.Labels" in fmt:
                # Docker renders a missing label as "<no value>".
                Result.stdout = f"{existing_fp}\n" if existing_fp else "<no value>\n"
            else:
                Result.stdout = f"{existing_mode}\n"
        elif len(cmd) > 1 and cmd[1] == "run":
            Result.stdout = "fresh-container-id\n"
        return Result()

    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(
        docker_env, "_creation_config_fingerprint", lambda *_a, **_k: _CURRENT_FP,
    )
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_env.DockerEnvironment, "_storage_opt_supported", lambda self: False)

    docker_env.DockerEnvironment(
        image="python:3.11",
        cwd="/workspace",
        timeout=60,
        task_id="reuse-guard-test",
        network=network,
        persist_across_processes=True,
    )
    return commands


def test_reuse_rejects_networked_container_when_lockdown_requested(monkeypatch):
    commands = _reuse_guard_harness(monkeypatch, existing_mode="bridge", network=False)

    assert any(cmd[1:3] == ["rm", "-f"] for cmd in commands), (
        "bridge-networked container must be removed when docker_network=false"
    )
    run_cmd = next(cmd for cmd in commands if len(cmd) > 2 and cmd[1:3] == ["run", "-d"])
    assert "--network=none" in run_cmd


def test_reuse_keeps_airgapped_container_when_lockdown_requested(monkeypatch):
    commands = _reuse_guard_harness(monkeypatch, existing_mode="none", network=False)

    assert not any(cmd[1] == "rm" for cmd in commands)
    assert not any(cmd[1] == "run" for cmd in commands), "matching container must be reused"


def test_reuse_skips_inspect_when_network_enabled(monkeypatch):
    commands = _reuse_guard_harness(monkeypatch, existing_mode="none", network=True)

    # Default-network config never churns containers, even air-gapped ones
    # (operators may have created them via docker_extra_args).
    inspected = [
        cmd[cmd.index("--format") + 1]
        for cmd in commands
        if cmd[1] == "inspect" and "--format" in cmd
    ]
    assert not any("NetworkMode" in fmt for fmt in inspected)
    assert not any(cmd[1] == "rm" for cmd in commands)
    assert not any(cmd[1] == "run" for cmd in commands)


def test_reuse_rejects_container_whose_mounts_predate_config_change(monkeypatch):
    """A container created before docker_volumes changed must not be reused.

    Regression: config.yaml declared a mount the persistent container had
    never been created with, so the agent could not see it and silently fell
    back to wrong behavior with nothing in the logs.
    """
    commands = _reuse_guard_harness(
        monkeypatch, existing_mode="bridge", network=True, existing_fp="cfg-stale",
    )

    assert any(cmd[1:3] == ["rm", "-f"] for cmd in commands), (
        "container built from an older docker_volumes/docker_extra_args set "
        "must be removed"
    )
    assert any(cmd[1:3] == ["run", "-d"] for cmd in commands)


def test_reuse_rejects_container_without_config_fingerprint(monkeypatch):
    """Containers predating the fingerprint label can't be proven current."""
    commands = _reuse_guard_harness(
        monkeypatch, existing_mode="bridge", network=True, existing_fp=None,
    )

    assert any(cmd[1:3] == ["rm", "-f"] for cmd in commands)


def test_reuse_keeps_container_when_creation_config_matches(monkeypatch):
    """The guard must not churn a container on every start."""
    commands = _reuse_guard_harness(
        monkeypatch, existing_mode="bridge", network=True, existing_fp=_CURRENT_FP,
    )

    assert not any(cmd[1] == "rm" for cmd in commands)
    assert not any(cmd[1] == "run" for cmd in commands)


def test_fresh_container_is_labeled_with_its_creation_config(monkeypatch):
    """Without the label written at create time the guard can never match."""
    commands = _reuse_guard_harness(
        monkeypatch, existing_mode="bridge", network=True, existing_fp="cfg-stale",
    )

    run_cmd = next(cmd for cmd in commands if cmd[1:3] == ["run", "-d"])
    assert f"hermes-config-fp={_CURRENT_FP}" in run_cmd
