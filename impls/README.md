# Independent implementations

Each directory holds a reader, writer and harness CLI that an agent built from
[SPEC.md](../SPEC.md) (format version 0, **revision 7**; the current spec is revision 8) and
[HARNESS.md](../conformance/HARNESS.md) alone. The agents had no access to the
reference implementation, the test vectors or each other's code. Each agent's
`SPEC_NOTES.md` lists the ambiguities it reported; notes from every round are
in [conformance/notes/](../conformance/notes/).

| dir | language | dependencies | HTTP | run |
|---|---|---|---|---|
| `rust/` | Rust | flate2, crc32fast (hand-written HTTP/1.1 client, JSON parser, URI and date code) | http | `cargo build --release`, then `./vzip` |
| `typescript/` | TypeScript (Node ≥ 23, type stripping) | none | http, https | `./vzip` |
| `python/` | Python 3.12 | standard library | http, https | `./vzip` (needs `uv`) |

**Against the revision-7 suite**, all three pass every check, including the
HTTP profile:
- 5120/5120 read queries;
- 11/11 write cases;
- 41/41 rejections;
- 19892/19892 cross-reads;
- 3136/3136 HTTP checks.

There is no divergence between them.

**Against revision 8** they fail only four rules that revision 8 added
because of their round-7 notes:
- a repeated `ETag` field is an error even on an unpinned read (all three);
- the `Content-Range` unit is case-insensitive (TypeScript);
- query files with lone-surrogate keys, or with numbers of 2^53 or more, are
  invalid (TypeScript and Python).
