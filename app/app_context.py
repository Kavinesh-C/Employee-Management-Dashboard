from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from .database import get_db
from .models import Notification, User
import hashlib

templates = Jinja2Templates(directory="templates")



def create_notification(
    db: Session,
    user_id: int,
    title: str,
    message: str | None = None,
    notif_type: str | None = None,
    link: str | None = None
) -> None:
    db.add(Notification(
        user_id=user_id,
        title=title,
        message=message,
        notif_type=notif_type,
        link=link
    ))


def hash_employee_id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user
