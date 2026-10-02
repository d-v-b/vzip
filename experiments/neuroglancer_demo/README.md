# Neuroglancer demos

Both demos use the vzip-enabled Neuroglancer fork,
https://github.com/d-v-b/neuroglancer (branch `vzip`). The scripts look for
it in `$NEUROGLANCER`, by default a clone next to this repository. Build it
first:

```bash
git clone --branch vzip https://github.com/d-v-b/neuroglancer.git ../neuroglancer
(cd ../neuroglancer && npm install && npm run build)
```

`screenshot.mjs` serves the built viewer together with a data directory, opens
a state from `states/` in headless Chromium, and saves a screenshot. It also
logs every data request the viewer makes.

## HDF5 image (local)

`make_hdf5_vzip.py` writes a zlib-chunked netCDF4/HDF5 image and a vzip that
exposes it as a Zarr v3 array. The vzip holds only metadata and 16 references
into `mandelbrot.nc`.

```bash
uv run python experiments/neuroglancer_demo/make_hdf5_vzip.py experiments/out/ng_demo
node experiments/neuroglancer_demo/screenshot.mjs experiments/out/ng_demo experiments/neuroglancer_demo/states/mandelbrot.json experiments/out/ng_demo/neuroglancer_vzip.png
```

`states/mandelbrot_scheme.json` opens the same data with the bare
`vzip://<archive-url>` source.

## OME-Zarr view of an IDR OME-TIFF (remote)

`experiments/tiff_to_vzip.py` reads the IFDs of a 487 MB pyramidal OME-TIFF
from IDR over HTTP: study idr0096 (Tratwal et al.,
[doi:10.17867/10000170](https://doi.org/10.17867/10000170), CC BY 4.0). It writes a 622 KB vzip holding an
OME-NGFF 0.5 multiscales group: 9 levels, 5,037 references to JPEG 2000 tiles.
Neuroglancer then reads the tiles straight from `ftp.ebi.ac.uk` and decodes
them with the `imagecodecs_jpeg2k` codec.

```bash
uv run python experiments/tiff_to_vzip.py --no-pins \
  'https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/4000_d11_m5_LT_2%20(20x_01).ome.tiff' \
  experiments/out/ng_idr/idr0096_4000_d11_m5_LT_2_unpinned.vzip
node experiments/neuroglancer_demo/screenshot.mjs experiments/out/ng_idr experiments/neuroglancer_demo/states/idr_ome_zarr.json experiments/out/ng_idr/neuroglancer_idr_overview.png 25000
node experiments/neuroglancer_demo/screenshot.mjs experiments/out/ng_idr experiments/neuroglancer_demo/states/idr_ome_zarr_detail.json experiments/out/ng_idr/neuroglancer_idr_detail.png 25000
```

Notes:

- **Unpinned archive.** The archive is written without pins (`--no-pins`). EBI
  allows cross-origin range requests, but its CORS preflight rejects `If-Match`
  and `If-Unmodified-Since`. It also does not expose `ETag`, `Last-Modified` or
  `Content-Range`. A browser therefore cannot check pins, and pinned reads fail
  closed. The pinned archive (`experiments/out/idr0096_4000_d11_m5_LT_2.vzip`,
  from `just archives-idr`) still works outside the browser.
- **Hidden `Content-Range`.** For the same reason, the driver accepts a
  cross-origin `206` whose `Content-Range` is hidden, as long as the body has
  exactly the requested length. This deviates from spec §6.2.
- **One layer per channel.** Each chunk holds one channel (`[1, 1024, 1024]`).
  Neuroglancer needs a channel dimension to sit within a single chunk, so the
  state shows the three channels as red, green and blue layers with additive
  blending.
