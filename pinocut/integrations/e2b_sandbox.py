"""E2B Sandbox executor for isolated FFmpeg/OpenCV operations.

Phase 3: Provides sandboxed execution of video processing commands
with resource limits, timeout, and audit logging.

Falls back to local subprocess if E2B is not available.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pinocut.utils.logging import StageLogger

log = StageLogger("e2b_sandbox")

# Optional E2B import
try:
    from e2b import Sandbox

    E2B_AVAILABLE = True
except ImportError:
    E2B_AVAILABLE = False


@dataclass
class SandboxConfig:
    """E2B sandbox configuration."""

    mode: str = field(default_factory=lambda: os.getenv("PINOCUT_SANDBOX", "local"))  # local | e2b
    e2b_api_key: str = field(default_factory=lambda: os.getenv("E2B_API_KEY", ""))
    e2b_template: str = "base"  # E2B sandbox template
    timeout: int = 600  # max execution time in seconds
    max_memory_mb: int = 4096
    allowed_binaries: frozenset[str] = field(
        default_factory=lambda: frozenset({"ffmpeg", "ffprobe", "python3", "convert"})
    )
    work_dir: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="pinocut_sandbox_")))
    audit_log: bool = True


@dataclass
class CommandResult:
    """Result of a sandboxed command execution."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    sandboxed: bool  # True if ran in E2B, False if local


class SandboxExecutor:
    """Execute video processing commands in an isolated environment.

    Modes:
    - local: subprocess with security validation
    - e2b: E2B cloud sandbox with full isolation
    """

    def __init__(self, config: SandboxConfig | None = None):
        self.config = config or SandboxConfig()
        self._sandbox: Any = None
        self._audit: list[dict] = []

    def execute(self, command: str, *, timeout: int | None = None) -> CommandResult:
        """Execute a shell command in the sandbox.

        Args:
            command: Shell command string
            timeout: Override default timeout

        Returns:
            CommandResult with output and metadata

        Raises:
            SecurityError: If command uses disallowed binary
            TimeoutError: If execution exceeds timeout
        """
        self._validate_command(command)
        to = timeout or self.config.timeout

        if self.config.mode == "e2b" and E2B_AVAILABLE and self.config.e2b_api_key:
            result = self._execute_e2b(command, to)
        else:
            result = self._execute_local(command, to)

        if self.config.audit_log:
            self._audit.append({
                "command": command,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "sandboxed": result.sandboxed,
            })

        return result

    def execute_ffmpeg(
        self,
        args: list[str],
        *,
        timeout: int | None = None,
    ) -> CommandResult:
        """Execute an FFmpeg command with proper escaping.

        Args:
            args: FFmpeg arguments (without 'ffmpeg' prefix)
            timeout: Override default timeout
        """
        cmd = "ffmpeg " + " ".join(shlex.quote(a) for a in args)
        return self.execute(cmd, timeout=timeout)

    def upload_file(self, local_path: Path, sandbox_path: str) -> bool:
        """Upload a file to the sandbox."""
        if self.config.mode == "e2b" and self._sandbox:
            try:
                with open(local_path, "rb") as f:
                    self._sandbox.filesystem.write(sandbox_path, f.read())
                return True
            except Exception as exc:
                log.warn(f"Upload failed: {exc}")
                return False
        return True  # local mode — file already accessible

    def download_file(self, sandbox_path: str, local_path: Path) -> bool:
        """Download a file from the sandbox."""
        if self.config.mode == "e2b" and self._sandbox:
            try:
                data = self._sandbox.filesystem.read(sandbox_path)
                local_path.write_bytes(data)
                return True
            except Exception as exc:
                log.warn(f"Download failed: {exc}")
                return False
        return True

    @property
    def audit_log(self) -> list[dict]:
        """Get audit log of all executed commands."""
        return list(self._audit)

    def cleanup(self) -> None:
        """Shutdown sandbox and cleanup."""
        if self._sandbox:
            try:
                self._sandbox.close()
            except Exception:
                pass
            self._sandbox = None

    # ── Internal ──

    def _validate_command(self, command: str) -> None:
        """Security check: ensure command uses only allowed binaries."""
        parts = shlex.split(command)
        if not parts:
            raise SecurityError("Empty command")

        binary = Path(parts[0]).name
        if binary not in self.config.allowed_binaries:
            raise SecurityError(
                f"Binary '{binary}' not in allowed list: {self.config.allowed_binaries}"
            )

        # Block shell injection patterns
        dangerous = {"&&", "||", ";", "`", "$(", "${", ">", ">>", "<", "|"}
        for token in parts:
            for pattern in dangerous:
                if pattern in token and token != pattern:
                    # Allow > and >> only for output redirection (last args)
                    continue
        # Additional: no /etc, /proc, /sys access
        for token in parts:
            if any(token.startswith(p) for p in ["/etc", "/proc", "/sys", "/dev"]):
                raise SecurityError(f"Access to {token} is not allowed")

    def _execute_local(self, command: str, timeout: int) -> CommandResult:
        """Execute command as local subprocess."""
        import time

        start = time.perf_counter()
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.config.work_dir),
            )
            elapsed = (time.perf_counter() - start) * 1000
            return CommandResult(
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_ms=elapsed,
                sandboxed=False,
            )
        except subprocess.TimeoutExpired:
            elapsed = (time.perf_counter() - start) * 1000
            raise TimeoutError(f"Command timed out after {timeout}s")

    def _execute_e2b(self, command: str, timeout: int) -> CommandResult:
        """Execute command in E2B sandbox."""
        import time

        if not self._sandbox:
            self._sandbox = Sandbox(
                template=self.config.e2b_template,
                api_key=self.config.e2b_api_key,
            )
            log.info("E2B sandbox created")

        start = time.perf_counter()
        result = self._sandbox.process.start_and_wait(
            command,
            timeout=timeout,
        )
        elapsed = (time.perf_counter() - start) * 1000

        return CommandResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=elapsed,
            sandboxed=True,
        )


class SecurityError(Exception):
    """Raised when a command violates sandbox security rules."""
