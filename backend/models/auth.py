from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

Role = Literal["admin", "staff", "user"]


@dataclass(frozen=True)
class Account:
    id: UUID
    username: str
    display_name: str
    role: Role
    is_active: bool
    password_hash: str = field(repr=False)
    created_at: datetime


@dataclass(frozen=True)
class Principal:
    account: Account
    session_id: UUID


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 401):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)
