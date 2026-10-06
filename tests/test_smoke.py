from triagewright import __version__
from triagewright.cli import main


def test_version() -> None:
    assert __version__


def test_cli_help_exits_zero() -> None:
    assert main([]) == 0
