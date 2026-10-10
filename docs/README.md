# vzip documentation

[COMPARISON.md](../design/COMPARISON.md) compares vzip with kerchunk JSON, kerchunk Parquet and Icechunk on the same virtual dataset, read over HTTP.

## Demos

Live demos are at https://d-v-b.github.io/vzip-demo/. For example, [image files to zarr](https://d-v-b.github.io/vzip-demo/image-to-zarr/) virtualizes a remote TIFF (including OME-TIFF and Aperio SVS), Hamamatsu NDPI or Nikon ND2 file into a vzip archive in the browser and opens it in Neuroglancer. The demo's code is in [js/demo/](../js/demo/), and [js/README.md](../js/README.md) says how to build and serve it.

## Virtualizing image files and stores

vzip can present TIFF, NDPI, ND2, CZI, DICOM, NIfTI and Imaris files, and N5, Zarr v2 and OME-Zarr 0.4 stores, as OME-Zarr, and Sentinel-2 SAFE products as GeoZarr, without copying their pixels. TIFF, ND2 and CZI are read by a Rust core ([rust/vzip-ir](../rust/vzip-ir/)) that runs natively from Python and as WebAssembly in the browser. [virtualize/](virtualize/README.md) has a page per format: who it is for, what you get, and how to run it from Python or in the browser.
