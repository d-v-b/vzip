"""vzip: a ZIP container for byte-range references (format version 0, spec revision 5)."""

from .errors import VzipError, ArchiveError, EntryError, BodyError, PayloadError, ResolutionError, RequestError
from .reader import Archive, open_archive
from .writer import write_archive, WriteInputError

__all__ = [
    "VzipError", "ArchiveError", "EntryError", "BodyError", "PayloadError", "ResolutionError",
    "RequestError", "Archive", "open_archive", "write_archive", "WriteInputError",
]
