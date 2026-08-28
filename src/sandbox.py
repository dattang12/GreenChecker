"""Isolated, disposable execution environment for one agent run.

Contract:
  * Every Sandbox starts from a pristine clone. No state survives teardown.
  * Every command is bounded by a timeout, enforced *inside* the container
    (coreutils `timeout`) with a host-side backstop. A hang surfaces as a
    normal ExecResult with timed_out=True -- never as a blocked process.
  * The container has no network by default. The agent edits code and runs
    tests; it has no business making outbound calls.
"""

from __future__ import annotations

import atexit
import shlex
import subprocess
import uuid
from dataclasses import dataclass

DEFAULT_IMAGE = "repo-triage-sandbox:latest"
REPO_DIR = "/work/repo"

# Host-side backstop fires this many seconds after the in-container timeout.
# If we ever hit it, the in-container `timeout` failed and something is wrong.
_BACKSTOP_GRACE = 15


class SandboxError(RuntimeError):
    """Raised when the sandbox itself is broken (vs. a command failing)."""


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class _HostResult:
    returncode: int
    stdout: str
    stderr: str


def _decode(raw: bytes) -> str:
    # Docker's exec stream can hand back CRLF on Windows hosts; normalise so
    # downstream parsing and diffing see plain LF.
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n")


def _run_host(argv: list[str], *, timeout: float, stdin: str | None = None) -> _HostResult:
    """Run a host command in *binary* mode.

    This must not use text=True. On Windows, Python's text-mode stdin performs
    newline translation, silently turning every '\\n' we send into '\\r\\n'
    inside the Linux container -- which corrupts unified diffs handed to
    `git apply` and writes CRLF source files. Encoding explicitly keeps the
    bytes we send identical to the bytes the model produced.
    """
    proc = subprocess.run(
        argv,
        input=stdin.encode("utf-8") if stdin is not None else None,
        capture_output=True,
        timeout=timeout,
    )
    return _HostResult(proc.returncode, _decode(proc.stdout), _decode(proc.stderr))


class Sandbox:
    """One container, one agent run."""

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        *,
        ref: str | None = None,
        network: bool = False,
        memory: str = "2g",
        cpus: str = "2",
    ) -> None:
        self.image = image
        self.ref = ref
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self.container_id: str | None = None
        self.name = f"triage-{uuid.uuid4().hex[:12]}"

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "Sandbox":
        argv = [
            "docker", "run", "-d",
            "--name", self.name,
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", "512",
        ]
        if not self.network:
            argv += ["--network", "none"]
        argv += [self.image, "sleep", "infinity"]

        proc = _run_host(argv, timeout=120)
        if proc.returncode != 0:
            raise SandboxError(f"could not start container: {proc.stderr.strip()}")
        self.container_id = proc.stdout.strip()
        atexit.register(self.stop)

        if self.ref:
            res = self.exec(f"git checkout --quiet {shlex.quote(self.ref)}", timeout=60)
            if not res.ok:
                self.stop()
                raise SandboxError(f"could not check out ref {self.ref!r}: {res.stderr.strip()}")
        return self

    def stop(self) -> None:
        if not self.container_id:
            return
        cid, self.container_id = self.container_id, None
        try:
            _run_host(["docker", "rm", "-f", cid], timeout=60)
        except subprocess.TimeoutExpired:  # pragma: no cover - teardown best effort
            pass

    def __enter__(self) -> "Sandbox":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- execution ---------------------------------------------------------

    def exec(
        self,
        command: str,
        *,
        timeout: float = 60,
        stdin: str | None = None,
        workdir: str = REPO_DIR,
    ) -> ExecResult:
        """Run `command` under `sh -c` inside the container.

        The in-container `timeout -k 5 N` is the real enforcement; exit code
        124 means it fired. The host timeout is only a backstop.
        """
        if not self.container_id:
            raise SandboxError("sandbox is not running -- call start() first")

        wrapped = f"timeout -k 5 {int(timeout)} sh -c {shlex.quote(command)}"
        argv = ["docker", "exec", "-w", workdir]
        if stdin is not None:
            argv.append("-i")
        argv += [self.container_id, "sh", "-c", wrapped]

        try:
            proc = _run_host(argv, timeout=timeout + _BACKSTOP_GRACE, stdin=stdin)
        except subprocess.TimeoutExpired:
            return ExecResult(124, "", f"command exceeded {timeout}s (host backstop)", True)

        timed_out = proc.returncode == 124
        stderr = proc.stderr
        if timed_out:
            stderr = f"command exceeded {timeout}s and was killed\n{stderr}"
        return ExecResult(proc.returncode, proc.stdout, stderr, timed_out)

    # -- convenience -------------------------------------------------------

    def pristine_sha(self) -> str:
        res = self.exec("cat /work/PRISTINE_SHA", timeout=15)
        return res.stdout.strip()

    def reset(self) -> None:
        """Return the working tree to the pristine clone state."""
        sha = self.ref or self.pristine_sha()
        res = self.exec(
            f"git checkout --quiet --force {shlex.quote(sha)} && git clean -xfdq",
            timeout=120,
        )
        if not res.ok:
            raise SandboxError(f"reset failed: {res.stderr.strip()}")


def image_exists(image: str = DEFAULT_IMAGE) -> bool:
    proc = _run_host(["docker", "image", "inspect", image], timeout=60)
    return proc.returncode == 0


def build_image(
    context_dir: str,
    *,
    image: str = DEFAULT_IMAGE,
    repo_url: str,
    repo_ref: str,
) -> None:
    argv = [
        "docker", "build",
        "-t", image,
        "--build-arg", f"REPO_URL={repo_url}",
        "--build-arg", f"REPO_REF={repo_ref}",
        context_dir,
    ]
    proc = subprocess.run(argv, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SandboxError("docker build failed -- see output above")
