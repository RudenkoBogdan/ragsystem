from typing import Optional
from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: int
    username: str

    class Config:
        from_attributes = True


class LlmKeyRequest(BaseModel):
    api_key: str = Field(min_length=8, max_length=512)


class LlmKeyStatus(BaseModel):
    has_key: bool
    last4: Optional[str] = None
