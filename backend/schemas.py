from typing import Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class AccessToken(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class StoreSummary(BaseModel):
    id: str
    code: str
    name: str


class ProductSummary(BaseModel):
    id: str
    code: str
    title: str
    category: str
    current_version: int
