#!/usr/bin/env python3
"""
Benchmark RapidOCR Engine trên tập ảnh thực tế /mnt/data/photos.
Đo lường chi tiết:
- Độ trễ (Latency: Mean, Median, Min, Max, P90, P95) cho từng giai đoạn (Detection, Classification, Recognition).
- Thông lượng (Throughput: FPS, giây/ảnh).
- Tỷ lệ nhận diện (Detection rate, số dòng chữ, điểm tin cậy trung bình).
- Phân tích nội dung và trích xuất từ khóa vật tư nông nghiệp thực tế.
- Lưu báo cáo chi tiết ra file JSON và xuất bảng Markdown.
"""

import glob
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List

import numpy as np
from PIL import Image

sys.path.insert(0, "/root/voice_to_data")

from services.engines.ocr_engine import OCRResult, ocr_engine

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("benchmark_ocr_photos")

PHOTOS_DIR = "/mnt/data/photos"
OUTPUT_JSON = "/root/voice_to_data/tools/benchmark/benchmark_ocr_photos_result.json"

AGRI_KEYWORDS = [
    "cách dùng", "liều dùng", "phân bón", "thuốc", "bvtv", "phun",
    "bình", "lít", "ml", "gam", "kg", "cây", "bưởi", "cam", "sâu",
    "bệnh", "thành phần", "công dụng", "hạn sử dụng", "ngày", "đt",
    "liên hệ", "công ty", "địa chỉ", "iso", "trị", "phòng", "npk"
]


def analyze_keywords(text: str) -> List[str]:
    """Phát hiện các từ khóa nông nghiệp xuất hiện trong văn bản nhận diện."""
    text_lower = text.lower()
    matched = [kw for kw in AGRI_KEYWORDS if kw in text_lower]
    return matched


def run_benchmark():
    photos = sorted(glob.glob(os.path.join(PHOTOS_DIR, "*.jpg")))
    total_photos = len(photos)

    print("=" * 80)
    print("🚀 BẮT ĐẦU BENCHMARK RAPIDOCR TRÊN TẬP DỮ LIỆU THỰC ĐỊA")
    print(f"📁 Thư mục ảnh: {PHOTOS_DIR}")
    print(f"📸 Tổng số ảnh: {total_photos}")
    print("=" * 80)

    if total_photos == 0:
        print("❌ Không tìm thấy file ảnh nào trong thư mục!")
        return

    # Warm-up engine với 1 ảnh đầu tiên
    print("\n[Warmup] Đang nạp model và chạy warm-up...")
    t_warm = time.time()
    ocr_engine.ocr_sync(photos[0])
    print(f"[Warmup] Hoàn thành sau {time.time() - t_warm:.2f}s\n")

    records: List[Dict[str, Any]] = []
    total_start_time = time.time()

    for idx, photo_path in enumerate(photos, 1):
        filename = os.path.basename(photo_path)
        file_size_kb = round(os.path.getsize(photo_path) / 1024, 1)

        try:
            with Image.open(photo_path) as im:
                img_w, img_h = im.size
        except Exception:
            img_w, img_h = 0, 0

        t0 = time.time()
        try:
            res: OCRResult = ocr_engine.ocr_sync(photo_path, min_score=0.5)
            elapsed = time.time() - t0
            is_success = True
            err_msg = ""
        except Exception as e:
            elapsed = time.time() - t0
            is_success = False
            err_msg = str(e)
            res = None

        if is_success and res:
            total_lines = res.total_lines
            avg_score = res.avg_score
            det_t = res.det_elapse
            cls_t = res.cls_elapse
            rec_t = res.rec_elapse
            total_t = res.total_elapse or elapsed
            text = res.text
            matched_kw = analyze_keywords(text)
            sample_lines = [l.text for l in res.lines[:3]]
        else:
            total_lines = 0
            avg_score = 0.0
            det_t = 0.0
            cls_t = 0.0
            rec_t = 0.0
            total_t = elapsed
            text = ""
            matched_kw = []
            sample_lines = []

        rec = {
            "index": idx,
            "filename": filename,
            "file_size_kb": file_size_kb,
            "resolution": f"{img_w}x{img_h}",
            "width": img_w,
            "height": img_h,
            "success": is_success,
            "error": err_msg,
            "total_lines": total_lines,
            "avg_score": round(float(avg_score), 4),
            "timing": {
                "det_elapse_ms": round(det_t * 1000, 1),
                "cls_elapse_ms": round(cls_t * 1000, 1),
                "rec_elapse_ms": round(rec_t * 1000, 1),
                "total_elapse_ms": round(total_t * 1000, 1),
            },
            "keywords_found": matched_kw,
            "sample_lines": sample_lines,
            "full_text": text,
        }
        records.append(rec)

        # In tiến độ gọn gàng
        status_icon = "✅" if total_lines > 0 else "⚪"
        kw_str = f"| KW: {len(matched_kw)}" if matched_kw else ""
        print(f"[{idx:03d}/{total_photos:03d}] {status_icon} {filename} ({img_w}x{img_h}, {file_size_kb}KB) -> {total_lines:2d} dòng | Score: {avg_score:.1%} | {total_t * 1000:.0f}ms (Det: {det_t*1000:.0f}ms, Rec: {rec_t*1000:.0f}ms) {kw_str}")

    total_batch_time = time.time() - total_start_time

    # ==============================================================================
    # Tính toán các chỉ số thống kê tổng hợp (Aggregation)
    # ==============================================================================
    successful_runs = [r for r in records if r["success"]]
    photos_with_text = [r for r in successful_runs if r["total_lines"] > 0]

    all_total_ms = [r["timing"]["total_elapse_ms"] for r in successful_runs]
    all_det_ms = [r["timing"]["det_elapse_ms"] for r in successful_runs]
    all_cls_ms = [r["timing"]["cls_elapse_ms"] for r in successful_runs]
    all_rec_ms = [r["timing"]["rec_elapse_ms"] for r in successful_runs]

    all_scores = [r["avg_score"] for r in photos_with_text if r["avg_score"] > 0]
    all_lines = [r["total_lines"] for r in successful_runs]

    def calc_stats(arr: List[float]) -> Dict[str, float]:
        if not arr:
            return {"mean": 0.0, "median": 0.0, "min": 0.0, "max": 0.0, "p90": 0.0, "p95": 0.0, "std": 0.0}
        np_arr = np.array(arr)
        return {
            "mean": round(float(np.mean(np_arr)), 2),
            "median": round(float(np.median(np_arr)), 2),
            "min": round(float(np.min(np_arr)), 2),
            "max": round(float(np.max(np_arr)), 2),
            "p90": round(float(np.percentile(np_arr, 90)), 2),
            "p95": round(float(np.percentile(np_arr, 95)), 2),
            "std": round(float(np.std(np_arr)), 2),
        }

    timing_stats = {
        "total_ms": calc_stats(all_total_ms),
        "det_ms": calc_stats(all_det_ms),
        "cls_ms": calc_stats(all_cls_ms),
        "rec_ms": calc_stats(all_rec_ms),
    }

    # Phân bố điểm tin cậy
    score_dist = {
        ">= 90%": len([s for s in all_scores if s >= 0.90]),
        "80% - 89%": len([s for s in all_scores if 0.80 <= s < 0.90]),
        "70% - 79%": len([s for s in all_scores if 0.70 <= s < 0.80]),
        "50% - 69%": len([s for s in all_scores if 0.50 <= s < 0.70]),
        "< 50%": len([s for s in all_scores if s < 0.50]),
    }

    # Phân bố số dòng nhận diện
    line_dist = {
        "0 dòng (không có chữ / cảnh thuần túy)": len([l for l in all_lines if l == 0]),
        "1 - 5 dòng": len([l for l in all_lines if 1 <= l <= 5]),
        "6 - 15 dòng": len([l for l in all_lines if 6 <= l <= 15]),
        "16 - 30 dòng": len([l for l in all_lines if 16 <= l <= 30]),
        "> 30 dòng (bao bì chi tiết, hóa đơn dài)": len([l for l in all_lines if l > 30]),
    }

    fps = total_photos / total_batch_time if total_batch_time > 0 else 0
    avg_sec_per_img = total_batch_time / total_photos if total_photos > 0 else 0

    summary = {
        "dataset": {
            "directory": PHOTOS_DIR,
            "total_photos": total_photos,
            "photos_with_text": len(photos_with_text),
            "detection_rate_pct": round(len(photos_with_text) / total_photos * 100, 2),
            "total_lines_detected": sum(all_lines),
            "avg_lines_per_photo": round(sum(all_lines) / total_photos, 2),
            "avg_lines_when_detected": round(sum(all_lines) / len(photos_with_text), 2) if photos_with_text else 0,
        },
        "performance": {
            "total_batch_time_sec": round(total_batch_time, 2),
            "avg_sec_per_image": round(avg_sec_per_img, 3),
            "throughput_fps": round(fps, 3),
            "timing_breakdown_ms": timing_stats,
        },
        "accuracy": {
            "mean_confidence_score": round(float(np.mean(all_scores)), 4) if all_scores else 0.0,
            "median_confidence_score": round(float(np.median(all_scores)), 4) if all_scores else 0.0,
            "score_distribution": score_dist,
            "line_count_distribution": line_dist,
        },
    }

    # Lưu kết quả đầy đủ sang JSON
    output_payload = {
        "summary": summary,
        "details": records,
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, ensure_ascii=False, indent=2)

    # In báo cáo tổng kết
    print("\n" + "=" * 80)
    print("📊 BÁO CÁO KẾT QUẢ BENCHMARK RAPIDOCR TRÊN TẬP ẢNH THỰC TẾ")
    print("=" * 80)
    print(f"• Tổng số ảnh kiểm thử          : {total_photos} ảnh")
    print(f"• Số ảnh phát hiện có chữ      : {len(photos_with_text)}/{total_photos} ({summary['dataset']['detection_rate_pct']}%)")
    print(f"• Tổng số dòng văn bản trích xuất: {summary['dataset']['total_lines_detected']} dòng (Trung bình: {summary['dataset']['avg_lines_when_detected']} dòng/ảnh có chữ)")
    print(f"• Điểm tin cậy trung bình        : {summary['accuracy']['mean_confidence_score']:.2%}")
    print(f"• Tổng thời gian chạy toàn tập  : {total_batch_time:.2f} giây (~{total_batch_time/60:.1f} phút)")
    print(f"• Tốc độ xử lý trung bình       : {avg_sec_per_img:.3f} giây/ảnh ({fps:.2f} FPS)")
    print("-" * 80)
    print("⏱️ CHI TIẾT THỜI GIAN XỬ LÝ (MILI-GIÂY / ẢNH):")
    print(f"  • Detection (Phát hiện vùng chữ) : Mean: {timing_stats['det_ms']['mean']}ms | Median: {timing_stats['det_ms']['median']}ms | P95: {timing_stats['det_ms']['p95']}ms")
    print(f"  • Classification (Góc xoay)      : Mean: {timing_stats['cls_ms']['mean']}ms | Median: {timing_stats['cls_ms']['median']}ms | P95: {timing_stats['cls_ms']['p95']}ms")
    print(f"  • Recognition (Nhận diện ký tự)  : Mean: {timing_stats['rec_ms']['mean']}ms | Median: {timing_stats['rec_ms']['median']}ms | P95: {timing_stats['rec_ms']['p95']}ms")
    print(f"  • TỔNG THỜI GIAN / ẢNH           : Mean: {timing_stats['total_ms']['mean']}ms | Median: {timing_stats['total_ms']['median']}ms | P95: {timing_stats['total_ms']['p95']}ms")
    print("-" * 80)
    print("🎯 PHÂN BỐ ĐIỂM TIN CẬY:")
    for k, v in score_dist.items():
        print(f"  • {k:12s}: {v:3d} ảnh ({v/len(photos_with_text)*100:.1f}%)" if photos_with_text else "")
    print("-" * 80)
    print("📦 PHÂN BỐ SỐ LƯỢNG DÒNG NHẬN DIỆN:")
    for k, v in line_dist.items():
        print(f"  • {k:40s}: {v:3d} ảnh ({v/total_photos*100:.1f}%)")
    print("=" * 80)
    print(f"💾 File JSON kết quả chi tiết đã được lưu tại: {OUTPUT_JSON}")


if __name__ == "__main__":
    run_benchmark()
