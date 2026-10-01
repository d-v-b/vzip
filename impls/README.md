# Independent implementations

Each directory holds a reader, writer and harness CLI that an agent built from
[SPEC.md](../SPEC.md) (format version 0, **revision 5**) and
[HARNESS.md](../conformance/HARNESS.md) alone. The agents had no access to the
reference implementation, the test vectors or each other's code. Each agent's
`SPEC_NOTES.md` lists the ambiguities it reported; notes from every round are
in [conformance/notes/](../conformance/notes/).

| dir | language | dependencies | HTTP | run |
|---|---|---|---|---|
| `rust/` | Rust | flate2, crc32fast, serde_json (hand-written HTTP/1.1 client) | http | `cargo build --release`, then `./vzip` |
| `typescript/` | TypeScript (Node ≥ 23, type stripping) | none | http, https | `./vzip` |
| `python/` | Python 3.12 | standard library | http, https | `./vzip` (needs `uv`) |

**Against the revision-5 suite**, all three pass every check, including the
HTTP profile:
- 5129/5129 read queries;
- 11/11 write cases;
- 32/32 rejections;
- 20000/20000 cross-reads;
- 1876/1876 HTTP checks.

There is no divergence between them.

**Against revision 6** they fail exactly one new behaviour, which revision 6
added because of their round-5 notes. When a server ignores
`If-Match`/`If-Unmodified-Since`, or sends no `ETag`, a pinned read must
compare the response's own `ETag`/`Last-Modified` with the pin and fail if
they disagree or are missing. These implementations return the bytes
instead, so their pins fail open on such servers.
