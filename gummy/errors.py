class GummyError(Exception):
    """An expected error safe to show to users; never include provider bodies or secrets."""


class ConfigurationError(GummyError):
    pass


class QuotaExceeded(GummyError):
    pass


class SourceError(GummyError):
    pass


class RunConflict(GummyError):
    pass


class ReportNotFound(GummyError):
    pass
