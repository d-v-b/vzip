"""Writes spec/virtualize/<profile>/schema.json for each virtualization convention.

    uv run python spec/generate_schemas.py

Each schema validates the zarr.json of one node of a virtual hierarchy that
declares the convention (spec/virtualize/<profile>.md): the convention's
metadata object in `zarr_conventions`, the property `vzip_virtualized`, and no
other attribute besides `ome`. The root carries the property's provenance
members; any other node (store profiles only) carries only the profile's
source-metadata member. SOURCE_METADATA holds each profile's member schema.
"""

from __future__ import annotations

import json
from pathlib import Path

from vzip.virtualize.common import CONVENTION_KEY, PROFILES, REVISION, convention

HERE = Path(__file__).parent
STORES = {"n5", "zarr2", "ome-zarr"}

# The schema of each profile's source-metadata member (README "Source metadata").
SOURCE_METADATA: dict[str, dict] = {
    "tiff": {"anyOf": [
        {
            "type": "object",
            "description": "The root's: the file's byte order and variant",
            "properties": {"byte_order": {"enum": ["little", "big"]}, "bigtiff": {"type": "boolean"}},
            "required": ["byte_order", "bigtiff"],
            "additionalProperties": False,
        },
        {"$ref": "#/$defs/mirror"},
        {"$ref": "#/$defs/mirrorView"},
    ]},
    "ndpi": {"oneOf": [{"$ref": "#/$defs/sourceNode"}, {"$ref": "#/$defs/ifd"}]},
    "nd2": {"anyOf": [
        {
            "type": "object",
            "description": "The root's: the signature and the decoded chunks it keeps (spec/virtualize/nd2.md §5)",
            "properties": {
                "signature": {"$ref": "#/$defs/text", "description": "The signature chunk's data up to its first NUL"},
                "chunks": {
                    "type": "object",
                    "description": "Each decoded chunk (lite variant or XML variant) the root keeps, by name, as a "
                                   "JSON tree: an object, or [name, value] pairs when a name repeats, when a name is "
                                   "an array index (0, 1, ... without leading zeros), or when the object would read "
                                   "as a tag ({\"utf16\": B}, {\"int\": digits}, {\"float\": "
                                   "\"NaN\"|\"Infinity\"|\"-Infinity\"|\"-0\"}); at most 65536 bytes of JSON in all",
                    "additionalProperties": {"type": ["object", "array"]},
                },
            },
            "required": ["signature", "chunks"],
            "additionalProperties": False,
        },
        {"$ref": "#/$defs/mirror"},
        {"$ref": "#/$defs/mirrorView"},
    ]},
    "dicom": {
        "type": "object",
        "properties": {
            "preamble": {"type": "string", "contentEncoding": "base64",
                         "description": "The 128-byte preamble, unless all zero"},
            "meta": {"$ref": "#/$defs/dataset",
                     "description": "The File Meta Information (on vzip_source: its large attributes)"},
            "dataset": {"$ref": "#/$defs/dataset",
                        "description": "The dataset (on vzip_source: the per-frame groups and the large attributes)"},
            "pixel_extra": {"type": "string",
                            "description": "The array holding the native pixel data after the frames"},
            "pixel_unreferenced": {"type": "string",
                                   "description": "The family holding the pixel data the Extended Offset Table skips"},
            "pixel_unreferenced_headers": {"const": True,
                                           "description": "Whether pixel_unreferenced also holds the frames' item "
                                                          "headers"},
            "pixel_fragments": {"type": "string",
                                "description": "The array holding each fragment's frame and data length, when a "
                                               "frame has more than one"},
            "pixel_offset_table": {"const": True, "description": "Whether the Basic Offset Table is not empty"},
            "trailing": {"type": "string", "description": "The array holding the bytes after the last element"},
        },
        "additionalProperties": False,
    },
    "nifti": {
        "type": "object",
        "description": "The root's source metadata",
        "properties": {
            "nifti_version": {"enum": [1, 2]},
            "byte_order": {"enum": ["little", "big"]},
            "header": {
                "type": "object",
                "description": "Every header field, by its NIfTI-1 or NIfTI-2 name, in the standard's order",
                "additionalProperties": {"anyOf": [
                    {"$ref": "#/$defs/headerValue"},
                    {"type": "object", "properties": {"bits": {"type": "string", "pattern": "^([0-9a-f]{8}|[0-9a-f]{16})$"}},
                    "required": ["bits"], "additionalProperties": False,
                    "description": "A float's IEEE 754 bits, most significant first: a negative zero, or a NaN "
                                   "other than the canonical one"},
                    {"type": "array", "items": {"anyOf": [{"type": "number"}, {"type": "string"},
                                                          {"type": "object", "properties": {"bits": {"type": "string", "pattern": "^([0-9a-f]{8}|[0-9a-f]{16})$"}},
                                                          "required": ["bits"], "additionalProperties": False,
                                                          "description": "A float's IEEE 754 bits, most significant first: a negative zero, or a NaN "
                                                                         "other than the canonical one"}]}},
                ]},
            },
            "header_rest": {
                "type": "object",
                "description": "The bytes after the first NUL of a character field, where not all NUL",
                "additionalProperties": {"type": "string", "contentEncoding": "base64"},
            },
            "extender": {"type": "string", "contentEncoding": "base64",
                         "description": "The 4 extender bytes, unless 00 00 00 00 or 01 00 00 00"},
            "extensions": {"oneOf": [
                {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ecode": {"type": "integer"},
                            "text": {"$ref": "#/$defs/text"},
                            "esize": {"type": "integer", "minimum": 8,
                                      "description": "A text extension's esize, when not the fewest NULs'"},
                            "edata": {"type": "string", "contentEncoding": "base64"},
                            "data": {"type": "string",
                                     "description": "The array of vzip_source that holds the data"},
                        },
                        "required": ["ecode"],
                        "maxProperties": 3,
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "object",
                    "description": "Over the root's budget: the ecodes and esizes, the text extensions that fit, "
                                   "and the others' data as a family of byte values",
                    "properties": {
                        "ecode": {"const": "vzip_source/extensions/ecode"},
                        "esize": {"const": "vzip_source/extensions/esize"},
                        "data": {"const": "vzip_source/extensions/data"},
                        "text": {"type": "object", "description": "The text values, by the extension's index",
                                 "propertyNames": {"pattern": "^(0|[1-9][0-9]*)$"},
                                 "additionalProperties": {"$ref": "#/$defs/text"}, "minProperties": 1},
                    },
                    "required": ["ecode", "esize", "data"],
                    "additionalProperties": False,
                },
            ]},
            "extensions_truncated": {"const": True},
            "unparsed": {"type": "string"},
            "trailing": {"type": "string"},
            "affine": {
                "type": "object",
                "properties": {"form": {"enum": ["sform", "qform"]}, "applied": {"type": "boolean"}},
                "required": ["form", "applied"],
                "additionalProperties": False,
            },
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
        "description": "An HDF5 group's or dataset's description, on its node under vzip_source/hdf5",
        "properties": {
            "attributes": {"$ref": "#/$defs/attributes"},
            "attribute_collisions": {"$ref": "#/$defs/attributeCollisions"},
            "names": {"type": "object", "description": "The members' names that are not UTF-8, by key",
                      "additionalProperties": {"$ref": "#/$defs/text"}},
            "collisions": {"type": "array", "items": {"$ref": "#/$defs/text"},
                           "description": "The names of members that read like an earlier member's"},
            "links": {
                "type": "object",
                "description": "The members that are links other than a hard link to an object met first",
                "additionalProperties": {"oneOf": [
                    {"type": "object", "properties": {"soft": {"$ref": "#/$defs/text"}}, "required": ["soft"],
                     "additionalProperties": False},
                    {"type": "object", "properties": {"hard": {"type": "integer", "minimum": 0,
                                                               "description": "The object's index in the object table"}},
                     "required": ["hard"], "additionalProperties": False},
                    {"type": "object", "properties": {"external": {
                        "type": "object", "properties": {"file": {"$ref": "#/$defs/text"}, "path": {"$ref": "#/$defs/text"}},
                        "required": ["file", "path"], "additionalProperties": False}},
                     "required": ["external"], "additionalProperties": False},
                    {"type": "object", "properties": {"user": {
                        "type": "object", "properties": {"type": {"type": "integer", "minimum": 2, "maximum": 255},
                                                         "value": {"type": ["string", "null"], "contentEncoding": "base64"}},
                        "required": ["type", "value"], "additionalProperties": False}},
                     "required": ["user"], "additionalProperties": False},
                ]},
            },
            "images": {
                "type": "object",
                "description": "The members that are the image's Data datasets",
                "additionalProperties": {
                    "type": "object",
                    "properties": {"level": {"type": "integer", "minimum": 0}, "t": {"type": "integer", "minimum": 0},
                                   "c": {"type": "integer", "minimum": 0},
                                   "shape": {"type": "array", "items": {"type": "integer", "minimum": 0},
                                             "minItems": 3, "maxItems": 3,
                                             "description": "The dataset's dimensions, padding included"},
                                   "attributes": {"$ref": "#/$defs/attributes"},
                                   "attribute_collisions": {"$ref": "#/$defs/attributeCollisions"}},
                    "required": ["level", "t", "c", "shape"],
                    "additionalProperties": False,
                },
            },
            "datatypes": {
                "type": "object",
                "description": "The members that are named datatypes",
                "additionalProperties": {
                    "type": "object",
                    "properties": {"datatype": {"$ref": "#/$defs/datatype"}, "attributes": {"$ref": "#/$defs/attributes"},
                                   "attribute_collisions": {"$ref": "#/$defs/attributeCollisions"}},
                    "required": ["datatype"],
                    "additionalProperties": False,
                },
            },
            "unsupported": {"type": "object", "description": "The members not mapped: why, and what can be read",
                            "additionalProperties": {"$ref": "#/$defs/unsupported"}},
            "overflow": {"type": "integer", "minimum": 1,
                         "description": "The number of members beyond the walk's 100,000 objects plus 5 per Data dataset of the image, the root included"},
            "datatype": {"$ref": "#/$defs/datatype"},
            "named": {"$ref": "#/$defs/named"},
            "shape": {"type": ["array", "null"], "items": {"type": "integer", "minimum": 0},
                      "description": "A family's or a null dataset's dimensions (null for a null dataspace)"},
            "fill": {"type": "string", "contentEncoding": "base64",
                     "description": "The fill value's bytes, when the Zarr fill value is not it"},
            "spilled": {"type": "string", "pattern": "^spilled/[0-9]+$",
                        "description": "The family of vzip_source that holds the entries over the document's budget, "
                                       "each a JSON text (spec/virtualize/ims.md §5.6)"},
        },
        "additionalProperties": False,
    },
    "n5": {
        "type": "object",
        "properties": {
            "attributes": {"type": "object", "minProperties": 1,
                           "description": "The node's attributes.json without the layout members"},
            "empty": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                      "description": "vzip_source only: the keys of the store's empty objects, empty chunk objects "
                                     "included"},
            "ignored": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                        "description": "vzip_source only: the recorded keys of the listed objects that spec/virtualize.md "
                                       "§1.4 ignores"},
            "metadata": {
                "type": "object", "minProperties": 1,
                "description": "The layout members the Zarr metadata does not reproduce",
                "properties": {"n5": {"description": "The member n5 (the container's version)"},
                               "compression": {"type": "object", "minProperties": 1,
                                               "description": "The compression's members the codec does not carry"},
                               "compressionType": {"description": "compressionType, when compression is present"}},
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    },
    "zarr2": {
        "type": "object",
        "properties": {
            "attributes": {"type": "object", "minProperties": 1, "description": "The node's .zattrs, as copied"},
            "empty": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                      "description": "vzip_source only: the keys of the store's empty objects, empty chunk objects "
                                     "included"},
            "ignored": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                        "description": "vzip_source only: the recorded keys of the listed objects that spec/virtualize.md "
                                       "§1.4 ignores"},
            "metadata": {"type": "object", "minProperties": 1,
                         "description": "The members of .zarray or .zgroup the Zarr metadata does not reproduce"},
        },
        "additionalProperties": False,
    },
    "ome-zarr": {
        "type": "object",
        "properties": {
            "attributes": {"type": "object", "minProperties": 1,
                           "description": "The node's .zattrs, without the OME members the ome object gives back"},
            "unversioned": {"type": "array", "minItems": 1, "uniqueItems": True,
                            "items": {"enum": ["multiscales", "omero", "image-label", "plate", "well"]},
                            "description": "The OME members that had no version, which ome gives back as they are"},
            "empty": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                      "description": "vzip_source only: the keys of the store's empty objects, empty chunk objects "
                                     "included"},
            "ignored": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                        "description": "vzip_source only: the recorded keys of the listed objects that spec/virtualize.md "
                                       "§1.4 ignores"},
            "metadata": {"type": "object", "minProperties": 1,
                         "description": "The members of .zarray or .zgroup the Zarr metadata does not reproduce"},
        },
        "additionalProperties": False,
    },
}

# An IFD and its tags (spec/virtualize/tiff.md §5).
TAG_MEMBERS = {
    "type": {"type": "integer", "minimum": 0, "maximum": 65535},
    "count": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "string", "pattern": "^[0-9]+$"}]},
    "value": {"anyOf": [
        {"$ref": "#/$defs/text"},
        {"type": "array", "items": {"anyOf": [
            {"type": "number"}, {"type": "string"},
            {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}]}},
    ]},
    "field": {"type": "string", "contentEncoding": "base64",
              "description": "The value field of an entry whose field type TIFF does not define"},
}
TIFF_DEFS = {
    "sourceNode": {
        "type": "object",
        "description": "vzip_source's: the number of IFDs in the main chain, each a group at ifds/<i>",
        "properties": {"ifd_count": {"type": "integer", "minimum": 1}},
        "required": ["ifd_count"],
        "additionalProperties": False,
    },
    "ifd": {
        "type": "object",
        "description": "An IFD's group",
        "properties": {
            "tags": {
                "type": "object",
                "description": "Each tag but the layout and pointer tags, by its number in decimal",
                "propertyNames": {"pattern": "^(0|[1-9][0-9]*)$"},
                "additionalProperties": {"$ref": "#/$defs/tag"},
            },
            "pointers": {
                "type": "object",
                "description": "Each pointer tag, by its number in decimal",
                "propertyNames": {"pattern": "^(0|[1-9][0-9]*)$"},
                "additionalProperties": {"$ref": "#/$defs/pointer"},
            },
            "duplicates": {"type": "array", "items": {"$ref": "#/$defs/duplicate"},
                           "description": "The entries after the first of their tag"},
            "record": {"type": "integer", "minimum": 0,
                       "description": "The IFD's record number, when a pointer tag's array refers to it"},
            "same_as": {
                "type": "object",
                "description": "Each array or family of bytes the IFD shares with one kept earlier, by its name: "
                               "the earlier one's path under vzip_source",
                "propertyNames": {"pattern": "^(data|strips|jpeg_q_tables|jpeg_dc_tables|jpeg_ac_tables|0|[1-9][0-9]*)$"},
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["tags"],
        "additionalProperties": False,
    },
    "tag": {
        "type": "object",
        "properties": TAG_MEMBERS,
        "required": ["type", "count"],
        "additionalProperties": False,
    },
    "duplicate": {
        "type": "object",
        "properties": {"tag": {"type": "integer", "minimum": 0, "maximum": 65535}, **TAG_MEMBERS},
        "required": ["tag", "type", "count"],
        "additionalProperties": False,
    },
    "pointer": {
        "type": "object",
        "properties": {
            "type": TAG_MEMBERS["type"],
            "count": TAG_MEMBERS["count"],
            "ifds": {"type": "array", "items": {"type": ["string", "null"]}, "maxItems": 64,
                     "description": "For each value (at most 64; with more, they are the array <path>/<tag> of "
                                    "record numbers), the path under vzip_source of the IFD it leads to, or null"},
        },
        "required": ["type", "count"],
        "additionalProperties": False,
    },
}

# A dataset in the DICOM JSON Model (PS3.18 §F.2), as spec/virtualize/dicom.md §5 writes it.
DICOM_DEFS = {
    "dataset": {
        "type": "object",
        "properties": {
            "duplicates": {
                "type": "array",
                "description": "The later elements with a tag already in the dataset, in file order",
                "items": {
                    "type": "object",
                    "patternProperties": {"^[0-9A-F]{8}$": {"$ref": "#/$defs/attribute"}},
                    "additionalProperties": False,
                    "minProperties": 1,
                    "maxProperties": 1,
                },
            },
        },
        "patternProperties": {"^[0-9A-F]{8}$": {"$ref": "#/$defs/attribute"}},
        "additionalProperties": False,
    },
    "attribute": {
        "type": "object",
        "properties": {
            "vr": {"type": "string", "pattern": "^[A-Z]{2}$"},
            "Value": {"type": "array"},
            "InlineBinary": {"type": "string", "contentEncoding": "base64"},
            "BulkDataURI": {"type": "string",
                            "description": "The array (or family) of vzip_source that holds the value"},
            "Structures": {"type": "array", "items": {"$ref": "#/$defs/dataset"},
                           "description": "A gathered sequence's distinct item structures; "
                                          "vzip_source/<its path>/items/structure gives each item's"},
            "Gathered": {"const": True,
                         "description": "A value in an item of a gathered sequence, at vzip_source/<the "
                                        "sequence's path>/items/<path within the item>/value, row or member "
                                        "<item index>"},
            "LittleEndian": {"const": True,
                             "description": "An explicit UN value in explicit VR big endian, whose bytes are in "
                                            "little endian"},
        },
        "required": ["vr"],
        "maxProperties": 3,
        "additionalProperties": False,
    },
}

# An HDF5 object's attributes and datatypes (spec/virtualize/ims.md §5).
IMS_DEFS = {
    "attributes": {
        "type": ["object", "null"],
        "description": "An object's attributes by key, or null if they could not be read",
        "additionalProperties": {"$ref": "#/$defs/attributeValue"},
    },
    "attributeValue": {
        "description": "An attribute's value (spec/virtualize/ims.md §5.3): Imaris's form, a text value, "
                       "or its datatype with its value, data, array or the reason it is not kept",
        "anyOf": [
            {"$ref": "#/$defs/text"},
            {
                "type": "object",
                "properties": {
                    "datatype": {"$ref": "#/$defs/datatype"},
                    "named": {"$ref": "#/$defs/named"},
                    "name": {"$ref": "#/$defs/text"},
                    "shape": {"type": ["array", "null"], "items": {"type": "integer", "minimum": 0}},
                    "value": {},
                    "data": {"type": "string", "contentEncoding": "base64"},
                    "array": {"type": "string", "description": "The array of vzip_source that holds the value"},
                    "json": {"type": "string",
                             "description": "The array of vzip_source that holds the value's JSON text"},
                    "reason": {"type": "string"},
                },
                "anyOf": [{"required": ["datatype"]}, {"required": ["named"]}, {"required": ["reason"]}],
                "additionalProperties": False,
            },
        ],
    },
    "attributeCollisions": {
        "type": "array",
        "description": "Attributes whose names read like an earlier attribute's, with their names",
        "items": {"allOf": [{"$ref": "#/$defs/attributeValue"}, {"type": "object", "required": ["name"]}]},
    },
    "named": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}],
              "description": "The committed datatype's index in the object table (its entry of datatypes "
                             "describes it), or null if the walk did not meet it"},
    "selection": {
        "type": "object",
        "description": "A dataspace selection (spec/virtualize/ims.md §5.3)",
        "properties": {
            "select": {"enum": ["all", "none", "points", "hyperslab"]},
            "rank": {"type": "integer", "minimum": 0},
            "points": {"type": "array"},
            "blocks": {"type": "array"},
            "start": {"type": "array"}, "stride": {"type": "array"}, "count": {"type": "array"}, "block": {"type": "array"},
        },
        "required": ["select"],
        "additionalProperties": False,
    },
    "unsupported": {
        "type": "object",
        "description": "An object that is not mapped (spec/virtualize/ims.md §5.5)",
        "properties": {
            "reason": {"type": "string"},
            "datatype": {"$ref": "#/$defs/datatype"},
            "named": {"$ref": "#/$defs/named"},
            "shape": {"type": ["array", "null"], "items": {"type": "integer", "minimum": 0}},
            "fill": {"type": "string", "contentEncoding": "base64"},
            "external": {"type": "array", "items": {
                "type": "object",
                "properties": {"file": {"$ref": "#/$defs/text"}, "offset": {"type": ["integer", "string"]},
                               "size": {"type": ["integer", "string"]}},
                "required": ["file", "offset", "size"], "additionalProperties": False}},
            "virtual": {"type": "array", "items": {
                "type": "object",
                "properties": {"file": {"$ref": "#/$defs/text"}, "dataset": {"$ref": "#/$defs/text"},
                               "source": {"$ref": "#/$defs/selection"}, "selection": {"$ref": "#/$defs/selection"}},
                "required": ["file", "dataset", "source", "selection"], "additionalProperties": False}},
            "attributes": {"$ref": "#/$defs/attributes"},
            "attribute_collisions": {"$ref": "#/$defs/attributeCollisions"},
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
    "datatype": {
        "type": "object",
        "description": "An HDF5 datatype (spec/virtualize/ims.md §5.2)",
        "properties": {
            "class": {"enum": ["integer", "float", "time", "string", "bitfield", "opaque", "compound", "reference",
                               "enum", "variable-length", "array"]},
            "size": {"type": "integer", "minimum": 0},
        },
        "required": ["class", "size"],
    },
}


_MEASURE = {"anyOf": [{"type": ["number", "string"]}, {
    "type": "object", "properties": {"value": {"type": ["number", "string"]}, "unit": {"type": "string"}},
    "required": ["value", "unit"], "additionalProperties": False}]}
_VALUE = {"type": ["number", "string"]}
_RECORDS = {"type": "array", "items": {"type": "object", "additionalProperties": _VALUE}, "minItems": 1}
SOURCE_METADATA["safe"] = {"oneOf": [
    {
        "type": "object",
        "description": "The root's: the product's identity, from the product and tile metadata (§6.3)",
        "properties": {
            **{k: {"type": "string"} for k in ("PRODUCT_URI", "PROCESSING_LEVEL", "PRODUCT_TYPE", "PROCESSING_BASELINE",
                                               "TILE_ID", "SENSING_TIME", "HORIZONTAL_CS_NAME", "HORIZONTAL_CS_CODE")},
            "Special_Values": _RECORDS,
            "U": _VALUE,
        },
        "required": ["HORIZONTAL_CS_CODE"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "A band array's: a derived convenience copied from the product metadata (§6.2), and the band "
                       "file's SIZ segment",
        "properties": {
            "IMAGE_FILE": {"type": "string"},
            "bandId": {"type": "integer", "minimum": 0},
            "physicalBand": {"type": "string"},
            "RESOLUTION": _VALUE,
            "Wavelength": {"type": "object", "properties": {k: _MEASURE for k in ("MIN", "MAX", "CENTRAL")},
                           "additionalProperties": False, "minProperties": 1},
            **{k: _VALUE for k in ("PHYSICAL_GAINS", "RADIO_ADD_OFFSET", "BOA_ADD_OFFSET")},
            **{k: _MEASURE for k in ("SOLAR_IRRADIANCE", "QUANTIFICATION_VALUE", "BOA_QUANTIFICATION_VALUE",
                                     "AOT_QUANTIFICATION_VALUE", "WVP_QUANTIFICATION_VALUE")},
            "Scene_Classification_List": _RECORDS,
            "siz": {"type": "string", "contentEncoding": "base64",
                    "description": "The band file's SIZ marker segment, which its chunks replace"},
        },
        "required": ["IMAGE_FILE", "siz"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "vzip_source's: the XML documents kept as text, the keys of those kept as arrays, of the empty "
                       "objects, the empty directories, and the keys of the ignored objects (§6.3)",
        "properties": {
            "xml": {"type": "object", "additionalProperties": {"$ref": "#/$defs/text"}, "minProperties": 1},
            "xml_arrays": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "empty": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "empty_dirs": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "ignored": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "anyOf": [{"required": ["xml"]}, {"required": ["xml_arrays"]}],
        "additionalProperties": False,
    },
]}
_GUID = {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
_DIMENSIONS = {"type": "object", "description": "The Start of each of S, B, H, I, R, V the series key has",
               "properties": {k: {"type": "integer"} for k in "SBHIRV"}, "minProperties": 1,
               "additionalProperties": False}
_ATTACHMENT = {"oneOf": [
    {
        "type": "object",
        "description": "An A1 attachment entry, and the form of its data at attachments/<k> (§5.6)",
        "properties": {
            "name": {"$ref": "#/$defs/text"},
            "content_file_type": {"$ref": "#/$defs/text"},
            "content_guid": _GUID,
            "form": {"enum": ["time_stamps", "focus_positions", "event_list", "bytes", "empty"]},
            "segment_entry": {"type": "string", "contentEncoding": "base64",
                              "description": "The attachment segment's copy of the entry, when it differs"},
        },
        "required": ["name", "content_file_type", "content_guid", "form"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "An entry of another schema: its 128 bytes",
        "properties": {"entry": {"type": "string", "contentEncoding": "base64"}},
        "required": ["entry"],
        "additionalProperties": False,
    },
]}
SOURCE_METADATA["czi"] = {"anyOf": [
    {
        "type": "object",
        "description": "The root's: the file header (spec/virtualize/czi.md §5.1)",
        "properties": {
            "version": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
            "primary_file_guid": _GUID,
            "file_guid": _GUID,
            "file_part": {"const": 0},
            "update_pending": {"type": "integer"},
        },
        "required": ["version", "primary_file_guid", "file_guid", "file_part", "update_pending"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "An image's: the dimensions that select its series (§4.2)",
        "properties": {"dimensions": _DIMENSIONS},
        "required": ["dimensions"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "description": "A tile array's: its tile position, the planes at its index 0, and its copy (§4.4)",
        "properties": {
            "dimensions": _DIMENSIONS,
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "size": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 2, "maxItems": 2},
            "stored_size": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 2,
                            "maxItems": 2},
            "planes": {"type": "object", "properties": {k: {"type": "integer"} for k in "tcz"},
                       "required": ["t", "c", "z"], "additionalProperties": False},
            "copy": {"type": "integer", "minimum": 0},
        },
        "required": ["x", "y", "size", "stored_size", "planes", "copy"],
        "additionalProperties": False,
    },
    {"$ref": "#/$defs/mirror"},
    {"$ref": "#/$defs/mirrorView"},
]}
# The IR mirror (spec/conventions.md §8) of the TIFF, ND2 and CZI conventions: vzip_source's
# source metadata, and the view documents of the groups under vzip_source/tree.
IR_PROFILES = ("tiff", "nd2", "czi")
MIRROR_DEFS = {
    "mirror": {
        "type": "object",
        "description": "vzip_source's: the IR mirror's description (spec/conventions.md §8.2)",
        "properties": {"ir": {
            "type": "object",
            "properties": {
                "version": {"const": 2},
                "size": {"type": "integer", "minimum": 0, "description": "The source's size in bytes"},
                "elements": {"type": "integer", "minimum": 1, "description": "The elements, column runs expanded"},
                "rows": {"type": "integer", "minimum": 1, "description": "The rows the table stores"},
                "names": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                          "prefixItems": [{"const": ""}],
                          "description": "The interned names, sorted; the first, \"\", is the root's (conventions §8.1)"},
                "types": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                          "prefixItems": [{"const": ""}], "description": "The interned types (§8.6)"},
                "forms": {"type": "array", "items": {"type": "string"},
                          "description": "The interned forms, each a JSON text (§8.5)"},
            },
            "required": ["version", "size", "elements", "rows", "names", "types", "forms"],
            "additionalProperties": False,
        }},
        "required": ["ir"],
        "additionalProperties": False,
    },
    "mirrorView": {
        "type": "object",
        "description": "A view group's (vzip_source/tree/...): a struct's view document (spec/conventions.md §8.7)",
        "properties": {
            "$vz": {"const": "derived", "description": "A derived element's document"},
            "$form": {"type": ["string", "null"], "description": "A derived element's form (§8.5)"},
            "$partial": {"const": True, "description": "The document stops short of the struct's children"},
        },
        "minProperties": 1,
        "additionalProperties": {"$ref": "#/$defs/viewValue"},
    },
    "viewValue": {
        "description": "A child's view: a JSON value, a nested document, or a $vz form",
        "anyOf": [
            {"type": ["string", "number", "boolean", "null", "array"]},
            {"type": "object", "properties": {"$vz": {"enum": [
                "alias", "group", "element", "derived", "int", "float", "bytes", "utf16"]}},
             "required": ["$vz"]},
            {"type": "object"},
        ],
    },
}

# The GeoZarr members a SAFE node may hold besides the convention's (spec/virtualize/safe.md §5); their
# own schemas (zarr-conventions proj, spatial and multiscales v0.1) validate them.
GEOZARR = {
    "proj:code": {"type": "string"},
    "proj:wkt2": {"type": "string"},
    **{f"spatial:{k}": {"type": "array"} for k in ("dimensions", "transform", "shape", "bbox")},
    "spatial:registration": {"type": "string"},
    "multiscales": {"type": "object"},
}


def schema(profile: str) -> dict:
    cmo = convention(profile)
    title = PROFILES[profile][2]
    url = ({"pattern": "^[Hh][Tt][Tt][Pp][Ss]?://[^?#]*/$",
            "description": "The store URL: http or https, its path ending in /, no query or fragment"}
           if profile in STORES else {"description": "The URL of the source: a .SAFE directory's store URL, or a zip file's"}
           if profile == "safe" else {"description": "The URL of the source file"})
    root = {
        "type": "object",
        "description": "The root's property: the profile and version that produced the hierarchy, its source, "
                       "and the root's source metadata",
        "properties": {
            "profile": {"type": "string", "const": profile},
            "version": {"type": "integer", "const": PROFILES[profile][1]},
            **({"revision": {"type": "integer", "const": REVISION,
                             "description": "The spec/virtualize.md revision that produced the hierarchy (version 0 only)"}}
               if PROFILES[profile][1] == 0 else {}),
            "source": {"type": "object", "properties": {"url": {"type": "string", **url}}, "required": ["url"],
                       "additionalProperties": False},
            profile: {"$ref": "#/$defs/sourceMetadata"},
        },
        "required": ["profile", "version", *(["revision"] if PROFILES[profile][1] == 0 else []), "source"],
        "additionalProperties": False,
    }
    node = {
        "type": "object",
        "description": "Any other node's property: its source metadata only",
        "properties": {profile: {"$ref": "#/$defs/sourceMetadata"}},
        "required": [profile],
        "additionalProperties": False,
    }
    prop = {"oneOf": [{"$ref": "#/$defs/rootProperty"}, {"$ref": "#/$defs/nodeProperty"}]}
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
            "description": "A translated header value (spec/conventions.md §6)",
            "anyOf": [{"type": "number"}, {"$ref": "#/$defs/text"},
                      {"type": "array", "items": {"anyOf": [{"type": "number"}, {"type": "string"}]}}],
        },
        "text": {
            "description": "A text value: a string (UTF-8), or its ISO 8859-1 reading when it is not UTF-8",
            "anyOf": [{"type": "string"}, {"type": "object", "properties": {"latin1": {"type": "string"}},
                                           "required": ["latin1"], "additionalProperties": False}],
        },
    }
    if profile == "ndpi":
        defs.update(TIFF_DEFS)
    if profile in IR_PROFILES:
        defs.update(MIRROR_DEFS)
    if profile == "ndpi":  # a level's strip's SOF0 segment (spec/virtualize/ndpi.md §5)
        ifd = json.loads(json.dumps(TIFF_DEFS["ifd"]))
        ifd["properties"]["sof0"] = {"type": "string", "contentEncoding": "base64",
                                     "description": "A McuStarts level's strip's SOF0 segment, which its chunks replace"}
        defs["ifd"] = ifd
    if profile == "dicom":
        defs.update(DICOM_DEFS)
    if profile == "ims":
        defs.update(IMS_DEFS)
    defs["nodeProperty"] = node
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": cmo["schema_url"],
        "title": f"vzip {title} convention, version {PROFILES[profile][1]}",
        "description": f"The zarr.json of a node that declares the vzip {title} convention ({cmo['spec_url']})",
        "type": "object",
        "properties": {
            "zarr_format": {"type": "integer", "const": 3},
            "node_type": {"type": "string", "enum": ["group", "array"]},
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
                    **(GEOZARR if profile == "safe" else {}),
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
        d = HERE / "virtualize" / profile
        d.mkdir(exist_ok=True)
        (d / "schema.json").write_text(json.dumps(schema(profile), indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(PROFILES)} schemas")


if __name__ == "__main__":
    main()
