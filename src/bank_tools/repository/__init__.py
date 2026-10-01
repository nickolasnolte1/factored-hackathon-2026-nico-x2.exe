"""Data access for the bank tools: one interface, a local SQLite snapshot and the Databricks SQL warehouse."""
from .base import GuardedRepository, Repository, normalize_row
from .local import LocalRepository

__all__ = ["Repository", "GuardedRepository", "LocalRepository", "DatabricksRepository", "normalize_row"]


def __getattr__(name):  # requests is imported only when the Databricks repository is used
    if name == "DatabricksRepository":
        from .databricks import DatabricksRepository
        return DatabricksRepository
    raise AttributeError(name)
