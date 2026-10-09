#!/usr/bin/env bash
# Builds the core's wasm32 module for the browser code: cargo's `wasm` profile (LTO,
# one codegen unit, opt-level 2, panic=abort), then binaryen's `wasm-opt -O3` when it
# is installed (without it the module is about 19% larger, and up to 8% slower). Writes
# rust/vzip-ir/target/web/vzip_ir.wasm. `build.sh test` builds the test module instead:
# the same, with the test-only hooks (feature `test-hooks`), in its own target
# directory, into rust/vzip-ir/target/web-test/vzip_ir.wasm; the shipped module never
# has them.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
if [ "${1:-}" = test ]; then
  cargo build --profile wasm --target wasm32-unknown-unknown --manifest-path "$here/Cargo.toml" \
    --features test-hooks --target-dir "$here/target/test-hooks"
  raw="$here/target/test-hooks/wasm32-unknown-unknown/wasm/vzip_ir.wasm"
  out="$here/target/web-test/vzip_ir.wasm"
else
  cargo build --profile wasm --target wasm32-unknown-unknown --manifest-path "$here/Cargo.toml"
  raw="$here/target/wasm32-unknown-unknown/wasm/vzip_ir.wasm"
  out="$here/target/web/vzip_ir.wasm"
fi
mkdir -p "$(dirname "$out")"
if command -v wasm-opt > /dev/null; then
  wasm-opt -O3 --enable-bulk-memory --enable-nontrapping-float-to-int --enable-sign-ext --enable-mutable-globals \
    "$raw" -o "$out"
else
  echo "wasm/build.sh: wasm-opt (binaryen) not found: the module is not optimized further" >&2
  cp "$raw" "$out"
fi
