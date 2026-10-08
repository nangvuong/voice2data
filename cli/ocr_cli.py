#!/usr/bin/env python3
"""
OCR CLI & Testing Tool - Công cụ dòng lệnh thử nghiệm nhận diện ký tự quang học (OCR) với RapidOCR.

Cách dùng:
1. Nhận diện file ảnh và in kết quả ra màn hình:
   python ocr_cli.py /path/to/invoice.jpg

2. Nhận diện và xuất ảnh có vẽ bounding box (visualization):
   python ocr_cli.py /path/to/invoice.jpg --vis result_vis.jpg

3. Xuất kết quả chi tiết dạng JSON:
   python ocr_cli.py /path/to/invoice.jpg --json

4. Lọc độ tin cậy và trích xuất hoạt động canh tác VietGAP qua AI:
   python ocr_cli.py /path/to/invoice.jpg --min-score 0.6 --extract

5. Chạy benchmark kiểm tra hiệu năng RapidOCR trên hệ thống:
   python ocr_cli.py --benchmark
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

from PIL import Image, ImageDraw
import numpy as np

from services.engines.ocr_engine import OCRError, OCRResult, ocr_engine

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ocr_cli")


def generate_benchmark_image(width=1000, height=600) -> Image.Image:
    """Tạo một ảnh tổng hợp mô phỏng hóa đơn mua bán vật tư nông nghiệp."""
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    lines = [
        "CỬA HÀNG VẬT TƯ NÔNG NGHIỆP SÁU THẮNG",
        "Địa chỉ: Ấp Phú Hòa, Xã Phú Thịnh, Huyện Tam Bình, Tỉnh Vĩnh Long",
        "Điện thoại: 0918.123.456 - Mã số thuế: 1500987654",
        "--------------------------------------------------------------------------------",
        "PHIẾU XUẤT KHO KIÊM BÁN HÀNG VẬT TƯ BVTV",
        "Số hóa đơn: HD-2026/09-0829 - Ngày lập: 28/09/2026",
        "Khách hàng: Nguyễn Văn Nông - Vùng trồng: Lô 2, Bưởi da xanh",
        "--------------------------------------------------------------------------------",
        "1. Phân bón NPK Đầu Trâu 20-20-15: 5 bao (250 kg) x 450.000 đ = 2.250.000 đ",
        "2. Thuốc trừ bệnh Ridomil Gold 68WG: 10 gói (1 kg) x 65.000 đ = 650.000 đ",
        "3. Chế phẩm sinh học Trichoderma: 4 hũ (4 kg) x 85.000 đ = 340.000 đ",
        "4. Phân bón lá Humic Acid 100%: 2 bao (50 kg) x 320.000 đ = 640.000 đ",
        "--------------------------------------------------------------------------------",
        "Tổng tiền thanh toán: 3.880.000 VNĐ (Ba triệu tám trăm tám mươi nghìn đồng)",
        "Ghi chú: Phun phòng ngừa bệnh thán thư và nấm Phytophthora đợt mưa đầu mùa.",
        "Thời gian cách ly tối thiểu: 14 ngày trước khi thu hoạch theo chuẩn VietGAP.",
    ]

    y = 30
    for line in lines:
        draw.text((40, y), line, fill=(0, 0, 0))
        y += 35

    return img


def run_benchmark():
    """Chạy bài kiểm thử hiệu năng RapidOCR."""
    print("=" * 80)
    print("           BÀI ĐO HIỆU NĂNG RAPIDOCR TRÊN HỆ THỐNG (BENCHMARK)")
    print("=" * 80)

    print("\n[1/3] Đang tạo ảnh thử nghiệm hóa đơn vật tư nông nghiệp (1000x600 px)...")
    bench_img = generate_benchmark_image()
    temp_path = os.path.join(ROOT_DIR, "tmp", "benchmark_ocr_sample.png")
    os.makedirs(os.path.dirname(temp_path), exist_ok=True)
    bench_img.save(temp_path)

    print("\n[2/3] Chạy warm-up inference...")
    ocr_engine.ocr_sync(temp_path)

    print("\n[3/3] Tiến hành đo đạc qua 5 lần lặp...")
    durations = []
    det_times = []
    cls_times = []
    rec_times = []
    total_lines = 0

    for i in range(5):
        t0 = time.time()
        res = ocr_engine.ocr_sync(temp_path)
        dur = time.time() - t0
        durations.append(dur)
        det_times.append(res.det_elapse)
        cls_times.append(res.cls_elapse)
        rec_times.append(res.rec_elapse)
        total_lines = res.total_lines
        print(f"  • Lần {i + 1}: {dur * 1000:.1f}ms (Det: {res.det_elapse * 1000:.1f}ms, Cls: {res.cls_elapse * 1000:.1f}ms, Rec: {res.rec_elapse * 1000:.1f}ms) | {res.total_lines} dòng")

    avg_dur = sum(durations) / len(durations)
    avg_det = sum(det_times) / len(det_times)
    avg_cls = sum(cls_times) / len(cls_times)
    avg_rec = sum(rec_times) / len(rec_times)
    fps = 1.0 / avg_dur if avg_dur > 0 else 0

    print("-" * 80)
    print("KẾT QUẢ HIỆU NĂNG TRUNG BÌNH:")
    print(f"  • Tổng thời gian/ảnh: {avg_dur * 1000:.1f} ms ({fps:.2f} ảnh/giây)")
    print(f"  • Text Detection    : {avg_det * 1000:.1f} ms ({avg_det / avg_dur * 100:.1f}%)")
    print(f"  • Direction Angle   : {avg_cls * 1000:.1f} ms ({avg_cls / avg_dur * 100:.1f}%)")
    print(f"  • Text Recognition  : {avg_rec * 1000:.1f} ms ({avg_rec / avg_dur * 100:.1f}%)")
    print(f"  • Tổng số dòng nhận diện: {total_lines} dòng")
    print("=" * 80)


def extract_vietgap_data(text: str):
    """Trích xuất thông tin hoạt động canh tác từ văn bản OCR qua AI Engine."""
    print("\n" + "=" * 80)
    print("TRÍCH XUẤT THÔNG TIN CANH TÁC VIETGAP TỪ VĂN BẢN OCR")
    print("=" * 80)

    try:
        from services.engines.gemini_engine import gemini_engine
        import asyncio

        print("[AI] Đang gửi văn bản OCR đến AI Engine...")
        result = asyncio.run(gemini_engine.extract_activities(text))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    except Exception as e:
        logger.warning("Gemini AI không khả dụng hoặc lỗi (%s), chuyển sang regex parser...", e)
        try:
            from services.vietgap.regex_parser import parse_with_regex
            reg_res = parse_with_regex(text)
            print(json.dumps(reg_res, ensure_ascii=False, indent=2))
            return reg_res
        except Exception as ex2:
            print("Lỗi trích xuất:", ex2)
            return None


def main():
    parser = argparse.ArgumentParser(
        description="RapidOCR CLI - Nhận diện ký tự quang học cho hệ thống Nông Ký",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("image_path", nargs="?", help="Đường dẫn đến file ảnh cần nhận diện")
    parser.add_argument("--vis", "-v", metavar="OUTPUT_PATH", help="Vẽ bounding box và lưu ảnh trực quan ra file")
    parser.add_argument("--json", "-j", action="store_true", help="In toàn bộ kết quả cấu trúc dưới dạng JSON")
    parser.add_argument("--min-score", "-s", type=float, default=0.5, help="Ngưỡng lọc độ tin cậy (mặc định 0.5)")
    parser.add_argument("--match", "-m", action="store_true", help="Dùng LLM 0.6B lấy tên thuốc/phân bón và fuzzy match với CSDL")
    parser.add_argument("--extract", "-e", action="store_true", help="Trích xuất tự động hoạt động canh tác VietGAP từ văn bản OCR")
    parser.add_argument("--benchmark", "-b", action="store_true", help="Chạy bài benchmark đo lường hiệu năng của engine")

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

    try:
        t0 = time.time()
        result: OCRResult = ocr_engine.ocr_sync(args.image_path, min_score=args.min_score)
        total_time = time.time() - t0

        if args.vis:
            out_path = result.save_visualization(args.image_path, args.vis)
            print(f"✅ Đã lưu ảnh visualization tại: {out_path}")

        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        else:
            print("-" * 60)
            print(f"📄 KẾT QUẢ NHẬN DIỆN OCR ({result.total_lines} dòng, thời gian: {total_time * 1000:.1f}ms, độ tin cậy TB: {result.avg_score:.2%}):")
            print("-" * 60)
            if result.text:
                for idx, line in enumerate(result.lines, 1):
                    print(f"[{idx:02d}] ({line.score:.2%}) {line.text}")
            else:
                print("(Không phát hiện được văn bản trong ảnh với ngưỡng score >= %.2f)" % args.min_score)
            print("-" * 60)

        if args.match and result.text:
            print("\n" + "=" * 60)
            print("🔍 ĐỐI SOÁT VẬT TƯ NÔNG NGHIỆP VỚI CSDL (LLM 0.6B + FUZZY MATCH)")
            print("=" * 60)
            from services.vietgap.material_matcher import material_matcher
            match_list = material_matcher.extract_and_match_sync(result.text)
            if not match_list:
                print("Không tìm thấy tên thuốc/phân bón nào trong văn bản nhận diện.")
            else:
                for idx, m in enumerate(match_list, 1):
                    cat_vn = "Thuốc BVTV" if m.category == "pesticide" else ("Phân bón" if m.category == "fertilizer" else "Chưa rõ")
                    status_icon = "✅" if m.matched else "⚠️"
                    print(f"[{idx:02d}] {status_icon} Tên bóc tách: '{m.raw_name}'")
                    if m.matched:
                        print(f"     ➜ Khớp CSDL: [{cat_vn}] ID {m.item_id}: '{m.canonical_name}' (Độ khớp: {m.score:.1f}%)")
                        if m.category == "pesticide":
                            print(f"     ➜ Hoạt chất: {m.active_ingredient or 'N/A'} | Cách ly: {m.min_isolation_days or 0} ngày")
                        elif m.category == "fertilizer":
                            print(f"     ➜ Loại: {m.fertilizer_type or 'N/A'} | NPK/Thành phần: {m.npk_ratio or 'N/A'}")
                        if m.manufacturer:
                            print(f"     ➜ Nhà sản xuất: {m.manufacturer}")
                    else:
                        print(f"     ➜ Không tìm thấy sản phẩm tương ứng trong CSDL (Gần nhất: '{m.canonical_name}', {m.score:.1f}%)")
            print("=" * 60)

        if args.extract and result.text:
            extract_vietgap_data(result.text)

    except OCRError as e:
        print(f"❌ Lỗi OCR: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        logger.exception("Lỗi không mong muốn:")
        sys.exit(3)


if __name__ == "__main__":
    main()
