"""
RAG Engine (services/rag_engine.py)
===================================
Hệ thống RAG thông minh tích hợp Google Gemini cho cây trồng theo tiêu chuẩn VietGAP:
1. Safety Guardrail: Kiểm tra chất cấm siêu tốc (< 1ms) theo Thông tư 75 & Luật BVTV 41/2013.
2. Hybrid Retrieval: Tìm kiếm đa tầng kết hợp Dense Embedding (E5-multilingual) + BM25Okapi + RRF.
3. Gemini Generation: Sinh câu trả lời chuẩn xác, văn phong chuyên nghiệp, có dẫn nguồn cụ thể [1], [2].
4. TTS Adaptor: Tự động trích xuất spoken text ngắn gọn, ấm áp cho giọng đọc Piper VITS.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import os
import re
import ssl
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import urllib.request
import urllib.error

import certifi
import aiohttp

import config
from services.rag.safety_checker import safety_checker, SafetyQueryResult

logger = logging.getLogger("services.rag_engine")

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

# Thứ tự ưu tiên các model Gemini
GEMINI_MODELS_POOL = [
    getattr(config, "GEMINI_MODEL", "gemini-3.5-flash-lite"),
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash",
]

RAG_SYSTEM_PROMPT = """Bạn là trợ lý Nông Ký AI kiêm kỹ sư nông nghiệp VietGAP chuyên nghiệp cho cây bưởi và cây ăn quả có múi.
Nhiệm vụ: Trả lời câu hỏi của bác nông dân DỰA TRÊN TÀI LIỆU THAM KHẢO VIETGAP được cung cấp.

QUY TẮC CỐT LÕI (BẮT BUỘC TUÂN THỦ NGHIÊM NGẶT):
1. VĂN PHONG HỘI THOẠI ĐỒNG NHẤT & LỄ PHÉP:
   - Luôn bắt đầu câu trả lời bằng từ 'Dạ' (ví dụ: 'Dạ, đối với...', 'Dạ, để phòng trừ...', 'Dạ, bác có thể dùng...').
   - Xưng hô thân mật, chuẩn mực: 'em' và 'bác'.
   - Trả lời tự nhiên bằng đoạn văn xuôi ngắn gọn. TUYỆT ĐỐI KHÔNG dùng gạch đầu dòng (-), bullet point (*), danh sách đánh số hay tiêu đề thô (###).
   - Tuyệt đối KHÔNG viết lời chào dài dòng kiểu diễn văn ('Chào bà con, tôi là...', 'Tôi xin trình bày...').

2. ĐI THẲNG TRỌNG TÂM & ĐỘ DÀI TỐI ƯU CHO GIỌNG ĐỌC (TTS):
   - Đi thẳng vào giải pháp ngay câu đầu tiên: Bác hỏi thuốc gì -> nêu thuốc; hỏi liều lượng -> nêu liều pha; hỏi cách ly -> nêu số ngày PHI; hỏi bón phân -> nêu loại phân và lượng bón.
   - Nêu rõ các thông số kỹ thuật cốt lõi: Tên thuốc/chế phẩm sinh học được phép + Liều lượng pha + Thời gian cách ly (PHI) nếu có trong tài liệu. In đậm tên thuốc và thông số quan trọng (ví dụ: **NOFARA 35WG**, **14 ngày**).
   - ĐỘ DÀI CHUẨN VÀNG: Trả lời cô đọng trong 3 đến 4 câu văn xuôi hoàn chỉnh (tổng cộng khoảng 50 - 75 từ, tương đương 300 - 450 ký tự) để người nghe nắm bắt trọn vẹn thông tin và loa đọc phát âm hết 100% nội dung một cách tự nhiên.

3. AN TOÀN VIETGAP & KHÔNG GHI NGUỒN / KHÔNG CHUYỂN HƯỚNG GHI SỔ:
   - Ưu tiên chế phẩm sinh học, vi sinh trước khi can thiệp hóa học.
   - Tuyệt đối KHÔNG gợi ý hoạt chất cấm theo Thông tư 75 (như Regent/Fipronil, Glyphosate, Paraquat, Wofatox...).
   - Tuyệt đối KHÔNG ghi số hiệu nguồn, ký hiệu trích dẫn hay tài liệu tham khảo (như [1], [2], [Tài liệu X]) trong câu trả lời.
   - Tuyệt đối KHÔNG gợi ý, không nhắc chuyển hướng sang ghi sổ nhật ký hay hỏi có muốn ghi nhật ký không khi đang trả lời câu hỏi tư vấn chuyên môn/dữ liệu.
"""

TTS_EXTRACTION_PROMPT = """Hãy tóm tắt câu trả lời tư vấn kỹ thuật trên thành 2-3 câu ngắn gọn để phát âm qua loa đọc (TTS) cho bác nông dân nghe.
Yêu cầu:
- Tuyệt đối không dùng ký hiệu markdown (*, #, -), số thứ tự hay bảng biểu.
- Nói thẳng vào cách xử lý chính và lời khuyên an toàn VietGAP.
- Xưng hô 'em' và 'bác' thân mật, lễ phép. Độ dài dưới 60 từ."""


@dataclass
class RAGAnswerResult:
    """Kết quả phản hồi hoàn chỉnh từ RAG Engine."""
    question: str
    answer: str
    spoken_text: str
    sources: List[Dict[str, Any]] = field(default_factory=list)
    retrieval_ms: float = 0.0
    llm_ms: float = 0.0
    total_ms: float = 0.0
    model_used: str = "gemini"
    is_safety_guardrail: bool = False
    safety_details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "spoken_text": self.spoken_text,
            "sources": self.sources,
            "retrieval_ms": round(self.retrieval_ms, 1),
            "llm_ms": round(self.llm_ms, 1),
            "total_ms": round(self.total_ms, 1),
            "model_used": self.model_used,
            "is_safety_guardrail": self.is_safety_guardrail,
            "safety_details": self.safety_details,
        }


class RAGEngine:
    """Bộ điều phối RAG thông minh tích hợp Google Gemini."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or config.GEMINI_API_KEY
        self._kb = None
        self._async_session: Optional[aiohttp.ClientSession] = None

    def _get_kb(self):
        """Khởi tạo lazy RAGKnowledgeBase."""
        if self._kb is None:
            from services.rag.rag_knowledge_base import RAGKnowledgeBase
            self._kb = RAGKnowledgeBase()
        return self._kb

    async def _get_async_session(self) -> aiohttp.ClientSession:
        if self._async_session is None or self._async_session.closed:
            connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT)
            self._async_session = aiohttp.ClientSession(connector=connector)
        return self._async_session

    async def close(self) -> None:
        """Đóng session aiohttp."""
        if self._async_session and not self._async_session.closed:
            await self._async_session.close()
            self._async_session = None

    @staticmethod
    def _clean_tts_text(raw_text: str) -> str:
        """Làm sạch văn bản để đọc mượt mà bằng giọng đọc Piper VITS."""
        clean = raw_text
        clean = re.sub(r"\[ID\s*\d+\]", "", clean)
        clean = re.sub(r"\[[\d\s,]+\]", "", clean)
        clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", clean)
        clean = re.sub(r"\*([^*]+)\*", r"\1", clean)
        clean = re.sub(r"#{1,6}\s*", "", clean)
        clean = re.sub(r"^[ \t]*[•\-\*]\s*", "", clean, flags=re.MULTILINE)
        clean = re.sub(r"[━─\-_=]{3,}", "", clean)
        clean = re.sub(r"[🌿🐛🧪🌱🌾🧾📷🔍📝📦💡⚖️⏳🎯✅⚠️✓✕🔊🔇●👉🌾🚜🏪💰📍🤝🛡️♻️⚒️⛔🚨]", "", clean)
        clean = re.sub(r"\n+", ". ", clean)
        clean = re.sub(r"\s*,\s*,", ",", clean)
        clean = re.sub(r"\s*,\s*\.", ".", clean)
        clean = re.sub(r"\s+([,.\?!])", r"\1", clean)
        clean = re.sub(r"\.\s*\.", ".", clean)
        # Bảo toàn 100% câu trả lời để giọng đọc TTS phát âm trọn vẹn không bị ngắt cụt.
        # Chỉ ngắt tại dấu chấm câu kết thúc nếu vượt quá 700 ký tự (phòng ngừa câu quá dài bất thường).
        if len(clean) > 700:
            truncated = clean[:700]
            last_dot = max(truncated.rfind("."), truncated.rfind("?"), truncated.rfind("!"))
            if last_dot > 350:
                clean = truncated[:last_dot + 1]
            else:
                clean = truncated.strip() + "."
        return clean

    # ──────────────────────────────────────────────────────────────────────────
    # Gemini API Callers (Sync & Async)
    # ──────────────────────────────────────────────────────────────────────────

    def _call_gemini_sync(
        self,
        system_instruction: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_output_tokens: int = 1024,
    ) -> Tuple[str, str]:
        """Gọi Google Gemini đồng bộ qua urllib, tự động fallback nếu model bận."""
        if not self.api_key:
            raise RuntimeError("Thiếu GEMINI_API_KEY trong cấu hình hệ thống.")

        last_error = None
        for model_name in GEMINI_MODELS_POOL:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.api_key}"
            payload = {
                "system_instruction": {"parts": [{"text": system_instruction}]},
                "contents": [{"parts": [{"text": user_prompt}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_output_tokens,
                },
            }
            data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data_bytes,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=config.GEMINI_TIMEOUT_SEC) as resp:
                    resp_json = json.loads(resp.read().decode("utf-8"))
                    candidates = resp_json.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            return parts[0].get("text", "").strip(), model_name
            except Exception as e:
                last_error = str(e)
                logger.warning("Gemini model %s lỗi: %s, đang thử model tiếp theo...", model_name, e)

        raise RuntimeError(f"Tất cả các model Gemini đều gặp lỗi. Lỗi cuối: {last_error}")

    async def _call_gemini_async(
        self,
        system_instruction: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_output_tokens: int = 1024,
    ) -> Tuple[str, str]:
        """Gọi Google Gemini bất đồng bộ qua aiohttp, tự động fallback model."""
        if not self.api_key:
            raise RuntimeError("Thiếu GEMINI_API_KEY trong cấu hình hệ thống.")

        session = await self._get_async_session()
        last_error = None

        for model_name in GEMINI_MODELS_POOL:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.api_key}"
            payload = {
                "system_instruction": {"parts": [{"text": system_instruction}]},
                "contents": [{"parts": [{"text": user_prompt}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_output_tokens,
                },
            }
            timeout = aiohttp.ClientTimeout(total=config.GEMINI_TIMEOUT_SEC)
            try:
                async with session.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=timeout) as resp:
                    if resp.status == 200:
                        resp_json = await resp.json()
                        candidates = resp_json.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            if parts:
                                return parts[0].get("text", "").strip(), model_name
                    else:
                        err_text = await resp.text()
                        last_error = f"HTTP {resp.status}: {err_text[:200]}"
            except Exception as e:
                last_error = str(e)
                logger.warning("Gemini model %s lỗi: %s, đang thử model tiếp theo...", model_name, e)

        raise RuntimeError(f"Tất cả các model Gemini đều gặp lỗi. Lỗi cuối: {last_error}")

    # ──────────────────────────────────────────────────────────────────────────
    # Core RAG Pipeline (Sync & Async)
    # ──────────────────────────────────────────────────────────────────────────

    def ask(
        self,
        question: str,
        top_k: int = 5,
        history: Optional[List[Dict[str, str]]] = None,
        crop_name: str = "Bưởi",
        temperature: float = 0.2,
    ) -> RAGAnswerResult:
        """
        Quy trình RAG hoàn chỉnh (Đồng bộ):
        1. Safety Guardrail (< 1ms).
        2. Hybrid Retrieval (E5 + BM25 + RRF).
        3. Tổng hợp và gọi Gemini.
        4. Trích xuất spoken text cho TTS.
        """
        t_start = time.perf_counter()

        # Bước 0: Chốt chặn an toàn hoá chất cấm
        safety_ans = safety_checker.route_and_answer_safety_query(question)
        if safety_ans and safety_ans.handled:
            t_total = (time.perf_counter() - t_start) * 1000
            return RAGAnswerResult(
                question=question,
                answer=safety_ans.markdown_answer,
                spoken_text=safety_ans.spoken_text,
                sources=[{"title": safety_ans.source_citation, "type": "legal_guardrail"}],
                retrieval_ms=t_total,
                llm_ms=0.0,
                total_ms=t_total,
                model_used="safety_guardrail",
                is_safety_guardrail=True,
                safety_details=safety_ans.to_dict(),
            )

        # Bước 1: Hybrid Retrieval
        kb = self._get_kb()
        t0_ret = time.perf_counter()
        results = kb.search(question, top_k=top_k, use_hybrid=True)
        t_ret = (time.perf_counter() - t0_ret) * 1000

        # Bước 2: Tạo Context Block và Danh sách nguồn
        context_parts = []
        sources = []
        for i, r in enumerate(results, 1):
            src_title = f"{r.get('section_title', '')} ({r.get('source_file', '')})"
            context_parts.append(f"- {src_title}\n{r.get('text', '')}")
            sources.append({
                "rank": i,
                "score": r.get("score"),
                "id": r.get("id"),
                "section_title": r.get("section_title"),
                "source_file": r.get("source_file"),
                "source_pages": r.get("source_pages", []),
            })
        context_text = "\n\n---\n\n".join(context_parts)

        # Bước 3: Chuẩn bị Prompt cho Gemini
        history_text = ""
        if history:
            tail = history[-4:]
            hist_lines = [f"{h.get('role', 'user').title()}: {h.get('content', '')}" for h in tail]
            history_text = "LỊCH SỬ TRAO ĐỔI TRƯỚC ĐÓ:\n" + "\n".join(hist_lines) + "\n\n"

        user_content = (
            f"GIỐNG CÂY TRỒNG MỤC TIÊU: {crop_name.upper()}\n\n"
            f"{history_text}"
            f"=== TÀI LIỆU THAM KHẢO VIETGAP ===\n"
            f"{context_text if context_text else 'Không có tài liệu cụ thể phù hợp.'}\n"
            f"=== HẾT TÀI LIỆU ===\n\n"
            f"CÂU HỎI CỦA BÀ CON NÔNG DÂN:\n\"{question}\""
        )

        # Bước 4: Gọi Gemini
        t0_llm = time.perf_counter()
        try:
            answer, model_used = self._call_gemini_sync(
                system_instruction=RAG_SYSTEM_PROMPT,
                user_prompt=user_content,
                temperature=temperature,
            )
        except Exception as e:
            logger.error("Lỗi gọi Gemini cho RAG: %s", e)
            answer = f"⚠️ Không thể kết nối với dịch vụ Gemini: {e}."
            model_used = "error"
        t_llm = (time.perf_counter() - t0_llm) * 1000

        # Làm sạch câu trả lời: gỡ bỏ hoàn toàn mọi ký hiệu trích dẫn
        answer = re.sub(r"\[ID\s*\d+\]", "", answer)
        answer = re.sub(r"\[[\d\s,]+\]", "", answer)
        answer = re.sub(r"\s+([,.\?!])", r"\1", answer)
        answer = re.sub(r"[ \t]+", " ", answer).strip()

        # Bước 5: Tạo Spoken text
        spoken_text = self._clean_tts_text(answer)

        t_total = (time.perf_counter() - t_start) * 1000
        return RAGAnswerResult(
            question=question,
            answer=answer,
            spoken_text=spoken_text,
            sources=sources,
            retrieval_ms=t_ret,
            llm_ms=t_llm,
            total_ms=t_total,
            model_used=model_used,
            is_safety_guardrail=False,
        )

    async def ask_async(
        self,
        question: str,
        top_k: int = 5,
        history: Optional[List[Dict[str, str]]] = None,
        crop_name: str = "Bưởi",
        temperature: float = 0.2,
    ) -> RAGAnswerResult:
        """Phiên bản bất đồng bộ của ask() tối ưu cho Web App / API."""
        t_start = time.perf_counter()

        # Bước 0: Chốt chặn an toàn hoá chất cấm
        safety_ans = safety_checker.route_and_answer_safety_query(question)
        if safety_ans and safety_ans.handled:
            t_total = (time.perf_counter() - t_start) * 1000
            return RAGAnswerResult(
                question=question,
                answer=safety_ans.markdown_answer,
                spoken_text=safety_ans.spoken_text,
                sources=[{"title": safety_ans.source_citation, "type": "legal_guardrail"}],
                retrieval_ms=t_total,
                llm_ms=0.0,
                total_ms=t_total,
                model_used="safety_guardrail",
                is_safety_guardrail=True,
                safety_details=safety_ans.to_dict(),
            )

        # Bước 1: Hybrid Retrieval
        kb = self._get_kb()
        t0_ret = time.perf_counter()
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, lambda: kb.search(question, top_k=top_k, use_hybrid=True))
        t_ret = (time.perf_counter() - t0_ret) * 1000

        # Bước 2: Tạo Context Block và Sources
        context_parts = []
        sources = []
        for i, r in enumerate(results, 1):
            src_title = f"{r.get('section_title', '')} ({r.get('source_file', '')})"
            context_parts.append(f"- {src_title}\n{r.get('text', '')}")
            sources.append({
                "rank": i,
                "score": r.get("score"),
                "id": r.get("id"),
                "section_title": r.get("section_title"),
                "source_file": r.get("source_file"),
                "source_pages": r.get("source_pages", []),
            })
        context_text = "\n\n---\n\n".join(context_parts)

        # Bước 3: Prompt
        history_text = ""
        if history:
            tail = history[-4:]
            hist_lines = [f"{h.get('role', 'user').title()}: {h.get('content', '')}" for h in tail]
            history_text = "LỊCH SỬ TRAO ĐỔI TRƯỚC ĐÓ:\n" + "\n".join(hist_lines) + "\n\n"

        user_content = (
            f"GIỐNG CÂY TRỒNG MỤC TIÊU: {crop_name.upper()}\n\n"
            f"{history_text}"
            f"=== TÀI LIỆU THAM KHẢO VIETGAP ===\n"
            f"{context_text if context_text else 'Không có tài liệu cụ thể phù hợp.'}\n"
            f"=== HẾT TÀI LIỆU ===\n\n"
            f"CÂU HỎI CỦA BÀ CON NÔNG DÂN:\n\"{question}\""
        )

        # Bước 4: Gọi Gemini
        t0_llm = time.perf_counter()
        try:
            answer, model_used = await self._call_gemini_async(
                system_instruction=RAG_SYSTEM_PROMPT,
                user_prompt=user_content,
                temperature=temperature,
            )
        except Exception as e:
            logger.error("Lỗi gọi Gemini async cho RAG: %s", e)
            answer = f"⚠️ Không thể kết nối với dịch vụ Gemini: {e}."
            model_used = "error"
        t_llm = (time.perf_counter() - t0_llm) * 1000

        # Làm sạch câu trả lời: gỡ bỏ hoàn toàn mọi ký hiệu trích dẫn
        answer = re.sub(r"\[ID\s*\d+\]", "", answer)
        answer = re.sub(r"\[[\d\s,]+\]", "", answer)
        answer = re.sub(r"\s+([,.\?!])", r"\1", answer)
        answer = re.sub(r"[ \t]+", " ", answer).strip()

        spoken_text = self._clean_tts_text(answer)
        t_total = (time.perf_counter() - t_start) * 1000

        return RAGAnswerResult(
            question=question,
            answer=answer,
            spoken_text=spoken_text,
            sources=sources,
            retrieval_ms=t_ret,
            llm_ms=t_llm,
            total_ms=t_total,
            model_used=model_used,
            is_safety_guardrail=False,
        )


# Singleton instance toàn cục
rag_engine = RAGEngine()
