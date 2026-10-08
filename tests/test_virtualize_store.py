"""Store inputs (VIRTUALIZE.md §1.4–§1.6): the listing endpoint, the listing
body parser, the strict JSON reader, listing over HTTP, and the writer's
bulk url-source path. The browser implementation has the same tests in
web/test/store.test.ts.
"""

import io
import urllib.error
import zipfile

import pytest

from vzip.archive import VZipWriter
from vzip.pb import Range, decode_source_table
from vzip.virtualize import Rejected
from vzip.virtualize.store import (
    HttpStore,
    ListingFailed,
    StoreLimit,
    listing_endpoint,
    object_url,
    open_store,
    parse_json,
    parse_listing,
)

XML = '<?xml version="1.0" encoding="UTF-8"?>\n<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{}</ListBucketResult>'


def listing(contents: str, truncated: str = "false", extra: str = "") -> bytes:
    return XML.format(f"<IsTruncated>{truncated}</IsTruncated>{extra}{contents}").encode()


def obj(key: str, size: str = "1") -> str:
    return f"<Contents><Key>{key}</Key><LastModified>x</LastModified><Size>{size}</Size></Contents>"


def test_listing_endpoints_and_object_urls():
    cases = {
        "https://janelia-cosem-datasets.s3.amazonaws.com/jrc_hela-2/jrc_hela-2.n5/em/":
            ("https://janelia-cosem-datasets.s3.amazonaws.com/", "jrc_hela-2/jrc_hela-2.n5/em/"),
        "https://janelia-cosem-datasets.s3.amazonaws.com/":
            ("https://janelia-cosem-datasets.s3.amazonaws.com/", ""),
        "https://bucket.s3.us-east-1.amazonaws.com/a/b/": ("https://bucket.s3.us-east-1.amazonaws.com/", "a/b/"),
        "https://bucket.s3-us-west-2.amazonaws.com/a/": ("https://bucket.s3-us-west-2.amazonaws.com/", "a/"),
        "HTTPS://Bucket.S3.AmazonAWS.com:443/a/": ("HTTPS://Bucket.S3.AmazonAWS.com:443/", "a/"),
        "https://s3.amazonaws.com/janelia-cosem-datasets/jrc_hela-2/jrc_hela-2.n5/em/":
            ("https://s3.amazonaws.com/janelia-cosem-datasets/", "jrc_hela-2/jrc_hela-2.n5/em/"),
        "https://s3.us-east-1.amazonaws.com/bucket/": ("https://s3.us-east-1.amazonaws.com/bucket/", ""),
        "http://127.0.0.1:8765/f/n5/x/": ("http://127.0.0.1:8765/f/", "n5/x/"),
        "https://storage.googleapis.com/b/p%20q/%C3%A9/": ("https://storage.googleapis.com/b/", "p q/é/"),
        "http://[::1]:9000/b/k/": ("http://[::1]:9000/b/", "k/"),
    }
    for url, want in cases.items():
        assert listing_endpoint(url) == want, url
    assert object_url("https://h/x/", "a b/0.0") == "https://h/x/a%20b/0.0"
    assert object_url("https://h/x/", "é/%/:@!$&'()*+,;=~") == "https://h/x/%C3%A9/%25/:@!$&'()*+,;=~"


@pytest.mark.parametrize("url", [
    "https://h/b/x",  # does not end in /
    "https://h/b/x/?q=1",
    "https://h/b/x/?",
    "https://h/b/x/#f",
    "https://user@h/b/x/",
    "ftp://h/b/x/",
    "https://h/",  # path-style without a bucket
    "https://h//x/",  # empty bucket
    "https://h/b/%FF/",  # prefix not UTF-8
    "https://h/b/a b/",  # not a URI
])
def test_rejects_store_urls(url):
    with pytest.raises(Rejected):
        listing_endpoint(url)


def test_parses_listings():
    cases = [
        (listing(obj("p/a", "5") + obj("p/b", "0")), [("p/a", 5), ("p/b", 0)], None),
        (listing(obj("p/a"), "true", "<NextContinuationToken>tok/+=</NextContinuationToken>"), [("p/a", 1)], "tok/+="),
        (listing(obj("p/a&amp;b&#x41;&#66;&lt;&gt;&quot;&apos;")), [("p/a&bAB<>\"'", 1)], None),
        (listing(obj("p/x", "007")), [("p/x", 7)], None),
        (listing("", extra="<!-- a comment --><Name>bucket</Name><CommonPrefixes><Prefix>p/</Prefix></CommonPrefixes>"),
         [], None),
        (b"  <!--c--><ListBucketResult a='1' b=\"&amp;\"><IsTruncated>false</IsTruncated><Empty/></ListBucketResult>\n",
         [], None),
        (listing(obj("p/é \t")), [("p/é \t", 1)], None),
        (listing(obj("p/a"), "false", "<NextContinuationToken>ignored</NextContinuationToken>"), [("p/a", 1)], None),
    ]
    for body, objects, token in cases:
        assert parse_listing(body, "p/") == (objects, token), body


@pytest.mark.parametrize("body", [
    b"<html><body>Index of /</body></html>",
    listing(obj("p/a"))[:-5],  # truncated
    listing(obj("p/a")) + b"<x/>",  # content after the root
    listing(obj("q/a")),  # outside the prefix
    listing(obj("p/a", "-1")),
    listing(obj("p/a", "1.5")),
    listing(obj("p/a", "9007199254740992")),
    listing(obj("p/a", "")),
    listing("<Contents><Key>p/a</Key></Contents>"),  # no Size
    listing("<Contents><Key>p/a</Key><Key>p/b</Key><Size>1</Size></Contents>"),
    listing("<Contents><Key>p/<b/></Key><Size>1</Size></Contents>"),  # Key with a child element
    listing(obj("p/a"), "true"),  # truncated without a token
    listing(obj("p/a"), "TRUE"),
    XML.format(obj("p/a")).encode(),  # no IsTruncated
    listing(obj("p/a"), "false", "<IsTruncated>false</IsTruncated>"),
    listing(obj("p/a"), "true", "<NextContinuationToken>a</NextContinuationToken>" * 2),
    listing(obj("p/&nbsp;")),  # unknown entity
    listing(obj("p/&#0;")),  # not a character
    listing(obj("p/\x01")),
    listing(obj("p/a]]>b")),
    listing("<![CDATA[x]]>"),
    b"<!DOCTYPE x><ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>",
    b"<?pi x?><ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>",
    b"<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResul>",
    b"<ListBucketResult><!-- a -- b --><IsTruncated>false</IsTruncated></ListBucketResult>",
    b"\xef\xbb\xbf<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>",
    b"<ListBucketResult><IsTruncated>false</IsTruncated><Key>\xff</Key></ListBucketResult>",
    b"<ListAllMyBucketsResult><IsTruncated>false</IsTruncated></ListAllMyBucketsResult>",
    b"<Error><Code>NoSuchBucket</Code></Error>",
])
def test_rejects_listings(body):
    with pytest.raises(Rejected):
        parse_listing(body, "p/")


def nested(depth: int) -> list:
    v: list = []
    for _ in range(depth - 1):
        v = [v]
    return v


def test_reads_json():
    cases = [
        (b'{"a": 1, "a": 2}', {"a": 2}),  # the last member wins
        (b' [1, 1.0, 6.4e1, -0, 1e-400] ', [1, 1.0, 64.0, 0, 0.0]),
        (b'{"x": 9007199254740993}', {"x": 9007199254740992.0}),  # beyond 2^53: its binary64 value
        (b'"\\ud800"', "\ud800"),
        (b"[" * 256 + b"]" * 256, nested(256)),
        ('{"é": "é"}'.encode(), {"é": "é"}),
    ]
    for data, want in cases:
        assert parse_json(data) == want, data


@pytest.mark.parametrize("data", [
    b'{"a": NaN}', b"[Infinity]", b"[-Infinity]", b"[1e400]", b"[-1e999]", b"1" + b"0" * 5000,
    b"[1,]", b"{,}", b"// x\n1", b"\xef\xbb\xbf{}", b'{"a": "\xff"}', b"", b"[" * 257 + b"]" * 257,
    b"'a'", b"[01]", b'"\x01"',
])
def test_rejects_json(data):
    with pytest.raises(Rejected):
        parse_json(data)


class Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def opener(pages: dict):
    """A fake urlopen: request URL → body, or HTTP status."""
    def open_(req):
        v = pages[req.full_url]
        if isinstance(v, int):
            raise urllib.error.HTTPError(req.full_url, v, "x", {}, None)
        if isinstance(v, tuple):
            return Response(v[1], v[0])
        return Response(v)
    return open_


def test_lists_over_http():
    base = "http://h/b/?list-type=2&prefix=s%2F"
    pages = {
        base: listing(obj("s/attributes.json", "10") + obj("s/x/0") + obj("s/") + obj("s/a//b") + obj("s/./c")
                      + obj("s/d/"), "true", "<NextContinuationToken>t 1/é</NextContinuationToken>"),
        base + "&continuation-token=t%201%2F%C3%A9": listing(obj("s/x/1", "0")),
    }
    store = HttpStore("http://h/b/s/", opener=opener(pages))
    assert store.objects == {"attributes.json": 10, "x/0": 1, "x/1": 0}
    assert (store.requests, store.listed) == (2, 7)


def test_reads_from_the_listed_location_when_renamed():
    # --url names the store in the output; objects are still read where it was listed.
    pages = {"http://h/b/?list-type=2&prefix=s%2F": listing(obj("s/attributes.json", "2")),
             "http://h/b/s/attributes.json": b"{}"}
    store = open_store("http://h/b/s/", "https://data.example/s/", opener=opener(pages))
    assert store.url == "https://data.example/s/"
    assert store.read("attributes.json") == b"{}"


@pytest.mark.parametrize("pages,error", [
    ({"http://h/b/?list-type=2&prefix=s%2F": 404}, Rejected),
    ({"http://h/b/?list-type=2&prefix=s%2F": 403}, Rejected),
    ({"http://h/b/?list-type=2&prefix=s%2F": b"<html>a directory index</html>"}, Rejected),
    ({"http://h/b/?list-type=2&prefix=s%2F": (204, b"")}, Rejected),
    ({"http://h/b/?list-type=2&prefix=s%2F": 400}, Rejected),
    ({"http://h/b/?list-type=2&prefix=s%2F": listing(obj("s/a") + obj("s/a"))}, Rejected),
    ({"http://h/b/?list-type=2&prefix=s%2F": listing(obj("s/a"), "true", "<NextContinuationToken>t</NextContinuationToken>"),
      "http://h/b/?list-type=2&prefix=s%2F&continuation-token=t": listing(obj("s/a"))}, Rejected),
    ({"http://h/b/?list-type=2&prefix=s%2F": 408}, ListingFailed),
])
def test_listing_errors(pages, error, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(error):
        HttpStore("http://h/b/s/", opener=opener(pages))


def test_listing_fails_on_server_errors(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(ListingFailed):
        HttpStore("http://h/b/s/", opener=opener({"http://h/b/?list-type=2&prefix=s%2F": 503}))


def test_listing_fails_past_a_resource_limit():
    pages = {"http://h/b/?list-type=2&prefix=s%2F": listing(obj("s/a") + obj("s/b") + obj("s/c"))}
    with pytest.raises(StoreLimit):
        HttpStore("http://h/b/s/", opener=opener(pages), max_objects=2)


def test_writer_bulk_url_sources():
    buf = io.BytesIO()
    w = VZipWriter(buf, page_size=1 << 16)
    w.add_url_refs((f"a/{i}", f"https://h/x/a/{i}", i) for i in range(5))
    w.add_bytes("zarr.json", b"{}", late=True)
    w.close()
    z = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    assert [s.url for s in decode_source_table(z.read("__vz__/sources"))] == [f"https://h/x/a/{i}" for i in range(5)]
    for i in range(5):
        info = z.getinfo(f"a/{i}")
        assert Range.decode(info.extra[4:]) == Range(source=i, offset=0, length=i)
