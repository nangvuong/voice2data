"""
Module Material Matcher:
- Sử dụng mô hình LLM 0.6B (Qwen3-0.6B trên NPU RK3588, cổng 8081) để trích xuất nhanh
  tên các loại thuốc bảo vệ thực vật (BVTV) và phân bón từ văn bản (kết quả OCR, STT, ghi chép).
- Áp dụng thuật toán so khớp mờ (Fuzzy Matching với rapidfuzz) để đối soát, chuẩn hóa
  và liên kết chính xác với danh mục trong CSDL MySQL vietgap_diary (bảng pesticides & fertilizers).
- Tự động lấy các thông số VietGAP chuẩn (hoạt chất, thời gian cách ly PHI, nồng độ, tỷ lệ NPK)
  và tri thức RAG tương ứng phục vụ tự động điền biểu mẫu nhật ký.
"""

import asyncio
from dataclasses import dataclass, field
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import aiohttp
from rapidfuzz import fuzz

import config

logger = logging.getLogger(__name__)

# Cổng mặc định cho classifier 0.6B
DEFAULT_06B_URL = getattr(config, "LOCAL_AI_CLASSIFIER_URL", "http://127.0.0.1:8081")
DEFAULT_2B_URL = getattr(config, "LOCAL_AI_BASE_URL", "http://127.0.0.1:8080")
CHAT_ENDPOINT = getattr(config, "LOCAL_AI_ENDPOINT", "/v1/chat/completions")
MODEL_06B_NAME = getattr(config, "LOCAL_AI_CLASSIFIER_MODEL", "qwen3-0.6b_w8a8_rk3588")

# Các mục trong CSDL mang tính generic hoặc placeholder, chỉ cho phép exact match, KHÔNG fuzzy match
GENERIC_EXCLUDE_FUZZY = {
    "thuốc bvtv", "thuoc bvtv", "không phát hiện", "khong phat hien", 
    "bạt phủ cỏ", "bat phu co", "phân chồng", "phan chong"
}

# Các từ khóa chỉ cây trồng, sâu bệnh hoặc tiêu đề nhãn - không phải tên thuốc/phân bón
NON_MATERIAL_TERMS = {
    "phân bón", "phan bon", "thuốc", "thuoc", "thuốc bvtv", "thuoc bvtv", "vật tư", "vat tu",
    "cây ăn trái", "cay an trai", "cây công nghiệp", "cay cong nghiep", "cây có múi", "cay co mui",
    "lúa", "lua", "rau màu", "rau mau", "xoài", "xoai", "mít", "mit", "sầu riêng", "sau rieng",
    "cam", "bưởi", "buoi", "chanh", "quýt", "quyt", "ớt", "ot", "cà phê", "ca phe", "tiêu", "tieu",
    "điều", "dieu", "thanh long", "bắp", "bap", "dưa hấu", "dua hau", "khoai mì", "khoai mi",
    "khoai lang", "khoai lang", "đậu phộng", "dau phong", "cây kiểng", "cay kieng",
    "thán thư", "than thu", "rầy nâu", "ray nau", "rệp sáp", "rep sap", "ruồi vàng", "ruoi vang",
    "nấm hồng", "nam hong", "ghẻ loét", "ghe loet", "bọ trĩ", "bo tri", "sâu vẽ bùa", "sau ve bua",
    "sâu cuốn lá", "sau cuon la", "nấm", "nam", "sâu", "sau", "rầy", "ray", "rệp", "rep",
    "thành phần", "thanh phan", "công dụng", "cong dung", "liều lượng", "lieu luong",
    "hướng dẫn", "huong dan", "khuyến nghị", "khuyen nghi", "quy cách", "quy cach",
    "thời điểm sử dụng", "thoi diem su dung", "loại cây trồng", "loai cay trong",
    "vi lượng", "vi luong", "vl luong", "vlluong", "dung tích", "khối lượng"
}

# Danh sách mẫu tiền tố loại bỏ khi làm sạch tên vật tư
PREFIX_PATTERNS = [
    r"^\s*(?:xịt|phun|bón|tưới|rải|mua|bán|dùng|sử dụng|pha|châm|chai|gói|can|hũ|bao|bình|thùng)\s+",
    r"\b\d+(?:[\.,]\d+)?\s*(?:ml|l|lit|lít|kg|g|gam|chai|can|hũ|bao|gói|bình|thùng|viên)\b",
    r"^\s*(?:thuốc trừ sâu|thuốc trừ bệnh|thuốc trị bệnh|thuốc bvtv|thuốc|phân bón vi lượng|phân bón lá|phân bón hữu cơ|phân bón|chế phẩm sinh học)\s+",
]


@dataclass
class MaterialMatchResult:
    """Kết quả đối soát một vật tư nông nghiệp với CSDL."""
    raw_name: str
    cleaned_name: str
    matched: bool
    category: Optional[str] = None          # 'pesticide' hoặc 'fertilizer'
    item_id: Optional[int] = None           # pesticide_id hoặc fertilizer_id
    canonical_name: Optional[str] = None    # Tên chuẩn lưu trong CSDL
    score: float = 0.0                      # Độ tương đồng (0 - 100)
    active_ingredient: Optional[str] = None
    formulation: Optional[str] = None
    min_isolation_days: Optional[int] = None
    max_concentration: Optional[str] = None
    registration_no: Optional[str] = None
    fertilizer_type: Optional[str] = None
    npk_ratio: Optional[str] = None
    manufacturer: Optional[str] = None
    rag_knowledge: Optional[Dict[str, Any]] = None
    is_banned: bool = False
    banned_info: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Xuất kết quả sang dict sẵn sàng serialize JSON."""
        return {
            "raw_name": self.raw_name,
            "cleaned_name": self.cleaned_name,
            "matched": self.matched,
            "category": self.category,
            "id": self.item_id,
            "name": self.canonical_name or self.raw_name,
            "score": round(self.score, 1),
            "is_banned": self.is_banned,
            "banned_info": self.banned_info,
            "details": {
                "active_ingredient": self.active_ingredient,
                "formulation": self.formulation,
                "min_isolation_days": self.min_isolation_days,
                "max_concentration": self.max_concentration,
                "registration_no": self.registration_no,
                "fertilizer_type": self.fertilizer_type,
                "npk_ratio": self.npk_ratio,
                "manufacturer": self.manufacturer,
            },
            "rag_knowledge": self.rag_knowledge,
        }


# ------------------------------------------------------------------------------
# Banned Substances Checker (Phụ lục II Thông tư 75 / Luật BVTV 41/2013)
# ------------------------------------------------------------------------------

_BANNED_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "knowledge", "safety", "banned_pesticides.json"
)
_BANNED_DATA: Optional[Dict[str, Any]] = None

def get_banned_pesticides_list() -> List[Dict[str, Any]]:
    """Tải danh sách hoạt chất cấm từ file JSON."""
    global _BANNED_DATA
    if _BANNED_DATA is None:
        if os.path.exists(_BANNED_DB_PATH):
            try:
                with open(_BANNED_DB_PATH, "r", encoding="utf-8") as f:
                    _BANNED_DATA = json.load(f)
            except Exception as e:
                logger.error("Không thể load danh sách hoạt chất cấm: %s", e)
                _BANNED_DATA = {"banned_ingredients": []}
        else:
            _BANNED_DATA = {"banned_ingredients": []}
    return _BANNED_DATA.get("banned_ingredients", [])


def check_banned_ingredient(text: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Kiểm tra một chuỗi văn bản (tên thuốc, hoạt chất OCR/VLM) có chứa hoạt chất cấm hay không.
    Sử dụng SafetyChecker chuyên sâu với 36 hoạt chất + alias thương mại phổ biến.
    Trả về: (is_banned, banned_info)
    """
    if not text or not text.strip():
        return False, None

    try:
        from services.rag.safety_checker import safety_checker
        res = safety_checker.check_banned_substance(text)
        if res.is_banned:
            return True, res.to_dict()
    except Exception as e:
        logger.warning("Lỗi kiểm tra chất cấm qua safety_checker: %s", e)

    # Fallback cơ bản nếu safety_checker gặp lỗi ngoại lệ
    t_norm = text.lower()
    banned_list = get_banned_pesticides_list()
    for item in banned_list:
        name = item.get("name", "").lower()
        if name and re.search(r"\b" + re.escape(name) + r"\b", t_norm):
            return True, item
        for alias in item.get("aliases", []):
            if alias and len(alias) >= 3:
                if re.search(r"\b" + re.escape(alias.lower()) + r"\b", t_norm):
                    return True, item

    return False, None



def clean_material_name(text: str) -> str:
    """Làm sạch các từ ngữ chỉ hành động, đơn vị hoặc tiền tố gây nhiễu."""
    if not text:
        return ""
    # Tách số và chữ dính liền: SIAMB12 -> SIAMB 12, 68WG -> 68 WG
    text = re.sub(r"([a-zA-ZÀ-ỹ])(\d)", r"\1 \2", text)
    text = re.sub(r"(\d)([a-zA-ZÀ-ỹ])", r"\1 \2", text)
    # Lặp để loại bỏ các tiền tố nối tiếp (ví dụ: "xịt thuốc ...", "bón 5 bao phân ...")
    while True:
        prev = text
        for pat in PREFIX_PATTERNS:
            text = re.sub(pat, " ", text, flags=re.IGNORECASE)
        text = text.strip()
        if text == prev:
            break
    # Bỏ ký tự đặc biệt thừa
    text = re.sub(r"[()\[\]{}:;,.*\\-_/\\\"\'~`!@#$%^&]", " ", text)
    # Chuẩn hóa khoảng trắng
    return re.sub(r"\s+", " ", text).strip()


def extract_header_lines(text: str, max_lines: int = 8) -> List[str]:
    """Trích xuất các dòng tiêu đề / đầu nhãn bao bì chai thuốc hoặc hóa đơn."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    stop_markers = (
        "thành phần", "thanh phan", "công dụng", "cong dung", 
        "hướng dẫn", "huong dan", "khuyến nghị", "khuyen nghi", 
        "liều lượng", "lieu luong", "đối tượng", "doi tuong",
        "nsx", "hsd", "quy cách", "bảo quản", "bao quan", "lưu ý", "luu y"
    )

    header_lines = []
    for line in lines[:max_lines]:
        line_lower = line.lower()
        if any(marker in line_lower for marker in stop_markers):
            break
        if len(line) >= 2:
            header_lines.append(line)
    return header_lines


class MaterialMatcher:
    """
    Quản lý việc trích xuất vật tư qua LLM 0.6B và fuzzy match với CSDL MySQL.
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(2)
        self._db_cache: List[Dict[str, Any]] = []
        self._cache_timestamp: float = 0.0
        self._cache_ttl_sec: float = 300.0  # Tải lại CSDL mỗi 5 phút

    def _ensure_session(self) -> aiohttp.ClientSession:
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None

        if (
            self._session is None
            or self._session.closed
            or getattr(self._session, "_loop", None) is None
            or self._session._loop.is_closed()
            or (curr_loop and self._session._loop != curr_loop)
        ):
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def close(self):
        """Đóng session khi ứng dụng tắt."""
        if self._session and not self._session.closed:
            await self._session.close()

    # --------------------------------------------------------------------------
    # Database Cache
    # --------------------------------------------------------------------------

    def reload_catalog(self, force: bool = False):
        """Tải toàn bộ danh mục thuốc BVTV và phân bón từ MySQL vào bộ nhớ đệm."""
        now = time.time()
        if not force and self._db_cache and (now - self._cache_timestamp < self._cache_ttl_sec):
            return

        import pymysql
        conn = pymysql.connect(
            host=config.MYSQL_HOST,
            port=config.MYSQL_PORT,
            user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD,
            database=config.MYSQL_DATABASE,
            unix_socket=config.MYSQL_SOCKET if config.MYSQL_SOCKET and os.path.exists(config.MYSQL_SOCKET) else None,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )

        items = []
        try:
            with conn.cursor() as cur:
                # 1. Tải danh mục thuốc BVTV
                cur.execute("""
                    SELECT pesticide_id, name, active_ingredient, formulation, 
                           registration_no, min_isolation_days, max_concentration, 
                           toxicity_group, allowed_crops, manufacturer, rag_knowledge 
                    FROM pesticides;
                """)
                for r in cur.fetchall():
                    rag = r.get("rag_knowledge")
                    if isinstance(rag, str):
                        try:
                            rag = json.loads(rag)
                        except Exception:
                            pass
                    items.append({
                        "category": "pesticide",
                        "id": r["pesticide_id"],
                        "name": r["name"],
                        "cleaned_name": clean_material_name(r["name"]),
                        "active_ingredient": r.get("active_ingredient"),
                        "formulation": r.get("formulation"),
                        "min_isolation_days": r.get("min_isolation_days"),
                        "max_concentration": r.get("max_concentration"),
                        "registration_no": r.get("registration_no"),
                        "manufacturer": r.get("manufacturer"),
                        "rag_knowledge": rag,
                    })

                # 2. Tải danh mục phân bón
                cur.execute("""
                    SELECT fertilizer_id, name, fertilizer_type, npk_ratio, 
                           registration_no, manufacturer, rag_knowledge 
                    FROM fertilizers;
                """)
                for r in cur.fetchall():
                    rag = r.get("rag_knowledge")
                    if isinstance(rag, str):
                        try:
                            rag = json.loads(rag)
                        except Exception:
                            pass
                    items.append({
                        "category": "fertilizer",
                        "id": r["fertilizer_id"],
                        "name": r["name"],
                        "cleaned_name": clean_material_name(r["name"]),
                        "fertilizer_type": r.get("fertilizer_type"),
                        "npk_ratio": r.get("npk_ratio"),
                        "registration_no": r.get("registration_no"),
                        "manufacturer": r.get("manufacturer"),
                        "rag_knowledge": rag,
                    })

            self._db_cache = items
            self._cache_timestamp = now
            logger.info("Đã nạp %d vật tư (thuốc & phân bón) từ CSDL vào bộ nhớ Fuzzy Match.", len(items))
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # LLM 0.6B Extraction
    # --------------------------------------------------------------------------

    async def extract_materials_with_llm(self, text: str) -> List[str]:
        """
        Sử dụng mô hình LLM 0.6B (Port 8081) để trích xuất tên thuốc/phân bón từ văn bản.
        Tự động fallback sang cổng 8080 (model 2B) nếu 8081 gặp lỗi.
        """
        if not text or not text.strip():
            return []

        # Nếu là văn bản nhiều dòng (nhãn bao bì/hóa đơn từ OCR), ưu tiên các dòng header
        header_lines = extract_header_lines(text)
        if len(header_lines) >= 1 and len(text.splitlines()) > 3:
            clean_input = "\n".join(header_lines)
        else:
            clean_input = text.strip()
            if len(clean_input) > 1000:
                clean_input = clean_input[:1000]

        system_prompt = (
            "Bạn là chuyên gia AI nông nghiệp. Hãy tìm tất cả các loại thuốc BVTV và phân bón (bao gồm cả phân chuồng, phân hữu cơ, NPK) có trong văn bản.\n"
            "Quy tắc:\n"
            "- Không lấy tên cây trồng (cam, bưởi, lúa, xoài, sầu riêng).\n"
            "- Không lấy tên sâu bệnh (thán thư, rầy nâu, rệp sáp).\n"
            "- Trích xuất đầy đủ cả thuốc BVTV và phân bón (ví dụ: Ridomil Gold, Phân chuồng, NPK, Anvil, SIAMB 12...).\n"
            'Định dạng JSON: {"materials": ["..."]}\n'
            'Nếu không có, trả về: {"materials": []}'
        )

        payload = {
            "model": MODEL_06B_NAME,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f'Văn bản: {clean_input}\nKết quả:'},
            ],
            "temperature": 0.0,
            "max_tokens": 128,
            "stream": False,
        }

        # Danh sách endpoint thử nghiệm: Ưu tiên cổng 8081 (0.6B NPU), sau đó 8080 (2B NPU)
        endpoints = [
            (f"{DEFAULT_06B_URL.rstrip('/')}{CHAT_ENDPOINT}", "0.6B-8081"),
            (f"{DEFAULT_2B_URL.rstrip('/')}{CHAT_ENDPOINT}", "2B-8080"),
        ]

        session = self._ensure_session()
        extracted_names: List[str] = []

        for url, tag in endpoints:
            t0 = time.time()
            try:
                async with self._semaphore:
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                        elapsed = time.time() - t0
                        if resp.status != 200:
                            logger.warning("[Material Extractor %s] HTTP %d (%.2fs)", tag, resp.status, elapsed)
                            continue

                        data = await resp.json()
                        raw_content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

                        # Xử lý thẻ think nếu có
                        if "</think>" in raw_content:
                            raw_content = raw_content.split("</think>", 1)[1].strip()

                        # Làm sạch markdown ```json ... ```
                        raw_clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_content, flags=re.MULTILINE).strip()
                        
                        try:
                            parsed = json.loads(raw_clean)
                            if isinstance(parsed, dict) and "materials" in parsed:
                                items = parsed["materials"]
                                if isinstance(items, list):
                                    extracted_names = [str(x).strip() for x in items if str(x).strip()]
                                    logger.info("[Material Extractor %s] Trích xuất thành công %d tên vật tư (%.2fs): %s", tag, len(extracted_names), elapsed, extracted_names)
                                    return extracted_names
                        except Exception as parse_err:
                            logger.warning("[Material Extractor %s] Lỗi parse JSON từ LLM (%s): %s", tag, parse_err, raw_clean[:100])

            except Exception as exc:
                logger.warning("[Material Extractor %s] Kết nối thất bại: %s", tag, exc)

        # Fallback Heuristic Regex nếu LLM không khả dụng
        logger.info("Chạy fallback Regex heuristic để tìm kiếm tên vật tư...")
        return self._regex_heuristic_extract(clean_input)

    def _regex_heuristic_extract(self, text: str) -> List[str]:
        """Tìm kiếm theo từ khóa nông nghiệp nếu LLM không khả dụng."""
        self.reload_catalog()
        found = []
        text_lower = text.lower()
        for item in self._db_cache:
            name_lower = item["name"].lower()
            clean_lower = item["cleaned_name"].lower()
            if clean_lower in GENERIC_EXCLUDE_FUZZY:
                continue
            if clean_lower and len(clean_lower) >= 3 and clean_lower in text_lower:
                found.append(item["name"])
            elif name_lower in text_lower:
                found.append(item["name"])
        # Loại trùng lặp giữ thứ tự
        seen = set()
        res = []
        for x in found:
            if x.lower() not in seen:
                seen.add(x.lower())
                res.append(x)
        return res

    # --------------------------------------------------------------------------
    # Fuzzy Matching Engine
    # --------------------------------------------------------------------------

    def fuzzy_match(
        self,
        raw_name: str,
        category_hint: Optional[str] = None,
        min_score: float = 60.0,
    ) -> MaterialMatchResult:
        """
        So khớp mờ tên vật tư với danh mục trong CSDL MySQL.
        """
        self.reload_catalog()
        cleaned = clean_material_name(raw_name)

        if not cleaned:
            return MaterialMatchResult(
                raw_name=raw_name,
                cleaned_name="",
                matched=False,
                score=0.0,
            )

        q_raw_lower = raw_name.lower().strip()
        q_clean_lower = cleaned.lower().strip()

        # Kiểm tra danh sách loại trừ cây trồng/sâu bệnh/từ phi vật tư
        if q_clean_lower in NON_MATERIAL_TERMS or q_raw_lower in NON_MATERIAL_TERMS:
            return MaterialMatchResult(
                raw_name=raw_name,
                cleaned_name=cleaned,
                matched=False,
                score=0.0,
            )

        best_item: Optional[Dict[str, Any]] = None
        best_score = 0.0

        for item in self._db_cache:
            # Lọc theo category_hint nếu có
            if category_hint and item["category"] != category_hint:
                continue

            target_name = item["name"]
            target_clean = item["cleaned_name"]

            t_name_lower = target_name.lower().strip()
            t_clean_lower = target_clean.lower().strip()

            # Nếu bản ghi trong CSDL là từ generic placeholder (Thuốc BVTV, Không Phát Hiện...), chỉ cho phép exact match
            if t_clean_lower in GENERIC_EXCLUDE_FUZZY or t_name_lower in GENERIC_EXCLUDE_FUZZY:
                if q_clean_lower == t_clean_lower or q_raw_lower == t_name_lower:
                    score = 100.0
                else:
                    continue
            else:
                # 1. So khớp tuyệt đối hoặc chuỗi con trực tiếp (Substring containment)
                if q_clean_lower == t_clean_lower or q_clean_lower == t_name_lower:
                    score = 100.0
                elif t_clean_lower and len(t_clean_lower) >= 3 and len(q_clean_lower) >= 3 and (t_clean_lower in q_clean_lower or q_clean_lower in t_clean_lower):
                    if len(q_clean_lower) >= 4 and len(t_clean_lower) >= 4:
                        score = 95.0
                    else:
                        score = 80.0
                else:
                    # 2. Thuật toán Fuzzy Matching đa tiêu chí kết hợp trọng số
                    s_set = fuzz.token_set_ratio(q_clean_lower, t_clean_lower)
                    s_sort = fuzz.token_sort_ratio(q_clean_lower, t_clean_lower)
                    s_ratio = fuzz.ratio(q_clean_lower, t_clean_lower)
                    s_part = fuzz.partial_ratio(q_clean_lower, t_name_lower)
                    score = round(s_set * 0.45 + s_sort * 0.25 + s_ratio * 0.15 + s_part * 0.15, 1)

            if score > best_score:
                best_score = score
                best_item = item
        # 3. Kiểm tra hoạt chất cấm
        is_banned, banned_info = check_banned_ingredient(raw_name)
        if not is_banned and cleaned:
            is_banned, banned_info = check_banned_ingredient(cleaned)

        if best_item and best_score >= min_score:
            if not is_banned and best_item.get("active_ingredient"):
                is_banned, banned_info = check_banned_ingredient(best_item["active_ingredient"])

            return MaterialMatchResult(
                raw_name=raw_name,
                cleaned_name=cleaned,
                matched=True,
                category=best_item["category"],
                item_id=best_item["id"],
                canonical_name=best_item["name"],
                score=best_score,
                active_ingredient=best_item.get("active_ingredient"),
                formulation=best_item.get("formulation"),
                min_isolation_days=best_item.get("min_isolation_days"),
                max_concentration=best_item.get("max_concentration"),
                registration_no=best_item.get("registration_no"),
                fertilizer_type=best_item.get("fertilizer_type"),
                npk_ratio=best_item.get("npk_ratio"),
                manufacturer=best_item.get("manufacturer"),
                rag_knowledge=best_item.get("rag_knowledge"),
                is_banned=is_banned,
                banned_info=banned_info,
            )

        return MaterialMatchResult(
            raw_name=raw_name,
            cleaned_name=cleaned,
            matched=False,
            score=best_score,
            canonical_name=best_item["name"] if best_item else None,
            category=best_item["category"] if best_item else None,
            is_banned=is_banned,
            banned_info=banned_info,
        )

    # --------------------------------------------------------------------------
    # Pipeline: Extract with LLM 0.6B + Fuzzy Match
    # --------------------------------------------------------------------------

    async def extract_and_match(
        self,
        text: str,
        min_score: float = 60.0,
    ) -> List[MaterialMatchResult]:
        """
        Pipeline trọn gói:
        1. Gọi LLM 0.6B (Port 8081) để bóc tách các tên thuốc / phân bón từ văn bản.
        2. Bổ sung các ứng viên tiềm năng từ header (nếu là kết quả OCR nhãn bao bì).
        3. Chạy Fuzzy Matching từng tên với CSDL MySQL vietgap_diary.
        4. Trả về danh sách MaterialMatchResult chuẩn hóa.
        """
        # 1. Trích xuất qua LLM 0.6B
        raw_names = await self.extract_materials_with_llm(text)
        if not isinstance(raw_names, list):
            raw_names = []

        # 2. Bổ trợ quét từ khóa danh mục CSDL (bắt nhanh các vật tư phổ biến như phân chuồng, NPK, Ridomil...)
        keyword_hits = self._regex_heuristic_extract(text)
        for kh in keyword_hits:
            if kh not in raw_names:
                raw_names.append(kh)

        results: List[MaterialMatchResult] = []
        seen_matched_ids = set()

        def _add_result(match_res: MaterialMatchResult):
            nonlocal results, seen_matched_ids
            item_key = (match_res.category, match_res.item_id) if match_res.matched else match_res.raw_name.lower()
            if item_key in seen_matched_ids:
                return

            if match_res.matched and match_res.canonical_name:
                cand_canon = match_res.canonical_name.lower()
                for idx, r in enumerate(results):
                    if r.matched and r.category == match_res.category and r.canonical_name:
                        exist_canon = r.canonical_name.lower()
                        # Nếu kết quả mới dài hơn và chứa kết quả cũ (ví dụ: 'Phân chuồng' vs 'Chuồng') -> thay thế
                        if exist_canon in cand_canon and len(cand_canon) > len(exist_canon):
                            seen_matched_ids.discard((r.category, r.item_id))
                            seen_matched_ids.add(item_key)
                            results[idx] = match_res
                            return
                        elif cand_canon in exist_canon:
                            return

            seen_matched_ids.add(item_key)
            results.append(match_res)

        for name in raw_names:
            match_res = self.fuzzy_match(name, min_score=min_score)
            _add_result(match_res)

        # 3. Nếu văn bản dạng nhiều dòng (nhãn OCR bao bì hoặc hóa đơn chứng từ)
        lines = [line.strip() for line in text.splitlines() if len(line.strip()) >= 3]
        if len(lines) > 1:
            for line in lines:
                if line.lower() in NON_MATERIAL_TERMS:
                    continue
                # Quét dòng với ngưỡng tin cậy cao >= 75.0 để tránh false positive
                line_match = self.fuzzy_match(line, min_score=75.0)
                if line_match.matched:
                    _add_result(line_match)

        return results

    def extract_and_match_sync(
        self,
        text: str,
        min_score: float = 60.0,
    ) -> List[MaterialMatchResult]:
        """Phiên bản đồng bộ cho CLI và Flask routes đồng bộ."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import nest_asyncio
                nest_asyncio.apply()
                return loop.run_until_complete(self.extract_and_match(text, min_score))
            return loop.run_until_complete(self.extract_and_match(text, min_score))
        except Exception:
            return asyncio.run(self.extract_and_match(text, min_score))


# Singleton instance dùng chung toàn hệ thống
material_matcher = MaterialMatcher()
