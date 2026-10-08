#!/usr/bin/env python3
"""
VLM CLI & Testing Tool - Công cụ dòng lệnh nhận diện sâu bệnh, tên thuốc BVTV,
phân bón từ hình ảnh sử dụng mô hình Google AI Gemini 3.1 Flash Lite.

Cách sử dụng:
1. Nhận diện tự động ảnh:
   python vlm_cli.py /mnt/data/photos/photo_1682_0.jpg

2. Chẩn đoán chuyên sâu sâu bệnh:
   python vlm_cli.py /path/to/disease_leaf.jpg --task pest_disease

3. Kèm câu hỏi / prompt bổ sung:
   python vlm_cli.py /path/to/leaf.jpg -p "Cây bưởi này có triệu chứng gì, xịt thuốc nào phù hợp?"

4. Xuất toàn bộ cấu trúc JSON:
   python vlm_cli.py /mnt/data/photos/photo_1755_0.jpg --json

5. Chạy benchmark đo tốc độ VLM:
   python vlm_cli.py --benchmark

6. Tự động nhận diện và thêm vào nhật ký canh tác VietGAP (4 hoạt động):
   python vlm_cli.py /mnt/data/photos/photo_1682_0.jpg --add-activity --intent fertilize
   python vlm_cli.py /mnt/data/photos/photo_1755_0.jpg --add-activity --intent spray
   python vlm_cli.py /mnt/data/photos/photo_1755_0.jpg --add-activity --intent purchase
   python vlm_cli.py /mnt/data/photos/photo_1765_0.jpg --add-activity --intent observation
"""

import argparse
import json
import logging
import os
import sys
import time

# Đảm bảo import được module từ thư mục gốc
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from services.engines.vlm_engine import vlm_engine, VLMAnalysisResult

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("vlm_cli")


def print_colored_result(res: VLMAnalysisResult):
    """In kết quả phân tích VLM trực quan, rõ ràng trên terminal."""
    print("=" * 80)
    print(f"🌿 KẾT QUẢ PHÂN TÍCH VLM (Gemini Model: {res.model_used} | Thời gian: {res.execution_time_ms:.1f}ms)")
    print("=" * 80)

    category_names = {
        "pest_disease": "🐛 CHẨN ĐOÁN SÂU BỆNH HẠI",
        "pesticide": "🧪 NHẬN DIỆN THUỐC BẢO VỆ THỰC VẬT",
        "fertilizer": "🌱 NHẬN DIỆN PHÂN BÓN",
        "field_crop": "🌾 HIỆN TRƯỜNG / VƯỜN CÂY CANH TÁC",
        "invoice_receipt": "🧾 HÓA ĐƠN / CHỨNG TỪ VẬT TƯ",
        "other": "📷 HÌNH ẢNH KHÁC",
    }
    cat_label = category_names.get(res.category, f"ĐỐI TƯỢNG: {res.category.upper()}")
    print(f"\n📌 PHÂN LOẠI: {cat_label}")
    print(f"📌 TIÊU ĐỀ  : {res.title}")
    if res.summary:
        print(f"📝 TÓM TẮT  : {res.summary}")

    # 1. Chi tiết Sâu bệnh hại
    if res.pest_disease:
        p = res.pest_disease
        print("\n" + "-" * 80)
        print("🔍 CHI TIẾT CHẨN ĐOÁN SÂU BỆNH HẠI:")
        print("-" * 80)
        print(f"  • Tên sâu bệnh      : {p.pest_name}" + (f" ({p.scientific_name})" if p.scientific_name else ""))
        print(f"  • Nhóm tác nhân     : {p.pest_type or 'Chưa rõ'}")
        if p.crop_name:
            print(f"  • Cây trồng bị hại  : {p.crop_name}")
        if p.affected_part:
            print(f"  • Bộ phận bị hại    : {p.affected_part}")
        if p.severity:
            print(f"  • Mức độ nghiêm trọng: {p.severity}")
        if p.symptoms:
            print(f"  • Triệu chứng       : {p.symptoms}")
        if p.causes_and_conditions:
            print(f"  • Điều kiện phát sinh: {p.causes_and_conditions}")
        if p.cultural_controls:
            print(f"  • Biện pháp canh tác: {p.cultural_controls}")
        if p.biological_controls:
            print(f"  • Biện pháp sinh học: {p.biological_controls}")
        if p.recommended_actives:
            print(f"  • Hoạt chất khuyến nghị: {', '.join(p.recommended_actives)}")

        if p.matched_farm_pesticides:
            print("\n  📦 THUỐC BVTV PHÙ HỢP CÓ SẴN TRONG CSDL TRANG TRẠI:")
            for idx, med in enumerate(p.matched_farm_pesticides, 1):
                phi = f"Cách ly {med['min_isolation_days']} ngày" if med.get('min_isolation_days') is not None else "PHI: N/A"
                print(f"    [{idx}] [ID {med['pesticide_id']}] {med['name']} ({med.get('formulation') or 'N/A'}) - {phi}")
                print(f"        Hoạt chất: {med.get('active_ingredient') or 'N/A'}")
                if med.get('dosage'):
                    print(f"        Liều lượng: {med['dosage']}")
                if med.get('registered_pests'):
                    print(f"        Phòng trừ: {med['registered_pests']}")
        else:
            print("\n  ⚠️ Chưa tìm thấy thuốc BVTV đặc trị tương ứng trong kho CSDL trang trại.")

    # 2. Chi tiết Thuốc BVTV
    if res.pesticide:
        pest = res.pesticide
        print("\n" + "-" * 80)
        print("🧪 CHI TIẾT THUỐC BẢO VỆ THỰC VẬT:")
        print("-" * 80)
        print(f"  • Tên thương phẩm   : {pest.trade_name}")
        if pest.active_ingredient:
            print(f"  • Hoạt chất         : {pest.active_ingredient}")
        if pest.formulation:
            print(f"  • Dạng thuốc        : {pest.formulation}")
        if pest.toxicity_group:
            print(f"  • Nhóm độc          : {pest.toxicity_group}")
        if pest.target_pests:
            print(f"  • Đối tượng phòng trừ: {', '.join(pest.target_pests)}")
        if pest.registered_crops:
            print(f"  • Cây trồng đăng ký : {', '.join(pest.registered_crops)}")
        if pest.dosage_and_usage:
            print(f"  • Liều lượng & cách dùng: {pest.dosage_and_usage}")
        if pest.isolation_days is not None:
            print(f"  • Thời gian cách ly (PHI): {pest.isolation_days} ngày")
        if pest.manufacturer:
            print(f"  • Nhà sản xuất / Đăng ký: {pest.manufacturer}")
        if pest.safety_precautions:
            print(f"  • Lưu ý an toàn     : {pest.safety_precautions}")

        if pest.db_match:
            db = pest.db_match
            print("\n  🔗 ĐỐI SOÁT VỚI CSDL MYSQL VIETGAP:")
            if db.get("matched"):
                print(f"    ✅ Khớp chính xác CSDL: [ID {db.get('id')}] '{db.get('name')}' (Độ tương đồng: {db.get('score')}%)")
                details = db.get("details") or {}
                if details.get("active_ingredient"):
                    print(f"       Hoạt chất chuẩn DB: {details.get('active_ingredient')}")
                if details.get("min_isolation_days") is not None:
                    print(f"       Thời gian cách ly chuẩn: {details.get('min_isolation_days')} ngày")
                rag = db.get("rag_knowledge") or {}
                if rag.get("lieu_luong_nong_do"):
                    print(f"       Liều lượng chuẩn VietGAP: {rag.get('lieu_luong_nong_do')}")
            else:
                print(f"    ⚠️ Chưa có sản phẩm này trong CSDL trang trại (Gần nhất: '{db.get('name')}', {db.get('score')}%)")

    # 3. Chi tiết Phân bón
    if res.fertilizer:
        f = res.fertilizer
        print("\n" + "-" * 80)
        print("🌱 CHI TIẾT PHÂN BÓN:")
        print("-" * 80)
        print(f"  • Tên phân bón      : {f.trade_name}")
        if f.fertilizer_type:
            print(f"  • Loại phân bón     : {f.fertilizer_type}")
        if f.composition_npk:
            print(f"  • Thành phần / NPK  : {f.composition_npk}")
        if f.benefits:
            print(f"  • Công dụng         : {', '.join(f.benefits)}")
        if f.dosage_and_usage:
            print(f"  • Liều lượng & cách bón: {f.dosage_and_usage}")
        if f.suitable_crops:
            print(f"  • Cây trồng phù hợp : {', '.join(f.suitable_crops)}")
        if f.manufacturer:
            print(f"  • Nhà sản xuất      : {f.manufacturer}")

        if f.db_match:
            db = f.db_match
            print("\n  🔗 ĐỐI SOÁT VỚI CSDL MYSQL VIETGAP:")
            if db.get("matched"):
                print(f"    ✅ Khớp chính xác CSDL: [ID {db.get('id')}] '{db.get('name')}' (Độ tương đồng: {db.get('score')}%)")
                details = db.get("details") or {}
                if details.get("fertilizer_type"):
                    print(f"       Loại phân bón DB: {details.get('fertilizer_type')}")
                if details.get("npk_ratio"):
                    print(f"       Thành phần chuẩn DB: {details.get('npk_ratio')}")
                rag = db.get("rag_knowledge") or {}
                if rag.get("lieu_luong_nong_do"):
                    print(f"       Liều lượng chuẩn VietGAP: {rag.get('lieu_luong_nong_do')}")
            else:
                print(f"    ⚠️ Chưa có sản phẩm này trong CSDL trang trại (Gần nhất: '{db.get('name')}', {db.get('score')}%)")

    print("=" * 80 + "\n")


def run_benchmark():
    """Chạy benchmark kiểm tra hiệu năng Gemini 3.1 Flash Lite trên tập ảnh mẫu."""
    print("=" * 80)
    print("          BENCHMARK VLM ENGINE (GEMINI 3.1 FLASH LITE) TRÊN ẢNH MẪU")
    print("=" * 80)

    sample_dir = "/mnt/data/photos"
    if not os.path.exists(sample_dir):
        print(f"Không tìm thấy thư mục ảnh {sample_dir}")
        return

    # Lấy 5 ảnh mẫu đại diện
    candidates = [
        ("photo_1682_0.jpg", "Phân bón SIAMB 12"),
        ("photo_1755_0.jpg", "Thuốc BVTV BP. Newone 500SC"),
        ("photo_1757_0.jpg", "Dầu khoáng Ds 98.8EC"),
        ("photo_1765_0.jpg", "Dũng sĩ diệt ruồi vàng An Phát"),
        ("photo_1661_0.jpg", "Canxi Bo - Silic"),
    ]

    durations = []
    success_count = 0

    for idx, (fname, desc) in enumerate(candidates, 1):
        fpath = os.path.join(sample_dir, fname)
        if not os.path.exists(fpath):
            continue
        print(f"\n[{idx}/5] Đang phân tích ảnh '{fname}' ({desc})...")
        t0 = time.time()
        res = vlm_engine.analyze_image_sync(fpath)
        dur = (time.time() - t0) * 1000
        durations.append(dur)
        if res.status == "success":
            success_count += 1
            print(f"  ✓ Thành công trong {dur:.1f}ms: [{res.category}] {res.title}")
            if res.pesticide and res.pesticide.db_match and res.pesticide.db_match.get("matched"):
                print(f"    ➜ Khớp CSDL: ID {res.pesticide.db_match.get('id')} - {res.pesticide.db_match.get('name')}")
            elif res.fertilizer and res.fertilizer.db_match and res.fertilizer.db_match.get("matched"):
                print(f"    ➜ Khớp CSDL: ID {res.fertilizer.db_match.get('id')} - {res.fertilizer.db_match.get('name')}")
        else:
            print(f"  ✗ Thất bại ({dur:.1f}ms): {res.error}")

    if durations:
        avg_dur = sum(durations) / len(durations)
        print("\n" + "=" * 80)
        print("TỔNG KẾT BENCHMARK:")
        print(f"  • Số ảnh thử nghiệm : {len(durations)}")
        print(f"  • Thành công        : {success_count}/{len(durations)} ({success_count/len(durations)*100:.1f}%)")
        print(f"  • Thời gian xử lý TB: {avg_dur:.1f} ms/ảnh ({avg_dur/1000:.2f} giây)")
        print("=" * 80)


def main():
    parser = argparse.ArgumentParser(
        description="VLM CLI - Nhận diện sâu bệnh, tên thuốc, phân bón qua Gemini 3.1 Flash Lite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("image_path", nargs="?", help="Đường dẫn đến file ảnh cần phân tích")
    parser.add_argument("--prompt", "-p", help="Chỉ dẫn bổ sung hoặc câu hỏi của người dùng")
    parser.add_argument("--task", "-t", choices=["auto", "pest_disease", "pesticide", "fertilizer"], default="auto", help="Nhiệm vụ phân tích cụ thể (mặc định: auto)")
    parser.add_argument("--model", "-m", help="Tên model Gemini ghi đè (mặc định: gemini-3.1-flash-lite)")
    parser.add_argument("--no-db", action="store_true", help="Không tự động đối soát với CSDL MySQL")
    parser.add_argument("--json", "-j", action="store_true", help="In kết quả dưới dạng JSON")
    parser.add_argument("--benchmark", "-b", action="store_true", help="Chạy bài benchmark kiểm thử trên tập ảnh mẫu")
    parser.add_argument("--add-activity", "-a", "--save", action="store_true", help="Lưu kết quả phân tích thành hoạt động nhật ký canh tác trong CSDL VietGAP")
    parser.add_argument("--intent", "-i", choices=["auto", "purchase", "spray", "fertilize", "observation"], default="auto", help="Loại hoạt động muốn thêm (auto/purchase/spray/fertilize/observation, mặc định: auto)")
    parser.add_argument("--farmer-id", type=int, help="ID của nông dân / người dùng ghi nhật ký")
    parser.add_argument("--parcel-id", "--plot-id", help="ID lô đất / thửa ruộng canh tác")
    parser.add_argument("--crop-name", help="Tên cây trồng ghi đè")
    parser.add_argument("--activity-date", "--date", help="Ngày thực hiện hoạt động (YYYY-MM-DD)")

    args = parser.parse_args()

    if args.benchmark:
        run_benchmark()
        return

    if not args.image_path:
        parser.print_help()
        sys.exit(1)

    if not os.path.exists(args.image_path):
        print(f"Lỗi: Không tìm thấy file ảnh '{args.image_path}'", file=sys.stderr)
        sys.exit(1)

    match_db = not args.no_db
    res = vlm_engine.analyze_image_sync(
        image_input=args.image_path,
        prompt=args.prompt,
        task=args.task,
        model=args.model,
        match_db=match_db,
    )

    if args.json:
        out_dict = res.to_dict()
        if args.add_activity and res.status == "success":
            db_save = res.save_to_database(
                farmer_id=args.farmer_id,
                intent=args.intent,
                parcel_id=args.parcel_id,
                crop_name=args.crop_name,
                activity_date=args.activity_date,
            )
            out_dict["diary_db_result"] = db_save
        print(json.dumps(out_dict, ensure_ascii=False, indent=2))
    else:
        if res.status != "success":
            print(f"❌ Lỗi phân tích: {res.error}", file=sys.stderr)
            sys.exit(2)
        print_colored_result(res)

        if args.add_activity:
            import shutil
            import uuid
            STATIC_PHOTOS_DIR = os.path.join(ROOT_DIR, "app", "static", "photos")
            os.makedirs(STATIC_PHOTOS_DIR, exist_ok=True)
            orig_ext = os.path.splitext(args.image_path)[1].lower() or ".jpg"
            photo_filename = f"vlm_cli_{uuid.uuid4().hex[:8]}{orig_ext}"
            dest_photo = os.path.join(STATIC_PHOTOS_DIR, photo_filename)
            shutil.copyfile(args.image_path, dest_photo)
            photo_url = f"/static/photos/{photo_filename}"

            db_save = res.save_to_database(
                farmer_id=args.farmer_id,
                intent=args.intent,
                photo_url=photo_url,
                parcel_id=args.parcel_id,
                crop_name=args.crop_name,
                activity_date=args.activity_date,
            )

            print("=" * 80)
            if db_save.get("status") == "success":
                print("📝 ĐÃ LƯU THÀNH CÔNG HOẠT ĐỘNG VÀO CSDL VIETGAP:")
                print("=" * 80)
                print(f"  • Loại hoạt động   : {args.intent.upper()}")
                print(f"  • Ảnh đính kèm     : {photo_url}")
                print(f"  • Bản ghi CSDL     : {db_save.get('created_records') or db_save}")
                if db_save.get("season_id"):
                    print(f"  • Season ID        : {db_save.get('season_id')}")
                if db_save.get("activity_id"):
                    print(f"  • Activity ID      : {db_save.get('activity_id')}")
                if db_save.get("purchase_id"):
                    print(f"  • Purchase ID      : {db_save.get('purchase_id')}")
                print("=" * 80)
            else:
                print("❌ LỖI LƯU CSDL:")
                print(f"  • Thông điệp: {db_save.get('message')}")
                print("=" * 80)


if __name__ == "__main__":
    main()
