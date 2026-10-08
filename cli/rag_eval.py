#!/usr/bin/env python3
"""
RAG Evaluation Script (cli/rag_eval.py)
========================================
Đánh giá chất lượng retrieval của RAG Knowledge Base trên bộ rag_eval.jsonl.

Metrics:
  - Recall@K  : có ít nhất 1 expected chunk trong top-K kết quả không?
  - MRR@K     : Mean Reciprocal Rank — rank trung bình của kết quả đúng đầu tiên
  - Per-level : breakdown theo level (chunk / fact)

Sử dụng:
    cd /root/voice_to_data
    source .venv/bin/activate
    python3 cli/rag_eval.py
    python3 cli/rag_eval.py --top-k 10 --min-score 0.6 --detail
    python3 cli/rag_eval.py --output results/eval_report.json
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.WARNING,  # chỉ show WARNING+ khi chạy eval
    format="%(levelname)s %(name)s: %(message)s",
)

# ──────────────────────────────────────────────
# ID normalization
# eval:  "01-Phn-01.pdf::chunk-0001"
# index: "01-Phn-01::chunk-0001"
# ──────────────────────────────────────────────

def _normalize_id(raw_id: str) -> str:
    """Chuẩn hoá ID: bỏ extension, giữ nguyên phần còn lại."""
    for ext in (".pdf", ".PDF", ".DOC", ".doc", ".docx", ".DOCX"):
        raw_id = raw_id.replace(ext, "")
    return raw_id.strip()


def _extract_chunk_suffix(chunk_id: str) -> str:
    """Trích phần chunk/fact suffix từ ID.
    Ví dụ: '01-Phn-01.pdf::chunk-0001' → 'chunk-0001'
            'section::chunk-0001::fact-01' → 'chunk-0001::fact-01'
    """
    parts = chunk_id.split("::")
    for i, p in enumerate(parts):
        if p.startswith("chunk") or p.startswith("fact"):
            return "::".join(parts[i:])
    return chunk_id


def _ids_match(
    expected_ids: List[str],
    retrieved: Dict[str, Any],
) -> bool:
    """
    Match retrieved record với expected_ids.
    1. So khớp trực tiếp retrieved['id'] == expected_id (rag_retrieval.jsonl chuẩn 100%)
    2. Fallback đối chiếu normalized ID và source_file + chunk suffix
    """
    ret_id = retrieved.get("id", "")
    if ret_id in expected_ids:
        return True

    ret_source = retrieved.get("source_file", "")
    ret_suffix = _extract_chunk_suffix(ret_id)

    for exp in expected_ids:
        if ret_id == exp:
            return True

        # Trực tiếp normalized match
        norm_ret = _normalize_id(ret_id)
        norm_exp = _normalize_id(exp)
        if norm_ret == norm_exp or norm_ret.startswith(norm_exp + "::") or norm_exp.startswith(norm_ret + "::"):
            return True

        parts = exp.split("::", 1)
        if len(parts) == 2:
            exp_source_raw, exp_suffix = parts[0], parts[1]
            source_match = _normalize_id(exp_source_raw) == _normalize_id(ret_source)
            suffix_match = (
                ret_suffix == exp_suffix
                or ret_suffix.startswith(exp_suffix + "::")
                or exp_suffix.startswith(ret_suffix + "::")
            )
            if source_match and suffix_match:
                return True

    return False




# ──────────────────────────────────────────────
# ANSI colors
# ──────────────────────────────────────────────

def _c(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"

GREEN  = lambda t: _c(t, "32")
RED    = lambda t: _c(t, "31")
YELLOW = lambda t: _c(t, "33")
CYAN   = lambda t: _c(t, "36")
BOLD   = lambda t: _c(t, "1")
DIM    = lambda t: _c(t, "2")


# ──────────────────────────────────────────────
# Main eval logic
# ──────────────────────────────────────────────

def run_eval(
    top_k: int = 5,
    min_score: float = 0.0,
    level_filter: Optional[List[str]] = None,
    show_detail: bool = False,
    output_path: Optional[str] = None,
    dataset_path: Optional[str] = None,
) -> Dict[str, Any]:
    from services.rag.rag_knowledge_base import RAGKnowledgeBase

    if dataset_path:
        eval_path = Path(dataset_path)
    elif (ROOT / "knowledge" / "test_set.jsonl").exists():
        eval_path = ROOT / "knowledge" / "test_set.jsonl"
    elif (ROOT / "data" / "rag" / "rag_eval.jsonl").exists():
        eval_path = ROOT / "data" / "rag" / "rag_eval.jsonl"
    else:
        eval_path = ROOT / "rag_eval.jsonl"

    if not eval_path.exists():
        print(RED(f"Không tìm thấy file: {eval_path}"))
        sys.exit(1)

    print(BOLD("\n📊 RAG Evaluation — multilingual-e5-small ONNX"))
    print(DIM("─" * 60))
    print(f"  top_k      = {top_k}")
    print(f"  min_score  = {min_score}")
    print(f"  level      = {level_filter or 'all'}")
    print(DIM("─" * 60))

    # Load index (from cache)
    t0 = time.perf_counter()
    print("⏳ Loading index ...", end="", flush=True)
    kb = RAGKnowledgeBase()
    info = kb.info()
    print(f"\r✅ Index loaded: {info['num_vectors']} vectors ({info['levels']})")

    # Load eval records
    eval_records = []
    with open(eval_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                eval_records.append(json.loads(line))

    total = len(eval_records)
    print(f"📋 Eval set: {total} câu hỏi\n")

    # ── Run ─────────────────────────────────────
    hits = 0
    mrr_sum = 0.0
    details: List[Dict] = []
    category_stats: Dict[str, Dict] = {}

    t_search_start = time.perf_counter()
    for i, rec in enumerate(eval_records):
        question     = rec.get("question", "")
        expected_ids = rec.get("expected_ids", [])
        category     = rec.get("category", "unknown")

        results = kb.search(
            question,
            top_k=top_k,
            min_score=min_score,
            level_filter=level_filter,
        )

        # Tính hit và reciprocal rank
        hit = False
        rr  = 0.0
        for rank, r in enumerate(results, start=1):
            if _ids_match(expected_ids, r):
                hit = True
                rr  = 1.0 / rank
                break

        if hit:
            hits += 1
        mrr_sum += rr

        # Category stats
        if category not in category_stats:
            category_stats[category] = {"total": 0, "hits": 0, "mrr": 0.0}
        category_stats[category]["total"] += 1
        if hit:
            category_stats[category]["hits"] += 1
        category_stats[category]["mrr"] += rr

        details.append({
            "id":            rec.get("id"),
            "question":      question,
            "category":      category,
            "expected_ids":  expected_ids,
            "hit":           hit,
            "rr":            round(rr, 4),
            "top_score":     results[0]["score"] if results else 0.0,
            "top_results":   [
                {
                    "rank":          j + 1,
                    "score":         r["score"],
                    "id":            r["id"],
                    "level":         r["level"],
                    "section_title": r.get("section_title", ""),
                }
                for j, r in enumerate(results[:3])
            ],
        })

        if show_detail:
            icon = GREEN("✓") if hit else RED("✗")
            print(f"  {icon} [{i+1:3d}/{total}] {question[:60]}")
            if not hit and results:
                print(DIM(f"       Expected: {expected_ids}"))
                print(DIM(f"       Got:      {results[0]['id']} (score={results[0]['score']})"))

    elapsed_s = time.perf_counter() - t_search_start

    # ── Metrics ─────────────────────────────────
    recall_at_k = hits / total if total else 0
    mrr         = mrr_sum / total if total else 0

    print(DIM("─" * 60))
    print(BOLD(f"\n📈 Kết quả tổng hợp ({total} câu hỏi):"))
    print(f"  Recall@{top_k:<3}  = {GREEN(f'{recall_at_k*100:.1f}%')}  ({hits}/{total} hits)")
    print(f"  MRR@{top_k:<6}  = {CYAN(f'{mrr:.4f}')}")
    print(f"  Thời gian   = {elapsed_s:.1f}s  (~{elapsed_s/total*1000:.0f}ms/q)")

    if category_stats:
        print(BOLD("\n📂 Theo danh mục:"))
        for cat, s in sorted(category_stats.items()):
            r = s["hits"] / s["total"] if s["total"] else 0
            m = s["mrr"] / s["total"] if s["total"] else 0
            print(
                f"  {YELLOW(cat):<20} "
                f"Recall={r*100:.0f}%  MRR={m:.3f}  "
                f"({s['hits']}/{s['total']})"
            )

    # Hiển thị các câu hỏi fail nếu không --detail
    if not show_detail:
        fails = [d for d in details if not d["hit"]]
        if fails:
            print(BOLD(f"\n❌ {len(fails)} câu hỏi không tìm được (top-5 fail):"))
            for d in fails[:5]:
                print(f"  {RED('✗')} {d['question'][:70]}")
                print(DIM(f"    expected: {d['expected_ids']}"))
                if d["top_results"]:
                    best = d["top_results"][0]
                    print(DIM(f"    got:      [{best['score']:.3f}] {best['id']}"))

    # ── Save JSON report ─────────────────────────
    report = {
        "timestamp":     datetime.now().isoformat(),
        "top_k":         top_k,
        "min_score":     min_score,
        "level_filter":  level_filter,
        "total":         total,
        "hits":          hits,
        f"recall_at_{top_k}": round(recall_at_k, 4),
        "mrr":           round(mrr, 4),
        "elapsed_s":     round(elapsed_s, 2),
        "category_stats": {
            cat: {
                "total": s["total"],
                "hits":  s["hits"],
                "recall": round(s["hits"]/s["total"], 4) if s["total"] else 0,
                "mrr":   round(s["mrr"]/s["total"], 4) if s["total"] else 0,
            }
            for cat, s in category_stats.items()
        },
        "details": details,
    }

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n💾 Báo cáo đã lưu: {out}")

    return report


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Đánh giá RAG retrieval trên rag_eval.jsonl",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--top-k",    type=int,   default=5,   help="Số kết quả lấy (default: 5)")
    parser.add_argument("--min-score", type=float, default=0.0, help="Ngưỡng similarity tối thiểu (default: 0.0)")
    parser.add_argument("--level",    nargs="+",  default=None,
                        choices=["chunk", "fact"],
                        help="Chỉ tìm trong level cụ thể (default: cả chunk và fact)")
    parser.add_argument("--detail",   action="store_true", help="Hiện chi tiết từng câu hỏi")
    parser.add_argument("--dataset",  type=str,   default=None, help="Đường dẫn file test dataset (.jsonl)")
    parser.add_argument("--output",   type=str,   default=None,
                        help="Lưu báo cáo JSON (vd: results/eval.json)")
    args = parser.parse_args()

    run_eval(
        top_k=args.top_k,
        min_score=args.min_score,
        level_filter=args.level,
        show_detail=args.detail,
        output_path=args.output,
        dataset_path=args.dataset,
    )



if __name__ == "__main__":
    main()
