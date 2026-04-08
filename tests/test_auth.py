"""Tests for magelab.auth — AuthMode, ResolvedAuth, resolve_sub, resolve_api_key, stage_credentials."""

import dataclasses
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from magelab.auth import AuthMode, ResolvedAuth, resolve_api_key, resolve_sub, stage_credentials


# =============================================================================
# ResolvedAuth
# =============================================================================


class TestResolvedAuth:
    def test_sub_mode_creation(self):
        """ResolvedAuth with SUB mode stores credentials_path."""
        auth = ResolvedAuth(mode=AuthMode.SUB, credentials_path=Path("/tmp/creds.json"))
        assert auth.mode == AuthMode.SUB
        assert auth.credentials_path == Path("/tmp/creds.json")
        assert auth.api_key is None

    def test_api_key_mode_creation(self):
        """ResolvedAuth with API_KEY mode stores api_key."""
        auth = ResolvedAuth(mode=AuthMode.API_KEY, api_key="sk-ant-test-key")
        assert auth.mode == AuthMode.API_KEY
        assert auth.api_key == "sk-ant-test-key"
        assert auth.credentials_path is None

    def test_frozen(self):
        """ResolvedAuth is frozen — attributes cannot be modified after creation."""
        auth = ResolvedAuth(mode=AuthMode.SUB, credentials_path=Path("/tmp/creds.json"))
        with pytest.raises(dataclasses.FrozenInstanceError):
            auth.mode = AuthMode.API_KEY
        with pytest.raises(dataclasses.FrozenInstanceError):
            auth.credentials_path = Path("/other")
        with pytest.raises(dataclasses.FrozenInstanceError):
            auth.api_key = "new-key"


# =============================================================================
# AuthMode
# =============================================================================


class TestAuthMode:
    def test_values(self):
        assert AuthMode.SUB.value == "sub"
        assert AuthMode.API_KEY.value == "api-key"


# =============================================================================
# resolve_sub
# =============================================================================


class TestResolveSub:
    def test_explicit_path_exists(self, tmp_path):
        """Explicit path that exists returns ResolvedAuth with SUB mode and that path."""
        creds = tmp_path / ".credentials.json"
        creds.write_text('{"token": "abc"}')
        result = resolve_sub(path=creds)
        assert result.mode == AuthMode.SUB
        assert result.credentials_path == creds

    def test_explicit_path_not_found(self, tmp_path):
        """Explicit path that doesn't exist raises RuntimeError."""
        missing = tmp_path / "nonexistent.json"
        with pytest.raises(RuntimeError, match="Credentials file not found"):
            resolve_sub(path=missing)

    def test_claude_config_dir_with_credentials(self, tmp_path, monkeypatch):
        """CLAUDE_CONFIG_DIR set with .credentials.json present finds it."""
        creds = tmp_path / ".credentials.json"
        creds.write_text('{"token": "abc"}')
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        result = resolve_sub()
        assert result.mode == AuthMode.SUB
        assert result.credentials_path == creds

    def test_claude_config_dir_without_credentials(self, tmp_path, monkeypatch):
        """CLAUDE_CONFIG_DIR set but no .credentials.json falls through to next resolution step."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        # Also patch Path.home so ~/.claude doesn't accidentally match
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        with pytest.raises(RuntimeError, match="No subscription credentials found"):
            resolve_sub()

    def test_default_home_credentials(self, tmp_path, monkeypatch):
        """~/.claude/.credentials.json exists and is found when no explicit path or env var."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        fake_home = tmp_path / "fakehome"
        claude_dir = fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        creds = claude_dir / ".credentials.json"
        creds.write_text('{"token": "abc"}')
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        result = resolve_sub()
        assert result.mode == AuthMode.SUB
        assert result.credentials_path == creds

    def test_nothing_exists_raises(self, tmp_path, monkeypatch):
        """No credentials anywhere raises RuntimeError with helpful message."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        with pytest.raises(RuntimeError, match="No subscription credentials found"):
            resolve_sub()

    def test_nothing_exists_error_mentions_options(self, tmp_path, monkeypatch):
        """Error message mentions --sub, claude login, and macOS Keychain."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        with pytest.raises(RuntimeError) as exc_info:
            resolve_sub()
        msg = str(exc_info.value)
        assert "--sub" in msg
        assert "claude login" in msg
        assert "macOS" in msg

    def test_claude_config_dir_takes_priority_over_home(self, tmp_path, monkeypatch):
        """CLAUDE_CONFIG_DIR credentials take priority over ~/.claude/ credentials."""
        # Set up ~/.claude/.credentials.json
        fake_home = tmp_path / "fakehome"
        claude_dir = fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        home_creds = claude_dir / ".credentials.json"
        home_creds.write_text('{"source": "home"}')

        # Set up CLAUDE_CONFIG_DIR/.credentials.json
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        config_creds = config_dir / ".credentials.json"
        config_creds.write_text('{"source": "config_dir"}')

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        result = resolve_sub()
        assert result.credentials_path == config_creds


# =============================================================================
# resolve_api_key
# =============================================================================


class TestResolveApiKey:
    def test_explicit_env_file_with_key(self, tmp_path, monkeypatch):
        """Explicit env file that exists with ANTHROPIC_API_KEY returns ResolvedAuth."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY=sk-ant-test-123\n")
        result = resolve_api_key(env_file=env_file)
        assert result.mode == AuthMode.API_KEY
        assert result.api_key == "sk-ant-test-123"

    def test_explicit_env_file_not_found(self, tmp_path):
        """Explicit env file that doesn't exist raises RuntimeError."""
        missing = tmp_path / "nonexistent.env"
        with pytest.raises(RuntimeError, match=".env file not found"):
            resolve_api_key(env_file=missing)

    def test_explicit_env_file_without_key(self, tmp_path, monkeypatch):
        """Explicit env file exists but has no ANTHROPIC_API_KEY raises RuntimeError."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER_VAR=hello\n")
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY not found"):
            resolve_api_key(env_file=env_file)

    def test_env_var_present(self, monkeypatch):
        """ANTHROPIC_API_KEY in environment returns it directly."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-key")
        result = resolve_api_key()
        assert result.mode == AuthMode.API_KEY
        assert result.api_key == "sk-ant-env-key"

    def test_find_dotenv_fallback(self, tmp_path, monkeypatch):
        """No env file, no env var, .env found by find_dotenv with ANTHROPIC_API_KEY."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        dotenv_path = str(tmp_path / ".env")
        Path(dotenv_path).write_text("ANTHROPIC_API_KEY=sk-ant-found\n")
        with patch("magelab.auth.find_dotenv", return_value=dotenv_path):
            result = resolve_api_key()
        assert result.mode == AuthMode.API_KEY
        assert result.api_key == "sk-ant-found"

    def test_nothing_found_raises(self, monkeypatch):
        """No env file, no env var, no .env found raises RuntimeError."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch("magelab.auth.find_dotenv", return_value=""):
            with pytest.raises(RuntimeError, match="No API key found"):
                resolve_api_key()

    def test_nothing_found_error_mentions_options(self, monkeypatch):
        """Error message mentions --api-key, ANTHROPIC_API_KEY, and .env."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch("magelab.auth.find_dotenv", return_value=""):
            with pytest.raises(RuntimeError) as exc_info:
                resolve_api_key()
        msg = str(exc_info.value)
        assert "--api-key" in msg
        assert "ANTHROPIC_API_KEY" in msg
        assert ".env" in msg

    def test_env_var_takes_priority_over_find_dotenv(self, tmp_path, monkeypatch):
        """ANTHROPIC_API_KEY in environment takes priority over find_dotenv discovery."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
        # Even if find_dotenv would find something, the env var wins
        dotenv_path = str(tmp_path / ".env")
        Path(dotenv_path).write_text("ANTHROPIC_API_KEY=sk-ant-from-dotenv\n")
        with patch("magelab.auth.find_dotenv", return_value=dotenv_path):
            result = resolve_api_key()
        assert result.api_key == "sk-ant-from-env"

    def test_explicit_env_file_with_existing_env_var(self, tmp_path, monkeypatch):
        """Explicit env file path with ANTHROPIC_API_KEY already in env still returns a key.

        Note: load_dotenv does not override existing env vars by default, so the
        pre-existing env var value wins. The function still succeeds because it
        reads os.environ after load_dotenv and finds a value either way.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY=sk-ant-from-file\n")
        result = resolve_api_key(env_file=env_file)
        # load_dotenv doesn't override existing env vars, so the env value persists
        assert result.api_key == "sk-ant-from-env"


# =============================================================================
# stage_credentials
# =============================================================================


class TestStageCredentials:
    def test_sub_mode_copies_file(self, tmp_path, logger):
        """SUB mode copies credentials file to output_dir/.sessions/_credentials/."""
        # Create source credentials file
        src = tmp_path / "source" / ".credentials.json"
        src.parent.mkdir()
        src.write_text('{"token": "secret"}')

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        auth = ResolvedAuth(mode=AuthMode.SUB, credentials_path=src)
        stage_credentials(auth, output_dir, logger)

        dest = output_dir / ".sessions" / "_credentials" / ".credentials.json"
        assert dest.exists()
        assert dest.read_text() == '{"token": "secret"}'

    def test_sub_mode_permissions(self, tmp_path, logger):
        """Staged credentials file has 0600 permissions."""
        src = tmp_path / ".credentials.json"
        src.write_text('{"token": "secret"}')

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        auth = ResolvedAuth(mode=AuthMode.SUB, credentials_path=src)
        stage_credentials(auth, output_dir, logger)

        dest = output_dir / ".sessions" / "_credentials" / ".credentials.json"
        file_stat = os.stat(dest)
        mode = stat.S_IMODE(file_stat.st_mode)
        assert mode == 0o600

    def test_api_key_mode_noop(self, tmp_path, logger):
        """API_KEY mode is a no-op — nothing is created in output_dir."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        auth = ResolvedAuth(mode=AuthMode.API_KEY, api_key="sk-ant-test")
        stage_credentials(auth, output_dir, logger)

        sessions_dir = output_dir / ".sessions"
        assert not sessions_dir.exists()

    def test_creates_destination_directory(self, tmp_path, logger):
        """Destination directory is created if it doesn't exist yet."""
        src = tmp_path / ".credentials.json"
        src.write_text('{"token": "secret"}')

        # output_dir itself exists, but .sessions/_credentials/ does not
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        auth = ResolvedAuth(mode=AuthMode.SUB, credentials_path=src)
        stage_credentials(auth, output_dir, logger)

        dest_dir = output_dir / ".sessions" / "_credentials"
        assert dest_dir.is_dir()

    def test_with_logger(self, tmp_path, logger):
        """stage_credentials accepts an optional logger without error."""
        src = tmp_path / ".credentials.json"
        src.write_text('{"token": "secret"}')

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        auth = ResolvedAuth(mode=AuthMode.SUB, credentials_path=src)
        # Should not raise
        stage_credentials(auth, output_dir, logger=logger)

        dest = output_dir / ".sessions" / "_credentials" / ".credentials.json"
        assert dest.exists()

    def test_overwrites_existing(self, tmp_path, logger):
        """Staging credentials a second time overwrites the existing file."""
        src = tmp_path / ".credentials.json"
        src.write_text('{"token": "original"}')

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        auth = ResolvedAuth(mode=AuthMode.SUB, credentials_path=src)
        stage_credentials(auth, output_dir, logger)

        # Update source and re-stage
        src.write_text('{"token": "updated"}')
        stage_credentials(auth, output_dir, logger)

        dest = output_dir / ".sessions" / "_credentials" / ".credentials.json"
        assert dest.read_text() == '{"token": "updated"}'
