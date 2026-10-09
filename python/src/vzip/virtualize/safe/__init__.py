"""The Sentinel-2 SAFE profile (spec/virtualize.md, §12)."""

from vzip.virtualize.safe.virtualize import SafeOutput, is_zip, virtualize_safe_store, virtualize_safe_zip

__all__ = ["SafeOutput", "is_zip", "virtualize_safe_store", "virtualize_safe_zip"]
