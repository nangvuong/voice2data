"""
STT Records - lưu các lần test STT mà người dùng đã bấm "Lưu" và thống kê.

Mỗi bản ghi: file đã test, tham số, văn bản thô / sau từ điển / bản đã sửa,
thời gian xử lý, tỉ lệ lỗi từ (WER) và các cặp sửa lỗi đã học.
Lưu ở data/stt_records.json.
"""

import json
import os
import re
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RECORDS_PATH = os.getenv("STT_RECORDS_PATH", os.path.join(_BASE, "data", "stt_records.json"))

_WORD_RE = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)


def _words(text: str) -> list:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def word_edit_distance(a: list, b: list) -> int:
    """Levenshtein trên danh sách từ."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def wer(reference_text: str, hypothesis_text: str):
    """WER của hypothesis so với reference (bản đã sửa). None nếu reference rỗng."""
    ref, hyp = _words(reference_text), _words(hypothesis_text)
    if not ref:
        return None
    return round(word_edit_distance(ref, hyp) / len(ref), 4)


class STTRecords:
    def __init__(self, path: str = RECORDS_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._items = []
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._items = json.load(f)
        except (FileNotFoundError, ValueError):
            self._items = []

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._items, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    @staticmethod
    def _enrich(rec: dict) -> dict:
        raw, dict_text, final = rec.get("raw_text", ""), rec.get("dict_text", ""), rec.get("final_text", "")
        rec["words"] = len(_words(final))
        rec["wer_raw"] = wer(final, raw)
        rec["wer_dict"] = wer(final, dict_text)
        rec["edited"] = _words(dict_text) != _words(final)
        d = rec.get("audio_duration") or 0
        rec["rtf"] = round(rec.get("process_time", 0) / d, 3) if d else None
        return rec

    def add(self, rec: dict) -> dict:
        with self._lock:
            rec = dict(rec)
            rec["id"] = uuid.uuid4().hex[:12]
            rec["time"] = int(time.time())
            self._items.append(self._enrich(rec))
            self._save()
            return rec

    def update(self, rec_id: str, fields: dict):
        with self._lock:
            for r in self._items:
                if r["id"] == rec_id:
                    r.update(fields)
                    r["updated"] = int(time.time())
                    self._enrich(r)
                    self._save()
                    return r
        return None

    def get(self, rec_id: str):
        with self._lock:
            return next((dict(r) for r in self._items if r["id"] == rec_id), None)

    def delete(self, rec_id: str):
        with self._lock:
            for r in self._items:
                if r["id"] == rec_id:
                    self._items.remove(r)
                    self._save()
                    return r
        return None

    def list(self) -> list:
        with self._lock:
            return [dict(r) for r in reversed(self._items)]

    # ---------- thống kê ----------
    def stats(self, dict_snapshot: dict = None) -> dict:
        with self._lock:
            items = [dict(r) for r in self._items]
        n = len(items)

        def avg(vals):
            vals = [v for v in vals if v is not None]
            return round(sum(vals) / len(vals), 4) if vals else None

        audio = sum(r.get("audio_duration") or 0 for r in items)
        proc = sum(r.get("process_time") or 0 for r in items)

        by_day = defaultdict(list)
        for r in items:
            by_day[datetime.fromtimestamp(r["time"]).strftime("%Y-%m-%d")].append(r)
        days = []
        for day in sorted(by_day)[-14:]:
            rs = by_day[day]
            days.append({
                "day": day, "count": len(rs),
                "wer_raw": avg([r.get("wer_raw") for r in rs]),
                "wer_dict": avg([r.get("wer_dict") for r in rs]),
                "audio": round(sum(r.get("audio_duration") or 0 for r in rs), 1),
            })

        errors = Counter()
        for r in items:
            for c in r.get("changes", []):
                errors[(c["wrong"], c["right"])] += 1

        methods = Counter(
            (r.get("params") or {}).get("decoding_method", "?") for r in items if r.get("params"))
        opt = Counter()
        for r in items:
            p = r.get("params") or {}
            for k in ("use_vad", "denoise", "normalize", "trim_silence"):
                if p.get(k):
                    opt[k] += 1

        res = {
            "total": n,
            "audio_files": sum(1 for r in items if r.get("audio_duration")),
            "audio_seconds": round(audio, 1),
            "process_seconds": round(proc, 1),
            "avg_rtf": round(proc / audio, 3) if audio else None,
            "words": sum(r.get("words", 0) for r in items),
            "edited": sum(1 for r in items if r.get("edited")),
            "avg_wer_raw": avg([r.get("wer_raw") for r in items]),
            "avg_wer_dict": avg([r.get("wer_dict") for r in items]),
            "trend": [
                {"i": i + 1, "wer_raw": r.get("wer_raw"), "wer_dict": r.get("wer_dict"),
                 "file": r.get("filename", "")}
                for i, r in enumerate(items[-30:])
            ],
            "by_day": days,
            "top_errors": [{"wrong": w, "right": rt, "count": c} for (w, rt), c in errors.most_common(15)],
            "methods": dict(methods),
            "options": dict(opt),
            "learned_entries": sum(len(r.get("changes", [])) for r in items),
            "new_vocab": sum(len(r.get("new_vocab", [])) for r in items),
        }
        if dict_snapshot is not None:
            es = dict_snapshot.get("entries", [])
            res["dictionary"] = {
                "entries": len(es),
                "auto": sum(1 for e in es if e.get("auto")),
                "manual": sum(1 for e in es if not e.get("auto")),
                "enabled": sum(1 for e in es if e.get("enabled", True)),
                "vocab": len(dict_snapshot.get("vocab", [])),
            }
        return res


stt_records = STTRecords()
