"""Pydantic models for request/response validation and internal data."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class Conversation(BaseModel):
    """A conversation record linking a user to an OpenClaw session key."""
    id: int
    user_sub: str
    user_name: str
    session_key: str
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationCreate(BaseModel):
    """Input for creating a new conversation."""
    title: str = "New conversation"


class ConversationUpdate(BaseModel):
    """Input for updating a conversation."""
    title: Optional[str] = None


class QuickAction(BaseModel):
    """A quick action button loaded from YAML config."""
    label: str
    prompt: str


class UserInfo(BaseModel):
    """Authenticated user information from OIDC."""
    sub: str
    name: str
    email: Optional[str] = None
    preferred_username: Optional[str] = None


class ChatMessage(BaseModel):
    """A message sent from the browser to the backend WebSocket."""
    content: str
    session_key: str
