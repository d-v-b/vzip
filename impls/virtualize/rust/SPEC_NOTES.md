# Notes on VIRTUALIZE.md (revision 4) from the Rust implementation

These notes come from implementing the TIFF and ND2 profiles from VIRTUALIZE.md
and HARNESS.md alone. The fourth revision is precise: all 95 fixtures behave as
their names say, and the three public inputs succeed. No rule was found under
which two careful implementers would produce different output for a real
TIFF, OME-TIFF or ND2 file. The issues below all involve degenerate or
adversarial inputs. They are listed from most to least likely to matter.

## 1. §4.3: an empty validity list is an object, so it rejects

> `pItemValid` (a list of flags, default absent), … checked on every node
>
> (§4.2) A level with at least one record, all with empty names, is a **list**
> … Any other level (including an empty one) is an **object**

Taken together, a `pItemValid` or `pPeriodValid` stored as an *empty LV level*
(type 11, `c = 0`) is an object, and it rejects the whole file, even on a node
the flattening never visits. An empty byte array (type 9, `b = 0`) is a list
and is accepted. The rule is clear, but it may be unintended: a writer that
stores "no validity entries" as an empty level produces a file that is
otherwise fine. `ppNextLevelEx`, `Points` and `pPeriod` accept "object or
list", so they are not affected.
*Decision:* followed the text (reject).
*Likelihood:* low. The real files seen store validity as byte arrays.

## 2. §4.5/§4.6: output size is unbounded when no frame is present

`uiComp` is bounded only by `uiWidth × uiComp × uiBpcInMemory / 8 ≤
uiWidthBytes ≤ 2^53 − 1`. When no frame is present, nothing ties it to the
file's size. A small corrupted file with `uiComp = 2^40` is then accepted, and
its output needs 2^40 `omero` channel objects. Implementations will run out of
memory, hang or crash in different ways.
*Decision:* above 10^7 channels the program *fails* (exit status 1, "too
large"). It does not reject, because the specification accepts the file.
*Suggestion:* add a bound, for example `uiComp ≤ 65536`, or require at least
one present frame.
*Likelihood:* real files are never affected. Fuzzed or corrupted inputs are.

## 3. §4.2: the nesting limit at exactly depth 100

> Levels MUST NOT be nested more than 100 deep: a chunk's top-level records are
> at depth 0, and the records of a level at depth `d` are at depth `d + 1`.

The text defines the depth of *records* but states the limit for *levels*.
Take a level record at depth 100 with `c = 0`. It has no record at depth 101,
yet it might count as a level "nested 101 deep".
*Decision:* reject only when a record at depth > 100 exists, so an empty level
at depth 100 is accepted. This agrees with `nd2_edge_nesting_100` (records at
depth 100, accepted) and `nd2_reject_nesting_101`.
*Suggestion:* say "no record may be at depth greater than 100".
*Likelihood:* negligible.

## 4. §3.1: are ignored duplicate tags still checked?

> **Tags** not in the table below are ignored: neither their field types nor
> their values are read or checked. Of duplicate tags in an IFD, the first is
> used and the others are ignored.
>
> **Checks on every IFD read.** For every tag of the table present in an IFD …

A second `ImageWidth` entry with field type 99, or with a value outside the
file, is "a tag of the table present in an IFD". It is also "ignored".
*Decision:* "ignored" means the same as for unknown tags (not checked), so
only the first entry of each tag is checked. `edge_duplicate_tags.tif` has
two well-formed entries and does not tell the readings apart.
*Suggestion:* say explicitly that later duplicates are not checked.
*Likelihood:* low. Duplicate tags are rare, and malformed duplicates rarer.

## 5. §3.1: is a SubIFD's next-IFD offset field read?

> Only the listed offsets are read: a SubIFD's own next-IFD offset and SubIFDs
> are ignored.

It is unclear whether the 4 or 8 bytes after a SubIFD's entries must lie
within the file. That is the case when the SubIFD ends exactly at the end of
the file.
*Decision:* the field is not read, so it need not lie within the file. On the
main chain it is read and must lie within the file.
*Likelihood:* negligible.

## 6. §4.1/§1.2: the data length `d` of chunk headers that are read

§1.2 rejects "an offset or length above 2^53 − 1". §4.1 lists the per-chunk
checks as "(≤ 2^53 − 1, chunk magic)" for the offset only. For the
lowest-numbered and highest-numbered uncompressed frames, `d` is only compared
with `8 + uiHeight × uiWidthBytes`. One implementer may also reject `d > 2^53
− 1` there; another may not.
*Decision:* reject any header that is read and has `d > 2^53 − 1`, following
§1.2.
*Likelihood:* negligible.

## 7. §3.2: UUID text, when references are decoded relative to removing skipped sections

> its **text**: … the characters of `X` from the end of the `UUID` tag to the
> start of the next tag, with skipped sections removed, references decoded as
> in attribute values, and leading and trailing whitespace removed.

The text does not say whether references are decoded after the pieces around
a skipped section are joined, or in each piece separately. `&am<!---->p;`
gives `&` under the first reading and `&am` + `p;` under the second. The rule
also differs from XML, where CDATA content is text, but that part is
explicit.
*Decision:* join the pieces, then decode, then trim, in the order the text
lists them.
*Likelihood:* negligible.

## 8. §3.1: integer magnitude check on ImageDescription

> each of its values of an integer type MUST be at most 2^53 − 1 in magnitude

This applies to ImageDescription too: kind "text", but any field type is
allowed. So an ImageDescription stored as LONG8, SLONG8 or IFD8 must have its
values read and range-checked in every IFD. An implementer who treats a "text"
tag as opaque bytes would skip that check.
*Decision:* check it. For SLONG8, the magnitude is the absolute value.
*Likelihood:* negligible.

## Not issues, recorded for comparison

- §4.4 row blocks: a 1-row block is a single range, so its payload is a
  `Range` message, not a `Concat`. h is found by testing divisors of
  `uiHeight` from the largest down, against the last block of the present
  frame with the highest start. Payload size never decreases as offsets grow,
  so that block is the largest.
- §4.3 `pPeriodValid` is read, as a list of flags, whenever an eType-8 node
  has `uLoopPars`, even if `pPeriod` is absent. It is not read on other
  nodes.
- §3.2 `PhysicalSize*`: the grammar is checked first, then the value is
  converted with correct rounding. Values that overflow to infinity, or round
  to 0, count as absent.
