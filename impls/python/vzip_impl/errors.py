class VzError(Exception):
    """A classified vzip error. cls is one of: archive, entry, body, payload, resolution, request."""

    def __init__(self, cls, msg):
        super().__init__(msg)
        self.cls = cls


class InvalidInput(Exception):
    """Writer input rejected (spec §9.1 / harness rules)."""
