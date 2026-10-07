"""
Quản lý trạng thái phiên hội thoại (Conversation State) cho Nông Ký AI.
Định nghĩa máy trạng thái (FSM), cấu trúc dữ liệu lượt nói (Turn),
và bộ nhớ lưu phiên (SessionStore) với thời gian hết hạn TTL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("conversation.state")


class DialogState(str, Enum):
    """Các trạng thái của máy trạng thái hội thoại (FSM)."""
    IDLE = "IDLE"                      # Chờ người dùng bắt đầu
    COLLECTING = "COLLECTING"          # Đang thu thập thông tin hoạt động ban đầu
    CLARIFYING = "CLARIFYING"          # Đang hỏi bổ sung các trường bắt buộc còn thiếu
    CONFIRMING = "CONFIRMING"          # Đã đủ thông tin, chờ người dùng xác nhận lưu
    IMAGE_CHOICE = "IMAGE_CHOICE"      # Vừa nhận ảnh, chờ người dùng chọn: [1] Ghi nhật ký hay [2] Tư vấn
    ADVISORY = "ADVISORY"              # Đang trong luồng tư vấn kỹ thuật (thuốc, phân, sâu bệnh)
    COMMITTED = "COMMITTED"            # Đã lưu thành công vào CSDL VietGAP
    CANCELLED = "CANCELLED"            # Phiên bị hủy bỏ


class UserIntent(str, Enum):
    """Ý định của người dùng trong từng lượt nói."""
    START_ACTIVITY = "START_ACTIVITY"  # Khai báo hoạt động canh tác mới
    PROVIDE_SLOT = "PROVIDE_SLOT"      # Trả lời câu hỏi điền khuyết
    CORRECTION = "CORRECTION"          # Sửa đổi/ghi đè thông tin trước đó
    CONFIRM = "CONFIRM"                # Đồng ý lưu ("lưu đi", "đúng rồi", "ok")
    CANCEL = "CANCEL"                  # Hủy bỏ ("thôi hủy đi", "để sau", "không lưu")
    CHOOSE_DIARY = "CHOOSE_DIARY"      # Chọn hướng [1] Ghi nhật ký từ ảnh
    CHOOSE_ADVICE = "CHOOSE_ADVICE"    # Chọn hướng [2] Tư vấn thông tin từ ảnh
    ASK_ADVICE = "ASK_ADVICE"          # Hỏi tư vấn kỹ thuật nông nghiệp
    CHITCHAT = "CHITCHAT"              # Chào hỏi hoặc chuyện phiếm
    UNKNOWN = "UNKNOWN"                # Chưa rõ ý định


@dataclass
class Turn:
    """Đại diện cho 1 lượt trao đổi giữa User và Trợ lý."""
    turn_id: int
    role: str                          # "user" | "assistant" | "system"
    text: str
    audio_path: Optional[str] = None
    image_path: Optional[str] = None
    intent: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "role": self.role,
            "text": self.text,
            "audio_path": self.audio_path,
            "image_path": self.image_path,
            "intent": self.intent,
            "timestamp": round(self.timestamp, 2),
        }


@dataclass
class ConversationState:
    """
    Trạng thái toàn diện của một phiên hội thoại nông nghiệp.
    """
    session_id: str
    farmer_id: Optional[int] = 1
    farmer_name: str = "Bác nông dân"
    season_id: Optional[int] = None
    plot_id: Optional[int] = None
    crop_name: str = "Cà gai leo"

    # Trạng thái FSM
    state: DialogState = DialogState.IDLE
    current_activity: Optional[str] = None  # 'spray', 'fertilize', 'purchase', 'harvest', 'sale', 'waste', 'observation', 'general'

    # Dữ liệu thu thập
    accumulated_slots: Dict[str, Any] = field(default_factory=dict)
    missing_slots: List[Dict[str, Any]] = field(default_factory=list)  # [{'key': ..., 'label': ...}]
    
    # Lịch sử hội thoại
    history: List[Turn] = field(default_factory=list)

    # Dữ liệu hình ảnh (nếu có)
    last_image_path: Optional[str] = None
    last_image_analysis: Optional[Dict[str, Any]] = None

    # Dữ liệu tư vấn RAG & Context Bridge (Chuyển đổi 1 chạm sang nhật ký)
    last_rag_result: Optional[Dict[str, Any]] = None
    bridge_slots: Dict[str, Any] = field(default_factory=dict)
    bridge_activity: Optional[str] = None

    # Kết quả lưu CSDL
    last_saved_record: Optional[Dict[str, Any]] = None
    last_saved_activity: Optional[str] = None
    last_saved_slots: Dict[str, Any] = field(default_factory=dict)

    # Danh sách các file audio TTS đã tạo trong phiên này
    tts_audio_files: List[str] = field(default_factory=list)

    # Thời gian theo dõi
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    meta: Dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        """Cập nhật thời gian hoạt động gần nhất."""
        self.last_active = time.time()

    def is_expired(self, ttl_sec: int = 600) -> bool:
        """Kiểm tra phiên có bị hết hạn do không tương tác (mặc định 10 phút)."""
        return (time.time() - self.last_active) > ttl_sec

    def register_tts_audio(self, audio_path: str) -> None:
        """Đăng ký file TTS audio phát sinh trong phiên để theo dõi và dọn dẹp khi hết phiên."""
        if audio_path and audio_path not in self.tts_audio_files:
            self.tts_audio_files.append(audio_path)

    def cleanup_tts_files(self) -> int:
        """
        Xóa toàn bộ các file audio TTS đã sinh ra trong phiên này khỏi thư mục tmp.
        Chỉ xóa các file tts_*.wav để tuyệt đối an toàn.
        """
        deleted_count = 0
        tmp_dir = "/root/voice_to_data/tmp"
        try:
            import config
            tmp_dir = getattr(config, "TMP_DIR", tmp_dir)
        except Exception:
            pass

        for path in list(self.tts_audio_files):
            try:
                fname = os.path.basename(str(path).split("?")[0])
                if fname.startswith("tts_") and fname.endswith(".wav"):
                    target = os.path.join(tmp_dir, fname)
                    if os.path.isfile(target):
                        os.remove(target)
                        deleted_count += 1
                        logger.info("🗑️ Đã xóa file TTS hết phiên (%s): %s", self.session_id, fname)
            except Exception as e:
                logger.debug("Lỗi khi xóa file TTS %s: %s", path, e)
        self.tts_audio_files.clear()
        return deleted_count

    def add_turn(
        self,
        role: str,
        text: str,
        audio_path: Optional[str] = None,
        image_path: Optional[str] = None,
        intent: Optional[str] = None,
    ) -> Turn:
        """Thêm một lượt nói vào lịch sử phiên."""
        turn_id = len(self.history) + 1
        t = Turn(
            turn_id=turn_id,
            role=role,
            text=text,
            audio_path=audio_path,
            image_path=image_path,
            intent=intent,
            timestamp=time.time(),
        )
        self.history.append(t)
        self.touch()
        return t

    def update_slots(self, new_slots: Dict[str, Any]) -> None:
        """Cập nhật hoặc ghi đè các trường thông tin."""
        if not isinstance(new_slots, dict):
            return
        for k, v in new_slots.items():
            if v is not None and str(v).strip() not in ("", "null", "None"):
                self.accumulated_slots[k] = v
        self.touch()

    def reset(self, keep_last_saved: bool = True, clear_history: bool = True) -> None:
        """Đặt lại trạng thái về IDLE, xóa các slot đang chờ và dọn dẹp các file TTS của phiên."""
        self.cleanup_tts_files()
        self.state = DialogState.IDLE
        self.current_activity = None
        self.accumulated_slots = {}
        self.missing_slots = []
        self.last_image_path = None
        self.last_image_analysis = None
        self.last_rag_result = None
        self.bridge_slots = {}
        self.bridge_activity = None
        if clear_history:
            self.history = []
        if not keep_last_saved:
            self.last_saved_activity = None
            self.last_saved_slots = {}
        self.touch()

    def get_summary(self) -> Dict[str, Any]:
        """Trả về bản tóm tắt nhanh trạng thái phiên."""
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "current_activity": self.current_activity,
            "accumulated_slots": self.accumulated_slots,
            "missing_slots": self.missing_slots,
            "turns_count": len(self.history),
            "last_active_sec_ago": round(time.time() - self.last_active, 1),
        }


class SessionStore:
    """Bộ nhớ lưu trữ các phiên hội thoại trong RAM, an toàn đa luồng."""

    def __init__(self, default_ttl_sec: int = 600):
        self._sessions: Dict[str, ConversationState] = {}
        self._lock = threading.Lock()
        self.default_ttl_sec = default_ttl_sec

    def get_or_create(
        self,
        session_id: str,
        farmer_id: Optional[int] = 1,
        season_id: Optional[int] = None,
        farmer_name: str = "Bác nông dân",
    ) -> ConversationState:
        """Lấy phiên hiện tại hoặc tạo mới nếu chưa có / đã hết hạn."""
        with self._lock:
            # Tự động dọn dẹp các phiên hết hạn trong bộ nhớ
            now = time.time()
            expired_keys = [
                k for k, v in self._sessions.items()
                if (now - v.last_active) > self.default_ttl_sec
            ]
            for k in expired_keys:
                exp_state = self._sessions.pop(k, None)
                if exp_state:
                    exp_state.cleanup_tts_files()

            state = self._sessions.get(session_id)
            if state is None or state.is_expired(self.default_ttl_sec):
                if state is not None:
                    state.cleanup_tts_files()
                state = ConversationState(
                    session_id=session_id,
                    farmer_id=farmer_id,
                    season_id=season_id,
                    farmer_name=farmer_name,
                )
                self._sessions[session_id] = state
            return state

    def get(self, session_id: str) -> Optional[ConversationState]:
        """Lấy phiên theo session_id."""
        with self._lock:
            return self._sessions.get(session_id)

    def remove(self, session_id: str) -> None:
        """Xóa một phiên hội thoại và dọn dẹp các file audio TTS liên quan."""
        with self._lock:
            state = self._sessions.pop(session_id, None)
            if state:
                state.cleanup_tts_files()

    def remove_by_farmer(self, farmer_id: int) -> int:
        """Dọn dẹp tất cả các phiên và file TTS thuộc về một nông dân (dùng khi đăng xuất)."""
        with self._lock:
            matched_keys = [
                k for k, v in self._sessions.items()
                if v.farmer_id == farmer_id
            ]
            total_deleted = 0
            for k in matched_keys:
                state = self._sessions.pop(k, None)
                if state:
                    total_deleted += state.cleanup_tts_files()
            if matched_keys:
                logger.info("🧹 Đã dọn dẹp %d phiên của nông dân %s (xóa %d file TTS)", len(matched_keys), farmer_id, total_deleted)
            return len(matched_keys)

    def cleanup_expired(self) -> int:
        """Dọn dẹp các phiên đã quá hạn TTL và xóa file audio TTS kèm theo."""
        with self._lock:
            now = time.time()
            expired_keys = [
                k for k, v in self._sessions.items()
                if (now - v.last_active) > self.default_ttl_sec
            ]
            total_files = 0
            for k in expired_keys:
                state = self._sessions.pop(k, None)
                if state:
                    total_files += state.cleanup_tts_files()
            if expired_keys:
                logger.info("🧹 Đã dọn dẹp %d phiên hết hạn TTL (xóa %d file TTS)", len(expired_keys), total_files)
            return len(expired_keys)

    def get_all_active_tts_files(self) -> set:
        """Lấy danh sách basename của toàn bộ file audio TTS đang thuộc các phiên hoạt động."""
        active_set = set()
        with self._lock:
            for state in self._sessions.values():
                for p in state.tts_audio_files:
                    fname = os.path.basename(str(p).split("?")[0])
                    if fname:
                        active_set.add(fname)
        return active_set


# Singleton instance toàn cục
session_store = SessionStore()
