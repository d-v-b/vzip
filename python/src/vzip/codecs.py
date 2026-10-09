"""Zarr v3 codecs needed to read virtualized image files and stores, which
zarr-python does not provide.

`imagecodecs_jpeg2k`: each chunk is one JPEG 2000 codestream (as in TIFF
compression 33003/33005/34712 with planar-separate tiles).
`imagecodecs_jpeg`: each chunk is one complete JPEG stream (as virtualized
JPEG-in-TIFF tiles are, spec/virtualize/tiff/profile.md §3.6).
`imagecodecs_jpegxr`: each chunk is one JPEG XR file (ITU-T T.832, with its
container), as CZI JpgXr subblocks are (spec/virtualize/czi.md §3.1).
`zlib`: a zlib stream (RFC 1950), as Neuroglancer names it (conventions §3, §9, §10).
`n5_default`: an N5 default-mode block, header included (zarr-extensions
`codecs/n5_default`; spec/virtualize/n5/profile.md).

The image codecs decode only; their encoders exist so zarr can create the
array metadata.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Self

import imagecodecs
import numpy as np
import zarr
from zarr.abc.codec import ArrayBytesCodec, BytesBytesCodec, Codec
from zarr.codecs import BytesCodec, TransposeCodec
from zarr.core.array_spec import ArraySpec
from zarr.core.buffer import Buffer, NDBuffer
from zarr.registry import get_pipeline_class, register_codec


@dataclass(frozen=True)
class Jpeg2kCodec(ArrayBytesCodec):
    is_fixed_size = False

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls()

    def to_dict(self) -> dict:
        return {"name": "imagecodecs_jpeg2k"}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        img = imagecodecs.jpeg2k_decode(chunk_bytes.to_bytes())
        img = np.asarray(img, dtype=chunk_spec.dtype.to_native_dtype()).reshape(chunk_spec.shape)
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(img)

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        arr = np.squeeze(chunk_array.as_numpy_array())
        return chunk_spec.prototype.buffer.from_bytes(imagecodecs.jpeg2k_encode(arr, level=0))

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("imagecodecs_jpeg2k", Jpeg2kCodec)


@dataclass(frozen=True)
class JpegCodec(ArrayBytesCodec):
    is_fixed_size = False

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls()

    def to_dict(self) -> dict:
        return {"name": "imagecodecs_jpeg"}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        img = imagecodecs.jpeg8_decode(chunk_bytes.to_bytes())
        img = np.asarray(img, dtype=chunk_spec.dtype.to_native_dtype()).reshape(chunk_spec.shape)
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(img)

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        arr = np.squeeze(chunk_array.as_numpy_array())
        return chunk_spec.prototype.buffer.from_bytes(imagecodecs.jpeg8_encode(arr))

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("imagecodecs_jpeg", JpegCodec)


@dataclass(frozen=True)
class JpegXrCodec(ArrayBytesCodec):
    """`imagecodecs_jpegxr`: one JPEG XR file per chunk, decoding to [y, x] or
    [y, x, c] (color samples in R, G, B (A) order), before `transpose`."""

    is_fixed_size = False

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls()

    def to_dict(self) -> dict:
        return {"name": "imagecodecs_jpegxr"}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        img = imagecodecs.jpegxr_decode(chunk_bytes.to_bytes())
        img = np.asarray(img, dtype=chunk_spec.dtype.to_native_dtype()).reshape(chunk_spec.shape)
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(img)

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        arr = np.squeeze(chunk_array.as_numpy_array())
        return chunk_spec.prototype.buffer.from_bytes(imagecodecs.jpegxr_encode(arr, level=1.0))

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("imagecodecs_jpegxr", JpegXrCodec)


@dataclass(frozen=True)
class ZlibCodec(BytesBytesCodec):
    """`zlib`: each chunk is one zlib stream."""

    is_fixed_size = False
    level: int = 1

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(**data.get("configuration", {}))

    def to_dict(self) -> dict:
        return {"name": "zlib", "configuration": {"level": self.level}}

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> Buffer:
        return chunk_spec.prototype.buffer.from_bytes(zlib.decompress(chunk_bytes.to_bytes()))

    async def _encode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> Buffer:
        return chunk_spec.prototype.buffer.from_bytes(zlib.compress(chunk_bytes.to_bytes(), self.level))

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("zlib", ZlibCodec)


class N5BlockError(ValueError):
    """An N5 block that the n5_default codec cannot decode."""


@dataclass(frozen=True)
class N5DefaultCodec(ArrayBytesCodec):
    """`n5_default`: one N5 block, header included.

    Configuration `{"codecs": [transpose (full reversal), bytes (big-endian, or
    no endian for 1-byte types), optional one bytes-to-bytes codec]}`. Decoding
    parses the header (mode, number of dimensions, block size in N5 dimension
    order, which is the array's), requires default mode (0) and the array's
    number of dimensions, decodes the body with the inner codecs at the
    block's own size, and pads (with the fill value) or truncates it to the
    chunk's shape, so truncated edge blocks read correctly.
    """

    is_fixed_size = False
    codecs: tuple[Codec, ...] = ()

    def __init__(self, *, codecs) -> None:
        from zarr.core.metadata.v3 import parse_codecs

        cs = parse_codecs(codecs)
        if not 2 <= len(cs) <= 3:
            raise ValueError(f"n5_default needs 2 or 3 inner codecs, got {len(cs)}")
        t, b = cs[0], cs[1]
        if not isinstance(t, TransposeCodec) or tuple(t.order) != tuple(range(len(t.order) - 1, -1, -1)):
            raise ValueError("n5_default's first codec must be a full-reversal transpose")
        if not isinstance(b, BytesCodec):
            raise ValueError("n5_default's second codec must be a bytes codec")
        if len(cs) == 3 and not isinstance(cs[2], BytesBytesCodec):
            raise ValueError("n5_default's third codec must be bytes-to-bytes")
        object.__setattr__(self, "codecs", tuple(cs))

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(codecs=data["configuration"]["codecs"])

    def to_dict(self) -> dict:
        return {"name": "n5_default", "configuration": {"codecs": [c.to_dict() for c in self.codecs]}}

    def evolve_from_array_spec(self, array_spec: ArraySpec) -> Self:
        return self

    def validate(self, *, shape, dtype, chunk_grid) -> None:
        if len(shape) != len(self.codecs[0].order):
            raise ValueError(f"n5_default: the array has {len(shape)} dimensions, the codec "
                             f"{len(self.codecs[0].order)}")
        # zarr-python gives a bytes codec without configuration the native endianness;
        # it only matters, and must be big, for types of more than one byte.
        endian = getattr(self.codecs[1].endian, "value", self.codecs[1].endian)
        if dtype.to_native_dtype().itemsize > 1 and endian != "big":
            raise ValueError("n5_default's bytes codec must be big-endian")

    def _pipeline(self):
        return get_pipeline_class().from_codecs(self.codecs)

    async def _decode_single(self, chunk_bytes: Buffer, chunk_spec: ArraySpec) -> NDBuffer:
        raw = chunk_bytes.to_bytes()
        if len(raw) < 4:
            raise N5BlockError("N5 block shorter than its header")
        mode, ndim = struct.unpack_from(">HH", raw)
        if mode != 0:
            raise N5BlockError(f"N5 block mode {mode} is not default (0)")
        if ndim != len(chunk_spec.shape):
            raise N5BlockError(f"N5 block has {ndim} dimensions, the array {len(chunk_spec.shape)}")
        if len(raw) < 4 + 4 * ndim:
            raise N5BlockError("N5 block shorter than its header")
        shape = struct.unpack_from(f">{ndim}I", raw, 4)
        body = chunk_spec.prototype.buffer.from_bytes(raw[4 + 4 * ndim :])
        spec = ArraySpec(tuple(shape), chunk_spec.dtype, chunk_spec.fill_value, chunk_spec.config,
                         chunk_spec.prototype)
        (decoded,) = await self._pipeline().decode([(body, spec)])
        block = decoded.as_numpy_array()
        if tuple(shape) == tuple(chunk_spec.shape):
            return decoded
        out = np.full(chunk_spec.shape, chunk_spec.fill_value, dtype=block.dtype)
        common = tuple(slice(0, min(a, b)) for a, b in zip(shape, chunk_spec.shape))
        out[common] = block[common]
        return chunk_spec.prototype.nd_buffer.from_ndarray_like(out)

    async def _encode_single(self, chunk_array: NDBuffer, chunk_spec: ArraySpec) -> Buffer:
        (body,) = await self._pipeline().encode([(chunk_array, chunk_spec)])
        header = struct.pack(f">HH{len(chunk_spec.shape)}I", 0, len(chunk_spec.shape), *chunk_spec.shape)
        return chunk_spec.prototype.buffer.from_bytes(header + body.to_bytes())

    def compute_encoded_size(self, input_byte_length: int, chunk_spec: ArraySpec) -> int:
        raise NotImplementedError


register_codec("n5_default", N5DefaultCodec)

# Other packages (zarr-n5) register an `n5_default` too; zarr-python then picks
# one arbitrarily unless the config names it. Name this one, unless the caller
# already chose.
if zarr.config.get("codecs.n5_default", None) is None:
    zarr.config.set({"codecs.n5_default": "vzip.codecs.N5DefaultCodec"})
