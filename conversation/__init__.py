"""
Package Conversation State cho hệ thống Nông Ký AI VietGAP.
Hỗ trợ hội thoại đa lượt (Multi-turn Dialogue State Tracking),
xử lý hình ảnh 2 hướng (Ghi nhật ký & Tư vấn kỹ thuật),
sinh câu hỏi điền khuyết bằng Template và tích hợp toàn diện các dịch vụ:
Gemini LLM, VLM Engine, STT, TTS, Normalizer, Material Matcher và MySQL Database.
"""

from conversation.state import (
    DialogState,
    UserIntent,
    ConversationState,
    Turn,
    SessionStore,
    session_store,
)
from conversation.manager import ConversationManager, conversation_manager

__all__ = [
    "DialogState",
    "UserIntent",
    "ConversationState",
    "Turn",
    "SessionStore",
    "session_store",
    "ConversationManager",
    "conversation_manager",
]
