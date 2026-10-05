"""Send a chat message and run the background turn at once, as the chat lane would."""

from finance.chat_runner import process_pending_turns
from finance.chat_services import send_message


def ask(principal, text, *, conversation_id=None, page_context=None, sleep=None, monotonic=None):
    conversation = send_message(principal, text, conversation_id=conversation_id, page_context=page_context)
    process_pending_turns(sleep=sleep or (lambda _s: None), monotonic=monotonic)
    conversation.refresh_from_db()
    return conversation
