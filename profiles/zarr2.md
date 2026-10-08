# Virtualizing Zarr v2 hierarchies

The Zarr v2 profile of [VIRTUALIZE.md](../VIRTUALIZE.md) (revision 16),
numbered as its §10. §1 is in VIRTUALIZE.md and applies here, in particular
the store input rules of §1.4–§1.6, and so does §2.

Profile version: 1 · Convention: [conventions/zarr2](../conventions/zarr2/README.md),
version 1. The output has the convention's layout for the input
([VIRTUALIZE.md §2](../VIRTUALIZE.md#2-the-zarr-layout)); this profile says
how the store is read, which inputs are rejected, and how each chunk
references the store. "The convention §n" below is a section of
conventions/zarr2/README.md.

## 10. Zarr v2 profile

The profile was chosen because the store's root has a `.zarray` or a
`.zgroup` and the store does not declare OME-NGFF 0.4 (§1.4); a store that
does is read by the OME-Zarr profile (§11, [ome-zarr.md](ome-zarr.md)),
which builds on this one. The store is listed (§1.5), and the documents
that [the convention §2](../conventions/zarr2/README.md#2-nodes) reads are
read as JSON (§1.6).

**Consolidated metadata.** A `.zmetadata` object (consolidated metadata) is
never read: nodes come from the listing and documents from their own
objects, so stale consolidated metadata cannot change the output. (Chunks
must come from the listing anyway, since `.zmetadata` does not list them.)

### 10.1 Rejection

The input is rejected when a rule of §1.4–§1.6 fails, and when the
convention gives it no layout: wherever it says that the input is
rejected, or that something MUST hold and it does not.

### 10.2 Chunks

Every chunk that the convention says is present
([the convention §3.2](../conventions/zarr2/README.md#32-chunks)) is an
entry under its own key, referencing the whole chunk object through its own
`url` source (§1.4). A chunk object of size 0 has no entry.

### 10.3 Summary

This section is informative. Both implementations print a one-line JSON
summary: `groups` (explicit and implicit), `arrays`, `chunks` (chunk
entries), `emptyChunks` (chunk objects of size 0), `objects` (the store's
objects) and `listingRequests`.
