# Build and verify tracked native source

Build snapshots compile the repository's maintained source directly. A failed
build leaves the published binary untouched; symlink installation refuses foreign
destinations. Upstream notices, tests, tools and assets retain their original repository-root paths.

Autoreconf and yacc generate their own support/parser outputs inside the isolated
build tree. Source exports preserve symlinks and include source manifests without
copying ignored build artifacts. The VTE oracle reads retained ring contents
correctly after saved-line purges; the local harness needs no retired sibling
project. State assertions wait for observable asynchronous VTE results and quiet
PTY pumps refresh delayed parser output instead of assuming a fixed frame delay.
This setup does not add a launcher, transport or process monitor.

## Verification

```sh
uv run pytest -q tests/test_source_build.py tests/test_native_resize.py tests/test_test_infrastructure.py tests/test_vte_history_readout.py tests/test_verification_inventory.py
```

Tests use disposable sockets, processes, HOME directories and emulator/GUI displays.
