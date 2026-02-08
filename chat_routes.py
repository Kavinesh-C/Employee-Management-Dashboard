from fastapi import APIRouter, Depends
from pydantic import BaseModel
from chat_store import add_message, get_messages, get_total_unread
from auth import get_current_user_from_session

router = APIRouter(prefix="/api/chat")


class MessageIn(BaseModel):
    receiver_id: int
    message: str


@router.get("/history/{receiver_id}")
def chat_history(
    receiver_id: int,
    user=Depends(get_current_user_from_session)
):
    return get_messages(user.id, receiver_id)


@router.post("/send")
def send_message(
    msg: MessageIn,
    user=Depends(get_current_user_from_session)
):
    add_message(user.id, msg.receiver_id, msg.message)
    return {"ok": True}


@router.get("/unread-count")
def unread_count(user=Depends(get_current_user_from_session)):
    return {"count": get_total_unread(user.id)}
