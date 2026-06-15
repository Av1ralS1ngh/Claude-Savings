# Building the Windows (`win_amd64`) yscompress engine wheel

This is the **release gate** for native Windows support: the CLI + gateway are pure-python and ship
today, but the ML compression engine (`yscompress`, the Rust `headroom._core` extension) currently has
**macOS/arm64 wheels only**. Until a `win_amd64` wheel is published, the gateway on Windows runs its
built-in pure-Python fallback (logs/JSON/HTML/tool-output compression, no ML/Kompress). The Windows
extension/installer already auto-install the engine wheel the moment it appears at the dist URL
(`install.ps1` HEAD-probes `…/yscompress-<pytag>-<pytag>-win_amd64.whl`).

## Good news: it's a CI build, not a port

Verified against `/Users/avii/Documents/YSCompress`:

1. **The Rust source is OS-portable.** The binding crate (`crates/headroom-py`) uses no unix-only APIs
   (`std::os::unix`, `nix`, unix `libc`). The only platform-specific code is `glibc_compat.c`, and its
   `build.rs` **explicitly self-skips off Linux-glibc** (`if target_os != "linux" || target_env != "gnu"
   { return; }`) — macOS, Windows, and musl never compile it.
2. **ONNX Runtime is handled automatically.** `headroom-core` depends on the `ort` crate with the
   **`ort-download-binaries-*`** feature, which auto-downloads the correct per-platform ONNX Runtime —
   the Cargo.toml notes it "emits DirectML link libs on Windows" — plus an `ort-load-dynamic` fallback.
   So there is **no manual Windows ONNX artifact to wrangle**; the crate fetches it at build time.
3. **maturin builds Windows wheels natively.** Build backend is already `maturin` (pyo3 bindings),
   `crate-type = ["cdylib"]`. maturin's first-class target is exactly this.

## The recipe (run in the YSCompress repo, on a Windows runner)

Per Python version the engine targets (cp312, cp313):

```powershell
# one-time toolchain
winget install Python.Python.3.12
rustup target add x86_64-pc-windows-msvc      # MSVC toolchain (default on Windows)
pip install maturin

# build the wheel for THIS python
cd C:\path\to\YSCompress
maturin build --release --interpreter python3.12   # -> target\wheels\yscompress-0.22.4-cp312-cp312-win_amd64.whl
maturin build --release --interpreter python3.13
```

GitHub Actions matrix (the durable form — add to YSCompress `.github/workflows/`):

```yaml
jobs:
  windows-wheel:
    runs-on: windows-latest
    strategy:
      matrix: { python: ["3.12", "3.13"] }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "${{ matrix.python }}" }
      - uses: dtolnay/rust-toolchain@stable
        with: { targets: x86_64-pc-windows-msvc }
      - run: pip install maturin
      - run: maturin build --release --interpreter python --out dist
      - uses: actions/upload-artifact@v4
        with: { name: win-${{ matrix.python }}, path: dist/*.whl }
```

## Validate the wheel before publishing

On the Windows runner (or a Windows VM), confirm the engine actually imports + compresses — not just
that it builds:

```powershell
python -m venv v; v\Scripts\pip install dist\yscompress-0.22.4-cp312-cp312-win_amd64.whl
v\Scripts\python -c "import headroom; r = headroom.compress([{'role':'tool','content':'2026 INFO step ok\n'*400}]); print('engine OK', r.tokens_before, '->', r.tokens_after)"
```

A non-trivial `tokens_before -> tokens_after` drop (and `headroom._core` importing without error) means
the Rust core + ONNX runtime loaded. If `import headroom` raises, the gateway will silently fall back to
pure-python (so a broken wheel degrades gracefully, but you want the ML path working).

## Publish + the Claude-Coder side auto-picks-it-up

Upload both wheels to the R2 dist bucket alongside the existing mac wheels:

```
yscompress-0.22.4-cp312-cp312-win_amd64.whl
yscompress-0.22.4-cp313-cp313-win_amd64.whl
```

No Claude-Coder code change is needed to consume them — `scripts/install.ps1` already constructs that
exact filename from the venv's Python tag and installs it when the HEAD probe succeeds. Once the wheels
are live and smoke-tested on Windows, flip the extension's "Windows supported" claim (version bump +
marketplace notes).

## Why this is the gate, not the fallback

Per the product decision: the first **Windows release** should have full ML compression parity, so it
waits on these wheels. Everything else (install.ps1, the extension Windows setup/uninstall UX, the
cross-OS CLI lifecycle) is already built and inert-on-mac/linux — it just isn't surfaced as "supported"
until the engine wheel lands.
