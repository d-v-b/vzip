"""Hand-written protobuf wire-format codec for the vzip schema (spec §5, Appendix A)."""

U64_MAX = (1 << 64) - 1
U32_MAX = (1 << 32) - 1
FIELD_MAX = (1 << 29) - 1

VARINT, I64, LEN, I32 = 0, 1, 2, 5


class ProtoError(Exception):
    """The message is malformed (spec §5.1)."""


# ---------------------------------------------------------------- decoding

def _read_varint(buf, pos, end):
    result = 0
    shift = 0
    for i in range(10):
        if pos >= end:
            raise ProtoError("truncated varint")
        b = buf[pos]
        pos += 1
        if i == 9:
            if b & 0x80:
                raise ProtoError("varint longer than 10 bytes")
            if b > 1:
                raise ProtoError("varint exceeds 2^64-1")
        result |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return result, pos
    raise ProtoError("varint longer than 10 bytes")  # pragma: no cover


def parse_fields(buf):
    """Split a message into (field_number, wire_type, value) triples.

    value is an int for VARINT/I64/I32 and a bytes object for LEN.
    """
    buf = bytes(buf)
    pos = 0
    end = len(buf)
    out = []
    while pos < end:
        tag, pos = _read_varint(buf, pos, end)
        wt = tag & 7
        num = tag >> 3
        if num == 0 or num > FIELD_MAX:
            raise ProtoError(f"invalid field number {num}")
        if wt == VARINT:
            val, pos = _read_varint(buf, pos, end)
        elif wt == I64:
            if pos + 8 > end:
                raise ProtoError("truncated fixed64")
            val = int.from_bytes(buf[pos:pos + 8], "little")
            pos += 8
        elif wt == I32:
            if pos + 4 > end:
                raise ProtoError("truncated fixed32")
            val = int.from_bytes(buf[pos:pos + 4], "little")
            pos += 4
        elif wt == LEN:
            n, pos = _read_varint(buf, pos, end)
            if n > end - pos:
                raise ProtoError("LEN field extends past end of message")
            val = buf[pos:pos + n]
            pos += n
        else:
            raise ProtoError(f"invalid wire type {wt}")
        out.append((num, wt, val))
    return out


# A schema maps field number -> (name, type, repeated, sub_schema)
# types: uint32, uint64, int64, string, bytes, message

def decode(buf, schema):
    """Decode a message per schema.

    Returns a dict name -> value for present fields (presence is tracked for
    every field, so callers can implement `optional`/`oneof`).  Repeated
    fields are lists.  For `oneof` groups the dict key '__oneof__' records
    the name of the last member seen.
    """
    fields = schema["fields"]
    oneof = schema.get("oneof", ())
    msg = {}
    for name, (_, _typ, rep, _sub) in ((f[0], f) for f in fields.values()):
        if rep:
            msg[name] = []
    for num, wt, val in parse_fields(buf):
        spec = fields.get(num)
        if spec is None:
            continue  # unknown or reserved field: skip
        name, typ, rep, sub = spec
        if typ in ("uint32", "uint64", "int64"):
            if wt != VARINT:
                raise ProtoError(f"field {name}: wrong wire type {wt}")
            if typ == "uint32" and val > U32_MAX:
                raise ProtoError(f"field {name}: uint32 overflow")
            if typ == "int64" and val >= (1 << 63):
                val -= 1 << 64
        else:
            if wt != LEN:
                raise ProtoError(f"field {name}: wrong wire type {wt}")
            if typ == "string":
                try:
                    val = val.decode("utf-8")
                except UnicodeDecodeError:
                    raise ProtoError(f"field {name}: invalid UTF-8") from None
            elif typ == "message":
                val = decode(val, sub)
        if rep:
            msg[name].append(val)
        else:
            if name in oneof:
                for other in oneof:
                    msg.pop(other, None)
            msg[name] = val
    return msg


def _f(name, typ, rep=False, sub=None):
    return (name, typ, rep, sub)


RANGE = {"fields": {1: _f("source", "uint32"), 3: _f("offset", "uint64"),
                    4: _f("length", "uint64"), 5: _f("data", "bytes")}}
CONCAT = {"fields": {1: _f("parts", "message", True, RANGE)}}
SOURCE = {"fields": {1: _f("url", "string"), 2: _f("key", "string"),
                     3: _f("data", "bytes"), 4: _f("size", "uint64"),
                     5: _f("etag", "string"), 6: _f("modified_not_after", "int64")},
          "oneof": ("url", "key", "data")}
SOURCE_TABLE = {"fields": {1: _f("sources", "message", True, SOURCE)}}
PAGE = {"fields": {1: _f("first_key", "string"), 2: _f("offset", "uint64"),
                   3: _f("length", "uint64")}}
PINNED = {"fields": {1: _f("key", "string"), 2: _f("data_offset", "uint64"),
                     3: _f("size", "uint64"), 4: _f("csize", "uint64"),
                     5: _f("method", "uint32")}}
CD_INDEX = {"fields": {1: _f("pages", "message", True, PAGE),
                       2: _f("pinned", "message", True, PINNED)}}


# ---------------------------------------------------------------- encoding

def enc_varint(v):
    if v < 0:
        v += 1 << 64
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(num, wt):
    return enc_varint((num << 3) | wt)


def enc_int(num, v, always=False):
    if v == 0 and not always:
        return b""
    return _tag(num, VARINT) + enc_varint(v)


def enc_len(num, data, always=False):
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not data and not always:
        return b""
    return _tag(num, LEN) + enc_varint(len(data)) + data


def encode_range(r):
    """r: {'data': bytes} or {'source','offset','length'}"""
    if "data" in r:
        return enc_len(5, r["data"], always=True)
    return (enc_int(1, r.get("source", 0)) + enc_int(3, r.get("offset", 0))
            + enc_int(4, r.get("length", 0)))


def encode_concat(parts):
    return b"".join(enc_len(1, encode_range(p), always=True) for p in parts)


def encode_source(s):
    out = b""
    if "url" in s:
        out += enc_len(1, s["url"], always=True)
    elif "key" in s:
        out += enc_len(2, s["key"], always=True)
    else:
        out += enc_len(3, s["data"], always=True)
    if s.get("size") is not None:
        out += enc_int(4, s["size"], always=True)
    if s.get("etag") is not None:
        out += enc_len(5, s["etag"], always=True)
    if s.get("modified_not_after") is not None:
        out += enc_int(6, s["modified_not_after"], always=True)
    return out


def encode_source_table(sources):
    return b"".join(enc_len(1, encode_source(s), always=True) for s in sources)


def encode_cd_index(pages, pinned):
    out = b""
    for first_key, off, length in pages:
        out += enc_len(1, enc_len(1, first_key) + enc_int(2, off) + enc_int(3, length),
                       always=True)
    for key, data_offset, size, csize, method in pinned:
        out += enc_len(2, enc_len(1, key) + enc_int(2, data_offset) + enc_int(3, size)
                       + enc_int(4, csize) + enc_int(5, method), always=True)
    return out
