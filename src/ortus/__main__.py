"""Entry point for `python -m ortus` and the `ortus` console script."""

from ortus.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
