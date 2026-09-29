"""
RAG Knowledge Base (services/rag_knowledge_base.py)
====================================================
Xây dựng và quản lý vector index từ dữ liệu rag_organized.jsonl.

Luồng hoạt động:
1. Load tất cả records từ rag_organized.jsonl
2. Lọc các record cần embed (chunk + fact có field "embedding_text")
3. Dùng EmbeddingEngine để encode → vector index (N, 384)
4. Cache index vào disk (NPY + metadata JSON) để tránh re-encode mỗi lần khởi động
5. Tìm kiếm: embed query → cosine similarity → trả về top-K kết quả kèm context

Cấu trúc index trên disk:
  rag_index/
    vectors.npy          - (N, 384) float32
    metadata.json        - list of {"id", "level", "source_file", "section_title", ...}
    index_info.json      - checksum, build time, model path
"""

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    from rank_bm25 import BM25Okapi
except ImportError:
    BM25Okapi = None

from services.engines.embedding_engine import EmbeddingEngine

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent.parent  # /root/voice_to_data
RAG_ORGANIZED_PATH = (
    BASE_DIR / "knowledge" / "retrieval_chunks.jsonl"
    if (BASE_DIR / "knowledge" / "retrieval_chunks.jsonl").exists()
    else BASE_DIR / "rag_retrieval.jsonl"
)
INDEX_DIR = BASE_DIR / "rag_index"
VECTORS_PATH = INDEX_DIR / "vectors.npy"
METADATA_PATH = INDEX_DIR / "metadata.json"
INDEX_INFO_PATH = INDEX_DIR / "index_info.json"



# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _file_checksum(path: Path) -> str:
    """MD5 checksum của file để phát hiện thay đổi."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_records(jsonl_path: Path) -> List[Dict[str, Any]]:
    """Load toàn bộ JSONL, trả về list dict."""
    records = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ──────────────────────────────────────────────
# RAGKnowledgeBase
# ──────────────────────────────────────────────

class RAGKnowledgeBase:
    """
    Vector index của knowledge base VietGAP bưởi.

    Sử dụng:
        kb = RAGKnowledgeBase()
        results = kb.search("cây bưởi cần bao nhiêu nước?", top_k=5)
        for r in results:
            print(r["score"], r["id"], r["text"][:80])
    """

    def __init__(
        self,
        engine: Optional[EmbeddingEngine] = None,
        index_dir: Path = INDEX_DIR,
        rebuild: bool = False,
    ):
        self._engine = engine or EmbeddingEngine()
        self._index_dir = index_dir
        self._vectors: Optional[np.ndarray] = None   # (N, 384)
        self._metadata: List[Dict[str, Any]] = []    # len = N
        self._bm25: Optional[Any] = None             # BM25Okapi instance
        self._bm25_corpus_tokens: Optional[List[List[str]]] = None

        self._load_or_build(rebuild=rebuild)

    # ── Tokenizer for BM25 ────────────────────

    STOPWORDS = {
        "và", "của", "cho", "với", "các", "những", "được", "trong", "có", "là",
        "thì", "để", "như", "thế", "nào", "sao", "gì", "ở", "tại", "này", "đó",
        "khi", "sau", "trước", "ra", "vào", "lại", "hay", "hoặc", "tôi", "bạn",
        "hãy", "cho", "hỏi", "biết", "cần", "phải", "không", "thế_nào", "như_thế_nào"
    }

    @classmethod
    def _tokenize(cls, text: str) -> List[str]:
        """Tách từ cho BM25 tiếng Việt + mã thực thể, loại bỏ stopwords thông dụng."""
        if not text:
            return []
        raw_tokens = re.findall(r"[\w]+", text.lower())
        tokens = [t for t in raw_tokens if len(t) > 1 and t not in cls.STOPWORDS]
        return tokens if tokens else [t for t in raw_tokens if len(t) > 1]

    def _init_bm25(self) -> None:
        """Khởi tạo BM25 index từ metadata đã load."""
        if BM25Okapi is None:
            logger.warning("rank_bm25 chưa được cài đặt, hybrid search sẽ bị vô hiệu.")
            return

        tokenized_corpus = []
        for m in self._metadata:
            # Kết hợp title, text và code để tra cứu BM25 chính xác
            searchable_text = f"{m.get('section_title', '')} {m.get('section_code', '')} {m.get('embedding_text', '') or m.get('text', '')}"
            tokenized_corpus.append(self._tokenize(searchable_text))
        
        self._bm25 = BM25Okapi(tokenized_corpus)
        logger.info("Đã khởi tạo BM25 index với %d documents", len(tokenized_corpus))

    # ── Index build / load ─────────────────────

    def _should_rebuild(self) -> bool:
        """Kiểm tra xem index có cần build lại không."""
        if not VECTORS_PATH.exists() or not METADATA_PATH.exists() or not INDEX_INFO_PATH.exists():
            return True
        try:
            with open(INDEX_INFO_PATH, encoding="utf-8") as f:
                info = json.load(f)
            current_checksum = _file_checksum(RAG_ORGANIZED_PATH)
            return info.get("checksum") != current_checksum
        except Exception:
            return True

    def _load_or_build(self, rebuild: bool = False) -> None:
        """Load index từ cache hoặc build mới."""
        if not rebuild and not self._should_rebuild():
            logger.info("Đang load RAG index từ cache %s ...", self._index_dir)
            self._load_index()
        else:
            logger.info("Đang build RAG index từ %s ...", RAG_ORGANIZED_PATH)
            self._build_index()
        self._init_bm25()

    def _load_index(self) -> None:
        """Load vectors + metadata từ disk."""
        self._vectors = np.load(str(VECTORS_PATH))
        with open(METADATA_PATH, encoding="utf-8") as f:
            self._metadata = json.load(f)
        logger.info(
            "Loaded %d vectors (dim=%d) từ %s",
            len(self._metadata), self._vectors.shape[1], self._index_dir
        )

    def _build_index(self) -> None:
        """Encode tất cả records → lưu index."""
        records = _load_records(RAG_ORGANIZED_PATH)

        # Lấy tất cả records có nội dung để embed
        indexable = []
        texts = []
        for r in records:
            embed_t = r.get("embedding_text") or r.get("retrieval_text") or r.get("text") or ""
            if embed_t.strip():
                indexable.append(r)
                texts.append(embed_t.strip())

        logger.info(
            "Tìm thấy %d records indexable (trong tổng %d records)",
            len(indexable), len(records)
        )

        t0 = time.time()
        vectors = self._engine.embed_passages(texts)
        elapsed = time.time() - t0
        logger.info(
            "Encoded %d texts → shape %s trong %.1fs",
            len(texts), vectors.shape, elapsed
        )

        # Chuẩn bị metadata hỗ trợ đầy đủ category và metadata entities
        metadata = []
        for r, emb_t in zip(indexable, texts):
            meta_dict = r.get("metadata", {}) or {}
            
            source_file = r.get("source_file") or meta_dict.get("regulation_file")
            if not source_file and "::" in r["id"]:
                source_file = r["id"].split("::")[0]
            if not source_file:
                source_file = meta_dict.get("kind", "")

            section_title = (
                r.get("section_title")
                or meta_dict.get("section")
                or meta_dict.get("product_name")
                or meta_dict.get("title")
                or ""
            )
            if not section_title:
                section_title = r["id"]

            category = r.get("knowledge_category_name") or r.get("knowledge_type") or r.get("level") or "general"
            level = r.get("level") or r.get("knowledge_type") or "chunk"
            source_pages = r.get("source_pages") or meta_dict.get("pages") or []

            metadata.append({
                "id": r["id"],
                "level": level,
                "category": category,
                "knowledge_type": r.get("knowledge_type", ""),
                "knowledge_category_name": r.get("knowledge_category_name", ""),
                "parent_id": r.get("parent_id") or r.get("parent_group_id"),
                "source_file": source_file,
                "source_pages": source_pages,
                "section_code": r.get("section_code") or meta_dict.get("section_code", ""),
                "section_title": section_title,
                "text": r.get("text") or emb_t,
                "retrieval_text": r.get("retrieval_text") or emb_t,
                "embedding_text": emb_t,
                "metadata": meta_dict,
                "knowledge": r.get("knowledge", {}),
            })

        # Lưu ra disk
        self._index_dir.mkdir(parents=True, exist_ok=True)
        np.save(str(VECTORS_PATH), vectors)
        with open(METADATA_PATH, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        checksum = _file_checksum(RAG_ORGANIZED_PATH)
        with open(INDEX_INFO_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "checksum": checksum,
                    "build_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "num_vectors": len(metadata),
                    "embedding_dim": int(vectors.shape[1]),
                    "model_path": str(VECTORS_PATH),
                    "source_path": str(RAG_ORGANIZED_PATH),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

        self._vectors = vectors
        self._metadata = metadata
        logger.info("RAG index sẵn sàng: %d vectors", len(metadata))

    # ── Search ─────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.0,
        level_filter: Optional[List[str]] = None,
        category_filter: Optional[List[str]] = None,
        use_hybrid: bool = True,
        hybrid_alpha: float = 0.5,
    ) -> List[Dict[str, Any]]:
        """
        Tìm kiếm các đoạn văn bản gần nhất với query.

        Args:
            query:           Câu hỏi người dùng (tiếng Việt).
            top_k:           Số kết quả trả về.
            min_score:       Ngưỡng cosine similarity tối thiểu (0.0 = không lọc).
            level_filter:    Chỉ tìm trong các level nhất định.
            category_filter: Chỉ tìm trong các danh mục tri thức nhất định.
            use_hybrid:      Bật kết hợp Dense Vector (E5) + BM25 Lexical (mặc định True).
            hybrid_alpha:    Trọng số cân bằng giữa dense và bm25 trong RRF (0.0 - 1.0).

        Returns:
            List of dicts với các field: score, id, level, category, section_title,
            source_file, source_pages, text, knowledge.
        """
        if self._vectors is None or len(self._metadata) == 0:
            return []

        n_docs = len(self._metadata)

        # 1. Dense Search (E5)
        q_vec = self._engine.embed_query(query)  # (384,)
        dense_scores = self._vectors @ q_vec      # (N,) dot product = cosine sim

        # Bộ lọc level
        filter_mask = np.zeros(n_docs, dtype=bool)
        if level_filter:
            level_set = set(level_filter)
            for i, m in enumerate(self._metadata):
                if m.get("level") not in level_set:
                    filter_mask[i] = True

        # Bộ lọc category
        if category_filter:
            cat_set = set(category_filter)
            for i, m in enumerate(self._metadata):
                cat = m.get("category") or m.get("knowledge_type") or m.get("knowledge_category_name")
                if cat not in cat_set:
                    filter_mask[i] = True

        if not use_hybrid or self._bm25 is None:
            # Thuần Dense Search
            if np.any(filter_mask):
                dense_scores = np.where(filter_mask, -2.0, dense_scores)
            top_indices = np.argsort(dense_scores)[::-1][:top_k]
            final_scores = {idx: float(dense_scores[idx]) for idx in top_indices}
        else:
            # 2. BM25 Search
            q_tokens = self._tokenize(query)
            bm25_scores = np.array(self._bm25.get_scores(q_tokens), dtype=np.float32) if q_tokens else np.zeros(n_docs, dtype=np.float32)

            if np.any(filter_mask):
                dense_scores = np.where(filter_mask, -2.0, dense_scores)
                bm25_scores = np.where(filter_mask, -1.0, bm25_scores)

            # 3. Reciprocal Rank Fusion (RRF)
            # RRF Score = alpha * (1 / (k + rank_dense)) + (1 - alpha) * (1 / (k + rank_bm25))
            rrf_k = 60
            dense_ranking = np.argsort(dense_scores)[::-1]
            bm25_ranking = np.argsort(bm25_scores)[::-1]

            rrf_scores = np.zeros(n_docs, dtype=np.float32)

            # Chỉ tính RRF cho top pool để tối ưu hiệu năng
            pool_size = min(n_docs, max(top_k * 5, 50))
            for rank, idx in enumerate(dense_ranking[:pool_size], start=1):
                if dense_scores[idx] > -1.0:
                    rrf_scores[idx] += hybrid_alpha * (1.0 / (rrf_k + rank))

            has_bm25_hits = np.any(bm25_scores > 0.0)
            if has_bm25_hits:
                for rank, idx in enumerate(bm25_ranking[:pool_size], start=1):
                    if bm25_scores[idx] > 0.0:
                        rrf_scores[idx] += (1.0 - hybrid_alpha) * (1.0 / (rrf_k + rank))

            top_indices = np.argsort(rrf_scores)[::-1][:top_k]
            final_scores = {idx: float(dense_scores[idx]) for idx in top_indices}

        results = []
        for idx in top_indices:
            score = final_scores[idx]
            if score < min_score:
                break
            meta = self._metadata[idx]
            results.append(
                {
                    "score": round(score, 4),
                    "id": meta["id"],
                    "level": meta["level"],
                    "category": meta.get("category", ""),
                    "knowledge_type": meta.get("knowledge_type", ""),
                    "knowledge_category_name": meta.get("knowledge_category_name", ""),
                    "parent_id": meta.get("parent_id"),
                    "section_title": meta.get("section_title", ""),
                    "section_code": meta.get("section_code", ""),
                    "source_file": meta.get("source_file", ""),
                    "source_pages": meta.get("source_pages", []),
                    "text": meta.get("text", ""),
                    "retrieval_text": meta.get("retrieval_text", ""),
                    "metadata": meta.get("metadata", {}),
                    "knowledge": meta.get("knowledge", {}),
                }
            )
        return results

    def search_with_context(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Tìm kiếm + tổng hợp kết quả thành context block cho LLM.

        Returns:
            {
              "query": str,
              "results": [...],         # raw search results
              "context_text": str,      # đoạn văn tổng hợp để đưa vào LLM prompt
              "sources": [...]          # danh sách nguồn
            }
        """
        results = self.search(query, top_k=top_k, min_score=min_score)

        context_parts = []
        sources = []
        seen_ids = set()

        for i, r in enumerate(results, 1):
            if r["id"] in seen_ids:
                continue
            seen_ids.add(r["id"])

            header = f"[{i}] {r['section_title']} ({r['source_file']})"
            context_parts.append(f"{header}\n{r['text']}")
            sources.append(
                {
                    "rank": i,
                    "score": r["score"],
                    "id": r["id"],
                    "section_title": r["section_title"],
                    "source_file": r["source_file"],
                    "source_pages": r["source_pages"],
                }
            )

        context_text = "\n\n---\n\n".join(context_parts)

        return {
            "query": query,
            "results": results,
            "context_text": context_text,
            "sources": sources,
        }

    # ── Index info ─────────────────────────────

    def info(self) -> Dict[str, Any]:
        """Trả về thông tin về index hiện tại."""
        n = len(self._metadata) if self._metadata else 0
        dim = int(self._vectors.shape[1]) if self._vectors is not None else 0
        levels: Dict[str, int] = {}
        categories: Dict[str, int] = {}
        for m in self._metadata:
            lv = m.get("level", "?")
            levels[lv] = levels.get(lv, 0) + 1
            cat = m.get("knowledge_category_name") or m.get("knowledge_type") or m.get("category", "?")
            categories[cat] = categories.get(cat, 0) + 1
        return {
            "num_vectors": n,
            "embedding_dim": dim,
            "levels": levels,
            "categories": categories,
            "index_dir": str(self._index_dir),
            "source_file": str(RAG_ORGANIZED_PATH),
        }

