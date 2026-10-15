"""
Hotwords Manager - Quản lý danh sách từ khóa ưu tiên (Contextual Biasing Hotwords)
dành cho mô hình STT sherpa-onnx.

Mỗi từ khóa được mã hoá thành chuỗi token tương ứng trong bảng tokens của mô hình
và được đồng bộ trực tiếp vào data/onnx_hotwords.txt để mô hình ASR ưu tiên
nhận diện trong quá trình Beam Search Decoding (không dùng quy tắc rule-based).
"""

import difflib
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import config

logger = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOTWORDS_JSON_PATH = os.path.join(_BASE, "data", "hotwords.json")
ONNX_HOTWORDS_PATH = os.path.join(_BASE, "data", "onnx_hotwords.txt")
TOKENS_PATH = getattr(config, "TOKENS_PATH", os.path.join(_BASE, "models", "tokens.txt"))


class HotwordsManager:
    def __init__(
        self,
        json_path: str = HOTWORDS_JSON_PATH,
        onnx_path: str = ONNX_HOTWORDS_PATH,
        tokens_path: str = TOKENS_PATH,
    ):
        self.json_path = json_path
        self.onnx_path = onnx_path
        self.tokens_path = tokens_path
        self._lock = threading.RLock()
        self._items: List[Dict] = []
        self._valid_tokens: set = set()
        self._load_valid_tokens()
        self._load()

    def _load_valid_tokens(self):
        """Đọc danh sách các token hợp lệ từ models/tokens.txt."""
        self._valid_tokens.clear()
        if os.path.exists(self.tokens_path):
            try:
                with open(self.tokens_path, "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.strip().split()
                        if parts:
                            self._valid_tokens.add(parts[0])
            except Exception as e:
                logger.error("Lỗi đọc tokens.txt: %s", e)

    def encode_tokens(self, word: str) -> str:
        """
        Mã hóa một từ / cụm từ thành chuỗi token hợp lệ cho sherpa-onnx.
        Ví dụ: 'NPK' -> 'N P K', 'Phân bón' -> 'P H Â N B Ó N'.
        """
        if not word:
            return ""
        tokens_list = []
        for subw in str(word).split():
            subw_clean = "".join(c for c in subw if c.isalnum()).upper()
            if not subw_clean:
                continue
            chars = [c for c in subw_clean if c in self._valid_tokens]
            if chars:
                tokens_list.append(" ".join(chars))
        return " ".join(tokens_list)

    def _load(self):
        """Nạp danh sách hotwords từ JSON, nếu chưa có thì migrate từ onnx_hotwords.txt."""
        with self._lock:
            if os.path.exists(self.json_path):
                try:
                    with open(self.json_path, "r", encoding="utf-8") as f:
                        self._items = json.load(f)
                    return
                except Exception as e:
                    logger.warning("Không đọc được hotwords.json: %s", e)

            # Khởi tạo từ file onnx_hotwords.txt nếu có sẵn
            self._items = []
            if os.path.exists(self.onnx_path):
                try:
                    with open(self.onnx_path, "r", encoding="utf-8") as f:
                        for line in f:
                            tok_line = line.strip()
                            if not tok_line:
                                continue
                            readable = "".join(tok_line.split())
                            self._items.append({
                                "id": readable.lower(),
                                "word": readable,
                                "tokens": tok_line,
                                "category": "Chung",
                                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            })
                    self._save_json()
                except Exception as e:
                    logger.error("Lỗi migrate hotwords: %s", e)

    def _save_json(self):
        """Lưu danh sách hotwords vào file JSON."""
        os.makedirs(os.path.dirname(self.json_path), exist_ok=True)
        tmp = self.json_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.json_path)

    def sync_to_onnx(self) -> int:
        """Đồng bộ tất cả tokens vào file data/onnx_hotwords.txt cho sherpa-onnx."""
        with self._lock:
            lines = set()
            for it in self._items:
                tokens = it.get("tokens", "").strip()
                if tokens:
                    lines.add(tokens)

            os.makedirs(os.path.dirname(self.onnx_path), exist_ok=True)
            tmp = self.onnx_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                if lines:
                    f.write("\n".join(sorted(lines)) + "\n")
                else:
                    f.write("")
            os.replace(tmp, self.onnx_path)
            return len(lines)

    def list_words(self, search: Optional[str] = None, category: Optional[str] = None) -> List[Dict]:
        """Lấy danh sách từ khóa có hỗ trợ tìm kiếm và lọc danh mục."""
        with self._lock:
            res = list(self._items)

        if search:
            q = search.strip().lower()
            res = [
                it for it in res
                if q in it.get("word", "").lower() or q in it.get("tokens", "").lower()
            ]

        if category and category != "all":
            res = [it for it in res if it.get("category", "").lower() == category.lower()]

        # Sắp xếp mới nhất lên đầu
        res.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return res

    def get_categories(self) -> List[str]:
        """Lấy danh sách các danh mục có sẵn."""
        with self._lock:
            cats = set()
            for it in self._items:
                c = it.get("category")
                if c:
                    cats.add(c)
            return sorted(cats) or ["Chung"]

    def add_word(self, word: str, category: str = "Chung") -> Tuple[bool, str, Optional[Dict]]:
        """
        Thêm một từ khóa hotwords mới.
        Trả về (thành_công, thông_báo, item_vừa_tạo).
        """
        clean_word = " ".join(str(word or "").strip().split())
        if not clean_word:
            return False, "Từ khóa không được để trống.", None

        tokens = self.encode_tokens(clean_word)
        if not tokens:
            return False, "Từ khóa không chứa ký tự hợp lệ với bảng tokens của mô hình.", None

        word_id = clean_word.lower()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self._lock:
            for it in self._items:
                if it.get("id") == word_id:
                    # Đã tồn tại, cập nhật category nếu đổi
                    it["category"] = category or it.get("category", "Chung")
                    it["tokens"] = tokens
                    it["updated_at"] = now_str
                    self._save_json()
                    self.sync_to_onnx()
                    return True, f"Từ khóa '{clean_word}' đã tồn tại, đã cập nhật.", it

            item = {
                "id": word_id,
                "word": clean_word,
                "tokens": tokens,
                "category": category or "Chung",
                "created_at": now_str,
            }
            self._items.append(item)
            self._save_json()
            self.sync_to_onnx()
            return True, f"Đã thêm từ khóa '{clean_word}' thành công.", item

    def add_batch(self, text_or_list, category: str = "Chung") -> Dict:
        """
        Thêm danh sách từ khóa hàng loạt (phân tách bởi dấu phẩy hoặc dòng mới).
        """
        if isinstance(text_or_list, str):
            raw_words = re.split(r"[\n,;]+", text_or_list)
        else:
            raw_words = list(text_or_list)

        added = 0
        skipped = 0
        errors = []

        for rw in raw_words:
            w = rw.strip()
            if not w:
                continue
            ok, msg, _ = self.add_word(w, category=category)
            if ok:
                added += 1
            else:
                skipped += 1
                errors.append(f"{w}: {msg}")

        return {
            "total_processed": added + skipped,
            "added": added,
            "skipped": skipped,
            "errors": errors[:5],
            "total_hotwords": len(self._items),
        }

    def delete_word(self, word_or_id: str) -> bool:
        """Xóa một từ khóa khỏi danh sách hotwords."""
        target_id = str(word_or_id or "").strip().lower()
        with self._lock:
            initial_len = len(self._items)
            self._items = [it for it in self._items if it.get("id") != target_id and it.get("word", "").lower() != target_id]
            if len(self._items) < initial_len:
                self._save_json()
                self.sync_to_onnx()
                return True
            return False

    def stats(self) -> Dict:
        """Thống kê tổng số từ hotwords, số token ONNX và dung lượng file."""
        with self._lock:
            total_words = len(self._items)
            cats = Counter(it.get("category", "Chung") for it in self._items)
            onnx_count = 0
            file_size = 0
            if os.path.exists(self.onnx_path):
                file_size = os.path.getsize(self.onnx_path)
                try:
                    with open(self.onnx_path, "r", encoding="utf-8") as f:
                        onnx_count = sum(1 for line in f if line.strip())
                except Exception:
                    pass

            return {
                "total_words": total_words,
                "total_onnx_tokens": onnx_count,
                "file_size_bytes": file_size,
                "categories": dict(cats),
                "hotwords_path": self.onnx_path,
            }


    def learn_from_correction(self, original: str, corrected: str, category: str = "Tự học từ bản sửa") -> List[Dict]:
        """
        So sánh bản nhận diện gốc và bản người dùng đã sửa, tự động phát hiện
        các từ/cụm từ mới và convert thành hotwords nạp vào mô hình ONNX.
        """
        if not original or not corrected or original.strip() == corrected.strip():
            return []

        a_tok = [w for w in original.split() if w]
        b_tok = [w for w in corrected.split() if w]
        a_low = [w.lower() for w in a_tok]
        b_low = [w.lower() for w in b_tok]

        sm = difflib.SequenceMatcher(a=a_low, b=b_low, autojunk=False)
        extracted = []

        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op in ("replace", "insert"):
                segment = b_tok[j1:j2]
                if len(segment) <= 3:
                    phrase = " ".join(segment)
                    clean = "".join(c for c in phrase if c.isalnum() or c.isspace()).strip()
                    if clean and not clean.isdigit():
                        extracted.append(clean)
                else:
                    for w in segment:
                        clean = "".join(c for c in w if c.isalnum()).strip()
                        if len(clean) >= 2 and not clean.isdigit() and clean.lower() not in a_low:
                            extracted.append(clean)

        added_items = []
        seen = set()
        for word in extracted:
            if word.lower() in seen:
                continue
            seen.add(word.lower())
            ok, msg, item = self.add_word(word, category=category)
            if ok and item:
                added_items.append(item)

        if added_items:
            logger.info("Đã tự động học %d từ hotwords từ bản sửa: %s", len(added_items), [it['word'] for it in added_items])

        return added_items


# Singleton instance
hotwords_manager = HotwordsManager()
