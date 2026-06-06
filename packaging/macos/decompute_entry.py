"""PyInstaller entry shim for the `decompute` CLI.

PyInstaller bundles a script, not a `module:attr` console entry-point, so this thin
wrapper invokes the Typer app exactly as the `decompute` console script does
(see packages/python-sdk/pyproject.toml -> [project.scripts]).
"""
from decompute_ai.cli import app

if __name__ == "__main__":
    app()
