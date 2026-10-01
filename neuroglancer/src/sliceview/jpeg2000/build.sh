#!/bin/bash -xve

# Builds `jpeg2000_decoder.wasm` with a local Rust toolchain:
#   rustup target add wasm32-unknown-unknown
#   ./build.sh
# `wasm-opt` (binaryen) is used if it is installed.

cd "$(dirname "$0")"

CRATE=hayro-jpeg2000-0.4.0
SHA256=bab0c77d09c0d9b144d429d0bf0fcd4c63c01d5f6c33f9b8ed501283e0f1ef76
rm -rf vendor
mkdir -p vendor
curl -sSfL "https://static.crates.io/crates/hayro-jpeg2000/${CRATE}.crate" \
  -o "vendor/${CRATE}.crate"
echo "${SHA256}  vendor/${CRATE}.crate" | shasum -a 256 -c
tar -xzf "vendor/${CRATE}.crate" -C vendor
mv "vendor/${CRATE}" vendor/hayro-jpeg2000
patch -d vendor/hayro-jpeg2000 -p1 < hayro-jpeg2000-layers.patch

RUSTFLAGS="-C target-feature=+simd128" \
  cargo build --target wasm32-unknown-unknown --release
cp target/wasm32-unknown-unknown/release/jpeg2000_wasm.wasm jpeg2000_decoder.wasm
if command -v wasm-opt > /dev/null; then
  wasm-opt -O3 --enable-simd --enable-bulk-memory jpeg2000_decoder.wasm -o jpeg2000_decoder.wasm
fi
rm -rf target vendor
