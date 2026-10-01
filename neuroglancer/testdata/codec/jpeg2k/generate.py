"""Generates the JPEG 2000 decoder fixtures and their expected decodes.

Each `<name>.j2k` has a `<name>.json` with the decoded shape and data type
and `<name>.raw`, the samples imagecodecs (OpenJPEG) decodes, little-endian,
interleaved by component.

`idr0096_level8_c0.j2k` is a tile from IDR dataset idr0096 (Tratwal et al.,
MarrowQuant; CC BY 4.0): level 8, channel 0 of
`4000_d11_m5_LT_2 (20x_01).ome.tiff`. It was written by JJ2000 with 33
quality layers.

Usage: uv run python testdata/codec/jpeg2k/generate.py
"""

import json
from pathlib import Path

import imagecodecs
import numpy as np

HERE = Path(__file__).parent
rng = np.random.default_rng(0)
y, x = np.mgrid[0:48, 0:64]
gradient = (x * 3 + y * 2).astype(np.uint16)

images = {
    "uint8_gray": (gradient % 256).astype(np.uint8),
    "uint8_rgb": np.stack([(gradient + 40 * i) % 256 for i in range(3)], axis=-1).astype(np.uint8),
    "uint16_gray": (gradient * 300 + rng.integers(0, 50, gradient.shape)).astype(np.uint16),
    "int16_gray": (gradient.astype(np.int32) * 40 - 3000).astype(np.int16),
}
for name, image in images.items():
    (HERE / f"{name}.j2k").write_bytes(
        imagecodecs.jpeg2k_encode(image, level=0, codecformat="J2K")
    )

for path in sorted(HERE.glob("*.j2k")):
    decoded = imagecodecs.jpeg2k_decode(path.read_bytes())
    name = path.stem
    if name in images:
        assert np.array_equal(decoded, images[name]), name
    path.with_suffix(".raw").write_bytes(decoded.astype(decoded.dtype.newbyteorder("<")).tobytes())
    path.with_suffix(".json").write_text(
        json.dumps({"shape": list(decoded.shape), "dtype": decoded.dtype.name}) + "\n"
    )
    print(name, decoded.shape, decoded.dtype, path.stat().st_size)
