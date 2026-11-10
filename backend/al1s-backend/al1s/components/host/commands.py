import os
import subprocess
import sys
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

    def stream_gzip(self, arguments: list[str], destination: Path, timeout: int = 600) -> None:
        compressor_args = [
            sys.executable,
            "-c",
            "import gzip,shutil,sys;"
            "f=gzip.GzipFile(filename='',fileobj=sys.stdout.buffer,mode='wb',"
            "compresslevel=1,mtime=0);"
            "shutil.copyfileobj(sys.stdin.buffer,f,1048576);f.close()",
        ]
        created = False
        started = time.monotonic()
        try:
            with destination.open("xb") as output:
                created = True
                os.chmod(destination, 0o600)
                producer = subprocess.Popen(
                    arguments, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                )
                compressor = None
                try:
                    assert producer.stdout is not None
                    try:
                        compressor = subprocess.Popen(
                            compressor_args,
                            stdin=producer.stdout,
                            stdout=output,
                            stderr=subprocess.DEVNULL,
                        )
                    finally:
                        producer.stdout.close()
                    compressor.wait(timeout=max(0, timeout - (time.monotonic() - started)))
                    if compressor.returncode:
                        raise subprocess.CalledProcessError(compressor.returncode, compressor_args)
                    producer.wait(timeout=max(0, timeout - (time.monotonic() - started)))
                    if producer.returncode:
                        raise subprocess.CalledProcessError(producer.returncode, arguments)
                finally:
                    for process in (producer, compressor):
                        if process is not None and process.poll() is None:
                            process.kill()
                    for process in (producer, compressor):
                        if process is not None:
                            process.wait()
        except Exception:
            if created:
                destination.unlink(missing_ok=True)
            raise
