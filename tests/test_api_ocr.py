"""
Unit tests cho OCR API endpoints trong app/app.py (/api/ocr và /api/upload-photo).
"""

import io
import json
import os
import unittest

from PIL import Image, ImageDraw

from app.app import app


class TestOCRAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()

        # Tạo file ảnh test
        img = Image.new("RGB", (600, 200), color=(255, 255, 255))
        d = ImageDraw.Draw(img)
        d.text((20, 30), "HOA DON BAN LE", fill=(0, 0, 0))
        d.text((20, 70), "Phan bon Kali Clorua: 100 kg", fill=(0, 0, 0))
        d.text((20, 110), "Gia tien: 1.200.000 d", fill=(0, 0, 0))

        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        cls.test_image_bytes = buf.getvalue()

    def test_api_ocr_file_upload(self):
        """Test endpoint /api/ocr khi upload file multipart."""
        data = {
            "photo": (io.BytesIO(self.test_image_bytes), "invoice.jpg", "image/jpeg"),
        }
        res = self.client.post("/api/ocr", data=data, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200)

        json_data = res.get_json()
        self.assertEqual(json_data["status"], "success")
        self.assertIn("text", json_data)
        self.assertGreater(json_data["total_lines"], 0)
        self.assertIn("photo_url", json_data)
        self.assertIn("lines", json_data)
        self.assertIn("timing", json_data)

    def test_api_ocr_visualization(self):
        """Test endpoint /api/ocr với cờ vis=true."""
        data = {
            "photo": (io.BytesIO(self.test_image_bytes), "invoice.jpg", "image/jpeg"),
        }
        res = self.client.post("/api/ocr?vis=true", data=data, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200)

        json_data = res.get_json()
        self.assertEqual(json_data["status"], "success")
        self.assertIsNotNone(json_data.get("vis_url"))

    def test_api_upload_photo_with_ocr_flag(self):
        """Test endpoint /api/upload-photo kèm cờ ocr=true."""
        data = {
            "photo": (io.BytesIO(self.test_image_bytes), "invoice.jpg", "image/jpeg"),
        }
        res = self.client.post("/api/upload-photo?ocr=true", data=data, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200)

        json_data = res.get_json()
        self.assertEqual(json_data["status"], "success")
        self.assertIn("ocr", json_data)
        self.assertIn("text", json_data["ocr"])
        self.assertGreater(json_data["ocr"]["total_lines"], 0)

    def test_api_ocr_missing_file(self):
        """Test endpoint /api/ocr khi không có ảnh."""
        res = self.client.post("/api/ocr", json={})
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
