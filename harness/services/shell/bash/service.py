import os
import sys
import time
import dataclasses
import subprocess
import threading
import textwrap
import pathlib
import logging
import requests
import mcp.server.mcpserver

# Set up logging for service.

class LogExceptionHandler(logging.StreamHandler):
    class ExceptionFormatter(logging.Formatter):
        def formatException(self, ei):
            return "ERROR " + super().formatException(ei).replace("\n", " ")

    def __init__(self, stream=None, fmt=None):
        super().__init__(stream=stream)
        self.setFormatter(LogExceptionHandler.ExceptionFormatter(fmt))

logging.basicConfig(
    handlers=[LogExceptionHandler(stream=sys.stdout, fmt="%(asctime)s %(levelname)s: %(message)s")],
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    force=True,
)

log = logging.getLogger()

# Initialize configuration.

service_name: str = os.environ.get("SERVICE_NAME", "shell-bash")
service_role: str = os.environ.get("SERVICE_ROLE", "shell")
service_port: int = int(os.environ.get("PORT", "8000"))
endpoint_url: str = os.environ.get("ENDPOINT_URL", "")
registry_url: str = os.environ.get("REGISTRY_URL", "")
direction_md: str = os.environ.get("DIRECTION_MD") or pathlib.Path(__file__).with_name("direction.md").read_text(encoding="utf-8")

# Create the mcp server.

mcp = mcp.server.mcpserver.MCPServer(service_name, log_level="WARNING")

class ShellJob:
    """
    ...
    """

    @dataclasses.dataclass
    class OutputLine:
        source: str
        text: str

    @dataclasses.dataclass
    class CommandLatest:
        output: list["ShellJob.OutputLine"]
        running: bool

    def __init__(self, cwd: str, env: dict[str, str]):
        self.process: subprocess.Popen[bytes] | None = None
        self._read_o: threading.Thread | None = None
        self._read_e: threading.Thread | None = None
        self._o_lock: threading.Lock = threading.Lock()
        self._output: list[ShellJob.OutputLine] = []
        self._closed = False
        self.env = {**os.environ.copy(), **env}
        self.cwd = cwd

    def _drain(self, stream, stream_name: str) -> None:
        try:
            for line in iter(stream.readline, b""):
                event = self.OutputLine(
                    source=stream_name,
                    text=line.decode("utf-8", errors="replace"),
                )

                with self._o_lock:
                    self._output.append(event)
        finally:
            stream.close()

    def run(self, command: str) -> int:
        if self._closed:
            raise RuntimeError("shell is closed")

        if self.process is not None:
            raise RuntimeError("shell has already been started")

        self.process = subprocess.Popen(
            ["/bin/bash", "-c", command],
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        if not self.process.stdout:
            raise RuntimeError("shell stdout not bound")

        if not self.process.stderr:
            raise RuntimeError("shell stderr not bound")

        self._read_o = threading.Thread(
            target=self._drain,
            args=(self.process.stdout, "stdout"),
            daemon=True,
        )
        self._read_o.start()

        self._read_e = threading.Thread(
            target=self._drain,
            args=(self.process.stderr, "stderr"),
            daemon=True,
        )
        self._read_e.start()

        return self.process.pid

    def pop_output(self) -> list[OutputLine]:
        with self._o_lock:
            copy = list(self._output)
            self._output.clear()
            return copy

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def close(self) -> None:
        if self._closed:
            return

        self._closed = True

        if self.process is None:
            return

        if self.process.poll() is None:
            self.process.terminate()

            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

        if self._read_o:
            self._read_o.join(timeout=1)

        if self._read_e:
            self._read_e.join(timeout=1)

        self.process = None

current: dict[str, ShellJob] = {}

@mcp.tool()
def start_command(command: str, cwd: str, env: dict[str, str]) -> str:
    """
    Start a shell command in the background and return an id for tracking it.

    The command runs under `/bin/bash -c` with its own stdout/stderr pipes
    drained on background threads, so this call returns immediately without
    waiting for the command to finish. Use the returned id with
    get_command_latest to poll for output and status, and with drop_command
    to terminate it and free its resources when done.

    Args:
        command: The shell command line to execute.
        cwd: Working directory to run the command in.
        env: Extra environment variables to set for the command, merged on
            top of this service's own environment (e.g. to override PATH
            entries or add secrets); pass {} to inherit as-is.

    Returns:
        A command id (e.g. "shell_job_00042") to pass to get_command_latest
        and drop_command.
    """

    job = ShellJob(cwd=cwd, env=env)
    key = f"shell_job_{job.run(command=command):05d}"

    current[key] = job

    return key

@mcp.tool()
def get_command_latest(command_id: str) -> ShellJob.CommandLatest:
    """
    Fetch and clear the output produced since the last call, plus whether
    the command is still running.

    Each call drains and returns only the output lines accumulated since
    the previous call (or since start_command, on the first call) — output
    already returned is not repeated on subsequent calls. Call this
    repeatedly to stream a long-running command's output; once `running`
    comes back False, all remaining output has been delivered and the
    command has exited.

    Args:
        command_id: The id returned by start_command.

    Returns:
        Output describing a list of content produced by the command with
        the stream source for each line (e.g., "stdout" or "stderr") and
        the status of the command, whether or not running.

    Raises:
        RuntimeError: If command_id is unknown (never started, already
        dropped, or invalid).
    """

    job = current.get(command_id)

    if job is None:
        raise RuntimeError("missing or invalid command")

    return ShellJob.CommandLatest(
        output=job.pop_output(),
        running=job.is_running(),
    )

@mcp.tool()
def drop_command(command_id: str):
    """
    Stop a command and release its resources.

    If the command is still running, it is terminated (killed after a 5s
    grace period if it doesn't exit on its own). Any output not yet
    fetched via get_command_latest is discarded. Always call this once
    you're done with a command, whether or not it has finished, to avoid
    leaking processes and buffered output.

    Args:
        command_id: The id returned by start_command.

    Raises:
        RuntimeError: If command_id is unknown (already dropped or
        invalid).
    """

    job = current.get(command_id)

    if job is None:
        raise RuntimeError("missing or invalid command")
    
    job.close()
    current.pop(command_id)

@mcp.tool()
def ping() -> str:
    """
    Health check. Returns "ok" when the service is up; "down" when down.
    """

    return "ok"

def add_to_registry(name: str, role: str, direction: str, endpoint: str, url: str, port: int):
    deadline = time.monotonic() + 30

    while time.monotonic() < deadline:
        try:
            requests.get(url=f"http://localhost:{port}", timeout=2)
            break
        except requests.exceptions.RequestException:
            time.sleep(1)
    else:
        log.warning("~ gave up waiting, continuing with registering")

    time.sleep(1)

    try:
        requests.post(
            url=url,
            json={
                "name": name,
                "role": role,
                "direction": direction,
                "endpoint": endpoint,
            },
            timeout=15,
        )
    except IOError:
        log.exception("! failed to add to registry")


if __name__ == "__main__":
    print(textwrap.dedent(r"""
    .                                           /           _  _                  
    .           _/_                      o     /    /      // //    /           / 
    .  __,  , , /  __ _ _ _   __,  _ _  ,  _, /(   /_  _  // //    /  __,  (   /_ 
    . (_/(_(_/_(__(_)/ / / /_(_/(_/ / /_(_(__//_)_/ /_(/_(/_(/_---/_)(_/(_/_)_/ /_
    .                                                                             
    . 
    . 
    . powered by automanic 🍣
    . """))

    if not registry_url:
        log.error("! registry url not configured, exiting")
        sys.exit(1)

    if not endpoint_url:
        log.error("! endpoint url not configured, exiting")
        sys.exit(1)

    log.info(f". managing durable shells as {service_name}")

    log.info(". available tools:")
    for tool in mcp._tool_manager.list_tools():
        log.info(f". {tool.name}")
    tooling = threading.Thread(
        target=lambda: add_to_registry(
            name=service_name,
            role=service_role,
            direction=direction_md,
            endpoint=endpoint_url,
            url=registry_url,
            port=service_port,
        ),
        name="service_tooling",
        daemon=True
    )
    tooling.start()
    try:
        mcp.run(
            transport="streamable-http",
            host="0.0.0.0",
            port=service_port,
            stateless_http=True,
            json_response=True,
        )
    finally:
        log.info(". shutting down 👋")
