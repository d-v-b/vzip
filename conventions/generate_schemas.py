"""Writes conventions/<profile>/schema.json for each virtualization convention.

    uv run python conventions/generate_schemas.py

Each schema validates the zarr.json of one node of a virtual hierarchy that
declares the convention (conventions/<profile>/README.md): the convention's
metadata object in `zarr_conventions`, the property `vzip_virtualized`, and no
other attribute besides `ome`. The root carries the property's provenance
members; any other node (store profiles only) carries only the profile's
source-metadata member. SOURCE_METADATA holds each profile's member schema.
"""

from __future__ import annotations

import json
from pathlib import Path

from vzip.virtualize.common import CONVENTION_KEY, PROFILES, convention

HERE = Path(__file__).parent
STORES = {"n5", "zarr2", "ome-zarr"}

# The schema of each profile's source-metadata member (README "Source metadata").
SOURCE_METADATA: dict[str, dict] = {
    "tiff": {
        "type": "object",
        "properties": {
            "byte_order": {"enum": ["little", "big"]},
            "bigtiff": {"type": "boolean"},
            "ifds": {"type": "array", "description": "The main chain's IFDs, in order", "items": {"$ref": "#/$defs/ifd"}},
        },
        "required": ["byte_order", "bigtiff", "ifds"],
        "additionalProperties": False,
    },
    "ndpi": {
        "type": "object",
        "properties": {
            "ifds": {"type": "array", "description": "The main chain's IFDs, in order", "items": {"$ref": "#/$defs/ifd"}},
        },
        "required": ["ifds"],
        "additionalProperties": False,
    },
    "nd2": {
        "type": "object",
        "properties": {
            "signature": {"type": "string", "description": "The signature chunk's data up to its first NUL, e.g. Ver3.0"},
            "chunks": {
                "type": "object",
                "description": "Every chunk of the chunk map but the frames, by name",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "lv": {"type": "object", "description": "A metadata chunk's lite-variant structure"},
                        "data": {"type": "string", "contentEncoding": "base64"},
                        "size": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "string", "pattern": "^[0-9]+$"}]},
                    },
                    "maxProperties": 1,
                    "additionalProperties": False,
                },
            },
        },
        "required": ["signature", "chunks"],
        "additionalProperties": False,
    },
    "dicom": {
        "type": "object",
        "properties": {
            "meta": {"$ref": "#/$defs/dataset", "description": "The File Meta Information"},
            "dataset": {"$ref": "#/$defs/dataset", "description": "The dataset, up to and including Pixel Data"},
        },
        "required": ["meta", "dataset"],
        "additionalProperties": False,
    },
    "nifti": {
        "type": "object",
        "properties": {
            "nifti_version": {"enum": [1, 2]},
            "byte_order": {"enum": ["little", "big"]},
            "header": {
                "type": "object",
                "description": "Every header field, by its NIfTI-1 or NIfTI-2 name, in the standard's order",
                "additionalProperties": {"$ref": "#/$defs/headerValue"},
            },
            "extensions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"ecode": {"type": "integer"}, "edata": {"type": "string", "contentEncoding": "base64"}},
                    "required": ["ecode", "edata"],
                    "additionalProperties": False,
                },
            },
            "extensions_truncated": {"const": True},
            "scaling": {
                "type": "object",
                "properties": {"slope": {"type": "number"}, "inter": {"type": "number"}},
                "required": ["slope", "inter"],
                "additionalProperties": False,
            },
        },
        "required": ["nifti_version", "byte_order", "header"],
        "additionalProperties": False,
    },
    "ims": {
        "type": "object",
        "properties": {
            "root": {"$ref": "#/$defs/attributes"},
            "DataSetInfo": {"type": "object", "additionalProperties": {"$ref": "#/$defs/attributes"}},
            "DataSet": {
                "type": "object",
                "description": "Each channel group, by its path under DataSet",
                "additionalProperties": {"$ref": "#/$defs/attributes"},
            },
        },
        "required": ["root", "DataSetInfo", "DataSet"],
        "additionalProperties": False,
    },
    "n5": {"type": "object", "description": "The node's attributes.json, whole"},
    "zarr2": {"type": "object", "description": "The node's .zattrs, as copied"},
    "ome-zarr": {"type": "object", "description": "The group's .zattrs without its OME-NGFF 0.4 members, or an array's .zattrs, as copied"},
}

# An IFD and its tags (conventions/tiff/README.md §5).
TIFF_DEFS = {
    "ifd": {
        "type": "object",
        "properties": {
            "tags": {
                "type": "object",
                "description": "Each tag by its number in decimal",
                "propertyNames": {"pattern": "^(0|[1-9][0-9]*)$"},
                "additionalProperties": {"$ref": "#/$defs/tag"},
            },
            "subifds": {"type": "array", "items": {"$ref": "#/$defs/ifd"}},
        },
        "required": ["tags"],
        "additionalProperties": False,
    },
    "tag": {
        "type": "object",
        "properties": {
            "type": {"type": "integer", "minimum": 0, "maximum": 65535},
            "count": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "string", "pattern": "^[0-9]+$"}]},
            "value": {"anyOf": [
                {"type": "string"},
                {"type": "array", "items": {"anyOf": [
                    {"type": "number"}, {"type": "string"},
                    {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}]}},
            ]},
        },
        "required": ["type", "count"],
        "additionalProperties": False,
    },
}

# A dataset in the DICOM JSON Model (PS3.18 §F.2), as conventions/dicom/README.md §5 writes it.
DICOM_DEFS = {
    "dataset": {
        "type": "object",
        "propertyNames": {"pattern": "^[0-9A-F]{8}$"},
        "additionalProperties": {"$ref": "#/$defs/attribute"},
    },
    "attribute": {
        "type": "object",
        "properties": {
            "vr": {"type": "string", "pattern": "^[A-Z]{2}$"},
            "Value": {"type": "array"},
            "InlineBinary": {"type": "string", "contentEncoding": "base64"},
        },
        "required": ["vr"],
        "not": {"required": ["Value", "InlineBinary"]},
        "additionalProperties": False,
    },
}

# An HDF5 object's attributes (conventions/ims/README.md §5), or null if they could not be read.
IMS_DEFS = {
    "attributes": {
        "type": ["object", "null"],
        "additionalProperties": {
            "anyOf": [
                {"type": "null"},
                {"type": "string"},
                {"type": "array", "items": {"type": ["number", "string"]}},
                {
                    "type": "object",
                    "properties": {"class": {"type": "integer"}, "size": {"type": "integer"},
                                   "data": {"type": "string", "contentEncoding": "base64"}},
                    "required": ["class", "size", "data"],
                    "additionalProperties": False,
                },
            ]
        },
    },
}


def schema(profile: str) -> dict:
    cmo = convention(profile)
    title = PROFILES[profile][2]
    url = ({"pattern": "^[Hh][Tt][Tt][Pp][Ss]?://[^?#]*/$",
            "description": "The store URL: http or https, its path ending in /, no query or fragment"}
           if profile in STORES else {"description": "The URL of the source file"})
    root = {
        "type": "object",
        "description": "The root's property: the profile and version that produced the hierarchy, its source, "
                       "and the root's source metadata",
        "properties": {
            "profile": {"type": "string", "const": profile},
            "version": {"type": "integer", "const": PROFILES[profile][1]},
            "source": {"type": "object", "properties": {"url": {"type": "string", **url}}, "required": ["url"],
                       "additionalProperties": False},
            profile: {"$ref": "#/$defs/sourceMetadata"},
        },
        "required": ["profile", "version", "source"],
        "additionalProperties": False,
    }
    node = {
        "type": "object",
        "description": "Any other node's property: its source metadata only",
        "properties": {profile: {"$ref": "#/$defs/sourceMetadata"}},
        "required": [profile],
        "additionalProperties": False,
    }
    prop = {"oneOf": [{"$ref": "#/$defs/rootProperty"}, {"$ref": "#/$defs/nodeProperty"}]} \
        if profile in STORES else {"$ref": "#/$defs/rootProperty"}
    defs = {
        "conventionMetadata": {
            "type": "object",
            "description": f"The {title} convention's metadata object in zarr_conventions",
            "properties": {k: {"type": "string", "const": v} for k, v in cmo.items()},
            "required": ["uuid"],
            "additionalProperties": False,
        },
        "rootProperty": root,
        "sourceMetadata": SOURCE_METADATA[profile],
        "headerValue": {
            "description": "A translated header value (conventions/README.md §6)",
            "anyOf": [{"type": "number"}, {"type": "string"},
                      {"type": "array", "items": {"anyOf": [{"type": "number"}, {"type": "string"}]}}],
        },
    }
    if profile in ("tiff", "ndpi"):
        defs.update(TIFF_DEFS)
    if profile == "dicom":
        defs.update(DICOM_DEFS)
    if profile == "ims":
        defs.update(IMS_DEFS)
    if profile in STORES:
        defs["nodeProperty"] = node
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": cmo["schema_url"],
        "title": f"vzip {title} convention, version {PROFILES[profile][1]}",
        "description": f"The zarr.json of a node that declares the vzip {title} convention ({cmo['spec_url']})",
        "type": "object",
        "properties": {
            "zarr_format": {"type": "integer", "const": 3},
            "node_type": {"type": "string", "enum": ["group", "array"] if profile in STORES else ["group"]},
            "attributes": {
                "type": "object",
                "properties": {
                    "ome": {"type": "object", "description": "OME-NGFF 0.5 metadata"},
                    "zarr_conventions": {
                        "type": "array",
                        "contains": {"$ref": "#/$defs/conventionMetadata"},
                        "minContains": 1,
                        "maxContains": 1,
                    },
                    CONVENTION_KEY: prop,
                },
                "required": ["zarr_conventions", CONVENTION_KEY],
                "additionalProperties": False,
            },
        },
        "required": ["zarr_format", "node_type", "attributes"],
        "$defs": defs,
    }


def main() -> None:
    for profile in PROFILES:
        d = HERE / profile
        d.mkdir(exist_ok=True)
        (d / "schema.json").write_text(json.dumps(schema(profile), indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(PROFILES)} schemas")


if __name__ == "__main__":
    main()
