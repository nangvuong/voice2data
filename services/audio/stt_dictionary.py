"""
STT Dictionary - từ điển sửa lỗi sau nhận diện giọng nói.

- entries: các cặp "sai -> đúng" áp dụng tự động lên kết quả STT.
- vocab:   danh sách từ/cụm từ mới (tên thuốc, phân bón, địa danh...) đã học.
- learn(): so sánh bản STT gốc với bản người dùng đã sửa (diff theo từ), tự động
  thêm cặp thay thế và từ mới vào từ điển.

Lưu ở data/stt_dictionary.json, lịch sử sửa ở data/stt_corrections.jsonl.
"""

import difflib
import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DICT_PATH = os.getenv("STT_DICT_PATH", os.path.join(_BASE, "data", "stt_dictionary.json"))
HISTORY_PATH = os.getenv("STT_CORRECTIONS_PATH", os.path.join(_BASE, "data", "stt_corrections.jsonl"))

MAX_PHRASE_WORDS = 4   # chỉ học các thay thế ngắn (tránh học nhầm cả câu)
_WORD_RE = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)


def _tokens(text: str) -> list:
    return _WORD_RE.findall(text or "")


def _norm(s: str) -> str:
    return " ".join(_tokens(s)).lower()


class STTDictionary:
    def __init__(self, path: str = DICT_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._data = {"entries": [], "vocab": []}
        self._compiled = None
        self._load()

    # ---------- lưu / tải ----------
    def _load(self):
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self._data = {"entries": d.get("entries", []), "vocab": d.get("vocab", [])}
            except FileNotFoundError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.warning("Không đọc được từ điển STT: %s", e)
            self._compiled = None

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._compiled = None

    # ---------- truy vấn ----------
    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def _find(self, wrong_norm: str):
        for e in self._data["entries"]:
            if _norm(e["wrong"]) == wrong_norm:
                return e
        return None

    # ---------- CRUD ----------
    def upsert_entry(self, wrong: str, right: str, enabled: bool = True, auto: bool = False) -> dict:
        wrong, right = " ".join(wrong.split()), " ".join(right.split())
        if not wrong or not right:
            raise ValueError("Cần cả từ sai và từ đúng.")
        if _norm(wrong) == _norm(right):
            raise ValueError("Từ sai và từ đúng giống nhau.")
        with self._lock:
            e = self._find(_norm(wrong))
            now = int(time.time())
            if e:
                e.update(right=right, enabled=enabled, updated=now)
                if auto:
                    e["count"] = e.get("count", 1) + 1
            else:
                e = {"wrong": wrong.lower(), "right": right, "count": 1 if auto else 0,
                     "enabled": enabled, "auto": auto, "created": now, "updated": now}
                self._data["entries"].append(e)
            self._save()
            return e

    def update_entry(self, old_wrong: str, wrong=None, right=None, enabled=None) -> dict:
        with self._lock:
            e = self._find(_norm(old_wrong))
            if not e:
                raise KeyError("Không tìm thấy mục.")
            if wrong is not None and _norm(wrong):
                e["wrong"] = " ".join(wrong.split()).lower()
            if right is not None and right.strip():
                e["right"] = " ".join(right.split())
            if enabled is not None:
                e["enabled"] = bool(enabled)
            e["updated"] = int(time.time())
            self._save()
            return e

    def delete_entry(self, wrong: str) -> bool:
        with self._lock:
            e = self._find(_norm(wrong))
            if not e:
                return False
            self._data["entries"].remove(e)
            self._save()
            return True

    def add_vocab(self, word: str) -> bool:
        word = " ".join(word.split())
        if not word:
            return False
        with self._lock:
            if word.lower() in (v.lower() for v in self._data["vocab"]):
                return False
            self._data["vocab"].append(word)
            self._save()
            return True

    def delete_vocab(self, word: str) -> bool:
        with self._lock:
            for v in self._data["vocab"]:
                if v.lower() == word.lower():
                    self._data["vocab"].remove(v)
                    self._save()
                    return True
        return False

    def import_data(self, data: dict, replace: bool = False) -> dict:
        with self._lock:
            if replace:
                self._data = {"entries": [], "vocab": []}
            n_e = n_v = 0
            for e in data.get("entries", []):
                try:
                    self.upsert_entry(e["wrong"], e["right"], e.get("enabled", True))
                    n_e += 1
                except (ValueError, KeyError):
                    pass
            for v in data.get("vocab", []):
                n_v += int(self.add_vocab(str(v)))
            self._save()
            return {"entries": n_e, "vocab": n_v}

    # ---------- áp dụng ----------
    def _build(self):
        ents = [e for e in self._data["entries"] if e.get("enabled", True)]
        ents.sort(key=lambda e: len(e["wrong"]), reverse=True)  # cụm dài ưu tiên
        compiled = []
        for e in ents:
            words = _tokens(e["wrong"])
            if not words:
                continue
            pat = r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)"
            compiled.append((re.compile(pat, re.IGNORECASE | re.UNICODE), e))
        self._compiled = compiled

    @staticmethod
    def _match_case(matched: str, right: str) -> str:
        letters = [c for c in matched if c.isalpha()]
        if len(letters) > 1 and all(c.isupper() for c in letters):
            return right.upper()
        if letters and letters[0].isupper() and matched.split()[0][1:].islower():
            return right[:1].upper() + right[1:]
        return right

    def apply(self, text: str):
        """Trả (text_đã_sửa, danh_sách_các_thay_thế_đã_áp_dụng)."""
        if not text:
            return text, []
        with self._lock:
            if self._compiled is None:
                self._build()
            compiled = list(self._compiled)
        applied = []
        for rx, e in compiled:
            def sub(m, e=e):
                applied.append({"wrong": e["wrong"], "right": e["right"]})
                return self._match_case(m.group(0), e["right"])
            text = rx.sub(sub, text)
        return text, applied

    # ---------- học tự động ----------
    def learn(self, original: str, corrected: str, source: str = "") -> dict:
        """
        So sánh bản gốc và bản đã sửa, tự thêm vào từ điển:
        - các cặp thay thế ngắn (replace) -> entries
        - từ mới xuất hiện ở bản sửa mà chưa có trong từ điển -> vocab
        Trả về {"added_entries": [...], "updated_entries": [...], "added_vocab": [...]}.
        """
        a_tok, b_tok = _tokens(original), _tokens(corrected)
        a_low, b_low = [t.lower() for t in a_tok], [t.lower() for t in b_tok]
        sm = difflib.SequenceMatcher(a=a_low, b=b_low, autojunk=False)

        added, updated, vocab_added = [], [], []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op != "replace":
                continue
            if i2 - i1 > MAX_PHRASE_WORDS or j2 - j1 > MAX_PHRASE_WORDS:
                continue
            wrong = " ".join(a_low[i1:i2])
            right_words = b_tok[j1:j2]
            # Nếu cả câu sửa đều viết HOA thì quy về chữ thường để áp dụng đúng kiểu chữ
            if corrected.isupper():
                right_words = [w.lower() for w in right_words]
            right = " ".join(right_words)
            with self._lock:
                existed = self._find(_norm(wrong))
            try:
                e = self.upsert_entry(wrong, right, auto=True)
            except ValueError:
                continue
            (updated if existed else added).append({"wrong": e["wrong"], "right": e["right"]})
            # từ/cụm mới trong phần đúng -> vocab
            for w in right_words:
                if w.lower() not in a_low and self.add_vocab(w):
                    vocab_added.append(w)

        # từ mới được chèn thêm (insert) cũng đưa vào vocab
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "insert":
                for w in b_tok[j1:j2]:
                    if not w.isdigit() and self.add_vocab(w.lower() if corrected.isupper() else w):
                        vocab_added.append(w)

        result = {"added_entries": added, "updated_entries": updated, "added_vocab": vocab_added}
        if a_low != b_low:
            self._log(original, corrected, result, source)
        return result

    # ---------- lịch sử ----------
    def _log(self, original, corrected, result, source):
        try:
            os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
            with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "time": int(time.time()), "source": source,
                    "original": original, "corrected": corrected, "learned": result,
                }, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("Không ghi được lịch sử sửa STT: %s", e)

    def history(self, limit: int = 100) -> list:
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
        except FileNotFoundError:
            return []
        out = []
        for ln in reversed(lines):
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out


stt_dictionary = STTDictionary()
