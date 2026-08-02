class DatasetSinkError(Exception):
    """Base exception for expected dataset sink failures."""


class ManifestError(DatasetSinkError):
    """The input manifest is invalid."""


class IntegrityError(DatasetSinkError):
    """Materialized bytes do not match the manifest."""


class ReleaseConflictError(DatasetSinkError):
    """An immutable release already exists with different metadata."""


class OptionalDependencyError(DatasetSinkError):
    """An optional integration dependency is not installed."""
