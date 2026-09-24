"""The frozen evaluation fixtures, shipped as package data.

Two PRDs and nothing else. They are installed with the wheel rather than kept
under `tests/` because the evaluation set is an operator-facing surface: the
runner hands these paths to `ortus plan`, so they have to exist in an
installed ortus, not only in a checkout.
"""
