"""Tests for magelab.pipeline.docker — ensure_image and run_in_docker."""

from unittest.mock import AsyncMock, patch

import pytest

from magelab.auth import AuthMode, ResolvedAuth
from magelab.org_config import ResumeMode
from magelab.orchestrator import RunOutcome
from magelab.pipeline.docker import ensure_image, run_in_docker

# Shared auth fixture for tests
_TEST_AUTH = ResolvedAuth(mode=AuthMode.API_KEY, api_key="sk-test-key")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_proc_mock(returncode=0, stdout=b""):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


# ---------------------------------------------------------------------------
# ensure_image tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_image_docker_not_available_raises():
    """When 'docker version' fails, RuntimeError is raised."""
    failing_proc = make_proc_mock(returncode=1)
    with patch("asyncio.create_subprocess_exec", return_value=failing_proc):
        with pytest.raises(RuntimeError, match="Docker is not installed or not running"):
            await ensure_image()


@pytest.mark.asyncio
async def test_ensure_image_already_exists_returns_without_building():
    """When image exists (inspect rc=0) and not force, returns without building."""
    docker_ok = make_proc_mock(returncode=0)
    inspect_ok = make_proc_mock(returncode=0)

    call_count = 0

    async def mock_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return docker_ok  # docker version
        return inspect_ok  # docker image inspect

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        # Should not raise and should not attempt a build (only 2 calls)
        await ensure_image()

    assert call_count == 2


@pytest.mark.asyncio
async def test_ensure_image_missing_dockerfile_raises(tmp_path):
    """When image is missing and no Dockerfile exists, RuntimeError is raised."""
    docker_ok = make_proc_mock(returncode=0)
    inspect_fail = make_proc_mock(returncode=1)

    call_count = 0

    async def mock_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return docker_ok
        return inspect_fail

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        # tmp_path has no Dockerfile, so _REPO_ROOT / 'Dockerfile' won't exist
        with patch("magelab.pipeline.docker._REPO_ROOT", tmp_path):
            with pytest.raises(RuntimeError, match="Cannot build Docker image"):
                await ensure_image()


@pytest.mark.asyncio
async def test_ensure_image_force_skips_inspect(tmp_path):
    """With force=True, inspect is skipped and build is attempted directly."""
    # Create a Dockerfile so build can proceed
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")

    docker_ok = make_proc_mock(returncode=0)
    build_ok = make_proc_mock(returncode=0)

    calls = []

    async def mock_exec(*args, **kwargs):
        calls.append(args[0])  # record the first arg (command name)
        if args[0] == "docker" and len(calls) == 1:
            return docker_ok  # docker version
        return build_ok  # docker build

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        with patch("magelab.pipeline.docker._REPO_ROOT", tmp_path):
            await ensure_image(force=True)

    # Should not have called inspect — only version and build
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_ensure_image_force_does_not_call_inspect(tmp_path):
    """With force=True, 'docker image inspect' is never invoked."""
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")

    docker_ok = make_proc_mock(returncode=0)
    build_ok = make_proc_mock(returncode=0)

    subcommands_seen = []

    async def mock_exec(*args, **kwargs):
        # args[1] is the docker subcommand (e.g. 'version', 'image', 'build')
        if len(args) > 1:
            subcommands_seen.append(args[1])
        if "version" in args:
            return docker_ok
        return build_ok

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        with patch("magelab.pipeline.docker._REPO_ROOT", tmp_path):
            await ensure_image(force=True)

    assert "image" not in subcommands_seen


@pytest.mark.asyncio
async def test_ensure_image_build_succeeds(tmp_path):
    """When image is missing but Dockerfile exists and build succeeds, no error."""
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")

    docker_ok = make_proc_mock(returncode=0)
    inspect_fail = make_proc_mock(returncode=1)
    build_ok = make_proc_mock(returncode=0)

    call_count = 0

    async def mock_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return docker_ok  # docker version
        if call_count == 2:
            return inspect_fail  # docker image inspect → not found
        return build_ok  # docker build → success

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        with patch("magelab.pipeline.docker._REPO_ROOT", tmp_path):
            # Should not raise
            await ensure_image()


@pytest.mark.asyncio
async def test_ensure_image_build_failure_raises(tmp_path):
    """When build exits non-zero, RuntimeError is raised."""
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")

    docker_ok = make_proc_mock(returncode=0)
    inspect_fail = make_proc_mock(returncode=1)
    build_fail = make_proc_mock(returncode=1, stdout=b"some build error")

    call_count = 0

    async def mock_exec(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return docker_ok
        if call_count == 2:
            return inspect_fail
        return build_fail

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        with patch("magelab.pipeline.docker._REPO_ROOT", tmp_path):
            with pytest.raises(RuntimeError, match="Docker build failed"):
                await ensure_image()


# ---------------------------------------------------------------------------
# run_in_docker tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_in_docker_config_not_inside_output_dir_raises(tmp_path):
    """config_path outside output_dir raises ValueError."""
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    config_path = str(tmp_path / "config.yaml")

    with pytest.raises(ValueError):
        await run_in_docker(
            config_path=config_path,
            output_dir=output_dir,
            frontend_port=None,
            resume_mode=None,
        )


@pytest.mark.asyncio
async def test_run_in_docker_success(tmp_path):
    """A run with exit code 0 returns RunOutcome.SUCCESS."""
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    config_path = str(output_dir / "config.yaml")

    success_proc = make_proc_mock(returncode=0)

    with patch("asyncio.create_subprocess_exec", return_value=success_proc):
        result = await run_in_docker(
            config_path=config_path,
            output_dir=output_dir,
            frontend_port=None,
            resume_mode=None,
            auth=_TEST_AUTH,
        )

    assert result == RunOutcome.SUCCESS


@pytest.mark.asyncio
async def test_run_in_docker_failure(tmp_path):
    """A run with exit code 3 returns RunOutcome.FAILURE."""
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    config_path = str(output_dir / "config.yaml")

    fail_proc = make_proc_mock(returncode=3)

    with patch("asyncio.create_subprocess_exec", return_value=fail_proc):
        result = await run_in_docker(
            config_path=config_path,
            output_dir=output_dir,
            frontend_port=None,
            resume_mode=None,
            auth=_TEST_AUTH,
        )

    assert result == RunOutcome.FAILURE


@pytest.mark.asyncio
async def test_run_in_docker_timeout(tmp_path):
    """A run with exit code 2 returns RunOutcome.TIMEOUT."""
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    config_path = str(output_dir / "config.yaml")

    timeout_proc = make_proc_mock(returncode=2)

    with patch("asyncio.create_subprocess_exec", return_value=timeout_proc):
        result = await run_in_docker(
            config_path=config_path,
            output_dir=output_dir,
            frontend_port=None,
            resume_mode=None,
            auth=_TEST_AUTH,
        )

    assert result == RunOutcome.TIMEOUT


@pytest.mark.asyncio
async def test_run_in_docker_no_frontend_flag(tmp_path):
    """When frontend_port is None, '--no-frontend' is included in the command."""
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    config_path = str(output_dir / "config.yaml")

    proc = make_proc_mock(returncode=0)
    captured_args = []

    async def mock_exec(*args, **kwargs):
        captured_args.extend(args)
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await run_in_docker(
            config_path=config_path,
            output_dir=output_dir,
            frontend_port=None,
            resume_mode=None,
            auth=_TEST_AUTH,
        )

    assert "--no-frontend" in captured_args


@pytest.mark.asyncio
async def test_run_in_docker_with_frontend_port(tmp_path):
    """When frontend_port is set, '-p' and '--frontend-port' appear in the command."""
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    config_path = str(output_dir / "config.yaml")

    proc = make_proc_mock(returncode=0)
    captured_args = []

    async def mock_exec(*args, **kwargs):
        captured_args.extend(args)
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await run_in_docker(
            config_path=config_path,
            output_dir=output_dir,
            frontend_port=8765,
            resume_mode=None,
            auth=_TEST_AUTH,
        )

    assert "-p" in captured_args
    assert "--frontend-port" in captured_args
    assert "--no-frontend" not in captured_args


@pytest.mark.asyncio
async def test_run_in_docker_with_resume_mode(tmp_path):
    """When resume_mode is set, '--resume' appears in the command."""
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    config_path = str(output_dir / "config.yaml")

    proc = make_proc_mock(returncode=0)
    captured_args = []

    async def mock_exec(*args, **kwargs):
        captured_args.extend(args)
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await run_in_docker(
            config_path=config_path,
            output_dir=output_dir,
            frontend_port=None,
            resume_mode=ResumeMode.CONTINUE,
            auth=_TEST_AUTH,
        )

    assert "--resume" in captured_args


@pytest.mark.asyncio
async def test_run_in_docker_no_resume_mode(tmp_path):
    """When resume_mode is None, '--resume' does not appear in the command."""
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    config_path = str(output_dir / "config.yaml")

    proc = make_proc_mock(returncode=0)
    captured_args = []

    async def mock_exec(*args, **kwargs):
        captured_args.extend(args)
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await run_in_docker(
            config_path=config_path,
            output_dir=output_dir,
            frontend_port=None,
            resume_mode=None,
            auth=_TEST_AUTH,
        )

    assert "--resume" not in captured_args
