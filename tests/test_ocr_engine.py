"""
Unit tests cho OCREngine (RapidOCR) trong services/ocr_engine.py.
"""

import asyncio
import io
import os
import shutil
import tempfile
import unittest

import numpy as np
from PIL import Image, ImageDraw

from services.engines.ocr_engine import (
    OCREngine,
    OCRError,
    OCRLine,
    OCRResult,
    ocr_engine,
)


class TestOCREngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp(prefix="test_ocr_")
        cls.sample_img_path = os.path.join(cls.test_dir, "sample_invoice.jpg")

        # Tạo một ảnh mẫu có nội dung chữ
        img = Image.new("RGB", (650, 220), color=(255, 255, 255))
        d = ImageDraw.Draw(img)
        d.text((25, 20), "HOA DON BAN HANG", fill=(0, 0, 0))
        d.text((25, 60), "Phan bon NPK Dau Trau", fill=(0, 0, 0))
        d.text((25, 100), "So luong: 250 kg", fill=(0, 0, 0))
        d.text((25, 140), "Ngay: 25/09/2026", fill=(0, 0, 0))
        img.save(cls.sample_img_path)

        with open(cls.sample_img_path, "rb") as f:
            cls.sample_bytes = f.read()

        cls.sample_pil = img
        cls.sample_np = np.array(img)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def test_singleton_instance(self):
        """Kiểm tra singleton ocr_engine được khởi tạo đúng cách."""
        self.assertIsInstance(ocr_engine, OCREngine)
        self.assertIsNotNone(ocr_engine._engine)

    def test_ocr_sync_filepath(self):
        """Kiểm tra ocr_sync với đường dẫn file."""
        res = ocr_engine.ocr_sync(self.sample_img_path)
        self.assertIsInstance(res, OCRResult)
        self.assertGreater(res.total_lines, 0)
        self.assertIn("HOA DON", res.text.upper())
        self.assertGreater(res.avg_score, 0.5)
        self.assertGreater(res.total_elapse, 0)

    def test_ocr_sync_bytes(self):
        """Kiểm tra ocr_sync với dữ liệu nhị phân bytes."""
        res = ocr_engine.ocr_sync(self.sample_bytes)
        self.assertIsInstance(res, OCRResult)
        self.assertGreater(res.total_lines, 0)
        self.assertIn("DAU TRAU", res.text.upper())

    def test_ocr_sync_pil_image(self):
        """Kiểm tra ocr_sync với đối tượng PIL Image."""
        res = ocr_engine.ocr_sync(self.sample_pil)
        self.assertIsInstance(res, OCRResult)
        self.assertGreater(res.total_lines, 0)

    def test_ocr_sync_numpy_array(self):
        """Kiểm tra ocr_sync với mảng numpy ndarray."""
        res = ocr_engine.ocr_sync(self.sample_np)
        self.assertIsInstance(res, OCRResult)
        self.assertGreater(res.total_lines, 0)

    def test_to_dict_and_lines(self):
        """Kiểm tra chuyển đổi kết quả sang dictionary."""
        res = ocr_engine.ocr_sync(self.sample_img_path)
        data = res.to_dict()

        self.assertIn("text", data)
        self.assertIn("lines", data)
        self.assertIn("total_lines", data)
        self.assertIn("avg_score", data)
        self.assertIn("image_size", data)
        self.assertIn("timing", data)

        if res.lines:
            line = res.lines[0]
            self.assertIsInstance(line, OCRLine)
            self.assertEqual(len(line.box), 4)
            xmin, ymin, xmax, ymax = line.rect
            self.assertLessEqual(xmin, xmax)
            self.assertLessEqual(ymin, ymax)

    def test_filter_by_score(self):
        """Kiểm tra lọc các dòng theo độ tin cậy."""
        res = ocr_engine.ocr_sync(self.sample_img_path)
        filtered = res.filter_by_score(0.999)
        self.assertLessEqual(filtered.total_lines, res.total_lines)

    def test_visualization(self):
        """Kiểm tra hàm vẽ visualization và lưu file kết quả."""
        res = ocr_engine.ocr_sync(self.sample_img_path)
        out_vis_path = os.path.join(self.test_dir, "vis_result.jpg")

        saved_path = res.save_visualization(self.sample_img_path, out_vis_path)
        self.assertTrue(os.path.exists(saved_path))
        self.assertGreater(os.path.getsize(saved_path), 0)

        vis_arr = res.draw_visualization(self.sample_bytes)
        self.assertIsInstance(vis_arr, np.ndarray)
        self.assertEqual(vis_arr.shape[:2], (220, 650))

    def test_async_ocr(self):
        """Kiểm tra hàm async ocr() qua hàng đợi bất đồng bộ."""
        async def _run():
            res = await ocr_engine.ocr(self.sample_bytes)
            return res

        res = asyncio.run(_run())
        self.assertIsInstance(res, OCRResult)
        self.assertGreater(res.total_lines, 0)

    def test_invalid_input(self):
        """Kiểm tra báo lỗi khi truyền input không hợp lệ."""
        with self.assertRaises(FileNotFoundError):
            ocr_engine.ocr_sync("/duong/dan/khong/ton/tai_12345.jpg")

        with self.assertRaises(OCRError):
            ocr_engine.ocr_sync(b"invalid corrupt bytes data")


if __name__ == "__main__":
    unittest.main()
