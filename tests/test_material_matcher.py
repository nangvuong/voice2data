"""
Unit tests cho MaterialMatcher (LLM 0.6B extraction & Fuzzy Matching với CSDL).
"""

import asyncio
import unittest

from services.vietgap.material_matcher import (
    MaterialMatchResult,
    clean_material_name,
    material_matcher,
)


class TestMaterialMatcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        material_matcher.reload_catalog(force=True)

    def test_clean_material_name(self):
        """Kiểm tra hàm làm sạch tiền tố và từ ngữ gây nhiễu."""
        self.assertEqual(clean_material_name("xịt thuốc Ridomil Gold"), "Ridomil Gold")
        self.assertEqual(clean_material_name("bón 5 bao phân chuồng"), "phân chuồng")
        self.assertEqual(clean_material_name("phun phân bón lá Canxi Bo"), "Canxi Bo")
        self.assertEqual(clean_material_name("chai TOBA NET 500ml"), "TOBA NET")

    def test_fuzzy_match_manure(self):
        """Kiểm tra fuzzy match với Phân chuồng trong CSDL."""
        res = material_matcher.fuzzy_match("phân chuồng hoai mục")
        self.assertTrue(res.matched)
        self.assertEqual(res.category, "fertilizer")
        self.assertEqual(res.canonical_name, "Phân chuồng")
        self.assertGreaterEqual(res.score, 80.0)
        self.assertIsNotNone(res.rag_knowledge)

        # Kiểm tra biến thể viết tắt
        res2 = material_matcher.fuzzy_match("bón phân chuồng")
        self.assertTrue(res2.matched)
        self.assertEqual(res2.canonical_name, "Phân chuồng")

    def test_fuzzy_match_pesticides(self):
        """Kiểm tra fuzzy match với các loại thuốc BVTV."""
        # 1. Ridomil Gold
        r1 = material_matcher.fuzzy_match("thuốc Ridomil Gold")
        self.assertTrue(r1.matched)
        self.assertEqual(r1.category, "pesticide")
        self.assertEqual(r1.canonical_name, "Ridomil Gold 68WG")
        self.assertEqual(r1.min_isolation_days, 14)

        # 2. Anvil
        r2 = material_matcher.fuzzy_match("xịt Anvil 5SC")
        self.assertTrue(r2.matched)
        self.assertEqual(r2.canonical_name, "Anvil 5SC")
        self.assertEqual(r2.min_isolation_days, 7)

        # 3. Spendora
        r3 = material_matcher.fuzzy_match("Spendora")
        self.assertTrue(r3.matched)
        self.assertEqual(r3.canonical_name, "Spendora 690WP")

        # 4. Dầu khoáng Ds
        r4 = material_matcher.fuzzy_match("Dầu khoáng Ds")
        self.assertTrue(r4.matched)
        self.assertEqual(r4.canonical_name, "Dầu khoáng Ds 98.8EC")
        self.assertEqual(r4.min_isolation_days, 2)

    def test_fuzzy_match_fertilizers(self):
        """Kiểm tra fuzzy match với các loại phân bón."""
        # 1. Canxi Bo Silic
        f1 = material_matcher.fuzzy_match("Canxi Bo Silic")
        self.assertTrue(f1.matched)
        self.assertEqual(f1.category, "fertilizer")
        self.assertEqual(f1.canonical_name, "Canxi Bo - Silic (FUNO-PN01)")

        # 2. TOBA NET
        f2 = material_matcher.fuzzy_match("phân bón TOBA NET")
        self.assertTrue(f2.matched)
        self.assertEqual(f2.canonical_name, "TOBA NET")

        # 3. Siêu ra rễ
        f3 = material_matcher.fuzzy_match("siêu ra rễ đẻ nhánh")
        self.assertTrue(f3.matched)
        self.assertEqual(f3.canonical_name, "Siêu ra rễ - đẻ nhánh")

    def test_llm_06b_extraction(self):
        """Kiểm tra gọi LLM 0.6B trích xuất tên vật tư từ câu thực tế."""
        async def _run():
            text = "Sáng nay tôi ra vườn bón 10kg phân chuồng và xịt thuốc Ridomil Gold cho cây bưởi"
            materials = await material_matcher.extract_materials_with_llm(text)
            return materials

        extracted = asyncio.run(_run())
        self.assertIsInstance(extracted, list)
        self.assertGreater(len(extracted), 0)
        # Kiểm tra có phát hiện ít nhất 1 vật tư (phân chuồng hoặc Ridomil)
        joined = " ".join(extracted).lower()
        self.assertTrue("chuồng" in joined or "ridomil" in joined)

    def test_extract_and_match_pipeline(self):
        """Kiểm tra toàn bộ pipeline: LLM 0.6B trích xuất -> Fuzzy match CSDL."""
        async def _run():
            text = "Đợt này xuất hiện nấm bệnh thán thư nên tôi dùng Spendora 690WP kết hợp bón phân lá TOBA NET"
            matches = await material_matcher.extract_and_match(text)
            return matches

        results = asyncio.run(_run())
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)

        names = [r.canonical_name for r in results if r.matched]
        self.assertTrue(any("Spendora" in n for n in names) or any("TOBA" in n for n in names))


if __name__ == "__main__":
    unittest.main()
