"""Hand-written protobuf wire-format encoding/decoding for the vzip schema (spec §5, Appendix A)."""

MAX64 = (1 << 64) - 1
MAX32 = (1 << 32) - 1
MAX_FIELD = (1 << 29) - 1


class Malformed(Exception):
    pass


# ---------------------------------------------------------------- decoding

def _varint(buf, pos, end):
    result = 0
    shift = 0
    for _ in range(10):
        if pos >= end:
            raise Malformed("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            if result > MAX64:
                raise Malformed("varint exceeds 2^64-1")
            return result, pos
        shift += 7
    raise Malformed("varint longer than 10 bytes")


def parse_fields(buf):
    """Return a list of (field_number, wire_type, value).

    value is an int for VARINT/I64/I32 and bytes for LEN.
    """
    pos = 0
    end = len(buf)
    out = []
    while pos < end:
        tag, pos = _varint(buf, pos, end)
        wt = tag & 7
        fn = tag >> 3
        if wt in (3, 4, 6, 7):
            raise Malformed("invalid wire type %d" % wt)
        if fn == 0 or fn > MAX_FIELD:
            raise Malformed("invalid field number %d" % fn)
        if wt == 0:
            v, pos = _varint(buf, pos, end)
        elif wt == 1:
            if end - pos < 8:
                raise Malformed("truncated I64")
            v = int.from_bytes(buf[pos:pos + 8], "little")
            pos += 8
        elif wt == 5:
            if end - pos < 4:
                raise Malformed("truncated I32")
            v = int.from_bytes(buf[pos:pos + 4], "little")
            pos += 4
        else:  # wt == 2
            ln, pos = _varint(buf, pos, end)
            if ln > end - pos:
                raise Malformed("LEN field extends past end of message")
            v = bytes(buf[pos:pos + ln])
            pos += ln
        out.append((fn, wt, v))
    return out


# Schema: {field_number: (name, type, label)}
#   type: 'uint32' | 'uint64' | 'int64' | 'string' | 'bytes' | dict (message schema)
#   label: 'single' | 'optional' | 'repeated' | 'oneof:<group>'

def decode(buf, schema):
    result = {}
    for fn, wt, v in parse_fields(buf):
        spec = schema.get(fn)
        if spec is None:
            continue  # unknown (or reserved) field: skipped
        name, typ, label = spec
        is_len = isinstance(typ, dict) or typ in ("string", "bytes")
        if wt != (2 if is_len else 0):
            raise Malformed("field %d has wrong wire type %d" % (fn, wt))
        if typ == "uint32":
            if v > MAX32:
                raise Malformed("uint32 field %d overflows" % fn)
        elif typ == "int64":
            if v >= 1 << 63:
                v -= 1 << 64
        elif typ == "string":
            try:
                v = v.decode("utf-8", "strict")
            except UnicodeDecodeError:
                raise Malformed("string field %d is not valid UTF-8" % fn)
        elif isinstance(typ, dict):
            v = decode(v, typ)
        if label == "repeated":
            result.setdefault(name, []).append(v)
        elif label.startswith("oneof:"):
            result[label[6:]] = (name, v)
        else:
            result[name] = v
    return result


RANGE = {
    1: ("source", "uint32", "single"),
    3: ("offset", "uint64", "single"),
    4: ("length", "uint64", "single"),
    5: ("data", "bytes", "optional"),
}
CONCAT = {1: ("parts", RANGE, "repeated")}
SOURCE = {
    1: ("url", "string", "oneof:kind"),
    2: ("key", "string", "oneof:kind"),
    3: ("data", "bytes", "oneof:kind"),
    4: ("size", "uint64", "optional"),
    5: ("etag", "string", "optional"),
    6: ("modified_not_after", "int64", "optional"),
}
SOURCE_TABLE = {1: ("sources", SOURCE, "repeated")}
PAGE = {
    1: ("first_key", "string", "single"),
    2: ("offset", "uint64", "single"),
    3: ("length", "uint64", "single"),
}
PINNED = {
    1: ("key", "string", "single"),
    2: ("data_offset", "uint64", "single"),
    3: ("size", "uint64", "single"),
    4: ("csize", "uint64", "single"),
    5: ("method", "uint32", "single"),
}
CD_INDEX = {1: ("pages", PAGE, "repeated"), 2: ("pinned", PINNED, "repeated")}


# ---------------------------------------------------------------- encoding

def enc_varint(n):
    if n < 0:
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _f_varint(fn, v):
    return enc_varint(fn << 3) + enc_varint(v)


def _f_len(fn, b):
    return enc_varint((fn << 3) | 2) + enc_varint(len(b)) + b


def encode_range(source=0, offset=0, length=0, data=None):
    out = b""
    if source:
        out += _f_varint(1, source)
    if offset:
        out += _f_varint(3, offset)
    if length:
        out += _f_varint(4, length)
    if data is not None:
        out += _f_len(5, bytes(data))
    return out


def encode_concat(encoded_parts):
    return b"".join(_f_len(1, p) for p in encoded_parts)


def encode_source(kind, value, size=None, etag=None, mnf=None):
    fn = {"url": 1, "key": 2, "data": 3}[kind]
    v = value.encode("utf-8") if kind != "data" else bytes(value)
    out = _f_len(fn, v)
    if size is not None:
        out += _f_varint(4, size)
    if etag is not None:
        out += _f_len(5, etag.encode("utf-8"))
    if mnf is not None:
        out += _f_varint(6, mnf)
    return out


def encode_source_table(encoded_sources):
    return b"".join(_f_len(1, s) for s in encoded_sources)


def encode_page(first_key, offset, length):
    out = b""
    if first_key:
        out += _f_len(1, first_key)
    if offset:
        out += _f_varint(2, offset)
    if length:
        out += _f_varint(3, length)
    return out


def encode_pinned(key, data_offset, size, csize, method):
    out = b""
    if key:
        out += _f_len(1, key)
    if data_offset:
        out += _f_varint(2, data_offset)
    if size:
        out += _f_varint(3, size)
    if csize:
        out += _f_varint(4, csize)
    if method:
        out += _f_varint(5, method)
    return out


def encode_cd_index(pages, pinned):
    return b"".join(_f_len(1, p) for p in pages) + b"".join(_f_len(2, p) for p in pinned)
