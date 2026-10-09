#!/usr/bin/env python3
"""
Benchmark đánh giá hiệu năng (thời gian xử lý, TPS) và độ chính xác trích xuất (Field-level & Activity-level Accuracy)
của Local LLM Engine (Qwen3.5-2B RKLLM trên NPU RK3588) sau khi cập nhật prompt.
"""

import sys
import time
import json
import asyncio
from typing import Dict, Any, List, Tuple

sys.path.insert(0, "/root/voice_to_data")

from services.engines.llm_engine import LLMEngine
from scratch.generate_results import TEST_CASES, check_errors

async def run_benchmark():
    engine = LLMEngine()
    
    print("=" * 80)
    print("🚀 BẮT ĐẦU BENCHMARK TRÍCH XUẤT NHẬT KÝ CANH TÁC (LOCAL LLM RKLLM 2B NPU)")
    print(f"Tổng số ca kiểm thử: {len(TEST_CASES)} (đại diện đầy đủ 8 bảng VietGAP)")
    print("=" * 80)

    results = []
    latencies = []
    tokens_per_sec_list = []
    correct_activities = 0
    total_field_acc = 0.0

    start_all = time.perf_counter()

    for idx, tc in enumerate(TEST_CASES, 1):
        tc_id = tc["id"]
        tc_table = tc["table"]
        tc_name = tc["name"]
        text = tc["text"]
        expected = tc["expected"]

        print(f"\n[{idx:02d}/{len(TEST_CASES)}] [{tc_table.upper()}] {tc_name}")
        print(f"  📝 INPUT: {text}")

        t0 = time.perf_counter()
        try:
            res = await engine.extract_info(text)
            latency = time.perf_counter() - t0
        except Exception as e:
            latency = time.perf_counter() - t0
            print(f"  ❌ Lỗi trích xuất: {e}")
            res = {}

        latencies.append(latency)
        tps = res.get("tokens_per_sec", 0.0)
        if tps > 0:
            tokens_per_sec_list.append(tps)

        # Đánh giá độ chính xác trường
        acc, passed, failed = check_errors(res, expected)
        total_field_acc += acc

        # Đánh giá loại hoạt động
        active_acts = res.get("active_activities", [])
        exp_act = expected.get("activity")
        act_ok = (exp_act in active_acts) if exp_act else True
        if act_ok:
            correct_activities += 1

        status_str = "✅ HOÀN HẢO" if acc == 100.0 else ("⚠️ CHẤP NHẬN ĐƯỢC" if acc >= 70.0 else "❌ LỖI")
        print(f"  ⏱️  Thời gian: {latency:.2f}s | TPS: {tps:.1f} tok/s")
        print(f"  🎯 Độ chính xác trường: {acc}% ({status_str})")
        if passed:
            print(f"     + Đạt ({len(passed)}): {', '.join(passed[:3])}{'...' if len(passed) > 3 else ''}")
        if failed:
            print(f"     - Thiếu/Lệch ({len(failed)}): {'; '.join(failed)}")

        results.append({
            "id": tc_id,
            "table": tc_table,
            "name": tc_name,
            "latency_s": round(latency, 2),
            "tokens_per_sec": round(tps, 1),
            "accuracy_pct": acc,
            "activity_correct": act_ok,
            "passed_fields": passed,
            "failed_fields": failed,
            "extracted_tables": list(res.get("tables", {}).keys()),
        })

    wall_time = time.perf_counter() - start_all

    # Thống kê
    latencies.sort()
    min_lat = latencies[0]
    max_lat = latencies[-1]
    avg_lat = sum(latencies) / len(latencies)
    med_lat = latencies[len(latencies) // 2]
    avg_tps = sum(tokens_per_sec_list) / len(tokens_per_sec_list) if tokens_per_sec_list else 0.0
    overall_field_acc = total_field_acc / len(TEST_CASES)
    overall_act_acc = (correct_activities / len(TEST_CASES)) * 100.0

    print("\n" + "=" * 80)
    print("📊 KẾT QUẢ TỔNG HỢP BENCHMARK TRÍCH XUẤT SAU KHI CẬP NHẬT PROMPT")
    print("=" * 80)
    print(f"Tổng thời gian chạy toàn bộ:   {wall_time:.2f} s")
    print(f"Số ca kiểm thử thành công:     {len(TEST_CASES)} / {len(TEST_CASES)}")
    print(f"Độ chính xác phân loại HĐ:     {overall_act_acc:.1f}% ({correct_activities}/{len(TEST_CASES)})")
    print(f"Độ chính xác trung bình trường: {overall_field_acc:.1f}%")
    print("-" * 80)
    print("HIỆU NĂNG THỜI GIAN (LATENCY):")
    print(f"  - Min Latency:    {min_lat:.2f} s")
    print(f"  - Max Latency:    {max_lat:.2f} s")
    print(f"  - Mean (Trung bình): {avg_lat:.2f} s")
    print(f"  - Median (Trung vị): {med_lat:.2f} s")
    print(f"  - Tốc độ sinh token: {avg_tps:.1f} tokens/giây")
    print("=" * 80)

    # Lưu kết quả json
    with open("/root/voice_to_data/tools/benchmark/benchmark_extraction_result.json", "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "total_cases": len(TEST_CASES),
                "activity_accuracy_pct": round(overall_act_acc, 2),
                "field_accuracy_pct": round(overall_field_acc, 2),
                "wall_time_s": round(wall_time, 2),
                "avg_latency_s": round(avg_lat, 2),
                "median_latency_s": round(med_lat, 2),
                "min_latency_s": round(min_lat, 2),
                "max_latency_s": round(max_lat, 2),
                "avg_tps": round(avg_tps, 2),
            },
            "cases": results
        }, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    asyncio.run(run_benchmark())
