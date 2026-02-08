# chat_store.py
from collections import defaultdict
from datetime import datetime

chat_messages = defaultdict(lambda: defaultdict(list))
unread_counts = defaultdict(lambda: defaultdict(int))
# unread_counts[user_id][sender_id] = count


def add_message(sender_id: int, receiver_id: int, message: str):
    msg = {
        "sender_id": sender_id,
        "content": message,
        "timestamp": datetime.now().strftime("%H:%M")
    }

    chat_messages[sender_id][receiver_id].append(msg)
    chat_messages[receiver_id][sender_id].append(msg)

    # increment unread for receiver
    unread_counts[receiver_id][sender_id] += 1


def get_messages(user_id: int, other_id: int):
    # reset unread when opening chat
    unread_counts[user_id][other_id] = 0
    return chat_messages[user_id][other_id]


def get_total_unread(user_id: int) -> int:
    return sum(unread_counts[user_id].values())
