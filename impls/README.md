# Independent implementations

Each directory holds a reader, writer and harness CLI that an agent built from
[SPEC.md](../SPEC.md) **revision 4** and [HARNESS.md](../conformance/HARNESS.md)
alone. The agents had no access to the reference implementation, the test
vectors or each other's code. Each agent's `SPEC_NOTES.md` is the list of
ambiguities it reported; notes from every round are in
[conformance/notes/](../conformance/notes/).

| dir | language | dependencies | run |
|---|---|---|---|
| `rust/` | Rust | flate2, crc32fast, serde_json | `cargo build --release`, then `./vzip` |
| `typescript/` | TypeScript (Node ≥ 23, type stripping) | none | `./vzip` |
| `python/` | Python 3.12 | standard library | `./vzip` (needs `uv`) |

Against the revision-4 suite, all three pass every check:
- 5129/5129 read queries;
- 9/9 write cases;
- 32/32 rejections;
- 20000/20000 cross-reads.

There is no divergence between them. The round-3 implementations, which
followed an encoded `%2E%2E` and rejected a valid archive containing a false
zip64 locator, are not kept here; their notes are in
`conformance/notes/r3/`.
