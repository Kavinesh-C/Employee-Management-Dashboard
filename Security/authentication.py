"""
SECURE USER AUTHENTICATION
==========================
Authenticate users using hashed credentials.

FLOW:
- Query user by username.
- Verify password hash.
- Return user on success.

WHY:
- Ensures only valid users can access the system.

HOW:
- Uses Argon2 hashes to verify passwords without storing raw secrets.
"""

from passlib.context import CryptContext
from passlib.exc import UnknownHashError
from sqlalchemy.orm import Session, load_only
from app.models import User
from Security.data_integrity import sha256_hex


pwd_context = CryptContext(
    schemes=["argon2", "bcrypt"],
    deprecated="auto",
)


def hash_password(password: str) -> str:
    """
    Hash a plain text password using Argon2.
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plain text password against a stored hash.
    """
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except UnknownHashError:
        return False


def authenticate_user(db: Session, username: str, password: str):
    """Authenticate user by verifying username and password hash."""
    username = (username or "").strip()
    username_hash = sha256_hex(username) if username else None
    user = (
        db.query(User)
        .options(load_only(User.id, User.employee_id, User.password_hash, User.role, User.employee_id_hash))
        .filter(User.employee_id_hash == username_hash)
        .first()
    )
    if user and verify_password(password, user.password_hash):
        return user
    return None
