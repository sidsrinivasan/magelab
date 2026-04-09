"""
Docker helpers for running orgs in containers.

Provides image management, containerized org execution, and workspace
subprocess helpers for the pipeline execution module.

Container lifecycle:
    start_container()   → creates a long-lived container (sleep infinity)
    run_in_docker()     → docker exec to run the magelab org
    run_in_workspace()  → docker exec to run arbitrary commands (setup, eval)
    cleanup_container() → docker rm
"""

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..auth import AuthMode, ResolvedAuth
from ..orchestrator import RunOutcome
from ..org_config import ResumeMode

_module_logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "magelab:latest"

# magelab repo root — used for building the Docker image.
# Only valid when running from the repo, not from a pip install.
_REPO_ROOT = Path(__file__).parent.parent.parent.parent


# =============================================================================
# Image management
# =============================================================================


async def ensure_image(image_name: str = DEFAULT_IMAGE, force: bool = False) -> None:
    """Ensure the Docker image exists, building it automatically if needed.

    Builds from the repo's Dockerfile on first use or when force=True.
    This only works when running from the magelab repo (not from a pip install).

    Args:
        image_name: Docker image name/tag.
        force: Force rebuild even if image already exists.
    """
    # Check docker is available
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "version",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            "Docker is not installed or not running. "
            "Install Docker Desktop from https://www.docker.com/products/docker-desktop/"
        )

    # Skip build if image exists and not forced
    if not force:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "image",
            "inspect",
            image_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if proc.returncode == 0:
            return

    # Build the image
    dockerfile = _REPO_ROOT / "Dockerfile"
    if not dockerfile.exists():
        raise RuntimeError(
            f"Cannot build Docker image: no Dockerfile found at {dockerfile}. "
            f"If magelab is installed via pip, pull a prebuilt image instead: "
            f"docker pull {image_name}"
        )

    _module_logger.info("Building Docker image %s...", image_name)

    proc = await asyncio.create_subprocess_exec(
        "docker",
        "build",
        "-f",
        str(dockerfile),
        "-t",
        image_name,
        str(_REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()

    if proc.returncode != 0:
        output = stdout.decode(errors="replace") if stdout else ""
        raise RuntimeError(f"Docker build failed:\n{output}")

    _module_logger.info("Docker image %s built successfully", image_name)


# =============================================================================
# Container lifecycle
# =============================================================================


def _container_marker(output_dir: Path) -> Path:
    """Path to the marker file that stores the running container name."""
    return output_dir / ".docker_container"


async def start_container(
    output_dir: Path,
    frontend_port: Optional[int],
    auth: Optional[ResolvedAuth] = None,
    image: str = DEFAULT_IMAGE,
) -> str:
    """Create and start a long-lived Docker container for the pipeline.

    The container runs ``sleep infinity`` as its main process and stays alive
    for the duration of the pipeline. All work (org runs, setup, eval) happens
    via ``docker exec``.

    Removes any stale container with the same name (handles crash recovery).

    Args:
        output_dir: Host directory mounted into the container at /app.
        frontend_port: Port to expose for the frontend dashboard (None = no frontend).
        auth: Resolved authentication credentials.
        image: Docker image name/tag.

    Returns:
        The container name.
    """
    container_name = f"magelab-{os.getpid()}-{output_dir.name}"

    # Remove stale container from a previous unclean exit
    await _remove_container(container_name)

    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--stop-timeout",
        "1",
        "-v",
        f"{output_dir.resolve()}:/app",
    ]

    # Run as host user so files aren't root-owned (not needed on Windows)
    if sys.platform != "win32":
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]

    # Pass API key to container if using API key auth.
    # SUB auth doesn't need env flags — credentials are staged in the mounted output_dir.
    if auth is not None and auth.mode == AuthMode.API_KEY and auth.api_key:
        cmd += ["-e", f"ANTHROPIC_API_KEY={auth.api_key}"]

    if frontend_port is not None:
        cmd += ["-p", f"{frontend_port}:{frontend_port}"]

    cmd += [image, "sleep", "infinity"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        error = stderr.decode(errors="replace") if stderr else ""
        raise RuntimeError(f"Failed to start Docker container: {error}")

    # Write marker after container is confirmed running
    _container_marker(output_dir).write_text(container_name)

    return container_name


async def cleanup_container(output_dir: Path) -> None:
    """Remove the Docker container for a pipeline run.

    Reads the container name from the .docker_container marker, removes the
    container, and deletes the marker file. No-op if no marker exists.
    """
    marker = _container_marker(output_dir)
    if not marker.exists():
        return
    container_name = marker.read_text().strip()
    await _remove_container(container_name)
    marker.unlink(missing_ok=True)


async def _remove_container(container_name: str) -> None:
    """Force-remove a container by name. No-op if it doesn't exist."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "rm",
        "-f",
        container_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()


# =============================================================================
# Execution
# =============================================================================


async def run_in_docker(
    config_path: str,
    output_dir: Path,
    frontend_port: Optional[int],
    resume_mode: Optional[ResumeMode],
    auth: Optional[ResolvedAuth] = None,
    logger: Optional[logging.Logger] = None,
) -> RunOutcome:
    """Run a single org phase inside the pipeline's Docker container.

    Uses ``docker exec`` on the container created by ``start_container``.

    Args:
        config_path: Path to the config YAML (must be inside output_dir).
        output_dir: Pipeline output directory (mounted at /app in the container).
        frontend_port: Port for the frontend dashboard (already exposed by start_container).
        resume_mode: Resume mode for the magelab CLI.
        auth: Resolved authentication credentials.
        logger: Logger for error messages.

    Returns:
        RunOutcome derived from the container's exit code.
    """
    marker = _container_marker(output_dir)
    if not marker.exists():
        raise RuntimeError("No Docker container running. Call start_container() first.")
    container_name = marker.read_text().strip()

    config_file = Path(config_path)
    try:
        rel_config = config_file.resolve().relative_to(output_dir.resolve())
    except ValueError:
        raise ValueError(f"Docker org runner requires config inside output_dir, got {config_path}")
    container_config = f"/app/{rel_config}"

    cmd = [
        "docker",
        "exec",
    ]
    if sys.platform != "win32":
        cmd += ["-u", f"{os.getuid()}:{os.getgid()}"]
    cmd += [
        container_name,
        "uv",
        "run",
        "--directory",
        "/opt/magelab",
        "magelab",
        container_config,
        "--output-dir",
        "/app",
    ]

    if frontend_port is not None:
        cmd += ["--frontend-port", str(frontend_port)]
    else:
        cmd += ["--no-frontend"]

    if resume_mode is not None:
        cmd += ["--resume", resume_mode.value]

    # Tell the containerized CLI which auth mode to use.
    # For SUB: credentials file is already staged in the mounted output_dir.
    # The container CLI will find it at /app/.sessions/_credentials/.credentials.json.
    # For API_KEY: ANTHROPIC_API_KEY is already in the container env via -e flag.
    if auth is not None:
        if auth.mode == AuthMode.SUB:
            cmd += ["--sub", "/app/.sessions/_credentials/.credentials.json"]
        elif auth.mode == AuthMode.API_KEY:
            cmd += ["--api-key"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await proc.communicate()
    except (asyncio.CancelledError, KeyboardInterrupt):
        stop = await asyncio.create_subprocess_exec(
            "docker",
            "stop",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await stop.communicate()
        raise

    outcome = RunOutcome.from_exit_code(proc.returncode)
    if outcome == RunOutcome.FAILURE and stdout:
        _log = logger or _module_logger
        _log.error(
            "Docker container failed (exit %d): %s",
            proc.returncode,
            stdout.decode(errors="replace")[-2000:],
        )

    return outcome


@dataclass
class WorkspaceResult:
    """Result of a run_in_workspace call, matching subprocess.CompletedProcess interface."""

    returncode: int
    stdout: str
    stderr: str


async def run_in_workspace(
    cmd: list[str],
    output_dir: Path,
    env: Optional[dict] = None,
    auth: Optional[ResolvedAuth] = None,
    timeout: Optional[float] = None,
    logger: Optional[logging.Logger] = None,
) -> WorkspaceResult:
    """Run a command in the workspace, using Docker if the pipeline used Docker.

    If a ``.docker_container`` marker exists (written by ``start_container``),
    uses ``docker exec`` on the existing container — preserving any packages
    agents installed during the org run. Otherwise runs locally via subprocess.

    This function is async to avoid blocking the event loop during concurrent
    pipeline runs.

    Args:
        cmd: Command to run (e.g. ["python", "src/predict.py", "data/test.json"]).
        output_dir: Pipeline output directory (parent of workspace/).
        env: Environment variables for local runs. Ignored in Docker mode.
        auth: Resolved auth credentials. For Docker mode, API key is passed
            via -e flag. For local mode, merged into env dict.
        timeout: Timeout in seconds.
        logger: Logger for error messages.

    Returns:
        WorkspaceResult with returncode, stdout, and stderr.
    """
    workspace_dir = output_dir / "workspace"
    if not workspace_dir.is_dir():
        raise FileNotFoundError(f"No workspace directory found at {workspace_dir}")

    container_marker = _container_marker(output_dir)

    if container_marker.exists():
        container_name = container_marker.read_text().strip()
        run_cmd = [
            "docker",
            "exec",
            "-w",
            "/app/workspace",
        ]
        if sys.platform != "win32":
            run_cmd += ["-u", f"{os.getuid()}:{os.getgid()}"]
        run_cmd += ["-e", "PYTHONPATH=/app/workspace"]
        if auth is not None and auth.mode == AuthMode.API_KEY and auth.api_key:
            run_cmd += ["-e", f"ANTHROPIC_API_KEY={auth.api_key}"]
        run_cmd += [container_name] + cmd
        proc_kwargs = {}
    else:
        run_cmd = cmd
        run_env = env
        if auth is not None and auth.mode == AuthMode.API_KEY and auth.api_key:
            run_env = {**(env or os.environ), "ANTHROPIC_API_KEY": auth.api_key}
        proc_kwargs = {"cwd": workspace_dir, "env": run_env}

    try:
        proc = await asyncio.create_subprocess_exec(
            *run_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **proc_kwargs,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        if logger:
            logger.error("run_in_workspace timed out after %ss: %s", timeout, " ".join(cmd))
        return WorkspaceResult(returncode=124, stdout="", stderr="Timed out")

    result = WorkspaceResult(
        returncode=proc.returncode,
        stdout=stdout.decode(errors="replace") if stdout else "",
        stderr=stderr.decode(errors="replace") if stderr else "",
    )
    if logger:
        if result.returncode != 0:
            logger.error("run_in_workspace failed (exit %d): %s\n%s", result.returncode, " ".join(cmd), result.stderr[-1000:])
        else:
            logger.info("run_in_workspace succeeded: %s", " ".join(cmd))
    return result
