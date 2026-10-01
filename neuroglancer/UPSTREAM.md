# Neuroglancer fork with vzip support

This directory is a fork of [google/neuroglancer](https://github.com/google/neuroglancer),
vendored from upstream commit `60c866eee95b0913d48d5d657ec8030f841152c3`
(2026-09-28), without its git history.

The only changes are the `vzip:` kvstore driver and what it needs; see the
git history of this directory after the vendoring commit. To update from
upstream, re-vendor a newer commit and re-apply those commits.
