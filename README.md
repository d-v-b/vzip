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
