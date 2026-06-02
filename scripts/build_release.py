#!/usr/bin/env python3
"""Build the distributable `decompute-ai` wheel with PyArmor-obfuscated source.

The CLI/SDK is a thin client; the valuable IP (the YSCompress engine) is a compiled Rust
core that stays server-side. This obfuscates whatever client logic ships so the published
wheel contains no readable source, while the `decompute` entry point keeps working.

Flow:  pyarmor gen -> assemble (obfuscated pkg + pyarmor runtime + metadata) -> pip wheel.

Usage:  python scripts/build_release.py            # build into ./dist
        python scripts/build_release.py --verify   # also install into a temp venv and smoke-test

Notes:
- Reuses the [project] metadata from packages/python-sdk/pyproject.toml (single source).
- The wheel is platform-specific (it bundles pyarmor's native runtime .so) — build once per
  target platform/python.
- A PyArmor LICENSE is required for production builds; the trial works for validating the
  pipeline. The runtime package name (pyarmor_runtime_XXXXXX) is detected dynamically, so a
  licensed build needs no script changes.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "packages" / "python-sdk"
SRC_PKG = SDK / "src" / "decompute_ai"
BUILD = ROOT / "build" / "release"
DIST = ROOT / "dist"


def run(cmd: list[str]) -> None:
    print("  $", " ".join(cmd))
    subprocess.run(cmd, check=True)


def build() -> Path:
    if shutil.which("pyarmor") is None:
        sys.exit("error: pyarmor not found — `pip install pyarmor` (a license is required for production).")

    print("→ cleaning build dir")
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)

    print("→ obfuscating decompute_ai with PyArmor")
    run(["pyarmor", "gen", "-O", str(BUILD), "-r", str(SRC_PKG)])

    runtime = next((p.name for p in BUILD.iterdir() if p.name.startswith("pyarmor_runtime")), None)
    if not runtime:
        sys.exit("error: pyarmor runtime package not found in build output")
    print(f"→ runtime package: {runtime}")

    shutil.copy(SDK / "README.md", BUILD / "README.md")

    # Carry the FULL source metadata (license/authors/keywords/classifiers/urls/scripts) into
    # the published wheel — only swap the src-layout package discovery for the obfuscated flat
    # layout (obfuscated decompute_ai + the pyarmor runtime package).
    src_pyproject = (SDK / "pyproject.toml").read_text()
    marker = '[tool.setuptools.packages.find]\nwhere = ["src"]'
    if marker not in src_pyproject:
        sys.exit("error: expected packages.find block not found in source pyproject — update build_release.py")
    obf_packages = (
        f'[tool.setuptools]\npackages = ["decompute_ai", "{runtime}"]\n\n'
        f'[tool.setuptools.package-data]\n"{runtime}" = ["*.so", "*.pyd", "*.dll", "*.dylib"]\n'
    )
    (BUILD / "pyproject.toml").write_text(src_pyproject.replace(marker, obf_packages), encoding="utf-8")

    # The pyarmor runtime ships a native .so, so this is NOT a pure-Python wheel. Force a
    # platform tag (has_ext_modules=True) so pip won't install a macOS build on Linux/Windows.
    (BUILD / "setup.py").write_text(
        "from setuptools import setup\n"
        "from setuptools.dist import Distribution\n"
        "class _Binary(Distribution):\n"
        "    def has_ext_modules(self):\n"
        "        return True\n"
        "setup(distclass=_Binary)\n",
        encoding="utf-8",
    )

    print("→ building wheel")
    DIST.mkdir(exist_ok=True)
    run([sys.executable, "-m", "pip", "wheel", str(BUILD), "--no-deps", "-w", str(DIST)])

    wheels = sorted(DIST.glob("decompute_ai-*.whl"), key=lambda p: p.stat().st_mtime)
    wheel = wheels[-1]
    print(f"\n✓ obfuscated wheel: {wheel}")
    return wheel


def verify(wheel: Path) -> None:
    """Install the wheel into a throwaway venv and confirm the obfuscated CLI runs."""
    import tempfile

    print("\n→ verifying in a fresh venv")
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        run([sys.executable, "-m", "venv", str(venv)])
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        pip = bindir / ("pip.exe" if os.name == "nt" else "pip")
        dec = bindir / ("decompute.exe" if os.name == "nt" else "decompute")
        run([str(pip), "install", "--quiet", str(wheel)])
        # The obfuscated cli.py must import + expose `app` via the pyarmor runtime.
        run([str(dec), "--help"])
        print("✓ obfuscated `decompute --help` runs from the installed wheel")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the obfuscated decompute-ai release wheel")
    ap.add_argument("--verify", action="store_true", help="install into a temp venv and smoke-test")
    args = ap.parse_args()
    wheel = build()
    if args.verify:
        verify(wheel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
