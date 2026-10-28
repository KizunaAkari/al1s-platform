import os
import socket

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Unix socket runtime is Linux only")


def test_socket_is_private_and_second_process_cannot_take_it_over(tmp_path):
    from al1s.quant.socket_runtime import secure_socket

    tmp_path.chmod(0o700)
    with secure_socket(tmp_path) as sock:
        assert sock.family == socket.AF_UNIX
        assert (tmp_path / "control.sock").stat().st_mode & 0o777 == 0o600
        with pytest.raises(BlockingIOError):
            with secure_socket(tmp_path):
                pass
    assert not (tmp_path / "control.sock").exists()


def test_socket_never_replaces_a_regular_file(tmp_path):
    from al1s.quant.socket_runtime import secure_socket

    tmp_path.chmod(0o700)
    target = tmp_path / "control.sock"
    target.write_text("preserve")
    with pytest.raises(ValueError, match="unsafe_socket"):
        with secure_socket(tmp_path):
            pass
    assert target.read_text() == "preserve"
