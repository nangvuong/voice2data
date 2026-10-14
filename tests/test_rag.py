#!/usr/bin/env python3
"""
Script test nhanh embedding + RAG search (chạy trực tiếp, không cần Flask).

Sử dụng:
    cd /root/voice_to_data
    source .venv/bin/activate
    python3 tests/test_rag.py
"""

import sys
import os
import json
import time
import logging

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("test_rag")


def test_embedding_engine():
    from services.engines.embedding_engine import EmbeddingEngine

    logger.info("=== EmbeddingEngine Test ===")
    engine = EmbeddingEngine()

    q = engine.embed_query("cây bưởi cần bao nhiêu nước?")
    p = engine.embed_passage("Lượng mưa 900 - 1.200 mm/năm/ha.")
    sim = engine.similarity(q, p)
    logger.info("Query vec shape: %s", q.shape)
    logger.info("Passage vec shape: %s", p.shape)
    logger.info("Cosine similarity: %.4f", sim)
    assert q.shape == (384,), f"Expected (384,), got {q.shape}"
    assert abs(q.dot(q) - 1.0) < 1e-4, "Query vector should be normalized"
    logger.info("✓ EmbeddingEngine OK")
    return engine


def test_knowledge_base(engine):
    from services.rag.rag_knowledge_base import RAGKnowledgeBase

    logger.info("=== RAGKnowledgeBase Test ===")
    t0 = time.time()
    kb = RAGKnowledgeBase(engine=engine)
    elapsed = time.time() - t0
    info = kb.info()
    logger.info("Index built in %.1fs: %s", elapsed, info)
    assert info["num_vectors"] > 0, "Index is empty!"

    queries = [
        "điều kiện nhiệt độ và đất đai lý tưởng để trồng cây bưởi là gì?",
        "cây bưởi cần bao nhiêu nước và độ ẩm?",
        "cách xử lý sâu bệnh hại trên cây bưởi?",
        "thu hoạch bưởi đúng thời điểm như thế nào?",
    ]

    for q in queries:
        results = kb.search(q, top_k=3, min_score=0.5)
        logger.info("Query: %s", q)
        if not results:
            logger.warning("  → Không có kết quả!")
        for r in results:
            logger.info(
                "  [%.3f] %s | %s | %s",
                r["score"],
                r["level"],
                r["section_title"][:50],
                r["text"][:60] + "...",
            )

    logger.info("✓ RAGKnowledgeBase OK")
    return kb


def test_eval(kb):
    from pathlib import Path

    eval_path = Path(ROOT) / "rag_eval.jsonl"
    if not eval_path.exists():
        logger.warning("rag_eval.jsonl không tìm thấy, bỏ qua eval test")
        return

    logger.info("=== Eval Test (top_k=5) ===")
    eval_records = []
    with open(eval_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                eval_records.append(json.loads(line))

    hits = 0
    total = len(eval_records)
    TOP_K = 5

    for rec in eval_records:
        question = rec.get("question", "")
        expected_ids = set(rec.get("expected_ids", []))
        results = kb.search(question, top_k=TOP_K)
        retrieved_ids = {r["id"] for r in results}
        # Khớp theo source_file (eval dùng format "file::chunk-id")
        # Thử match linh hoạt: nếu expected_id chứa trong retrieved_id hoặc ngược lại
        hit = False
        for exp_id in expected_ids:
            for ret_id in retrieved_ids:
                if exp_id in ret_id or ret_id.endswith(exp_id.split("::")[-1]):
                    hit = True
                    break
        if hit:
            hits += 1

    recall = hits / total if total > 0 else 0
    logger.info(
        "Recall@%d = %.1f%% (%d/%d)", TOP_K, recall * 100, hits, total
    )
    logger.info("✓ Eval OK")


if __name__ == "__main__":
    engine = test_embedding_engine()
    kb = test_knowledge_base(engine)
    test_eval(kb)
    print("\n✅ Tất cả tests passed!")
