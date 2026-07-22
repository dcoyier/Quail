from quail import __version__


def test_version() -> None:
    assert __version__.startswith("0.11")
