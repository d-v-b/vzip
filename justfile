# refkv. Run `just` to list recipes; `just python::build`, `just js::test`,
# `just impls::rust::build` and so on reach the packages' own justfiles.

set shell := ["bash", "-euo", "pipefail", "-c"]

mod python
mod js
mod impls "conformance/impls"

idr_tiff := "https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/4000_d11_m5_LT_2%20(20x_01).ome.tiff"

[private]
default:
    @just --list --list-submodules

# Test the Python package (python/src/refkv; it virtualizes TIFF, NDPI, ND2, CZI, DICOM, NIfTI, IMS and SAFE, and Zarr v2, N5 and OME-Zarr stores, with the Rust core, built first)
test *args: ir-build
    uv run pytest -q python/tests {{args}}

# Build refkv (python/: the package, with its bindings to the IR core rust/refkv-ir, the
# parsers, the read planner, the projections and the mirror, as the module refkv._core) into
# the workspace's environment: `uv sync` (and `uv run`) build it with maturin, in release, and
# again whenever the bindings, the core's Rust sources or its source models change
# (python/pyproject.toml's cache-keys)
ir-build:
    uv sync -q --locked

# Build the IR core for the browser (wasm32, without the Python bindings) and run its ND2,
# TIFF, CZI, DICOM, NIfTI and IMS parsers, and its Zarr v2 and N5 store formats, from Node on fixtures
ir-wasm:
    rust/refkv-ir/wasm/build.sh
    node rust/refkv-ir/wasm/nd2.mjs fixtures/nd2/nd2_tz_uint16.nd2
    node rust/refkv-ir/wasm/run.mjs fixtures/tiff/jpeg_gray.tif fixtures/czi/czi_zstd.czi fixtures/dicom/dicom_jpeg_mono_bot.dcm fixtures/nifti/nifti_dims_2d.nii fixtures/ims/ims_latest_dense_links.ims
    node rust/refkv-ir/wasm/store.mjs fixtures/zarr2/zarr2_hierarchy > /dev/null
    node rust/refkv-ir/wasm/store.mjs --format n5 fixtures/n5/n5_cosem > /dev/null

# Test the IR core (Rust) and its Python host (after ir-build)
ir-test *args: ir-rust-test ir-build
    uv run pytest -q python/tests/ir {{args}}

# Test the IR core's Rust crate, and build its Python bindings' crate (python/Cargo.toml) alone
ir-rust-test:
    cargo test --release --manifest-path rust/refkv-ir/Cargo.toml
    just python::cargo-test

# Test the vzip archive writer (rust/refkv-vzip), the phkv store's reader and writers
# (rust/refkv-phkv), and the `vzip convert` CLI (rust/refkv-cli), and build the CLI
# (rust/refkv-cli/target/release/vzip)
cli-test:
    cargo test --release --manifest-path rust/refkv-vzip/Cargo.toml
    cargo test --release --manifest-path rust/refkv-phkv/Cargo.toml
    cargo test --release --manifest-path rust/refkv-cli/Cargo.toml
    cargo build --release --manifest-path rust/refkv-cli/Cargo.toml

# Convert the fixture corpus with `python -m refkv.virtualize` and with the Rust CLI, from local
# paths and over HTTP (a server on 127.0.0.1), with and without --checksums, and check that both
# give the same archives, byte for byte (rust/refkv-cli/tests/identical.py; e.g. `--only nd2`)
cli-identical *args: ir-build
    cargo build --release --manifest-path rust/refkv-cli/Cargo.toml
    uv run python rust/refkv-cli/tests/identical.py {{args}}

# Run every offline test: the Python reference, the browser code, the independent implementations,
# and the Rust CLI against the Python one
test-all: test js::test impls::test cli-test cli-identical

# Type-check the browser code
check: js::typecheck

# Check the package without its extras, and with only the zarr extra (`just python::check`):
# pass the Python of an environment where only `pip install python/` (or
# `pip install 'python/[zarr]'`, with mode=zarr) was run (default: a fresh one of each); a
# relative path is the current directory's
base-install-check python="" mode="":
    just python::check {{quote(if python == "" { "" } else { absolute_path(python) })}} {{quote(mode)}}

# Run the example of docs/virtual-datasets.md on NOAA's data (network: 1 to 1.5 minutes,
# 50 MB), in a temporary directory
example-oisst:
    cd "$(mktemp -d)" && uv run --project "{{justfile_directory()}}" python "{{justfile_directory()}}/docs/examples/oisst.py"

# Check that every section reference ("§n") is a link to a heading that exists, and test the check
sections:
    uv run pytest -q spec/tools
    uv run python spec/tools/sections.py check

# Run the spec/bindings/vzip.md conformance suite against the three independent implementations
conformance out="conformance/results/latest": impls::build
    uv run python conformance/vzip/run.py --out {{out}} \
        --impl rust=conformance/impls/rust/vzip --impl typescript=conformance/impls/typescript/vzip --impl python=conformance/impls/python/vzip

# Regenerate the synthetic files in fixtures/<format> (all but NDPI, which needs the network)
fixtures: fixtures-tiff fixtures-nd2 fixtures-dicom fixtures-nifti fixtures-ims fixtures-n5 fixtures-zarr2 fixtures-ome-zarr fixtures-safe fixtures-czi

# Regenerate the fixtures twice and check that both runs give the same bytes, and the committed ones
fixtures-check:
    #!/usr/bin/env bash
    set -euo pipefail
    sums() { (cd fixtures && find . -path ./generators -prune -o -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256); }
    just fixtures > /dev/null
    first="$(sums)"
    sleep 2  # past a one-second timestamp
    just fixtures > /dev/null
    if [ "$first" != "$(sums)" ]; then
        diff <(echo "$first") <(sums) >&2 || true
        echo "fixtures-check: two runs of the generators differ" >&2
        exit 1
    fi
    git diff --exit-code --stat -- fixtures
    if [ -n "$(git status --porcelain -- fixtures)" ]; then
        git status --short -- fixtures >&2
        echo "fixtures-check: the generators' output differs from the committed fixtures" >&2
        exit 1
    fi

# Regenerate the synthetic TIFFs (including JPEG-tiled, SVS-like ones)
fixtures-tiff:
    uv run python fixtures/generators/tiff/write_fixtures.py
    uv run python fixtures/generators/tiff/write_edge_fixtures.py
    uv run python fixtures/generators/tiff/write_jpeg_fixtures.py

# Regenerate the NDPI files, cut from OpenSlide's CMU-1.ndpi (network, once)
fixtures-ndpi:
    uv run python fixtures/generators/ndpi/write_fixtures.py

# Regenerate the synthetic ND2 files and their expected pixels
fixtures-nd2:
    uv run python fixtures/generators/nd2/write_fixtures.py

# Regenerate the synthetic DICOM files
fixtures-dicom:
    uv run python fixtures/generators/dicom/write_fixtures.py

# Regenerate the synthetic NIfTI files
fixtures-nifti:
    uv run python fixtures/generators/nifti/write_fixtures.py

# Regenerate the synthetic Imaris IMS files
fixtures-ims:
    uv run python fixtures/generators/ims/write_fixtures.py

# Regenerate the synthetic N5 stores (one directory each)
fixtures-n5:
    uv run python fixtures/generators/n5/write_fixtures.py

# Regenerate the synthetic Zarr v2 stores (one directory each)
fixtures-zarr2:
    uv run python fixtures/generators/zarr2/write_fixtures.py

# Regenerate the synthetic OME-Zarr 0.4 stores (one directory each)
fixtures-ome-zarr:
    uv run python fixtures/generators/ome-zarr/write_fixtures.py

# Regenerate the synthetic Sentinel-2 SAFE products (directories and .SAFE.zip files)
fixtures-safe:
    uv run python fixtures/generators/safe/write_fixtures.py

# Regenerate the synthetic CZI files
fixtures-czi:
    uv run python fixtures/generators/czi/write_fixtures.py

# Check the browser virtualizer's pixels for every format
verify: verify-tiff verify-ndpi verify-nd2 verify-dicom verify-nifti verify-ims verify-n5 verify-zarr2 verify-ome-zarr

# Check the browser virtualizer's TIFF pixels against tifffile
verify-tiff:
    uv run python js/test/tiff/verify.py

# Check the browser virtualizer's NDPI pixels against tifffile
verify-ndpi:
    uv run python js/test/ndpi/verify.py

# Check the browser virtualizer's ND2 pixels against the synthetic files' pixels
verify-nd2:
    uv run python js/test/nd2/verify.py

# Check the browser virtualizer's DICOM pixels against pydicom
verify-dicom:
    uv run python js/test/dicom/verify.py

# Check the browser virtualizer's NIfTI pixels against nibabel
verify-nifti:
    uv run python js/test/nifti/verify.py

# Check the browser virtualizer's IMS pixels against h5py
verify-ims:
    uv run python js/test/ims/verify.py

# Check the browser virtualizer's N5 arrays against an independent block reader and zarr-n5
verify-n5:
    uv run python js/test/n5/verify.py

# Check the browser virtualizer's Zarr v2 arrays against zarr-python's Zarr v2 reader
verify-zarr2:
    uv run python js/test/zarr2/verify.py

# Check the browser virtualizer's OME-Zarr 0.5 output against ome-zarr-models and zarr-python's Zarr v2 reader
verify-ome-zarr:
    uv run python js/test/ome-zarr/verify.py

# Store one virtual dataset as kerchunk JSON, kerchunk Parquet, Icechunk and vzip, and read each over HTTP (see design/comparison/README.md)
compare-formats *args:
    uv run python design/comparison/compare.py {{args}}

# spec/virtualize.md: compare implementations on the corpus (network; e.g. `just compare --quick`)
compare *args:
    uv run python conformance/virtualize/compare.py conformance/results/virtualize {{args}}

# spec/virtualize.md: compare implementations on corrupted copies of the synthetic files
compare-mutants count="10" seed="0" *args:
    rm -rf conformance/results/mutants
    python3 conformance/virtualize/mutate.py conformance/results/mutants {{count}} {{seed}}
    uv run python conformance/virtualize/compare.py conformance/results/mutants-out \
        --fixtures conformance/results/mutants {{args}}

# On main after a merge: tag each convention's version as virtualize-<profile>-v<N> at HEAD
# (spec/conventions.md §1
# <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#1-conventions>); version 0, before
# the release, has no tags
tag-conventions:
    #!/usr/bin/env bash
    set -euo pipefail
    branch="$(git rev-parse --abbrev-ref HEAD)"
    if [ "$branch" != "main" ]; then
        echo "tag-conventions: HEAD is on '$branch'; the convention tags are made on main" >&2
        exit 1
    fi
    tags="$(uv run python -c 'from refkv.virtualize.common import PROFILES
    for p, (_, v, _) in PROFILES.items(): print(f"virtualize-{p}-v{v}")')"
    for tag in $tags; do
        if [ "${tag##*-v}" = "0" ]; then
            echo "$tag: version 0 is not tagged (its URLs name main), kept untagged"
            continue
        fi
        if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
            echo "$tag exists (at $(git rev-list -n 1 "$tag")), kept"
        else
            git tag -a "$tag" -m "spec/virtualize.md convention ${tag#virtualize-}"
            echo "$tag created at $(git rev-parse HEAD)"
        fi
    done
    echo "push them with: git push origin --tags"

# Virtualize an image file (TIFF, NDPI, ND2, DICOM, NIfTI or IMS; a URL or a path), or an N5,
# Zarr v2 or OME-Zarr 0.4 store (a URL ending in "/"), into a vzip archive
virtualize src out:
    uv run python -m refkv.virtualize {{quote(src)}} {{quote(out)}}

# Regenerate the (untracked) example archives in design/experiments/out (network)
archives: archives-nd2 archives-idr

# Virtualize the public ND2 files of the corpus into design/experiments/out/nd2
archives-nd2:
    grep -v '^#' conformance/virtualize/corpus_nd2.txt | grep . | while IFS='|' read -r url name _; do \
        uv run python -m refkv.virtualize "$url" "design/experiments/out/nd2/$name.vzip"; \
    done

# Virtualize the IDR idr0096 OME-TIFF (pinned, and unpinned for browsers) into design/experiments/out
archives-idr:
    mkdir -p design/experiments/out/ng_idr
    uv run python design/experiments/tiff_to_vzip.py {{quote(idr_tiff)}} design/experiments/out/idr0096_4000_d11_m5_LT_2.vzip
    uv run python design/experiments/tiff_to_vzip.py --no-pins {{quote(idr_tiff)}} \
        design/experiments/out/ng_idr/idr0096_4000_d11_m5_LT_2_unpinned.vzip

# Build and publish a demo to https://d-v-b.github.io/vzip-demo/ (see demo/pages.sh)
deploy-demo *args: js::build
    demo/pages.sh {{args}}
