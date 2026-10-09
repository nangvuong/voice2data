import sys
import time
from pathlib import Path
import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, "/root/voice_to_data")
sys.path.insert(0, "/root/voice_to_data/models/vietocr_onnx")
from rapidocr_onnxruntime import RapidOCR
from vietocr_onnx import VietOCROnnxEngine

rapid = RapidOCR()
vietocr = VietOCROnnxEngine(onnx_dir="/root/voice_to_data/models/vietocr_onnx")

def get_perspective_crop(image, points):
    pts = np.float32(points)
    crop_width = int(max(np.linalg.norm(pts[0] - pts[1]), np.linalg.norm(pts[2] - pts[3])))
    crop_height = int(max(np.linalg.norm(pts[0] - pts[3]), np.linalg.norm(pts[1] - pts[2])))
    if crop_width <= 4 or crop_height <= 4:
        return None
    pts_std = np.float32([[0, 0], [crop_width, 0], [crop_width, crop_height], [0, crop_height]])
    matrix = cv2.getPerspectiveTransform(pts, pts_std)
    crop = cv2.warpPerspective(image, matrix, (crop_width, crop_height), borderMode=cv2.BORDER_REPLICATE)
    h, w = crop.shape[:2]
    if h * 1.0 / w >= 1.5:
        crop = np.rot90(crop, k=3)
    return crop

def test_top_largest_regions(img_path, top_k=3):
    print("=" * 70)
    print(f"Ảnh kiểm thử: {img_path}")
    img_cv2 = cv2.imread(img_path)
    if img_cv2 is None:
        print("Không đọc được ảnh:", img_path)
        return

    h_img, w_img = img_cv2.shape[:2]
    print(f"Kích thước ảnh gốc: {w_img}x{h_img} px")

    t0 = time.time()
    # 1. Phát hiện toàn bộ vùng chữ bằng RapidOCR Text Detection
    rapid_results, elapse = rapid(img_cv2)
    t_rapid_total = time.time() - t0

    if not rapid_results:
        print("Không phát hiện được văn bản trong ảnh!")
        return

    print(f"Tổng số vùng chữ phát hiện: {len(rapid_results)} boxes (Thời gian Det+Cls: {elapse[0]+elapse[1]:.3f}s)")

    # 2. Tính kích thước và lọc lấy các vùng lớn nhất (Title, Tên thuốc)
    # Các vùng lớn nhất tương ứng với text tiêu đề hoặc tên thương mại của chai thuốc
    regions = []
    for r in rapid_results:
        box = r[0]
        rapid_text = r[1]
        rapid_score = r[2]
        pts = np.array(box, dtype=np.float32)
        crop_width = int(max(np.linalg.norm(pts[0] - pts[1]), np.linalg.norm(pts[2] - pts[3])))
        crop_height = int(max(np.linalg.norm(pts[0] - pts[3]), np.linalg.norm(pts[1] - pts[2])))
        area = crop_width * crop_height
        center_y = float(np.mean(pts[:, 1]))
        center_x = float(np.mean(pts[:, 0]))

        regions.append({
            "pts": box,
            "rapid_text": rapid_text,
            "rapid_score": rapid_score,
            "width": crop_width,
            "height": crop_height,
            "area": area,
            "center_y": center_y,
            "center_x": center_x
        })

    # Sắp xếp theo diện tích / chiều cao giảm dần
    regions_by_size = sorted(regions, key=lambda x: x["area"], reverse=True)

    # Chọn TOP K vùng lớn nhất
    top_regions = regions_by_size[:top_k]

    # Sắp xếp lại TOP K theo vị trí trục Y (từ trên xuống dưới) để người đọc dễ theo dõi
    top_regions = sorted(top_regions, key=lambda x: x["center_y"])

    # 3. Dùng VietOCR nhận diện chuyên biệt cho TOP K vùng lớn nhất này
    t_viet_start = time.time()
    print(f"\n--- KẾT QUẢ SO SÁNH TRÊN TOP {len(top_regions)} VÙNG CHỮ LỚN NHẤT ---")
    for i, reg in enumerate(top_regions, 1):
        crop = get_perspective_crop(img_cv2, reg["pts"])
        if crop is None:
            continue
        crop_pil = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        
        t_crop0 = time.time()
        viet_text, viet_prob = vietocr.predict(crop_pil)
        t_crop = time.time() - t_crop0

        w, h, a = reg["width"], reg["height"], reg["area"]
        print(f"[{i}] Kích thước: {w}x{h} px (Diện tích: {a} px^2)")
        print(f"    - RapidOCR (Gốc)     : \"{reg['rapid_text']}\" (conf: {reg['rapid_score']:.2f})")
        print(f"    - VietOCR (Có dấu)   : \"{viet_text}\" (conf: {viet_prob:.2f}) [Thời gian: {t_crop:.3f}s]")

    t_viet_total = time.time() - t_viet_start
    t_pipeline = elapse[0] + elapse[1] + t_viet_total
    print(f"\n=> TỔNG THỜI GIAN HYBRID (Det + VietOCR TOP {len(top_regions)}): {t_pipeline:.3f}s")
    print(f"   (RapidOCR Det+Cls: {elapse[0]+elapse[1]:.3f}s, VietOCR {len(top_regions)} boxes: {t_viet_total:.3f}s)")

if __name__ == "__main__":
    for photo in ["/mnt/data/photos/photo_1662_0.jpg", "/mnt/data/photos/photo_1661_0.jpg", "/mnt/data/photos/photo_1660_0.jpg"]:
        if Path(photo).exists():
            test_top_largest_regions(photo, top_k=3)
