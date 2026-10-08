"""Probes the corpus hosts for HTTP/2 (ALPN) and multi-range requests, and
measures their round-trip time and single-stream bandwidth.

For each URL: the protocol the server picks by ALPN when offered h2; the
answer to `Range: bytes=0-99,1000-1099,50000-50099` (206 multipart/byteranges,
206 single range, or 200 whole object); the time to first byte of 10 small
range reads on one kept-alive connection (median = RTT + server time); and the
throughput of one 8 MiB range read.

Usage: uv run python experiments/ndpi_access/probe_hosts.py [--no-bw] [url ...]
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import ssl
import statistics
import time
from urllib.parse import urlparse

URLS = [
    "https://openslide.cs.cmu.edu/download/openslide-testdata/Hamamatsu/CMU-1.ndpi",
    "https://zenodo.org/api/records/15001649/files/bfGetReaderTest.ims/content",
    "https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/20210609-ftp-ome-tiffs/4000_d11_m5_LT_2%20(20x_01).ome.tiff",
    "https://janelia-cosem-datasets.s3.amazonaws.com/jrc_hela-2/jrc_hela-2.n5/labels/gt/attributes.json",
    "https://storage.googleapis.com/idc-open-data/d42b19a9-89fe-47e1-9dcf-834d1ae510da/b03a7cd0-78c9-41f7-bae3-5f74e90d6194.dcm",
    "https://uk1s3.embassy.ebi.ac.uk/idr/zarr/v0.4/idr0072B/9512.zarr/.zattrs",
]
UA = "vzip-ndpi-access-probe (https://github.com/d-v-b/vzip)"


def alpn(host: str) -> str:
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    with socket.create_connection((host, 443), timeout=20) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            return s.selected_alpn_protocol() or "none"


def connect(url: str) -> tuple[http.client.HTTPSConnection, str]:
    p = urlparse(url)
    return http.client.HTTPSConnection(p.hostname, timeout=60), p.path + (f"?{p.query}" if p.query else "")


def get(conn, path: str, rng: str, host_url: str, redirects: int = 5):
    conn.request("GET", path, headers={"Range": rng, "User-Agent": UA, "Accept-Encoding": "identity"})
    r = conn.getresponse()
    body = r.read()
    if r.status in (301, 302, 303, 307, 308) and redirects:
        loc = r.getheader("Location")
        c2, p2 = connect(loc if "://" in loc else f"https://{urlparse(host_url).hostname}{loc}")
        return get(c2, p2, rng, loc, redirects - 1)
    return r, body, conn, path


def probe(url: str, bw: bool) -> dict:
    out: dict = {"url": url, "alpn": alpn(urlparse(url).hostname)}
    conn, path = connect(url)
    r, body, conn, path = get(conn, path, "bytes=0-99,1000-1099,50000-50099", url)
    ctype = r.getheader("Content-Type") or ""
    if r.status == 206 and ctype.startswith("multipart/byteranges"):
        out["multirange"] = f"yes (206 multipart, {len(body)} B for 300 B)"
    elif r.status == 206:
        out["multirange"] = f"no (206 single range {r.getheader('Content-Range')})"
    else:
        out["multirange"] = f"no ({r.status}, {len(body)} B)"
    ttfb = []
    for i in range(10):
        t = time.perf_counter()
        r, body, conn, path = get(conn, path, f"bytes={i * 100}-{i * 100 + 99}", url)
        ttfb.append(time.perf_counter() - t)
    out["rtt_ms"] = round(1000 * statistics.median(ttfb), 1)
    if bw:
        t = time.perf_counter()
        r, body, conn, path = get(conn, path, f"bytes=0-{(8 << 20) - 1}", url)
        dt = time.perf_counter() - t
        out["mbit_s"] = round(8 * len(body) / max(dt - statistics.median(ttfb), 1e-3) / 1e6, 1)
        out["bw_bytes"] = len(body)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-bw", action="store_true")
    ap.add_argument("urls", nargs="*", default=URLS)
    a = ap.parse_args()
    for u in a.urls:
        try:
            print(json.dumps(probe(u, not a.no_bw)), flush=True)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"url": u, "error": f"{type(e).__name__}: {e}"}), flush=True)


if __name__ == "__main__":
    main()
