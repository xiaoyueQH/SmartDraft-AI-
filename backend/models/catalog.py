from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

from backend.models.orders import ProductView


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    category: str
    description: str
    specifications: dict[str, str]
    suggested_price_cents: int
    source: Literal["mock_catalog_v1"] = "mock_catalog_v1"
    is_demo: Literal[True] = True
    imported_product_id: UUID | None = None


class CandidatePage(BaseModel):
    items: list[Candidate]
    categories: list[str]
    has_more: bool


class ProductFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    price_cents: int = Field(strict=True, ge=1, le=100_000_000)
    stock: int = Field(strict=True, ge=0, le=1_000_000)


class ImportProduct(ProductFields):
    candidate_id: str = Field(min_length=1, max_length=64)


class ManualProduct(ProductFields):
    request_id: UUID
    sku: Annotated[
        str, StringConstraints(to_upper=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
    ]


class ProductMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    expected_version: int = Field(strict=True, ge=1)


class EditProduct(ProductFields, ProductMutation):
    pass


class ProductState(ProductMutation):
    is_active: StrictBool


ProductAction = Literal["edit", "state", "delete", "restore"]


class ImportResult(BaseModel):
    product: ProductView
    created: bool


class CandidateNotFound(Exception):
    pass
