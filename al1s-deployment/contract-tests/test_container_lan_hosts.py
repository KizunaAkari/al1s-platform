import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "lan_hosts", Path(__file__).parents[1] / "tools/container_lan_hosts.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
rewrite = module.rewrite_hosts


def test_refresh_and_removal_preserve_other_entries():
    original = "127.0.0.1 localhost\n172.17.0.2 platform\n# operator comment\n"
    first = rewrite(original, "board.local", "192.168.5.21")
    assert rewrite(first, "board.local", "192.168.5.21") == first
    changed = rewrite(first, "board.local", "192.168.5.33")
    assert "192.168.5.21" not in changed
    assert rewrite(changed, "board.local", "") == original


@pytest.mark.parametrize("address", ["198.18.0.28", "127.0.0.1", "8.8.8.8", "::1", "bad"])
def test_invalid_addresses_rejected(address):
    with pytest.raises(ValueError):
        rewrite("", "board.local", address)


def test_unmanaged_entry_not_overwritten():
    with pytest.raises(ValueError, match="unmanaged_hostname_conflict"):
        rewrite("192.168.1.4 board.local\n", "board.local", "192.168.5.21")


def test_hostname_injection_rejected():
    with pytest.raises(ValueError):
        rewrite("", "board.local\nevil", "192.168.5.21")


def test_other_managed_host_preserved():
    first = rewrite("", "first.local", "10.0.0.1")
    both = rewrite(first, "second.local", "10.0.0.2")
    assert rewrite(both, "second.local", "") == first
