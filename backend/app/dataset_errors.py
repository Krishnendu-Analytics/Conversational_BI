"""Dataset and file ingestion error types."""

from __future__ import annotations


class DatasetError(Exception):
    code: str = "dataset_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class UnsupportedFileTypeError(DatasetError):
    code = "unsupported_file_type"


class FileTooLargeError(DatasetError):
    code = "file_too_large"


class MalformedFileError(DatasetError):
    code = "malformed_file"


class IngestionLimitError(DatasetError):
    code = "ingestion_limit"


class DatasetNotReadyError(DatasetError):
    code = "dataset_not_ready"


class DatasetNotFoundError(DatasetError):
    code = "dataset_not_found"


class DatasetAccessDeniedError(DatasetError):
    code = "dataset_access_denied"


class SchemaDiscoveryError(DatasetError):
    code = "schema_discovery_error"
