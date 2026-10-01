"""Error classes (spec §8.4)."""


class VzipError(Exception):
    cls = "unknown"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


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
    """The writer's input is invalid (spec §9.1)."""
