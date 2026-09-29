"""
RAG Service - Flask Blueprint (services/rag_service.py)
========================================================
Flask Blueprint cung cấp REST API cho RAG (Retrieval-Augmented Generation).

Endpoints:
  GET  /rag/health          - Kiểm tra service + index info
  POST /rag/search          - Tìm kiếm vector similarity
  POST /rag/search/context  - Tìm kiếm + tổng hợp context cho LLM
  POST /rag/embed/query     - Embed 1 câu hỏi → vector
  POST /rag/embed/passages  - Embed nhiều đoạn → vectors
  POST /rag/eval            - Chạy evaluation trên rag_eval.jsonl
  POST /rag/index/rebuild   - Rebuild index thủ công

Khởi tạo lazy: RAGKnowledgeBase và EmbeddingEngine chỉ được load lần đầu
khi có request, tránh block startup của app chính.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

rag_bp = Blueprint("rag", __name__, url_prefix="/rag")

# ──────────────────────────────────────────────
# Lazy singleton
# ──────────────────────────────────────────────

_kb_lock = threading.Lock()
_kb: Optional[Any] = None  # RAGKnowledgeBase


def _get_kb():
    """Lazy-load RAGKnowledgeBase (thread-safe)."""
    global _kb
    if _kb is None:
        with _kb_lock:
            if _kb is None:
                from services.rag.rag_knowledge_base import RAGKnowledgeBase
                logger.info("Khởi tạo RAGKnowledgeBase ...")
                _kb = RAGKnowledgeBase()
    return _kb


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _json_error(msg: str, status: int = 400) -> Response:
    return jsonify({"error": msg}), status


# ──────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────

@rag_bp.get("/health")
def health():
    """Kiểm tra service và trả về thông tin index."""
    try:
        kb = _get_kb()
        return jsonify({"status": "ok", "index": kb.info()})
    except Exception as exc:
        logger.exception("RAG health error")
        return _json_error(str(exc), 500)


@rag_bp.post("/search")
def search():
    """
    Tìm kiếm vector similarity.

    Body JSON:
        {
          "query": "câu hỏi",
          "top_k": 5,               // optional, default 5
          "min_score": 0.0,         // optional, default 0.0
          "level_filter": ["fact"]  // optional, default null (all levels)
        }
    """
    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()
    if not query:
        return _json_error("Thiếu trường 'query'")

    top_k = int(data.get("top_k", 5))
    min_score = float(data.get("min_score", 0.0))
    level_filter = data.get("level_filter")  # None hoặc list[str]

    try:
        kb = _get_kb()
        t0 = time.perf_counter()
        results = kb.search(
            query,
            top_k=top_k,
            min_score=min_score,
            level_filter=level_filter,
        )
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

        return jsonify(
            {
                "query": query,
                "results": results,
                "num_results": len(results),
                "elapsed_ms": elapsed_ms,
            }
        )
    except Exception as exc:
        logger.exception("RAG search error: query=%r", query)
        return _json_error(str(exc), 500)


@rag_bp.post("/search/context")
def search_context():
    """
    Tìm kiếm + tổng hợp context text để đưa vào LLM prompt.

    Body JSON:
        {
          "query": "câu hỏi",
          "top_k": 5,
          "min_score": 0.0
        }

    Response bao gồm:
        - results: raw search results
        - context_text: đoạn text tổng hợp dạng markdown
        - sources: danh sách nguồn tài liệu
    """
    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()
    if not query:
        return _json_error("Thiếu trường 'query'")

    top_k = int(data.get("top_k", 5))
    min_score = float(data.get("min_score", 0.0))

    try:
        kb = _get_kb()
        t0 = time.perf_counter()
        result = kb.search_with_context(query, top_k=top_k, min_score=min_score)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        result["elapsed_ms"] = elapsed_ms
        return jsonify(result)
    except Exception as exc:
        logger.exception("RAG search/context error: query=%r", query)
        return _json_error(str(exc), 500)


@rag_bp.post("/embed/query")
def embed_query():
    """
    Embed 1 câu hỏi thành vector (E5 prefix "query: ...").

    Body JSON: {"query": "câu hỏi"}
    Response:  {"vector": [...384 floats...]}
    """
    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()
    if not query:
        return _json_error("Thiếu trường 'query'")

    try:
        kb = _get_kb()
        vec = kb._engine.embed_query(query)
        return jsonify({"query": query, "vector": vec.tolist(), "dim": len(vec)})
    except Exception as exc:
        logger.exception("RAG embed/query error")
        return _json_error(str(exc), 500)


@rag_bp.post("/embed/passages")
def embed_passages():
    """
    Embed nhiều đoạn văn bản (E5 prefix "passage: ...").

    Body JSON: {"passages": ["đoạn 1", "đoạn 2", ...]}
    Response:  {"vectors": [[...384 floats...], ...]}
    """
    data = request.get_json(silent=True) or {}
    passages = data.get("passages", [])
    if not passages or not isinstance(passages, list):
        return _json_error("Thiếu hoặc sai kiểu trường 'passages' (cần list[str])")

    try:
        kb = _get_kb()
        t0 = time.perf_counter()
        vecs = kb._engine.embed_passages(passages)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        return jsonify(
            {
                "num_passages": len(passages),
                "vectors": vecs.tolist(),
                "dim": vecs.shape[1],
                "elapsed_ms": elapsed_ms,
            }
        )
    except Exception as exc:
        logger.exception("RAG embed/passages error")
        return _json_error(str(exc), 500)


@rag_bp.post("/eval")
def run_eval():
    """
    Chạy evaluation trên rag_eval.jsonl.
    Tính Recall@K cho từng câu hỏi eval.

    Body JSON: {"top_k": 5, "min_score": 0.0}  (optional)
    """
    data = request.get_json(silent=True) or {}
    top_k = int(data.get("top_k", 5))
    min_score = float(data.get("min_score", 0.0))

    base_dir = Path(__file__).resolve().parent.parent.parent
    eval_path = base_dir / "data" / "rag" / "rag_eval.jsonl"
    if not eval_path.exists():
        eval_path = base_dir / "rag_eval.jsonl"
    if not eval_path.exists():
        return _json_error(f"Không tìm thấy file eval: {eval_path}", 404)

    eval_records = []
    with open(eval_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                eval_records.append(json.loads(line))

    try:
        kb = _get_kb()
        total = len(eval_records)
        hits = 0
        details = []

        t0 = time.perf_counter()
        for rec in eval_records:
            question = rec.get("question", "")
            expected_ids = set(rec.get("expected_ids", []))

            results = kb.search(question, top_k=top_k, min_score=min_score)
            retrieved_ids = {r["id"] for r in results}

            # Recall@K: có bất kỳ expected_id nào trong top-K không?
            matched = expected_ids & retrieved_ids
            hit = bool(matched)
            if hit:
                hits += 1

            details.append(
                {
                    "id": rec.get("id"),
                    "question": question,
                    "category": rec.get("category"),
                    "expected_ids": list(expected_ids),
                    "retrieved_ids": list(retrieved_ids),
                    "hit": hit,
                    "top_score": results[0]["score"] if results else 0.0,
                }
            )

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        recall_at_k = round(hits / total, 4) if total > 0 else 0.0

        return jsonify(
            {
                "total": total,
                "hits": hits,
                f"recall_at_{top_k}": recall_at_k,
                "top_k": top_k,
                "elapsed_ms": elapsed_ms,
                "details": details,
            }
        )
    except Exception as exc:
        logger.exception("RAG eval error")
        return _json_error(str(exc), 500)


@rag_bp.post("/index/rebuild")
def rebuild_index():
    """
    Rebuild toàn bộ vector index từ rag_organized.jsonl.
    Hữu ích khi data thay đổi.
    """
    global _kb
    try:
        from services.rag.rag_knowledge_base import RAGKnowledgeBase
        logger.info("Rebuilding RAG index theo yêu cầu ...")
        t0 = time.perf_counter()
        new_kb = RAGKnowledgeBase(rebuild=True)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        with _kb_lock:
            _kb = new_kb
        return jsonify(
            {
                "status": "rebuilt",
                "index": new_kb.info(),
                "elapsed_ms": elapsed_ms,
            }
        )
    except Exception as exc:
        logger.exception("RAG rebuild error")
        return _json_error(str(exc), 500)


@rag_bp.post("/ask")
def ask():
    """
    Hỏi đáp RAG thông minh bằng Google Gemini kết hợp Hybrid Search.

    Body JSON:
        {
          "query": "câu hỏi của bà con",
          "top_k": 5,                 // optional, default 5
          "temperature": 0.2,         // optional, default 0.2
          "crop_name": "Bưởi"         // optional, default "Bưởi"
        }
    """
    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()
    if not query:
        return _json_error("Thiếu trường 'query'")

    top_k = int(data.get("top_k", 5))
    temp = float(data.get("temperature", 0.2))
    crop = data.get("crop_name", "Bưởi")

    try:
        from services.rag.rag_engine import rag_engine
        result = rag_engine.ask(
            question=query,
            top_k=top_k,
            crop_name=crop,
            temperature=temp,
        )
        return jsonify(result.to_dict())
    except Exception as exc:
        logger.exception("RAG /ask error: query=%r", query)
        return _json_error(str(exc), 500)

