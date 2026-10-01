"""Error classes of spec §8.4. Each is a ValueError so existing callers keep working."""


class VzipError(ValueError):
    cls = "error"


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
