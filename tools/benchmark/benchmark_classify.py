import sys
sys.path.insert(0, "/root/voice_to_data")
import asyncio
import time
import json
from collections import defaultdict, Counter
from services.engines.llm_engine import llm_engine

label_map = {
    'BẢNG 1': 'purchase',
    'BẢNG 2': 'fertilize',
    'BẢNG 3': 'spray',
    'BẢNG 4': 'waste',
    'BẢNG 5': 'harvest',
    'BẢNG 6': 'sale',
    'BẢNG 7': 'observation',
    'BẢNG 8': 'general',
}

def load_dataset(file_path):
    samples = []
    current_label = None
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            for k, v in label_map.items():
                if k in line and 'BẢNG' in line:
                    current_label = v
                    break
            if line.startswith('#'):
                continue
            if current_label:
                samples.append((current_label, line))
    return samples

async def run_benchmark():
    samples = load_dataset('/root/voice_to_data/sample.txt')
    print(f"Tổng số mẫu cần test: {len(samples)}")
    
    results = []
    correct_count = 0
    total_time = 0.0
    latencies = []
    
    class_stats = defaultdict(lambda: {'total': 0, 'correct': 0, 'pred_count': 0, 'latencies': []})
    confusion_matrix = defaultdict(Counter)
    mismatches = []
    
    start_all = time.time()
    
    for i, (expected, text) in enumerate(samples, 1):
        t0 = time.time()
        pred = await llm_engine._classify_with_llm(text)
        latency = time.time() - t0
        
        latencies.append(latency)
        total_time += latency
        is_correct = (pred == expected)
        if is_correct:
            correct_count += 1
        else:
            mismatches.append({
                "index": i,
                "text": text,
                "expected": expected,
                "predicted": pred,
                "latency_s": round(latency, 3)
            })
            
        class_stats[expected]['total'] += 1
        class_stats[expected]['latencies'].append(latency)
        if is_correct:
            class_stats[expected]['correct'] += 1
        if pred:
            class_stats[pred]['pred_count'] += 1
        confusion_matrix[expected][pred or "None"] += 1
        
        if i % 10 == 0 or i == len(samples):
            current_acc = (correct_count / i) * 100
            avg_lat = sum(latencies) / len(latencies)
            print(f"[{i:3d}/{len(samples)}] Accuracy: {current_acc:5.1f}% ({correct_count}/{i}) | Latency avg: {avg_lat:.2f}s | Mẫu: '{text[:40]}...' -> {pred}")
            
    wall_clock_time = time.time() - start_all
    
    # Tính toán chỉ số thống kê thời gian
    latencies.sort()
    min_lat = latencies[0]
    max_lat = latencies[-1]
    avg_lat = sum(latencies) / len(latencies)
    median_lat = latencies[len(latencies) // 2]
    p95_lat = latencies[int(len(latencies) * 0.95)]
    
    summary = {
        "total_samples": len(samples),
        "correct": correct_count,
        "accuracy_pct": round((correct_count / len(samples)) * 100, 2),
        "wall_clock_time_s": round(wall_clock_time, 2),
        "total_inference_time_s": round(total_time, 2),
        "avg_latency_s": round(avg_lat, 3),
        "min_latency_s": round(min_lat, 3),
        "median_latency_s": round(median_lat, 3),
        "p95_latency_s": round(p95_lat, 3),
        "max_latency_s": round(max_lat, 3),
        "throughput_samples_per_sec": round(len(samples) / total_time, 2),
        "class_breakdown": {},
        "mismatches": mismatches
    }
    
    for cls, stat in class_stats.items():
        tot = stat['total']
        corr = stat['correct']
        pred_cnt = stat['pred_count']
        precision = (corr / pred_cnt * 100) if pred_cnt > 0 else 0.0
        recall = (corr / tot * 100) if tot > 0 else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
        cls_avg_lat = sum(stat['latencies']) / len(stat['latencies']) if stat['latencies'] else 0
        
        summary["class_breakdown"][cls] = {
            "total": tot,
            "correct": corr,
            "recall_pct": round(recall, 2),
            "precision_pct": round(precision, 2),
            "f1_score": round(f1, 2),
            "avg_latency_s": round(cls_avg_lat, 3)
        }
        
    summary["confusion_matrix"] = {k: dict(v) for k, v in confusion_matrix.items()}
    
    with open('/root/voice_to_data/tools/benchmark/benchmark_classify_result.json', 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
        
    print("\n" + "="*60)
    print("HOÀN TẤT BENCHMARK:")
    print(f"Tổng số mẫu: {len(samples)}")
    print(f"Đúng: {correct_count}/{len(samples)} ({summary['accuracy_pct']}%)")
    print(f"Tổng thời gian: {wall_clock_time:.2f}s (~{wall_clock_time/60:.1f} phút)")
    print(f"Độ trễ trung bình: {avg_lat:.3f}s / mẫu (Median: {median_lat:.3f}s, P95: {p95_lat:.3f}s)")
    print(f"Tốc độ: {summary['throughput_samples_per_sec']} mẫu/giây")
    print("="*60)
    
    if mismatches:
        print(f"\nDanh sách {len(mismatches)} mẫu phân loại sai:")
        for m in mismatches:
            print(f"  #{m['index']}: Kỳ vọng '{m['expected']}' -> Dự đoán '{m['predicted']}' | '{m['text']}'")
    else:
        print("\nTuyệt vời! Không có mẫu nào bị sai (100%).")
        
    await llm_engine.stop()

if __name__ == '__main__':
    asyncio.run(run_benchmark())
