"""
Authentication resolution for magelab.

Two auth modes, each with a separate delivery mechanism:

- **Subscription (SUB):** OAuth via `.credentials.json` file. The SDK needs the file
  in each agent's CLAUDE_CONFIG_DIR so it can refresh access tokens (they expire ~8h).
  File is staged to output_dir/.sessions/_credentials/ and fanned out by the orchestrator.

- **API key (API_KEY):** Static key forwarded as ANTHROPIC_API_KEY env var to agent
  subprocesses. No files on disk in the output dir.
"""

import logging
import os
import shutil
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from dotenv import find_dotenv, load_dotenv


class AuthMode(Enum):
    SUB = "sub"
    API_KEY = "api-key"


@dataclass(frozen=True)
class ResolvedAuth:
    """Resolved authentication credentials.

    For SUB mode: credentials_path points to a .credentials.json file.
    For API_KEY mode: api_key holds the key string.
    source describes where the credentials were found.
    """

    mode: AuthMode
    source: str = ""
    credentials_path: Optional[Path] = None
    api_key: Optional[str] = None


def resolve_sub(path: Optional[Path] = None) -> ResolvedAuth:
    """Resolve subscription (OAuth) credentials.

    Resolution order:
    1. Explicit path provided — validate and use directly.
    2. $CLAUDE_CONFIG_DIR/.credentials.json if CLAUDE_CONFIG_DIR is set.
    3. ~/.claude/.credentials.json (Linux/Windows default).

    On macOS, credentials are stored in the Keychain, not as a file.
    macOS users must export their credentials from Keychain Access and
    provide the file via --sub <path>.

    Raises RuntimeError if no credentials can be found.
    """
    # 1. Explicit path
    if path is not None:
        if not path.is_file():
            raise RuntimeError(f"Credentials file not found: {path}")
        return ResolvedAuth(mode=AuthMode.SUB, source=f"explicit path: {path}", credentials_path=path)

    # 2. CLAUDE_CONFIG_DIR override
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if config_dir:
        creds = Path(config_dir) / ".credentials.json"
        if creds.is_file():
            return ResolvedAuth(mode=AuthMode.SUB, source=f"CLAUDE_CONFIG_DIR: {creds}", credentials_path=creds)

    # 3. Default ~/.claude/.credentials.json
    default_creds = Path.home() / ".claude" / ".credentials.json"
    if default_creds.is_file():
        return ResolvedAuth(mode=AuthMode.SUB, source="~/.claude/.credentials.json", credentials_path=default_creds)

    raise RuntimeError(
        "No subscription credentials found. Options:\n"
        "  - Provide a credentials file: --sub <path>\n"
        "  - On Linux/Windows: log in with 'claude login' (creates ~/.claude/.credentials.json)\n"
        "  - On macOS: export credentials from Keychain Access and pass the file path"
    )


def resolve_api_key(env_file: Optional[Path] = None) -> ResolvedAuth:
    """Resolve API key authentication.

    Resolution order:
    1. env_file provided — load it with load_dotenv().
    2. Check ANTHROPIC_API_KEY in environment.
    3. If not found, search for .env from CWD upward via find_dotenv().
    4. Error if still not found.

    Raises RuntimeError if no API key can be found.
    """
    # 1. Explicit env file
    if env_file is not None:
        if not env_file.is_file():
            raise RuntimeError(f".env file not found: {env_file}")
        load_dotenv(env_file)
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            return ResolvedAuth(mode=AuthMode.API_KEY, source=f"env file: {env_file}", api_key=api_key)
        raise RuntimeError(f"ANTHROPIC_API_KEY not found in {env_file}")

    # 2. Check environment
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        return ResolvedAuth(mode=AuthMode.API_KEY, source="environment variable", api_key=api_key)

    # 3. Search for .env from CWD upward
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path)
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            return ResolvedAuth(mode=AuthMode.API_KEY, source=f"env file: {dotenv_path}", api_key=api_key)

    raise RuntimeError(
        "No API key found. Options:\n"
        "  - Provide a .env file: --api-key <path>\n"
        "  - Set ANTHROPIC_API_KEY in your environment\n"
        "  - Place a .env file with ANTHROPIC_API_KEY in your working directory"
    )


def stage_credentials(
    auth: ResolvedAuth,
    output_dir: Path,
    logger: logging.Logger,
) -> None:
    """Stage credentials file into the output directory for orchestrator fan-out.

    For SUB mode: copies .credentials.json to output_dir/.sessions/_credentials/
    with 0600 permissions.
    For API_KEY mode: no-op.
    """
    logger.info("Auth: %s (%s)", auth.mode.value, auth.source)
    if auth.mode == AuthMode.API_KEY:
        return
    assert auth.credentials_path is not None
    dest_dir = output_dir / ".sessions" / "_credentials"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ".credentials.json"
    if dest.resolve() != auth.credentials_path.resolve():
        shutil.copy2(auth.credentials_path, dest)
        os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        logger.info("Staged credentials to %s", dest)
    else:
        logger.info("Credentials already staged at %s", dest)
