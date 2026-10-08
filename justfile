# vzip. Run `just` to list recipes; `just web::test`, `just impls::rust::build`
# and so on reach the packages' own justfiles.

set shell := ["bash", "-euo", "pipefail", "-c"]

mod web
mod impls

idr_tiff := "https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/4000_d11_m5_LT_2%20(20x_01).ome.tiff"

[private]
default:
    @just --list --list-submodules

# Test the Python reference (src/vzip)
test *args:
    uv run pytest -q tests {{args}}

# Run every offline test: the Python reference, the browser code and the independent implementations
test-all: test web::test impls::test

# Type-check the browser code
check: web::typecheck

# Run the SPEC.md conformance suite against the three independent implementations
conformance out="conformance/results/latest": impls::build
    uv run python conformance/run.py --out {{out}} \
        --impl rust=impls/rust/vzip --impl typescript=impls/typescript/vzip --impl python=impls/python/vzip

# Regenerate the synthetic files in web/test/fixtures/<format> (all but NDPI, which needs the network)
fixtures: fixtures-tiff fixtures-nd2 fixtures-dicom fixtures-nifti fixtures-ims fixtures-n5 fixtures-zarr2 fixtures-ome-zarr

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

# Virtualize an image file (TIFF, NDPI, ND2, DICOM, NIfTI or IMS; a URL or a path), or an N5,
# Zarr v2 or OME-Zarr 0.4 store (a URL ending in "/"), into a vzip archive
virtualize src out:
    uv run python -m vzip.virtualize {{quote(src)}} {{quote(out)}}

# Regenerate the (untracked) example archives in experiments/out (network)
archives: archives-nd2 archives-idr

# Virtualize the public ND2 files of the corpus into experiments/out/nd2
archives-nd2:
    grep -v '^#' conformance/virtualize/corpus_nd2.txt | grep . | while IFS='|' read -r url name; do \
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
