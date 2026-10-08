# Migrating OME-Zarr 0.4 to OME-Zarr 0.5

The OME-Zarr profile of [VIRTUALIZE.md](../VIRTUALIZE.md) (revision 16),
numbered as its §11. §1 is in VIRTUALIZE.md and applies here, in particular
the store input rules of §1.4–§1.6, and so does §2. The profile builds on
the Zarr v2 profile ([zarr2.md](zarr2.md), §10).

Profile version: 1 · Convention: [conventions/ome-zarr](../conventions/ome-zarr/README.md),
version 1. The output has the convention's layout for the input
([VIRTUALIZE.md §2](../VIRTUALIZE.md#2-the-zarr-layout)); this profile says
how the store is read, which inputs are rejected, and how each chunk
references the store. "The convention §n" below is a section of
conventions/ome-zarr/README.md.

## 11. OME-Zarr profile

The profile was chosen because the store's root has a `.zarray` or a
`.zgroup` and the store declares OME-NGFF 0.4 (§1.4). The store is read
as §10 reads it.

### 11.1 Rejection

The input is rejected when a rule of §10 rejects it, and when the
convention gives it no layout: wherever it says that the input is
rejected, or that something MUST hold and it does not (in particular, a
rule of [its §4](../conventions/ome-zarr/README.md#4-validation)).

### 11.2 Chunks and OME-XML

Chunks are §10's: each nonempty chunk object is an entry under its own key,
referencing the whole object through its own `url` source (§1.4). Each
OME-XML object of [the convention §7](../conventions/ome-zarr/README.md#7-chunks-and-ome-xml)
is referenced whole in the same way: an entry under its key, with its own
source, in the order of §1.4 among the chunk entries (an object of size 0
has no entry).

### 11.3 Summary

This section is informative. Both implementations print a one-line JSON
summary: `groups` (explicit and implicit), `arrays`, `chunks` (chunk
entries), `emptyChunks` (chunk objects of size 0), `objects` (the store's
objects), `images`, `labels` (label images), `droppedLabelLevels` (the
datasets L7 drops, over all label images), `plates`, `wells`, `fields`
(the images wells list), `omeXml` (OME-XML entries) and `listingRequests`.

