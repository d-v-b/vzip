"""The reader policy of spec §8.7, and the range checksum of spec §5.2."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import google_crc32c

from vzip.errors import ResolutionError

# The defaults of spec §8.7's table.
MAX_SOURCES = 1 << 22
MAX_READS = 1024
MAX_FORMAT_ENTRY = 256 << 20

_ENCODED = re.compile(r"%(2[eEfF]|5[cC])")


def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli, RFC 3720 §B.4) of `data`."""
    return google_crc32c.value(bytes(data))


def _scheme(url: str) -> str:
    """The scheme of an absolute URL, lowercase, with https counted as http (§8.7 rule 1)."""
    s = url.split(":", 1)[0].lower() if ":" in url else ""
    return "http" if s == "https" else s


def matches_prefix(url: str, prefix: str) -> bool:
    """Does `url` match `prefix` (spec §8.7 rule 2)?"""
    if not url.startswith(prefix):
        return False
    rest = url[len(prefix):]
    if not (prefix.endswith("/") or rest == "" or rest[0] in "/?#"):
        return False
    path = re.split(r"[?#]", url, maxsplit=1)[0]
    return not _ENCODED.search(path)


# ------------------------------------------------------------ address classes (§8.7 rule 3)

LOOPBACK, PRIVATE, LINK_LOCAL, SPECIAL, PUBLIC = "loopback", "private", "link-local", "special", "public"

_NETS = [(c, ipaddress.ip_network(n)) for c, ns in [
    (LOOPBACK, ["127.0.0.0/8", "::1/128"]),
    (PRIVATE, ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"]),
    (LINK_LOCAL, ["169.254.0.0/16", "fe80::/10"]),
    (SPECIAL, ["0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15",
               "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/3", "::/8", "64:ff9b:1::/48",
               "100::/63", "2001::/23", "2001:db8::/32", "3fff::/20", "5f00::/16", "fec0::/10",
               "ff00::/8"]),
] for n in ns]
_MAPPED = ipaddress.ip_network("::ffff:0:0/96")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_6TO4 = ipaddress.ip_network("2002::/16")


def address_class(address: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """The class of an IP address (spec §8.7 rule 3): loopback, private,
    link-local, special or public. An IPv6 address that embeds an IPv4 one
    (IPv4-mapped, NAT64, 6to4) has the IPv4 address's class."""
    if isinstance(address, str):
        address = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone
    if isinstance(address, ipaddress.IPv6Address):
        n = int(address)
        if address in _MAPPED or address in _NAT64:
            return address_class(ipaddress.IPv4Address(n & 0xFFFFFFFF))
        if address in _6TO4:
            return address_class(ipaddress.IPv4Address((n >> 80) & 0xFFFFFFFF))
    for c, net in _NETS:
        if address.version == net.version and address in net:
            return c
    return PUBLIC


def host_class(host: str) -> str | None:
    """The class of a URL host as it is written: an IP literal's class, loopback
    for `localhost` and names under `.localhost` (RFC 6761), and None for any
    other name, whose class depends on what it resolves to."""
    h = host.lower().rstrip(".")
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h == "localhost" or h.endswith(".localhost"):
        return LOOPBACK
    try:
        return address_class(h)
    except ValueError:
        return None


@dataclass(frozen=True)
class Policy:
    """What a reader may resolve, and its resource limits (spec §8.7).

    - `schemes`: schemes allowed besides those rule 1 allows by default
      (`http` and `https` for every archive, `file` for a local one), such as
      `s3`. It may not name `file`: see `allow_files_from_remote_archives`.
    - `prefixes`: if given, a URL is allowed if and only if it matches one of
      them (rule 2), whatever its scheme. Rule 3 still applies.
    - `allow_files_from_remote_archives`: UNSAFE. Lets an archive opened from
      a URL name `file:` sources, that is, read the reader's local files.
    - `allow_private_hosts`: UNSAFE. Lets the archive send requests to
      loopback, private, link-local (cloud metadata) and other special
      addresses (rule 3), which every archive, local or remote, is otherwise
      refused. Rule 3's checks are then skipped, proxies included.
    - `allow_unchecked_proxy`: UNSAFE. Lets requests go through an HTTP proxy
      while rule 3 applies. The proxy connects, so the reader cannot check
      the address it reaches; it checks what the name resolves to locally,
      which DNS rebinding gets past. Without it, a request that would go
      through a proxy is refused.
    - `max_sources`, `max_reads`, `max_format_entry`: rules 4 to 6.

    Rule 7 (unverifiable pins) has no option here: this reader always sees a
    response's headers, so every pin it is given can be checked.
    """

    schemes: frozenset[str] = frozenset()
    prefixes: tuple[str, ...] | None = None
    allow_files_from_remote_archives: bool = False
    allow_private_hosts: bool = False
    allow_unchecked_proxy: bool = False
    max_sources: int = MAX_SOURCES
    max_reads: int = MAX_READS
    max_format_entry: int = MAX_FORMAT_ENTRY

    def __post_init__(self) -> None:
        if any(s.lower() == "file" for s in self.schemes):
            raise ValueError("Policy.schemes may not name 'file': a local archive reads files "
                             "already, and a remote one needs the unsafe "
                             "allow_files_from_remote_archives=True")

    def check(self, base: str, url: str) -> None:
        """Raise ResolutionError unless rules 1 and 2 allow `url` for an archive at `base`,
        and rule 3 allows its host as written (an IP literal, or `localhost`)."""
        if self.prefixes is not None:
            if not any(matches_prefix(url, p) for p in self.prefixes):
                raise ResolutionError(f"{url}: not under an allowed URL prefix (spec §8.7)")
        else:
            s, local = _scheme(url), _scheme(base) == "file"
            if s == "file":
                if not (local or self.allow_files_from_remote_archives):
                    raise ResolutionError(
                        f"{url}: an archive opened from a URL may not read local files "
                        f"(spec §8.7)")
            elif s != "http" and s not in {_scheme(x + ":") for x in self.schemes}:
                raise ResolutionError(f"{url}: the scheme {s!r} is not allowed (spec §8.7)")
        if _scheme(url) == "http":
            try:
                host = urlsplit(url).hostname
            except ValueError:
                host = None
            c = host_class(host) if host else None
            if c is not None:
                self._check_class(url, c)

    def check_address(self, url: str, address: str) -> None:
        """Raise ResolutionError unless rule 3 lets the reader connect to `address` for `url`."""
        self._check_class(url, address_class(address), address)

    def _check_class(self, url: str, c: str, address: str | None = None) -> None:
        if c == PUBLIC or self.allow_private_hosts:
            return
        at = f" (at {address})" if address else ""
        raise ResolutionError(f"{url}: the host{at} is a {c} address, which the reader does not "
                              f"reach without allow_private_hosts=True (spec §8.7)")
