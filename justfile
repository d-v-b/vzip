# vzip. Run `just` to list recipes; `just web::test`, `just impls::rust::build`
# and so on reach the packages' own justfiles.

set shell := ["bash", "-euo", "pipefail", "-c"]

mod web
mod impls

idr_tiff := "https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/4000_d11_m5_LT_2%20(20x_01).ome.tiff"

[private]
default:
    @just --list --list-submodules

# Test the Python package (src/vzip; it virtualizes TIFF, ND2 and CZI with the Rust core, built first)
test *args: ir-build
    uv run pytest -q tests {{args}}

# Build the IR core (rust/vzip-ir: the parsers, the read planner, the projections and the
# mirror) into the project's environment as the Python module vzip_ir, which vzip.virtualize uses
ir-build:
    uv run --with maturin env VIRTUAL_ENV="$PWD/.venv" maturin develop --release -m rust/vzip-ir/Cargo.toml

# Build the IR core for the browser (wasm32, without the Python bindings) and run its ND2,
# TIFF and CZI parsers from Node on fixtures
ir-wasm:
    rust/vzip-ir/wasm/build.sh
    node rust/vzip-ir/wasm/nd2.mjs web/test/fixtures/nd2/nd2_tz_uint16.nd2
    node rust/vzip-ir/wasm/run.mjs web/test/fixtures/tiff/jpeg_gray.tif web/test/fixtures/czi/czi_zstd.czi

# Test the IR core (Rust) and the IR prototype (Python, after ir-build)
ir-test *args: ir-build
    cargo test --release --manifest-path rust/vzip-ir/Cargo.toml
    uv run pytest -q tests/ir {{args}}

# Run every offline test: the Python reference, the browser code and the independent implementations
test-all: test web::test impls::test

# Type-check the browser code
check: web::typecheck

# Run the SPEC.md conformance suite against the three independent implementations
conformance out="conformance/results/latest": impls::build
    uv run python conformance/run.py --out {{out}} \
        --impl rust=impls/rust/vzip --impl typescript=impls/typescript/vzip --impl python=impls/python/vzip

# Regenerate the synthetic files in web/test/fixtures/<format> (all but NDPI, which needs the network)
fixtures: fixtures-tiff fixtures-nd2 fixtures-dicom fixtures-nifti fixtures-ims fixtures-n5 fixtures-zarr2 fixtures-ome-zarr fixtures-safe fixtures-czi

# Regenerate the fixtures twice and check that both runs give the same bytes, and the committed ones
fixtures-check:
    #!/usr/bin/env bash
    set -euo pipefail
    sums() { (cd web/test/fixtures && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256); }
    just fixtures > /dev/null
    first="$(sums)"
    sleep 2  # past a one-second timestamp
    just fixtures > /dev/null
    if [ "$first" != "$(sums)" ]; then
        diff <(echo "$first") <(sums) >&2 || true
        echo "fixtures-check: two runs of the generators differ" >&2
        exit 1
    fi
    git diff --exit-code --stat -- web/test/fixtures
    if [ -n "$(git status --porcelain -- web/test/fixtures)" ]; then
        git status --short -- web/test/fixtures >&2
        echo "fixtures-check: the generators' output differs from the committed fixtures" >&2
        exit 1
    fi

# Regenerate the synthetic TIFFs (including JPEG-tiled, SVS-like ones)
fixtures-tiff:
    uv run python web/test/tiff/write_fixtures.py
    uv run python web/test/tiff/write_edge_fixtures.py
    uv run python web/test/tiff/write_jpeg_fixtures.py

# Regenerate the NDPI files, cut from OpenSlide's CMU-1.ndpi (network, once)
fixtures-ndpi:
    uv run python web/test/ndpi/write_fixtures.py

# Regenerate the synthetic ND2 files and their expected pixels
fixtures-nd2:
    uv run python web/test/nd2/write_fixtures.py

# Regenerate the synthetic DICOM files
fixtures-dicom:
    uv run python web/test/dicom/write_fixtures.py

# Regenerate the synthetic NIfTI files
fixtures-nifti:
    uv run python web/test/nifti/write_fixtures.py

# Regenerate the synthetic Imaris IMS files
fixtures-ims:
    uv run python web/test/ims/write_fixtures.py

# Regenerate the synthetic N5 stores (one directory each)
fixtures-n5:
    uv run python web/test/n5/write_fixtures.py

# Regenerate the synthetic Zarr v2 stores (one directory each)
fixtures-zarr2:
    uv run python web/test/zarr2/write_fixtures.py

# Regenerate the synthetic OME-Zarr 0.4 stores (one directory each)
fixtures-ome-zarr:
    uv run python web/test/ome-zarr/write_fixtures.py

# Regenerate the synthetic Sentinel-2 SAFE products (directories and .SAFE.zip files)
fixtures-safe:
    uv run python web/test/safe/write_fixtures.py

# Regenerate the synthetic CZI files
fixtures-czi:
    uv run python web/test/czi/write_fixtures.py

# Check the browser virtualizer's pixels for every format
verify: verify-tiff verify-ndpi verify-nd2 verify-dicom verify-nifti verify-ims verify-n5 verify-zarr2 verify-ome-zarr

# Check the browser virtualizer's TIFF pixels against tifffile
verify-tiff:
    uv run python web/test/tiff/verify.py

# Check the browser virtualizer's NDPI pixels against tifffile
verify-ndpi:
    uv run python web/test/ndpi/verify.py

# Check the browser virtualizer's ND2 pixels against the synthetic files' pixels
verify-nd2:
    uv run python web/test/nd2/verify.py

# Check the browser virtualizer's DICOM pixels against pydicom
verify-dicom:
    uv run python web/test/dicom/verify.py

# Check the browser virtualizer's NIfTI pixels against nibabel
verify-nifti:
    uv run python web/test/nifti/verify.py

# Check the browser virtualizer's IMS pixels against h5py
verify-ims:
    uv run python web/test/ims/verify.py

# Check the browser virtualizer's N5 arrays against an independent block reader and zarr-n5
verify-n5:
    uv run python web/test/n5/verify.py

# Check the browser virtualizer's Zarr v2 arrays against zarr-python's Zarr v2 reader
verify-zarr2:
    uv run python web/test/zarr2/verify.py

# Check the browser virtualizer's OME-Zarr 0.5 output against ome-zarr-models and zarr-python's Zarr v2 reader
verify-ome-zarr:
    uv run python web/test/ome-zarr/verify.py

# Store one virtual dataset as kerchunk JSON, kerchunk Parquet, Icechunk and vzip, and read each over HTTP (see comparison/README.md)
compare-formats *args:
    uv run python comparison/compare.py {{args}}

# VIRTUALIZE.md: compare implementations on the corpus (network; e.g. `just compare --quick`)
compare *args:
    uv run python conformance/virtualize/compare.py conformance/results/virtualize {{args}}

# VIRTUALIZE.md: compare implementations on corrupted copies of the synthetic files
compare-mutants count="10" seed="0" *args:
    rm -rf conformance/results/mutants
    python3 conformance/virtualize/mutate.py conformance/results/mutants {{count}} {{seed}}
    uv run python conformance/virtualize/compare.py conformance/results/mutants-out \
        --fixtures conformance/results/mutants {{args}}

# On main after a merge: tag each convention's version as virtualize-<profile>-v<N> at HEAD
# (conventions/README.md §1); version 0, before the release, has no tags
tag-conventions:
    #!/usr/bin/env bash
    set -euo pipefail
    branch="$(git rev-parse --abbrev-ref HEAD)"
    if [ "$branch" != "main" ]; then
        echo "tag-conventions: HEAD is on '$branch'; the convention tags are made on main" >&2
        exit 1
    fi
    tags="$(uv run python -c 'from vzip.virtualize.common import PROFILES
    for p, (_, v, _) in PROFILES.items(): print(f"virtualize-{p}-v{v}")')"
    for tag in $tags; do
        if [ "${tag##*-v}" = "0" ]; then
            echo "$tag: version 0 is not tagged (its URLs name main), kept untagged"
            continue
        fi
        if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
            echo "$tag exists (at $(git rev-list -n 1 "$tag")), kept"
        else
            git tag -a "$tag" -m "VIRTUALIZE.md convention ${tag#virtualize-}"
            echo "$tag created at $(git rev-parse HEAD)"
        fi
    done
    echo "push them with: git push origin --tags"

# Virtualize an image file (TIFF, NDPI, ND2, DICOM, NIfTI or IMS; a URL or a path), or an N5,
# Zarr v2 or OME-Zarr 0.4 store (a URL ending in "/"), into a vzip archive
virtualize src out:
    uv run python -m vzip.virtualize {{quote(src)}} {{quote(out)}}

# Regenerate the (untracked) example archives in experiments/out (network)
archives: archives-nd2 archives-idr

# Virtualize the public ND2 files of the corpus into experiments/out/nd2
archives-nd2:
    grep -v '^#' conformance/virtualize/corpus_nd2.txt | grep . | while IFS='|' read -r url name _; do \
        uv run python -m vzip.virtualize "$url" "experiments/out/nd2/$name.vzip"; \
    done

# Virtualize the IDR idr0096 OME-TIFF (pinned, and unpinned for browsers) into experiments/out
archives-idr:
    mkdir -p experiments/out/ng_idr
    uv run python experiments/tiff_to_vzip.py {{quote(idr_tiff)}} experiments/out/idr0096_4000_d11_m5_LT_2.vzip
    uv run python experiments/tiff_to_vzip.py --no-pins {{quote(idr_tiff)}} \
        experiments/out/ng_idr/idr0096_4000_d11_m5_LT_2_unpinned.vzip

# Build and publish a demo to https://d-v-b.github.io/vzip-demo/ (see web/pages.sh)
deploy-demo *args: web::build
    web/pages.sh {{args}}
