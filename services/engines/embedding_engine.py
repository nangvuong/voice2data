"""
Embedding Engine (services/embedding_engine.py)
================================================
Service chạy mô hình multilingual-e5-small (ONNX) để tạo vector embedding
phục vụ RAG (Retrieval-Augmented Generation) trên knowledge base VietGAP.

Đặc điểm:
- Model: intfloat/multilingual-e5-small, chạy qua ONNX Runtime (CPU-only, không cần GPU/PyTorch).
- Model path: /mnt/data/multilingual-e5-small/onnx/model.onnx
- Vector dim: 384, normalized (cosine similarity = dot product).
- Hỗ trợ E5 prefix convention: "query: ..." cho câu hỏi, "passage: ..." cho đoạn văn bản.
- Thread-safe: dùng threading.Lock để serialize ONNX session (1 session, nhiều thread).
- Batch encode cho hiệu năng tốt khi build index.
"""

import logging
import threading
from typing import List, Optional

import numpy as np
import onnxruntime as ort
from transformers import AutoTokenizer

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

MODEL_DIR = "/mnt/data/multilingual-e5-small"
ONNX_MODEL_PATH = f"{MODEL_DIR}/onnx/model.onnx"
EMBEDDING_DIM = 384
MAX_SEQ_LEN = 512


# ──────────────────────────────────────────────
# EmbeddingEngine
# ──────────────────────────────────────────────

class EmbeddingEngine:
    """
    Singleton-safe wrapper quanh ONNX session của multilingual-e5-small.

    Sử dụng:
        engine = EmbeddingEngine()
        # Embed query (prefix "query:")
        qvec = engine.embed_query("điều kiện đất trồng bưởi là gì?")
        # Embed passages (prefix "passage:")
        pvecs = engine.embed_passages(["nhiệt độ thích hợp là 23-29°C", ...])
    """

    def __init__(
        self,
        model_path: str = ONNX_MODEL_PATH,
        model_dir: str = MODEL_DIR,
        max_seq_len: int = MAX_SEQ_LEN,
        batch_size: int = 32,
    ):
        self._lock = threading.Lock()
        self._max_seq_len = max_seq_len
        self._batch_size = batch_size

        logger.info("Loading tokenizer từ %s ...", model_dir)
        self._tokenizer = AutoTokenizer.from_pretrained(model_dir)

        logger.info("Khởi tạo ONNX session từ %s ...", model_path)
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 2
        opts.intra_op_num_threads = 4
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            model_path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self._input_names = {i.name for i in self._session.get_inputs()}
        logger.info(
            "EmbeddingEngine sẵn sàng. Input nodes: %s", self._input_names
        )

    # ── Internal helpers ───────────────────────

    def _mean_pool_normalize(
        self, last_hidden: np.ndarray, attention_mask: np.ndarray
    ) -> np.ndarray:
        """Mean pooling + L2 normalize."""
        mask_exp = attention_mask[:, :, np.newaxis].astype(np.float32)
        sum_emb = (last_hidden * mask_exp).sum(axis=1)
        sum_mask = mask_exp.sum(axis=1).clip(min=1e-9)
        emb = sum_emb / sum_mask
        norms = np.linalg.norm(emb, axis=1, keepdims=True).clip(min=1e-9)
        return (emb / norms).astype(np.float32)

    def _encode_batch(self, texts: List[str]) -> np.ndarray:
        """Encode 1 batch texts → (N, 384) float32 array."""
        enc = self._tokenizer(
            texts,
            return_tensors="np",
            max_length=self._max_seq_len,
            truncation=True,
            padding=True,
        )
        batch_size, seq_len = enc["input_ids"].shape
        feed: dict = {
            "input_ids": enc["input_ids"],
            "attention_mask": enc["attention_mask"],
        }
        # multilingual-e5-small ONNX yêu cầu token_type_ids (all zeros)
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.zeros((batch_size, seq_len), dtype=np.int64)

        with self._lock:
            outputs = self._session.run(None, feed)

        last_hidden = outputs[0]  # (batch, seq, 384)
        return self._mean_pool_normalize(last_hidden, enc["attention_mask"])

    def _encode_texts(
        self, texts: List[str], prefix: str = ""
    ) -> np.ndarray:
        """Encode danh sách texts theo từng batch, trả về (N, 384)."""
        if not texts:
            return np.empty((0, EMBEDDING_DIM), dtype=np.float32)

        prefixed = [f"{prefix}{t}" if prefix else t for t in texts]
        results: List[np.ndarray] = []
        for i in range(0, len(prefixed), self._batch_size):
            batch = prefixed[i : i + self._batch_size]
            results.append(self._encode_batch(batch))
        return np.vstack(results)

    # ── Public API ─────────────────────────────

    def embed_query(self, query: str) -> np.ndarray:
        """
        Embed 1 câu hỏi (E5 prefix "query: ...").
        Returns: (384,) float32 normalized vector.
        """
        vecs = self._encode_texts([query], prefix="query: ")
        return vecs[0]

    def embed_queries(self, queries: List[str]) -> np.ndarray:
        """
        Embed nhiều câu hỏi.
        Returns: (N, 384) float32.
        """
        return self._encode_texts(queries, prefix="query: ")

    def embed_passages(self, passages: List[str]) -> np.ndarray:
        """
        Embed nhiều đoạn văn bản (E5 prefix "passage: ...").
        Returns: (N, 384) float32.
        """
        return self._encode_texts(passages, prefix="passage: ")

    def embed_passage(self, passage: str) -> np.ndarray:
        """
        Embed 1 đoạn văn bản.
        Returns: (384,) float32 normalized vector.
        """
        vecs = self._encode_texts([passage], prefix="passage: ")
        return vecs[0]

    def similarity(self, vec_a: np.ndarray, vec_b: np.ndarray) -> float:
        """Cosine similarity (= dot product vì đã normalize)."""
        return float(np.dot(vec_a, vec_b))
