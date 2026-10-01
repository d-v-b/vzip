"""Error classes (spec section 8.4)."""


class VzipError(Exception):
    cls = "archive"


class ArchiveError(VzipError):
    cls = "archive"


class EntryError(VzipError):
    cls = "entry"


class BodyError(VzipError):
    cls = "body"


class PayloadError(VzipError):
    cls = "payload"


class ResolutionError(VzipError):
    cls = "resolution"


class RequestError(VzipError):
    cls = "request"


class WriteError(Exception):
    """The writer rejected its input (spec section 9.1)."""
