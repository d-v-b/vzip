# vzip: virtual zip

## about

A spec for storing two kinds of things in a Zip archive:
1. bytes
2. ranges of bytes in external objects

read the [pitch](https://github.com/d-v-b/vzip/blob/main/PITCH.md), or the [spec](https://github.com/d-v-b/vzip/blob/main/SPEC.md). there are some implementations here too.

## status

experimental, proof of concept, anything can change

## how this was made

I prompted Claude to explore serialization formats for the kind of virtual zarr stores created by [VirtualiZarr](https://virtualizarr.readthedocs.io/en/stable/index.html). Key to the prompt was the goal
of re-using "boring" technology like Zip archives. Once Claude had cooked up a rough spec, I instructed Claude to have subagents write Python, Typescript, and Rust implementations, and to take notes along the way. 
This ran in a loop, refining the spec each time. After 7 revs, we got something convergent.

## license

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT License ([LICENSE-MIT](LICENSE-MIT))

at your option. This covers the specification, the schema, and all code in
this repository.

Unless you explicitly state otherwise, any contribution you intentionally
submit for inclusion in this repository, as defined in the Apache-2.0
license, is dual licensed as above, without any additional terms or
conditions.

### third-party data

Some files in `experiments/out/` derive from IDR study
[idr0096](https://doi.org/10.17867/10000170), "Quantification of Bone Marrow
Compartments in Histological Sections" (Tratwal et al.,
[doi:10.3389/fendo.2020.00480](https://doi.org/10.3389/fendo.2020.00480)),
licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/):

- `idr0096_4000_d11_m5_LT_2.vzip` and `ng_idr/idr0096_4000_d11_m5_LT_2_unpinned.vzip`
  embed the image's OME-XML metadata;
- the screenshots in `ng_idr/` and `web_demo/` show the image.

These files are not covered by the licenses above.
