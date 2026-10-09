#!/usr/bin/env python3
"""
Script trích xuất toàn diện 16 ca kiểm thử trên sample.txt (đã chuẩn hóa chính xác theo từng bảng)
cho 3 mô hình:
1. Google Gemini 3.5 Flash-Lite
2. RKLLM 2B (Qwen3.5-2B_RK3588_w8a8)
3. RKLLM 0.6B (qwen3-0.6b_w8a8_rk3588)

Kết quả được ghi vào /root/voice_to_data/result.txt kèm phân tích chi tiết các lỗi sai của mô hình 2B.
"""

import os
import sys
import time
import json
import asyncio
import subprocess
from typing import Dict, Any, List, Tuple

PROJECT_ROOT = "/root/voice_to_data"
sys.path.insert(0, PROJECT_ROOT)

from services.engines.gemini_engine import gemini_engine
from services.engines.llm_engine import llm_engine

TEST_CASES = [
    # BẢNG 1: MUA / NHẬP VẬT TƯ (purchase)
    {
        "id": 1,
        "table": "purchase",
        "name": "Mua thuốc BVTV (đủ 6 trường)",
        "text": "nhập hai mươi chai thuốc trừ sâu sinh học nano ecotech thể tích năm trăm mi li lít từ công ty trách nhiệm hữu hạn quang minh t n a hạn sử dụng đến tháng mười hai năm hai không hai sáu",
        "expected": {
            "activity": "purchase",
            "material_name": "nano ecotech",
            "material_type": ["pesticide", "thuốc", "bvtv"],
            "quantity": 20,
            "unit": "chai",
            "supplier_name": "quang minh",
            "expiry_date": "2026-12"
        }
    },
    {
        "id": 2,
        "table": "purchase",
        "name": "Mua phân bón NPK (đủ 6 trường)",
        "text": "mua năm mươi bao phân bón n p k hai mươi hai mươi mười lăm đầu trâu loại năm mươi ki lô gam tại đại lý vật tư ba doanh hạn sử dụng đến tháng mười hai năm hai không hai bảy",
        "expected": {
            "activity": "purchase",
            "material_name": "n p k",
            "material_type": ["fertilizer", "phân"],
            "quantity": 50,
            "unit": "bao",
            "supplier_name": "ba doanh",
            "expiry_date": "2027-12"
        }
    },
    # BẢNG 2: BÓN PHÂN (fertilize)
    {
        "id": 3,
        "table": "fertilize",
        "name": "Bón NPK theo tạ/ha",
        "text": "cây ở giai đoạn sinh trưởng được bón năm tạ trên một héc ta phân n p k",
        "expected": {
            "activity": "fertilize",
            "ten_phan_bon": "n p k",
            "luong_phan_bon": ["5 tạ", "500", "5"],
            "growth_stage": "sinh trưởng"
        }
    },
    {
        "id": 4,
        "table": "fertilize",
        "name": "Bón phân dạng nước Nano Ecotech",
        "text": "bón phân dạng nước bằng thuốc sinh học nano ecotech hai lít pha năm trăm lít nước trên một héc ta mua từ công ty trách nhiệm hữu hạn quang minh t n a cho cây phát triển",
        "expected": {
            "activity": "fertilize",
            "ten_phan_bon": "nano ecotech",
            "luong_phan_bon": "2",
            "growth_stage": "phát triển"
        }
    },
    # BẢNG 3: PHUN THUỐC BVTV (spray)
    {
        "id": 5,
        "table": "spray",
        "name": "Phun thuốc Nano Ecotech & Top 1 (câu ghép thực tế)",
        "text": "tiến hành phun thuốc và bón phân dạng nước để trị sâu ăn lá rầy nâu bằng thuốc sinh học nano ecotech top một với liều lượng hai lít pha năm trăm lít nước trên một héc ta mua từ công ty trách nhiệm hữu hạn quang minh t n a và tập đoàn mind group với thời gian cách ly mười bốn ngày",
        "expected": {
            "activity": "spray",
            "ten_thuoc_bvtv": "nano ecotech",
            "target_pest": ["sâu ăn lá", "rầy nâu"],
            "nong_do_va_luong_dung": "2",
            "thoi_gian_cach_ly": "14"
        }
    },
    {
        "id": 6,
        "table": "spray",
        "name": "Phun thuốc trừ sâu",
        "text": "phun thuốc sinh học nano ecotech trị sâu ăn lá liều lượng một lít pha bốn trăm lít nước thời gian cách ly mười bốn ngày",
        "expected": {
            "activity": "spray",
            "ten_thuoc_bvtv": "nano ecotech",
            "target_pest": "sâu ăn lá",
            "nong_do_va_luong_dung": "1",
            "thoi_gian_cach_ly": "14"
        }
    },
    # BẢNG 4: XỬ LÝ BAO BÌ (waste)
    {
        "id": 7,
        "table": "waste",
        "name": "Thu gom vỏ chai thuốc",
        "text": "thu gom mười vỏ chai thuốc bảo vệ thực vật sau khi phun bỏ vào hố rác tập trung có nắp đậy đúng quy định vietgap",
        "expected": {
            "activity": "waste",
            "waste_type": "vỏ chai",
            "storage_location": "hố rác",
            "treatment_method": "thu gom"
        }
    },
    {
        "id": 8,
        "table": "waste",
        "name": "Súc rửa vỏ chai thuốc",
        "text": "súc rửa ba lần vỏ chai thuốc bảo vệ thực vật và thu gom vào bể chứa rác thải nguy hại",
        "expected": {
            "activity": "waste",
            "waste_type": "vỏ chai",
            "storage_location": "bể chứa",
            "treatment_method": "súc rửa"
        }
    },
    # BẢNG 5: THU HOẠCH (harvest)
    {
        "id": 9,
        "table": "harvest",
        "name": "Thu hoạch băm cắt chở về xưởng",
        "text": "thu hoạch cà gai leo bằng máy băm cắt chở về xưởng sấy sản lượng ước tính một tấn rưỡi trên một héc ta",
        "expected": {
            "activity": "harvest",
            "crop_name": "cà gai leo",
            "san_luong_thu_hoach": ["1.5", "một tấn rưỡi", "1,5"],
            "dia_diem_cach_so_che": "xưởng"
        }
    },
    {
        "id": 10,
        "table": "harvest",
        "name": "Cắt tỉa thu hoạch đợt 1",
        "text": "cắt tỉa thu hoạch cà gai leo đợt một sản lượng hai tấn chở về xưởng sơ chế phơi sấy",
        "expected": {
            "activity": "harvest",
            "crop_name": "cà gai leo",
            "san_luong_thu_hoach": "2",
            "dia_diem_cach_so_che": "xưởng"
        }
    },
    # BẢNG 6: TIÊU THỤ / XUẤT BÁN (sale)
    {
        "id": 11,
        "table": "sale",
        "name": "Xuất bán cho Sandu & thương lái",
        "text": "xuất bán năm ngàn ba trăm sáu mươi mốt ki lô gam cho công ty sandu và thương lái",
        "expected": {
            "activity": "sale",
            "khoi_luong_ban": "5361",
            "co_so_thu_mua": "sandu"
        }
    },
    {
        "id": 12,
        "table": "sale",
        "name": "Xuất bán cà gai leo",
        "text": "xuất bán năm trăm mười hai ki lô gam cà gai leo",
        "expected": {
            "activity": "sale",
            "crop_name": "cà gai leo",
            "khoi_luong_ban": "512"
        }
    },
    # BẢNG 7: QUAN SÁT SÂU BỆNH (observation)
    {
        "id": 13,
        "table": "observation",
        "name": "Quan sát rầy nâu",
        "text": "đi thăm vườn phát hiện rầy nâu mật độ nhẹ trên đọt non cây đang nuôi cành lá tiếp tục theo dõi chưa cần phun thuốc",
        "expected": {
            "activity": "observation",
            "sinh_vat_gay_hai": "rầy nâu",
            "muc_do": "nhẹ",
            "growth_stage": ["đọt non", "cành lá"],
            "action_taken": "theo dõi"
        }
    },
    {
        "id": 14,
        "table": "observation",
        "name": "Quan sát vườn sạch bệnh",
        "text": "kiểm tra toàn bộ vườn cà gai leo bảy héc ta cây sinh trưởng phát triển tốt không phát hiện sinh vật gây hại",
        "expected": {
            "activity": "observation",
            "sinh_vat_gay_hai": ["không phát hiện", "không"],
            "growth_stage": "sinh trưởng"
        }
    },
    # BẢNG 8: HOẠT ĐỘNG CHUNG (general)
    {
        "id": 15,
        "table": "general",
        "name": "Làm cỏ chia 3 tổ",
        "text": "với lực lượng chia làm ba tổ mỗi tổ mười đến mười lăm người làm cỏ cho cây sinh trưởng phát triển",
        "expected": {
            "activity": "general",
            "cong_viec": "làm cỏ",
            "so_luong": ["10-15", "10 đến 15", "ba tổ", "3 tổ"],
            "growth_stage": "sinh trưởng"
        }
    },
    {
        "id": 16,
        "table": "general",
        "name": "Xây dựng mở rộng xưởng sấy",
        "text": "tiến hành xây dựng mở rộng xưởng lò sấy và nhà máy sản xuất",
        "expected": {
            "activity": "general",
            "cong_viec": "xây dựng"
        }
    }
]


def check_errors(result: Dict[str, Any], expected: Dict[str, Any]) -> Tuple[float, List[str], List[str]]:
    """
    Kiểm tra chi tiết từng trường của kết quả so với mong đợi.
    Trả về (accuracy_pct, passed_fields, missing_or_wrong_fields)
    """
    if not result:
        return 0.0, [], [f"Toàn bộ trường: Model không trả về kết quả"]

    act_data = result.get("activities", {}) or {}
    raw_tables = result.get("tables", {}) or {}
    extracted = result.get("extracted_fields", {}) or {}

    flat = {}
    for source in [act_data, raw_tables, extracted]:
        if isinstance(source, dict):
            for k, v in source.items():
                if isinstance(v, dict):
                    for sub_k, sub_v in v.items():
                        flat[sub_k.lower()] = str(sub_v).lower() if sub_v is not None else ""
                else:
                    flat[k.lower()] = str(v).lower() if v is not None else ""

    active_acts = result.get("active_activities") or []
    if isinstance(active_acts, str):
        active_acts = [active_acts]
    active_acts = [str(a).lower() for a in active_acts]

    passed = []
    failed = []

    for exp_k, exp_v in expected.items():
        if exp_k == "activity":
            exp_act = str(exp_v).lower()
            if exp_act in active_acts or exp_act in act_data:
                passed.append(f"activity='{exp_act}'")
            else:
                failed.append(f"activity: mong đợi '{exp_act}', thực tế nhận '{active_acts}'")
            continue

        actual_v = flat.get(exp_k.lower(), "")
        found = False
        if actual_v:
            if isinstance(exp_v, list):
                if any(target.lower() in actual_v for target in exp_v):
                    found = True
            elif str(exp_v).lower() in actual_v:
                found = True

        if not found:
            # Thử tìm trong toàn bộ giá trị flat
            for k, val in flat.items():
                if isinstance(exp_v, list):
                    if any(target.lower() in str(val).lower() for target in exp_v):
                        found = True
                        actual_v = f"{k}={val}"
                        break
                elif str(exp_v).lower() in str(val).lower():
                    found = True
                    actual_v = f"{k}={val}"
                    break

        if found:
            passed.append(f"{exp_k} (tìm thấy: '{actual_v}')")
        else:
            failed.append(f"{exp_k}: mong đợi chứa '{exp_v}', nhưng thực tế không tìm thấy (giá trị có: '{actual_v or 'None'}')")

    total = len(passed) + len(failed)
    acc = round((len(passed) / total) * 100.0, 1) if total > 0 else 100.0
    return acc, passed, failed


def switch_rkllm_model(model_path: str):
    """Chuyển đổi model trong rkllm.service và restart service."""
    print(f"\n🔄 Đang chuyển đổi RKLLM Model sang: {model_path}...")
    service_file = "/etc/systemd/system/rkllm.service"
    with open(service_file, "r") as f:
        content = f.read()

    import re
    new_content = re.sub(r'Environment="RKLLM_MODEL=.*?"', f'Environment="RKLLM_MODEL={model_path}"', content)
    with open(service_file, "w") as f:
        f.write(new_content)

    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "restart", "rkllm.service"], check=True)
    time.sleep(8)

    import urllib.request
    for attempt in range(20):
        try:
            req = urllib.request.Request("http://127.0.0.1:8080/v1/models")
            with urllib.request.urlopen(req, timeout=3) as response:
                if response.status == 200:
                    print("✅ RKLLM Server đã sẵn sàng!")
                    return
        except Exception:
            time.sleep(2)
    print("⚠️ Cảnh báo: Server khởi động chậm hơn dự kiến.")


async def run_all():
    print("🚀 BẮT ĐẦU CHẠY TRÍCH XUẤT VÀ PHÂN TÍCH LỖI CHO 3 MODEL TRÊN SAMPLE ĐÃ CHUẨN HÓA...")

    # 1. Chạy Gemini
    print("\n--- 1. Chạy Google Gemini ---")
    await gemini_engine.start()
    gemini_data = []
    for tc in TEST_CASES:
        t0 = time.time()
        res = None
        for attempt in range(4):
            try:
                res = await gemini_engine.extract_info(tc["text"], job_type=tc["table"])
                break
            except Exception as e:
                if "429" in str(e) or "quota" in str(e).lower():
                    wait_time = 15 + attempt * 10
                    print(f"⚠️ Gemini chạm Rate Limit (429), chờ {wait_time}s rồi thử lại... (lần {attempt+1}/4)")
                    await asyncio.sleep(wait_time)
                else:
                    raise
        if res is None:
            raise RuntimeError(f"Không thể gọi Gemini sau 4 lần thử: {tc['name']}")

        elapsed = time.time() - t0
        acc, passed, failed = check_errors(res, tc["expected"])
        gemini_data.append({
            "tc": tc,
            "latency": res.get("latency_s") or round(elapsed, 2),
            "tps": res.get("tokens_per_sec") or 0.0,
            "accuracy": acc,
            "passed": passed,
            "failed": failed,
            "activities": res.get("activities", {}),
            "tables": res.get("tables", {})
        })
        print(f"Gemini [{tc['id']:02d}/16]: Acc {acc}% | Failed: {len(failed)}")
        await asyncio.sleep(2)  # Tránh chạm ngưỡng 15 RPM
    await gemini_engine.stop()

    # 2. Chạy RKLLM 2B (đang chạy sẵn)
    print("\n--- 2. Chạy RKLLM 2B ---")
    await llm_engine.start()
    rkllm_2b_data = []
    for tc in TEST_CASES:
        t0 = time.time()
        res = await llm_engine.extract_info(tc["text"], job_type=tc["table"])
        elapsed = time.time() - t0
        acc, passed, failed = check_errors(res, tc["expected"])
        rkllm_2b_data.append({
            "tc": tc,
            "latency": res.get("latency_s") or round(elapsed, 2),
            "tps": res.get("tokens_per_sec") or 0.0,
            "accuracy": acc,
            "passed": passed,
            "failed": failed,
            "activities": res.get("activities", {}),
            "tables": res.get("tables", {})
        })
        print(f"RKLLM 2B [{tc['id']:02d}/16]: Acc {acc}% | Failed: {len(failed)}")
    await llm_engine.stop()

    # 3. Chuyển sang RKLLM 0.6B và chạy
    print("\n--- 3. Chuyển sang RKLLM 0.6B ---")
    switch_rkllm_model("/mnt/data/qwen3-0.6b_w8a8_rk3588.rkllm")
    await llm_engine.start()
    rkllm_06b_data = []
    for tc in TEST_CASES:
        t0 = time.time()
        res = await llm_engine.extract_info(tc["text"], job_type=tc["table"])
        elapsed = time.time() - t0
        acc, passed, failed = check_errors(res, tc["expected"])
        rkllm_06b_data.append({
            "tc": tc,
            "latency": res.get("latency_s") or round(elapsed, 2),
            "tps": res.get("tokens_per_sec") or 0.0,
            "accuracy": acc,
            "passed": passed,
            "failed": failed,
            "activities": res.get("activities", {}),
            "tables": res.get("tables", {})
        })
        print(f"RKLLM 0.6B [{tc['id']:02d}/16]: Acc {acc}% | Failed: {len(failed)}")
    await llm_engine.stop()

    # 4. Trả lại RKLLM 2B
    switch_rkllm_model("/mnt/data/Qwen3.5-2B_RK3588_w8a8.rkllm")

    # 5. Tạo nội dung file result.txt
    print("\n📝 Đang tổng hợp và ghi kết quả vào /root/voice_to_data/result.txt...")
    lines = []
    lines.append("="*90)
    lines.append("BÁO CÁO KẾT QUẢ TRÍCH XUẤT CHI TIẾT & PHÂN TÍCH LỖI CÁC MÔ HÌNH (GEMINI vs RKLLM 2B vs RKLLM 0.6B)")
    lines.append("Tập dữ liệu: sample.txt (16 ca kiểm thử bao quát 8 bảng VietGAP đã chuẩn hóa)")
    lines.append("Thời gian thực nghiệm: " + time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("="*90 + "\n")

    lines.append("MỤC LỤC:")
    lines.append("PHẦN 1: TỔNG QUAN VÀ PHÂN TÍCH LỖI CỤ THỂ CỦA MÔ HÌNH RKLLM 2B (2B SAI Ở ĐÂU?)")
    lines.append("PHẦN 2: SO SÁNH LỖI GIỮA CÁC MÔ HÌNH (GEMINI vs 2B vs 0.6B)")
    lines.append("PHẦN 3: CHI TIẾT KẾT QUẢ TRÍCH XUẤT 16 CA KIỂM THỬ TRÊN CẢ 3 MÔ HÌNH")
    lines.append("\n" + "="*90 + "\n")

    # PHẦN 1: PHÂN TÍCH LỖI 2B
    lines.append("="*90)
    lines.append("PHẦN 1: PHÂN TÍCH LỖI CỤ THỂ CỦA MÔ HÌNH RKLLM 2B (2B SAI Ở ĐÂU?)")
    lines.append("="*90)

    total_2b_failed = 0
    cases_with_errors_2b = []
    for d in rkllm_2b_data:
        if d["failed"]:
            total_2b_failed += len(d["failed"])
            cases_with_errors_2b.append(d)

    lines.append(f"\nTổng số ca kiểm thử: {len(TEST_CASES)}")
    lines.append(f"Số ca 2B đúng tuyệt đối (100%): {len(TEST_CASES) - len(cases_with_errors_2b)}/{len(TEST_CASES)}")
    lines.append(f"Số ca 2B có trường bị sai/bỏ sót: {len(cases_with_errors_2b)}/{len(TEST_CASES)}")
    lines.append(f"Tổng số trường bị sai/thiếu: {total_2b_failed} trường.\n")

    lines.append("CÁC LỖI CỤ THỂ CỦA RKLLM 2B THEO TỪNG CA KIỂM THỬ:")
    for d in cases_with_errors_2b:
        tc = d["tc"]
        lines.append(f"\n▶ [Ca {tc['id']:02d}] {tc['table'].upper()}: {tc['name']}")
        lines.append(f"  - Câu đầu vào STT: \"{tc['text']}\"")
        lines.append(f"  - Độ chính xác: {d['accuracy']}%")
        lines.append("  - Trường trích xuất đúng:")
        for p in d["passed"]:
            lines.append(f"    ✓ {p}")
        lines.append("  - TRƯỜNG BỊ SAI / THIẾU (LỖI CỦA 2B):")
        for f in d["failed"]:
            lines.append(f"    ✗ {f}")

    # PHẦN 2: SO SÁNH LỖI GIỮA CÁC MÔ HÌNH
    lines.append("\n" + "="*90)
    lines.append("PHẦN 2: BẢNG SO SÁNH ĐỘ CHÍNH XÁC & LỖI GIỮA 3 MÔ HÌNH")
    lines.append("="*90 + "\n")
    lines.append(f"{'ID':<4} | {'Bảng':<12} | {'Tên ca':<35} | {'Gemini Acc':<11} | {'2B Acc':<10} | {'0.6B Acc':<10}")
    lines.append("-" * 90)
    for g, b2, b06 in zip(gemini_data, rkllm_2b_data, rkllm_06b_data):
        tc = g["tc"]
        lines.append(f"{tc['id']:<4} | {tc['table']:<12} | {tc['name'][:35]:<35} | {g['accuracy']:<10.1f}% | {b2['accuracy']:<9.1f}% | {b06['accuracy']:<9.1f}%")

    avg_gem = sum(d["accuracy"] for d in gemini_data) / len(gemini_data)
    avg_2b = sum(d["accuracy"] for d in rkllm_2b_data) / len(rkllm_2b_data)
    avg_06b = sum(d["accuracy"] for d in rkllm_06b_data) / len(rkllm_06b_data)
    lines.append("-" * 90)
    lines.append(f"{'TB':<4} | {'TẤT CẢ':<12} | {'ĐỘ CHÍNH XÁC TRUNG BÌNH':<35} | {avg_gem:<10.1f}% | {avg_2b:<9.1f}% | {avg_06b:<9.1f}%")

    # PHẦN 3: CHI TIẾT KẾT QUẢ TRÍCH XUẤT CỦA TỪNG CA
    lines.append("\n" + "="*90)
    lines.append("PHẦN 3: CHI TIẾT DỮ LIỆU TRÍCH XUẤT (RAW JSON) CỦA 16 CA KIỂM THỬ TRÊN CẢ 3 MÔ HÌNH")
    lines.append("="*90)

    for g, b2, b06 in zip(gemini_data, rkllm_2b_data, rkllm_06b_data):
        tc = g["tc"]
        lines.append("\n" + "#"*80)
        lines.append(f"CA KIỂM THỬ {tc['id']:02d}/16: [{tc['table'].upper()}] {tc['name']}")
        lines.append(f"Văn bản STT: \"{tc['text']}\"")
        lines.append("Kỳ vọng (Expected): " + json.dumps(tc["expected"], ensure_ascii=False))
        lines.append("#"*80)

        lines.append("\n--- [1] GOOGLE GEMINI (3.5 FLASH-LITE) ---")
        lines.append(f"Latency: {g['latency']}s | TPS: {g['tps']} | Accuracy: {g['accuracy']}%")
        lines.append("Trích xuất hoạt động (Activities):")
        lines.append(json.dumps(g["activities"], ensure_ascii=False, indent=2))

        lines.append("\n--- [2] RKLLM 2B (NPU RK3588) ---")
        lines.append(f"Latency: {b2['latency']}s | TPS: {b2['tps']} | Accuracy: {b2['accuracy']}%")
        lines.append("Trích xuất hoạt động (Activities):")
        lines.append(json.dumps(b2["activities"], ensure_ascii=False, indent=2))
        if b2["failed"]:
            lines.append("Các lỗi của 2B:")
            for f in b2["failed"]:
                lines.append(f"  ✗ {f}")

        lines.append("\n--- [3] RKLLM 0.6B (NPU RK3588) ---")
        lines.append(f"Latency: {b06['latency']}s | TPS: {b06['tps']} | Accuracy: {b06['accuracy']}%")
        lines.append("Trích xuất hoạt động (Activities):")
        lines.append(json.dumps(b06["activities"], ensure_ascii=False, indent=2))
        if b06["failed"]:
            lines.append("Các lỗi của 0.6B:")
            for f in b06["failed"]:
                lines.append(f"  ✗ {f}")

    result_path = "/root/voice_to_data/result.txt"
    with open(result_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n✅ ĐÃ HOÀN TẤT VÀ LƯU VÀO: {result_path}")

    # Lưu thêm ra benchmark_results.json
    json_path = "/root/.gemini/antigravity-cli/brain/f6109d8c-6d49-4d2b-a665-6a86e2afda23/scratch/benchmark_results.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "gemini": gemini_data,
            "rkllm_2b": rkllm_2b_data,
            "rkllm_06b": rkllm_06b_data,
            "timestamp": time.time()
        }, f, ensure_ascii=False, indent=2)
    print(f"✅ ĐÃ CẬP NHẬT JSON BENCHMARK: {json_path}")


if __name__ == "__main__":
    asyncio.run(run_all())
