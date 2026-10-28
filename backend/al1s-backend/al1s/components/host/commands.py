import subprocess
import tempfile
import time
from pathlib import Path


class CommandRunner:
    """Fixed argument vectors only; large backups stream directly to a private file."""

    def run(self, arguments: list[str], timeout: int = 30) -> str:
        # A spooled file bounds memory even if an image prints arbitrary output.
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(arguments, stdout=output, stderr=subprocess.DEVNULL)
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if output.seek(0, 2) > 2 * 1024**2:
                        raise ValueError("component_command_output_limit")
                    if time.monotonic() - started > timeout:
                        raise subprocess.TimeoutExpired(arguments, timeout)
                    time.sleep(0.05)
                if process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, arguments)
                if output.seek(0, 2) > 2 * 1024**2:
                    raise ValueError("component_command_output_limit")
                output.seek(0)
                return output.read().decode("utf-8").strip()
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()

    def stream(self, arguments: list[str], destination: Path, timeout: int = 600) -> None:
        with destination.open("xb") as output:
            subprocess.run(
                arguments, check=True, stdout=output, stderr=subprocess.DEVNULL, timeout=timeout
            )
