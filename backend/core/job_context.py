"""Per-execution fencing identity. Never contains a customer payload or login token."""

from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class JobLease:
    job_id: UUID
    token: UUID


job_lease: ContextVar[JobLease | None] = ContextVar("job_lease", default=None)
