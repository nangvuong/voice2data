"""
Unit tests cho VLM Engine (Gemini 3.1 Flash Lite - services/vlm_engine.py).
"""

import os
import unittest
from PIL import Image

from services.engines.vlm_engine import (
    VLMEngine,
    VLMAnalysisResult,
    PestDiseaseDiagnosis,
    PesticideDetection,
    FertilizerDetection,
    prepare_image_base64,
    find_farm_pesticides_for_pest,
    vlm_engine,
)


class TestVLMEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_img_path = "tmp/test_vlm_sample.jpg"
        os.makedirs("tmp", exist_ok=True)
        # Tạo ảnh thử nghiệm 2000x2000 để kiểm tra logic auto-resize
        img = Image.new("RGB", (2000, 2000), color=(100, 150, 200))
        img.save(cls.test_img_path, format="JPEG")

    def test_prepare_image_base64(self):
        """Kiểm tra xử lý, resize và mã hóa base64 hình ảnh."""
        b64, mime = prepare_image_base64(self.test_img_path, max_dimension=800)
        self.assertEqual(mime, "image/jpeg")
        self.assertIsInstance(b64, str)
        self.assertGreater(len(b64), 100)

    def test_find_farm_pesticides_for_pest(self):
        """Kiểm tra chức năng tra cứu thuốc BVTV trong CSDL cho sâu bệnh hại."""
        # 1. Thán thư
        res_tt = find_farm_pesticides_for_pest("Bệnh thán thư")
        self.assertGreater(len(res_tt), 0)
        names_tt = [r["name"] for r in res_tt]
        self.assertTrue(any("Ridomil" in n or "Spendora" in n for n in names_tt))

        # 2. Nhện đỏ
        res_nd = find_farm_pesticides_for_pest("Nhện đỏ hại lá")
        self.assertGreater(len(res_nd), 0)
        names_nd = [r["name"] for r in res_nd]
        self.assertTrue(any("Newone" in n or "Dầu khoáng" in n or "Movento" in n or "Pesieu" in n for n in names_nd))

        # 3. Ruồi vàng
        res_rv = find_farm_pesticides_for_pest("Ruồi vàng đục quả")
        self.assertGreater(len(res_rv), 0)
        names_rv = [r["name"] for r in res_rv]
        self.assertTrue(any("ruồi vàng" in n.lower() or "Tumazine" in n for n in names_rv))

    def test_data_classes_serialization(self):
        """Kiểm tra serialize kết quả sang dict."""
        diag = PestDiseaseDiagnosis(
            pest_name="Bệnh sương mai",
            scientific_name="Phytophthora infestans",
            pest_type="Nấm bệnh",
            crop_name="Cà chua",
            severity="Trung bình",
            symptoms="Vết đốm thâm đen góc lá",
            recommended_actives=["Metalaxyl", "Mancozeb"],
        )
        d_dict = diag.to_dict()
        self.assertEqual(d_dict["pest_name"], "Bệnh sương mai")
        self.assertEqual(d_dict["scientific_name"], "Phytophthora infestans")
        self.assertEqual(len(d_dict["recommended_actives"]), 2)

        res = VLMAnalysisResult(
            status="success",
            category="pest_disease",
            title="Chẩn đoán bệnh sương mai",
            pest_disease=diag,
            execution_time_ms=1250.5,
            model_used="gemini-3.1-flash-lite",
        )
        r_dict = res.to_dict()
        self.assertEqual(r_dict["status"], "success")
        self.assertEqual(r_dict["category"], "pest_disease")
        self.assertIsNotNone(r_dict["pest_disease"])

    def test_live_vlm_analysis(self):
        """Kiểm tra gọi trực tiếp Gemini 3.1 Flash Lite trên ảnh mẫu phân bón."""
        sample_path = "/mnt/data/photos/photo_1682_0.jpg"
        if not os.path.exists(sample_path):
            self.skipTest(f"Không có ảnh mẫu {sample_path}")

        res = vlm_engine.analyze_image_sync(sample_path)
        self.assertEqual(res.status, "success")
        self.assertEqual(res.category, "fertilizer")
        self.assertIsNotNone(res.fertilizer)
        self.assertIn("SIAMB", res.fertilizer.trade_name)
        # Kiểm tra đối soát CSDL
        self.assertIsNotNone(res.fertilizer.db_match)
        self.assertTrue(res.fertilizer.db_match.get("matched"))
    def test_diary_payload_mapping(self):
        """Kiểm tra mapping chính xác sang 4 hoạt động nhật ký VietGAP: purchase, spray, fertilize, observation."""
        # 1. Fertilizer -> fertilize
        fert = FertilizerDetection(
            trade_name="Phân bón NPK 20-20-15",
            fertilizer_type="Phân vô cơ",
            composition_npk="20-20-15",
            dosage_and_usage="Bón 50kg/ha",
            db_match={"matched": True, "id": 1, "name": "NPK 20-20-15 Đầu Trâu"},
        )
        res_f = VLMAnalysisResult(
            status="success",
            category="fertilizer",
            title="Nhận diện NPK 20-20-15",
            fertilizer=fert,
        )
        payload_f = res_f.to_diary_payload(intent="fertilize", crop_name="Sầu riêng Ri6")
        self.assertIsNotNone(payload_f)
        self.assertEqual(payload_f["task_types"], ["fertilize"])
        self.assertIn("fertilize", payload_f["tables"])
        self.assertEqual(payload_f["tables"]["fertilize"]["fertilizer_id"], 1)
        self.assertEqual(payload_f["tables"]["fertilize"]["crop_name"], "Sầu riêng Ri6")

        # 2. Pesticide -> spray
        pest = PesticideDetection(
            trade_name="Anvil 5SC",
            active_ingredient="Hexaconazole 50g/l",
            target_pests=["Nấm hồng", "Rỉ sắt"],
            dosage_and_usage="1ml / lít nước",
            isolation_days=14,
            db_match={"matched": True, "id": 5, "name": "Anvil 5SC"},
        )
        res_p = VLMAnalysisResult(
            status="success",
            category="pesticide",
            title="Nhận diện Anvil 5SC",
            pesticide=pest,
        )
        payload_s = res_p.to_diary_payload(intent="spray")
        self.assertIsNotNone(payload_s)
        self.assertEqual(payload_s["task_types"], ["spray"])
        self.assertEqual(payload_s["tables"]["spray"]["pesticide_id"], 5)
        self.assertEqual(payload_s["tables"]["spray"]["isolation_days"], 14)

        # 3. Pesticide / Fertilizer -> purchase
        payload_buy = res_p.to_diary_payload(intent="purchase")
        self.assertIsNotNone(payload_buy)
        self.assertEqual(payload_buy["task_types"], ["purchase"])
        self.assertEqual(payload_buy["tables"]["purchase"]["pesticide_id"], 5)
        self.assertEqual(payload_buy["tables"]["purchase"]["material_type"], "Thuốc BVTV")

        # 4. Pest Disease -> observation
        diag = PestDiseaseDiagnosis(
            pest_name="Rầy xanh gây hại",
            pest_type="Sâu hại",
            crop_name="Sầu riêng",
            severity="Trung bình",
            affected_part="Chồi non",
        )
        res_o = VLMAnalysisResult(
            status="success",
            category="pest_disease",
            title="Chẩn đoán rầy xanh",
            pest_disease=diag,
        )
        payload_obs = res_o.to_diary_payload(intent="observation")
        self.assertIsNotNone(payload_obs)
        self.assertEqual(payload_obs["task_types"], ["observation"])
        self.assertEqual(payload_obs["tables"]["observation"]["pest_name"], "Rầy xanh gây hại")
        self.assertEqual(payload_obs["tables"]["observation"]["severity"], "Trung bình")

    def test_save_to_database(self):
        """Kiểm tra lưu hoạt động VLM vào CSDL MySQL vietgap_diary."""
        pest = PesticideDetection(
            trade_name="BP. Newone 500SC",
            active_ingredient="Chlorfenapyr 500g/l",
            target_pests=["Nhện đỏ", "Sâu đục trái"],
            isolation_days=7,
            db_match={"matched": True, "id": 2, "name": "BP. Newone 500SC"},
        )
        res = VLMAnalysisResult(
            status="success",
            category="pesticide",
            title="BP. Newone 500SC",
            pesticide=pest,
        )
        save_res = res.save_to_database(
            farmer_id=1,
            intent="spray",
            photo_url="/static/photos/test_save.jpg",
            crop_name="Bưởi da xanh",
        )
        self.assertEqual(save_res.get("status"), "success")
        self.assertTrue("cultivation_activities" in save_res.get("created_records", {}))
        self.assertTrue("pesticide_applications" in save_res.get("created_records", {}))


if __name__ == "__main__":
    unittest.main()
