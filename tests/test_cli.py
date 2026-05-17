"""Smoke test that the CLI module imports and exposes the typer app."""

import importlib


def test_cli_imports() -> None:
    cli = importlib.import_module("ortus.cli")
    assert cli.app is not None


def test_main_module_imports() -> None:
    main = importlib.import_module("ortus.__main__")
    assert callable(main.main)


def test_package_version() -> None:
    ortus = importlib.import_module("ortus")
    assert ortus.__version__
