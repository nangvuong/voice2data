"""
Google AI Gemini Engine (services/gemini_engine.py)
--------------------------------------------------
Dịch vụ trích xuất thông tin nhật ký nông nghiệp sử dụng Google AI Gemini API.
Hỗ trợ các model như gemini-3.5-flash-lite, gemini-2.5-flash-lite, gemini-2.0-flash, gemini-1.5-flash,...
Đặc điểm:
- Giao tiếp bất đồng bộ qua REST API của Google Generative Language.
- Sử dụng cơ chế Structured Outputs (JSON Schema bắt buộc 8 trường chuẩn).
- Tích hợp chuẩn hóa dữ liệu với normalizer pipeline.
"""

import asyncio
import json
import logging
import re
import ssl
import time
from datetime import datetime, date, timedelta
from typing import Any, Dict, Optional, Tuple

import aiohttp

import config
from services.normalizer import (
    normalize_activity_list,
    classify_into_tables,
    normalize_vietgap_tables,
    tables_to_flat_dict,
    to_iso_date,
    resolve_spoken_date,
    strip_expiry_phrase,
    extract_spoken_expiry_date,
)

logger = logging.getLogger(__name__)

# SSL context
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()


class GeminiEngineError(Exception):
    """Lỗi phát sinh trong quá trình gọi Google AI Gemini API."""


class GeminiEngine:
    # 7 Hoạt động chính trên Dòng thời gian (Timeline Activities) theo từng loại công việc
    TIMELINE_ACTIVITIES = [
        "spray",       # Phun thuốc BVTV
        "fertilize",   # Bón phân
        "purchase",    # Mua vật tư / cây giống / phân bón / thuốc BVTV
        "harvest",     # Thu hoạch
        "sale",        # Tiêu thụ / Bán nông sản
        "waste",       # Xử lý bao bì / rác BVTV
        "observation", # Quan sát / Sâu bệnh
        "general",     # Hoạt động chung
    ]

    # Schema có cấu trúc phân tầng trực tiếp theo các hoạt động Timeline
    EXTRACT_SCHEMA = {
        "type": "OBJECT",
        "properties": {
            "active_activities": {
                "type": "ARRAY",
                "items": {
                    "type": "STRING",
                    "enum": ["spray", "fertilize", "purchase", "harvest", "sale", "waste", "observation", "general"]
                }
            },
            "growth_stage": {
                "type": "STRING",
                "nullable": True
            },
            "spray": {
                "type": "OBJECT",
                "nullable": True,
                "properties": {
                    "activity_date": {"type": "STRING", "nullable": True},
                    "ten_thuoc_bvtv": {"type": "STRING", "nullable": True},
                    "target_pest": {"type": "STRING", "nullable": True},
                    "nong_do_va_luong_dung": {"type": "STRING", "nullable": True},
                    "application_method": {"type": "STRING", "nullable": True},
                    "thoi_gian_cach_ly": {"type": "STRING", "nullable": True},
                    "growth_stage": {"type": "STRING", "nullable": True}
                },
                "required": ["activity_date", "ten_thuoc_bvtv", "target_pest", "nong_do_va_luong_dung", "application_method", "thoi_gian_cach_ly", "growth_stage"]
            },
            "fertilize": {
                "type": "OBJECT",
                "nullable": True,
                "properties": {
                    "activity_date": {"type": "STRING", "nullable": True},
                    "ten_phan_bon": {"type": "STRING", "nullable": True},
                    "luong_phan_bon": {"type": "STRING", "nullable": True},
                    "phuong_phap_bon": {"type": "STRING", "nullable": True},
                    "growth_stage": {"type": "STRING", "nullable": True}
                },
                "required": ["activity_date", "ten_phan_bon", "luong_phan_bon", "phuong_phap_bon", "growth_stage"]
            },
            "purchase": {
                "type": "OBJECT",
                "nullable": True,
                "properties": {
                    "purchase_date": {"type": "STRING", "nullable": True},
                    "material_type": {"type": "STRING", "nullable": True},
                    "material_name": {"type": "STRING", "nullable": True},
                    "quantity": {"type": "STRING", "nullable": True},
                    "unit": {"type": "STRING", "nullable": True},
                    "supplier_name": {"type": "STRING", "nullable": True},
                    "expiry_date": {"type": "STRING", "nullable": True}
                },
                "required": ["purchase_date", "material_type", "material_name", "quantity", "unit", "supplier_name", "expiry_date"]
            },
            "harvest": {
                "type": "OBJECT",
                "nullable": True,
                "properties": {
                    "harvest_date": {"type": "STRING", "nullable": True},
                    "crop_name": {"type": "STRING", "nullable": True},
                    "san_luong_thu_hoach": {"type": "STRING", "nullable": True},
                    "dia_diem_cach_so_che": {"type": "STRING", "nullable": True}
                },
                "required": ["harvest_date", "crop_name", "san_luong_thu_hoach", "dia_diem_cach_so_che"]
            },
            "sale": {
                "type": "OBJECT",
                "nullable": True,
                "properties": {
                    "sale_date": {"type": "STRING", "nullable": True},
                    "crop_name": {"type": "STRING", "nullable": True},
                    "khoi_luong_ban": {"type": "STRING", "nullable": True},
                    "khoi_luong_tieu_thu": {"type": "STRING", "nullable": True},
                    "co_so_thu_mua": {"type": "STRING", "nullable": True}
                },
                "required": ["sale_date", "crop_name", "khoi_luong_ban", "co_so_thu_mua"]
            },
            "waste": {
                "type": "OBJECT",
                "nullable": True,
                "properties": {
                    "treatment_date": {"type": "STRING", "nullable": True},
                    "material_name": {"type": "STRING", "nullable": True},
                    "waste_type": {"type": "STRING", "nullable": True},
                    "storage_location": {"type": "STRING", "nullable": True},
                    "treatment_method": {"type": "STRING", "nullable": True}
                },
                "required": ["treatment_date", "material_name", "waste_type", "storage_location", "treatment_method"]
            },
            "observation": {
                "type": "OBJECT",
                "nullable": True,
                "properties": {
                    "activity_date": {"type": "STRING", "nullable": True},
                    "sinh_vat_gay_hai": {"type": "STRING", "nullable": True},
                    "muc_do": {"type": "STRING", "nullable": True},
                    "growth_stage": {"type": "STRING", "nullable": True},
                    "action_taken": {"type": "STRING", "nullable": True},
                },
                "required": ["activity_date", "sinh_vat_gay_hai", "muc_do", "growth_stage", "action_taken"]
            },
            "general": {
                "type": "OBJECT",
                "nullable": True,
                "properties": {
                    "activity_date": {"type": "STRING", "nullable": True},
                    "cong_viec": {"type": "STRING", "nullable": True},
                    "so_luong": {"type": "STRING", "nullable": True},
                    "growth_stage": {"type": "STRING", "nullable": True},
                    "notes": {"type": "STRING", "nullable": True},
                },
                "required": ["activity_date", "cong_viec", "growth_stage", "notes"]
            }
        },
        "required": ["active_activities", "growth_stage", "spray", "fertilize", "purchase", "harvest", "sale", "waste", "observation", "general"],
    }

    def __init__(self):
        self.api_key = config.GEMINI_API_KEY
        self.default_model = getattr(config, "GEMINI_MODEL", "gemini-3.5-flash-lite")
        self.timeout_sec = getattr(config, "GEMINI_TIMEOUT_SEC", 60)
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        """Khởi tạo aiohttp session."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT)
            self._session = aiohttp.ClientSession(connector=connector)
            logger.info("GeminiEngine đã khởi động HTTP session.")

    async def stop(self):
        """Đóng session khi ứng dụng tắt."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            logger.info("GeminiEngine đã dừng HTTP session.")

    def _build_multi_task_prompt(self) -> str:
        """System prompt bóc tách tổng hợp các HOẠT ĐỘNG TRÊN DÒNG THỜI GIAN (TIMELINE) khi người dùng chọn Tự động."""
        return """
Bạn là AI trích xuất thực thể văn bản (Text Entity Extractor) từ giọng nói nông dân cho các HOẠT ĐỘNG CANH TÁC trên DÒNG THỜI GIAN (TIMELINE) của ứng dụng Nông Ký theo chuẩn VietGAP.
Nhiệm vụ: Tách ĐÚNG NGUYÊN VĂN các chuỗi văn bản (raw text) có trong câu nói vào từng trường tương ứng của JSON object.
LƯU Ý QUAN TRỌNG:
- LLM CHỈ TÁCH TEXT NGUYÊN VĂN từ câu nói, TUYỆT ĐỐI KHÔNG cần chuẩn hoá thành dữ liệu chuẩn database (không đổi ngày sang YYYY-MM-DD, không đổi chữ thành số, không quy đổi đơn vị). Bộ Normalizer phía sau sẽ tự động chuyển text sang data, number, date.
- Trường nào câu nói KHÔNG nhắc đến thì để null. Tuyệt đối không tự bịa đặt dữ liệu.

Dòng thời gian (Timeline) theo dõi đúng 7 nhóm hoạt động canh tác theo từng loại công việc:

1. spray (Phun thuốc BVTV):
- activity_date: Luôn để null (KHÔNG trích xuất ngày hoạt động từ câu nói; trường này được khóa theo thời gian ghi nhật ký).
- ten_thuoc_bvtv: Tên thuốc BVTV mang đi phun/xịt (vd: "thuốc trừ sâu Chess 50WG", "Anvil", "Nano Ecotech").
- target_pest: Đối tượng sâu bệnh hại cần phòng trừ như được nói (vd: "rầy nâu", "sâu đục thân", "không có sâu bệnh", "sạch bệnh"... hoặc null nếu không nhắc).
- nong_do_va_luong_dung: Liều lượng, nồng độ pha hoặc số lượng thuốc mang phun kèm đơn vị như được nói (vd: "2 chai pha 400 lít nước", "20ml/bình 25L", "2 lít pha 500 lít nước/ha").
- application_method: Cách phun hoặc thiết bị như được nói (vd: "phun bình máy", "bình đeo vai", "phun ướt đều tán lá").
- thoi_gian_cach_ly: Thời gian cách ly như được nói (vd: "7 ngày", "14 ngày"). Nếu câu nói "không cách ly" hoặc không nhắc đến thì để null.
- growth_stage: Giai đoạn sinh trưởng của cây như được nói, chuẩn hóa về đúng 1 trong 5 giai đoạn: "Sau thu hoạch", "Ra hoa", "Quả non", "Sinh trưởng", "Thu hoạch" (hoặc null nếu không nhắc).

2. fertilize (Bón phân):
- activity_date: Luôn để null (KHÔNG trích xuất ngày hoạt động từ câu nói; trường này được khóa theo thời gian ghi nhật ký).
- ten_phan_bon: Tên loại phân bón như được nói (vd: "phân NPK Đầu Trâu", "phân hữu cơ Úc", "phân chuồng", "phân gà", "Nano Eco").
- luong_phan_bon: Lượng phân bón kèm đơn vị như được nói (vd: "hai lít rưỡi", "2 bao", "50 kg", "1.5 tạ", "5 tạ trên một héc ta").
- phuong_phap_bon: Phương pháp bón như được nói (vd: "rải quanh gốc", "bón lót", "bón thúc", "hòa nước tưới").
- growth_stage: Giai đoạn sinh trưởng của cây như được nói, chuẩn hóa về đúng 1 trong 5 giai đoạn: "Sau thu hoạch", "Ra hoa", "Quả non", "Sinh trưởng", "Thu hoạch" (hoặc null nếu không nhắc).

3. purchase (Mua / Nhập vật tư, Cây giống, Hạt giống, Phân bón, Thuốc BVTV):
- Dành cho hành động MUA, NHẬP, LẤY VỀ vật tư nông nghiệp: phân bón, thuốc BVTV, hóa chất, CÂY GIỐNG, HẠT GIỐNG, CÂY CON, BẦU GIỐNG.
- purchase_date: Luôn để null (KHÔNG trích xuất ngày mua từ câu nói; trường này được khóa theo thời gian ghi nhật ký).
- material_type: Bắt buộc chọn đúng một trong: "Hạt / Cây giống", "Phân bón", "Thuốc BVTV", "Hóa chất", "Khác".
- material_name: Tên vật tư hoặc tên giống cây trồng trong câu nói (vd: "cây giống cà gai leo", "cây giống sầu riêng Ri6", "phân hữu cơ Úc", "thuốc Chess 50WG").
- quantity: Số lượng hoặc khối lượng mua/nhập như được nói (vd: "năm mươi ngàn", "50", "100", "100000", "20", "5 tạ").
- unit: Đơn vị tính nếu được nói (vd: "cây", "gốc", "bầu", "bịch", "gói", "bao", "chai", "kg", "lít", "tạ").
- supplier_name: Nơi bán, cơ sở cung cấp, đại lý, trại giống, vựa giống (vd: "trại giống Ba Tèo", "trại giống Hai Triều", "đại lý Quang Minh"). NẾU CÂU NÓI KHÔNG CÓ THÌ ĐỂ null, TUYỆT ĐỐI KHÔNG TỰ BỊA ĐẶT.
- expiry_date: Hạn sử dụng (hoặc null).

4. harvest (Thu hoạch nông sản):
- harvest_date: Luôn để null (KHÔNG trích xuất ngày thu hoạch từ câu nói; trường này được khóa theo thời gian ghi nhật ký).
- crop_name: Tên nông sản / giống cây trồng thu hoạch như được nói (vd: "sầu riêng Ri6", "cam sành", "cà gai leo").
- san_luong_thu_hoach: Sản lượng hái/cắt được kèm đơn vị như được nói (vd: "hai tấn rưỡi", "2 tấn", "500 kg", "15 tạ").
- dia_diem_cach_so_che: Địa điểm và cách sơ chế như được nói (vd: "tại vườn lau sạch đóng thùng xốp", "chở về xưởng băm sấy khô", "đóng gói").

5. sale (Tiêu thụ / Xuất bán nông sản):
- sale_date: Luôn để null (KHÔNG trích xuất ngày bán từ câu nói; trường này được khóa theo thời gian ghi nhật ký).
- crop_name: Tên nông sản / giống cây trồng xuất bán như được nói (vd: "sầu riêng Ri6", "cam sành", "cà gai leo").
- khoi_luong_tieu_thu: Khối lượng bán kèm đơn vị như được nói (vd: "năm trăm mười hai ki lô gam", "2 tấn", "500 kg", "5361 kg").
- co_so_thu_mua: Tên thương lái hoặc cơ sở thu mua như được nói (vd: "thương lái Ba Tèo", "vựa Út Hạnh", "công ty SanDu").

6. waste (Xử lý bao bì):
- treatment_date: Luôn để null (KHÔNG trích xuất ngày thực hiện từ câu nói; trường này được khóa theo thời gian ghi nhật ký).
- material_name: Tên thuốc hoặc vật tư liên quan (vd: "thuốc Chess 50WG", "phân NPK").
- waste_type: Loại bao bì, vỏ chai, chất thải như được nói (vd: "vỏ chai thuốc BVTV", "bao bì ni lông").
- storage_location: Nơi lưu giữ, hố chứa như được nói (vd: "hố chứa bao bì BVTV có nắp", "bể xi măng").
- treatment_method: Cách thức xử lý như được nói (vd: "súc rửa 3 lần, bàn giao đơn vị thu gom", "đem bỏ hố rác vườn").

7. general (Công việc canh tác khác: dọn cỏ, làm cỏ, tỉa cành, làm đất, tưới nước, bao trái, dặm cây, thăm đồng, ươm hạt, xây dựng...):
- activity_date: Luôn để null (KHÔNG trích xuất ngày hoạt động từ câu nói; trường này được khóa theo thời gian ghi nhật ký).
- cong_viec: Tên công việc cụ thể nông dân đã làm (vd: "Dọn cỏ", "Cày bừa dọn cỏ", "Lắp ống nước béc tưới", "Trồng cây cà gai leo", "Tưới nước giữ ẩm", "Đi dặm lại cây", "Phay đất", "Rải bạt phủ cỏ", "Cắt tỉa cành tạo tán").
- so_luong: Số lượng hoặc số người thực hiện như được nói trong câu (ví dụ: "mười đến mười lăm người", "năm người", "1000 cây", "500 lít nước"). Nếu không có thì để null.
- target_pest: Tình trạng sinh vật gây hại như được nói trong câu (vd: "không có sinh vật gây hại", "sạch sâu bệnh", "rầy nâu", "sâu ăn lá"... hoặc null nếu không nhắc).
- growth_stage: Giai đoạn sinh trưởng của cây nếu có nhắc tới, chuẩn hóa về đúng 1 trong 5 giai đoạn: "Sau thu hoạch", "Ra hoa", "Quả non", "Sinh trưởng", "Thu hoạch" (hoặc null nếu không nhắc).
- notes: Ghi chú thêm nếu có.

QUY TẮC BẮT BUỘC:
1. Trường active_activities: Chứa danh sách các mã hoạt động phát sinh từ câu nói, ví dụ: ["general"], ["purchase"], ["harvest", "sale"], ["fertilize", "spray"].
2. Hoạt động nào KHÔNG xuất hiện trong câu nói thì BẮT BUỘC gán giá trị null.
3. KHÔNG trích xuất ngày hoạt động: TUYỆT ĐỐI KHÔNG trích xuất ngày hoạt động (activity_date, purchase_date, treatment_date, harvest_date, sale_date) từ câu nói. Luôn gán null cho các trường ngày này. Hệ thống sẽ khóa và điền theo thời gian ghi nhật ký do người dùng chọn.
4. KHÔNG suy đoán ngoài câu nói. Trường nào không có thông tin thì để null.
5. NGẮN GỌN TUYỆT ĐỐI: Mỗi trường giá trị chỉ chứa từ ngữ ngắn gọn chính xác từ câu nói. TUYỆT ĐỐI KHÔNG giải thích, suy diễn, lập luận, bình luận hay trò chuyện vào giá trị bất kỳ trường nào.
6. Ngôn ngữ: Bắt buộc 100% tiếng Việt chuẩn.
7. QUY TẮC CẬP NHẬT GIAI ĐOẠN ĐỘC LẬP: Nếu câu nói CHỈ thông báo hoặc cập nhật giai đoạn sinh trưởng / mùa vụ (ví dụ: "vườn đang ở giai đoạn ra hoa", "hiện tại là giai đoạn nuôi trái", "chuyển sang giai đoạn thu hoạch", "cà gai leo đang ở thời kỳ cây con", "giai đoạn chuẩn bị đất", "sau thu hoạch") mà KHÔNG có hoạt động canh tác cụ thể nào (không phun thuốc, không bón phân, không hái quả, không xuất bán, không mua vật tư, không làm cỏ, không tưới nước...):
- BẮT BUỘC trích xuất và chuẩn hóa tên giai đoạn đó vào trường "growth_stage" ở cấp gốc về đúng 1 trong 5 giai đoạn: "Sau thu hoạch", "Ra hoa", "Quả non", "Sinh trưởng", "Thu hoạch".
- BẮT BUỘC gán "active_activities": [] (danh sách rỗng).
- TUYỆT ĐỐI KHÔNG tự bịa đặt công việc như "Thăm vườn", "Kiểm tra" hay gán vào general khi người dân không nói làm công việc đó.
- Tất cả các trường spray, fertilize, purchase, harvest, sale, waste, observation, general BẮT BUỘC gán null.

VÍ DỤ MẪU (BÓC TÁCH THEO TỪNG LOẠI CÔNG VIỆC):

Ví dụ 1 (Công việc canh tác khác - general):
Input: ngày mười hai tháng hai năm hai không hai lăm trong giai đoạn chuẩn bị đất công việc là cày bừa dọn cỏ không phát hiện sinh vật gây hại
Output:
{
  "active_activities": ["general"],
  "spray": null,
  "fertilize": null,
  "purchase": null,
  "harvest": null,
  "sale": null,
  "waste": null,
  "general": {
    "activity_date": null,
    "cong_viec": "Cày bừa dọn cỏ",
    "so_luong": null,
    "target_pest": "Không phát hiện",
    "growth_stage": "Sinh trưởng",
    "notes": null
  }
}

Ví dụ 2 (Bón phân - fertilize):
Input: ngày mười lăm tháng sáu năm hai không hai lăm giai đoạn sinh trưởng bón năm tạ trên một héc ta phân n p k không có sinh vật gây hại
Output:
{
  "active_activities": ["fertilize"],
  "spray": null,
  "fertilize": {
    "activity_date": null,
    "ten_phan_bon": "Phân NPK",
    "luong_phan_bon": "5 tạ/ha",
    "phuong_phap_bon": null,
    "growth_stage": "Sinh trưởng"
  },
  "purchase": null,
  "harvest": null,
  "sale": null,
  "waste": null,
  "general": null
}

Ví dụ 3 (Phun thuốc - spray):
Input: ngày hai mươi tháng ba năm hai không hai sáu cây giai đoạn phát triển phun thuốc trừ rầy nâu bằng thuốc sinh học anvil hai chai pha bốn trăm lít nước cách ly mười bốn ngày
Output:
{
  "active_activities": ["spray"],
  "spray": {
    "activity_date": null,
    "ten_thuoc_bvtv": "Thuốc sinh học Anvil",
    "target_pest": "Rầy nâu",
    "nong_do_va_luong_dung": "2 chai pha 400 lít nước",
    "application_method": null,
    "thoi_gian_cach_ly": "14 ngày",
    "growth_stage": "Sinh trưởng"
  },
  "fertilize": null,
  "purchase": null,
  "harvest": null,
  "sale": null,
  "waste": null,
  "general": null
}

Ví dụ 4 (Mua / Nhập cây giống - purchase):
Input: ngày mùng mười tháng tư năm hai không hai lăm nhập năm mươi ngàn cây giống xoài cát từ trại giống ba tèo không có sinh vật gây hại
Output:
{
  "active_activities": ["purchase"],
  "spray": null,
  "fertilize": null,
  "purchase": {
    "purchase_date": null,
    "material_type": "Hạt / Cây giống",
    "material_name": "Cây giống xoài cát",
    "quantity": "50000",
    "unit": "cây",
    "supplier_name": "Trại giống Ba Tèo",
    "expiry_date": null
  },
  "harvest": null,
  "sale": null,
  "waste": null,
  "general": null
}

Ví dụ 5 (Thu hoạch & Tiêu thụ cùng câu - harvest + sale):
Input: ngày mười tám tháng mười năm hai không hai lăm thu hoạch hai tấn bưởi da xanh và xuất bán cho vựa út hạnh
Output:
{
  "active_activities": ["harvest", "sale"],
  "spray": null,
  "fertilize": null,
  "purchase": null,
  "harvest": {
    "harvest_date": null,
    "crop_name": "Bưởi da xanh",
    "san_luong_thu_hoach": "2 tấn",
    "dia_diem_cach_so_che": null
  },
  "sale": {
    "sale_date": null,
    "crop_name": "Bưởi da xanh",
    "khoi_luong_tieu_thu": "2 tấn",
    "co_so_thu_mua": "Vựa Út Hạnh"
  },
  "waste": null,
  "general": null
}

Ví dụ 6 (Xử lý bao bì - waste):
Input: chiều nay gom vỏ chai bao bì thuốc trừ sâu bỏ vào hố chứa rác có nắp đậy của vườn
Output:
{
  "active_activities": ["waste"],
  "spray": null,
  "fertilize": null,
  "purchase": null,
  "harvest": null,
  "sale": null,
  "waste": {
    "treatment_date": null,
    "material_name": "Thuốc trừ sâu",
    "waste_type": "Vỏ chai bao bì thuốc trừ sâu",
    "storage_location": "Hố chứa rác có nắp đậy của vườn",
    "treatment_method": "Thu gom bỏ vào hố chứa rác"
  },
  "general": null
}

Ví dụ 7 (Đa công việc: spray + fertilize + purchase):
Input: ngày mùng năm tháng bảy năm hai không hai lăm phun thuốc và bón phân dạng nước nano trị sâu ăn lá rầy nâu liều lượng hai lít pha năm trăm lít nước mua từ công ty quang minh thời gian cách ly mười bốn ngày
Output:
{
  "active_activities": ["spray", "fertilize", "purchase"],
  "spray": {
    "activity_date": null,
    "ten_thuoc_bvtv": "Thuốc Nano",
    "target_pest": "Sâu ăn lá và rầy nâu",
    "nong_do_va_luong_dung": "2 lít pha 500 lít nước",
    "application_method": null,
    "thoi_gian_cach_ly": "14 ngày",
    "growth_stage": null
  },
  "fertilize": {
    "activity_date": null,
    "ten_phan_bon": "Phân dạng nước Nano",
    "luong_phan_bon": "2 lít pha 500 lít nước",
    "phuong_phap_bon": "hòa nước tưới",
    "growth_stage": null
  },
  "purchase": {
    "purchase_date": null,
    "material_type": "Thuốc BVTV",
    "material_name": "Thuốc Nano",
    "quantity": "2",
    "unit": "lít",
    "supplier_name": "Công ty Quang Minh",
    "expiry_date": null
  },
  "harvest": null,
  "sale": null,
  "waste": null,
  "general": null
}

Ví dụ 8 (Chỉ cập nhật giai đoạn mùa vụ, không có hoạt động canh tác):
Input: vườn sầu riêng hiện tại đang ở giai đoạn ra hoa
Output:
{
  "active_activities": [],
  "growth_stage": "Ra hoa",
  "spray": null,
  "fertilize": null,
  "purchase": null,
  "harvest": null,
  "sale": null,
  "waste": null,
  "observation": null,
  "general": null
}

Ví dụ 9 (Chỉ cập nhật giai đoạn mùa vụ):
Input: chuyển sang giai đoạn nuôi trái non rồi
Output:
{
  "active_activities": [],
  "growth_stage": "Quả non",
  "spray": null,
  "fertilize": null,
  "purchase": null,
  "harvest": null,
  "sale": null,
  "waste": null,
  "observation": null,
  "general": null
}

Ví dụ 10 (Chỉ cập nhật giai đoạn mùa vụ sau thu hoạch):
Input: vườn sầu riêng đã xong, hiện tại đang ở giai đoạn sau thu hoạch
Output:
{
  "active_activities": [],
  "growth_stage": "Sau thu hoạch",
  "spray": null,
  "fertilize": null,
  "purchase": null,
  "harvest": null,
  "sale": null,
  "waste": null,
  "observation": null,
  "general": null
}
""".strip()

    def _build_spray_prompt(self) -> str:
        """Prompt chuyên biệt CHỈ dành riêng cho công việc PHUN THUỐC BVTV."""
        return """Bạn là AI trích xuất thực thể văn bản (Text Entity Extractor) từ giọng nói nông dân cho nhật ký PHUN THUỐC BVTV theo chuẩn VietGAP.
Nhiệm vụ: Tách ĐÚNG NGUYÊN VĂN các chuỗi văn bản (raw text) có trong câu nói vào từng trường tương ứng của JSON object.
LƯU Ý QUAN TRỌNG:
- LLM CHỈ TÁCH TEXT NGUYÊN VĂN từ câu nói, TUYỆT ĐỐI KHÔNG cần chuẩn hoá thành dữ liệu chuẩn database (không đổi ngày sang YYYY-MM-DD, không đổi chữ thành số, không quy đổi đơn vị). Bộ Normalizer phía sau sẽ tự động chuyển text sang data, number, date.
- Trường nào câu nói KHÔNG nhắc đến thì để null. Tuyệt đối không tự bịa đặt dữ liệu.

CÁC TRƯỜNG CẦN TRÍCH XUẤT TRONG "spray":
- activity_date: Luôn để null (KHÔNG trích xuất ngày hoạt động từ câu nói; trường này được khóa theo thời gian ghi nhật ký).
- ten_thuoc_bvtv: Tên thuốc trừ sâu, trừ bệnh, thuốc BVTV (ví dụ: Anvil, Regent 800WG, Ridomil Gold, Antracol, Karate...).
- target_pest: Đối tượng sâu bệnh hại cần phòng trừ như được nói (ví dụ: "rầy nâu", "sâu cuốn lá", "không có sâu bệnh", "sạch bệnh"... hoặc null nếu không nhắc).
- nong_do_va_luong_dung: Liều lượng, nồng độ pha hoặc số lượng thuốc sử dụng kèm đơn vị như được nói (ví dụ: "2 chai pha 400L nước/ha", "20ml/bình 25L", "2 lít"...).
- application_method: Cách thức phun hoặc thiết bị phun như được nói (ví dụ: "máy phun", "bình xịt", null nếu không nhắc).
- thoi_gian_cach_ly: Thời gian cách ly sau phun như được nói (ví dụ: "7 ngày", "14 ngày", null nếu không nhắc).
- growth_stage: Giai đoạn sinh trưởng của cây như được nói, chuẩn hóa về 1 trong 5 giai đoạn: "Sau thu hoạch", "Ra hoa", "Quả non", "Sinh trưởng", "Thu hoạch" (null nếu không nhắc).

QUY TẮC BẮT BUỘC:
1. Trường "active_activities": Luôn luôn là ["spray"].
2. Trường "spray": Chứa đối tượng với đầy đủ các trường trên. Trường nào không có thông tin trong câu nói thì để null.
3. Chỉ trích xuất thông tin liên quan đến việc PHUN THUỐC BVTV.

VÍ DỤ MẪU:
User: Sáng nay tôi phun 2 chai Anvil pha 400 lít nước trừ rầy nâu cho vườn sầu riêng cách ly 7 ngày
Assistant: {"active_activities": ["spray"], "spray": {"activity_date": null, "ten_thuoc_bvtv": "Anvil", "target_pest": "rầy nâu", "nong_do_va_luong_dung": "2 chai pha 400 lít nước", "application_method": null, "thoi_gian_cach_ly": "7 ngày", "growth_stage": null}}""".strip()

    def _build_fertilize_prompt(self) -> str:
        """Prompt chuyên biệt CHỈ dành riêng cho công việc BÓN PHÂN."""
        return """Bạn là AI trích xuất thực thể văn bản (Text Entity Extractor) từ giọng nói nông dân cho nhật ký BÓN PHÂN theo chuẩn VietGAP.
Nhiệm vụ: Tách ĐÚNG NGUYÊN VĂN các chuỗi văn bản (raw text) có trong câu nói vào từng trường tương ứng của JSON object.
LƯU Ý QUAN TRỌNG:
- LLM CHỈ TÁCH TEXT NGUYÊN VĂN từ câu nói, TUYỆT ĐỐI KHÔNG cần chuẩn hoá thành dữ liệu chuẩn database (không đổi ngày sang YYYY-MM-DD, không đổi chữ thành số, không quy đổi đơn vị). Bộ Normalizer phía sau sẽ tự động chuyển text sang data, number, date.
- Trường nào câu nói KHÔNG nhắc đến thì để null. Tuyệt đối không tự bịa đặt dữ liệu.

CÁC TRƯỜNG CẦN TRÍCH XUẤT TRONG "fertilize":
- activity_date: Luôn để null (KHÔNG trích xuất ngày hoạt động từ câu nói; trường này được khóa theo thời gian ghi nhật ký).
- ten_phan_bon: Tên phân bón như được nói (ví dụ: NPK 20-20-15 Đầu Trâu, phân hữu cơ vi sinh Úc, phân gà hoai, Urê, DAP, Kali...).
- luong_phan_bon: Lượng phân bón kèm đơn vị như được nói (ví dụ: "hai lít rưỡi", "5 bao", "100 kg", "1.2 kg/gốc"...).
- phuong_phap_bon: Phương pháp bón phân như được nói (ví dụ: "rải quanh tán cây", "bón lót", "bón thúc", "hòa nước tưới"...).
- growth_stage: Giai đoạn sinh trưởng của cây trồng như được nói, chuẩn hóa về 1 trong 5 giai đoạn: "Sau thu hoạch", "Ra hoa", "Quả non", "Sinh trưởng", "Thu hoạch" (null nếu không nhắc).

QUY TẮC BẮT BUỘC:
1. Trường "active_activities": Luôn luôn là ["fertilize"].
2. Trường "fertilize": Chứa đối tượng với đầy đủ các trường trên. Trường nào không có thông tin thì để null.
3. Chỉ trích xuất thông tin liên quan đến việc BÓN PHÂN.

VÍ DỤ MẪU:
User: Chiều nay bón 5 bao phân NPK 20-20-15 Đầu Trâu rải quanh gốc vườn mít đang nuôi trái non
Assistant: {"active_activities": ["fertilize"], "fertilize": {"activity_date": null, "ten_phan_bon": "NPK 20-20-15 Đầu Trâu", "luong_phan_bon": "5 bao", "phuong_phap_bon": "rải quanh gốc", "growth_stage": "Quả non"}}""".strip()

    def _build_purchase_prompt(self) -> str:
        """Prompt chuyên biệt CHỈ dành riêng cho công việc MUA / NHẬP VẬT TƯ."""
        return """Bạn là AI trích xuất thực thể văn bản (Text Entity Extractor) từ giọng nói nông dân cho nhật ký MUA / NHẬP VẬT TƯ theo chuẩn VietGAP.
Nhiệm vụ: Tách ĐÚNG NGUYÊN VĂN các chuỗi văn bản (raw text) có trong câu nói vào từng trường tương ứng của JSON object.
LƯU Ý QUAN TRỌNG:
- LLM CHỈ TÁCH TEXT NGUYÊN VĂN từ câu nói, TUYỆT ĐỐI KHÔNG cần chuẩn hoá thành dữ liệu chuẩn database (không đổi ngày sang YYYY-MM-DD, không đổi chữ thành số, không quy đổi đơn vị). Bộ Normalizer phía sau sẽ tự động chuyển text sang data, number, date.
- Trường nào câu nói KHÔNG nhắc đến thì để null. Tuyệt đối không tự bịa đặt dữ liệu.

CÁC TRƯỜNG CẦN TRÍCH XUẤT TRONG "purchase":
- purchase_date: Luôn để null (KHÔNG trích xuất ngày mua từ câu nói; trường này được khóa theo thời gian ghi nhật ký).
- material_type: Loại vật tư. Chọn đúng một trong: "Phân bón", "Thuốc BVTV", "Hạt / Cây giống", "Hóa chất", "Khác".
- material_name: Tên vật tư hoặc tên giống cây trong câu (ví dụ: Anvil, NPK 20-20-15, Cây giống cà gai leo...).
- quantity: Số lượng hoặc khối lượng mua như được nói (ví dụ: "năm mươi ngàn", "10", "50", "100", "2"...).
- unit: Đơn vị tính nếu có (ví dụ: "chai", "gói", "bao", "kg", "lít", "cây"...).
- supplier_name: Nơi bán / cơ sở cung cấp nếu trong câu có nhắc đến (ví dụ: "Đại lý Ba Doanh", "Trại giống Ba Tèo"... null nếu không nhắc).
- expiry_date: Hạn sử dụng của vật tư (nếu có nhắc tới, hoặc null).

QUY TẮC BẮT BUỘC:
1. Trường "active_activities": Luôn luôn là ["purchase"].
2. Trường "purchase": Chứa đối tượng với các trường trên. Trường nào không có thông tin thì để null.
3. Chỉ trích xuất thông tin liên quan đến MUA / NHẬP VẬT TƯ.

VÍ DỤ MẪU:
User: Hôm nay mua năm mươi ngàn cây giống cà gai leo tại trại giống Ba Tèo
Assistant: {"active_activities": ["purchase"], "purchase": {"purchase_date": null, "material_type": "Hạt / Cây giống", "material_name": "Cây giống cà gai leo", "quantity": "năm mươi ngàn", "unit": "cây", "supplier_name": "Trại giống Ba Tèo", "expiry_date": null}}""".strip()

    def _build_harvest_prompt(self) -> str:
        """Prompt chuyên biệt CHỈ dành riêng cho công việc THU HOẠCH."""
        return """Bạn là AI trích xuất thực thể văn bản (Text Entity Extractor) từ giọng nói nông dân cho nhật ký THU HOẠCH NÔNG SẢN theo chuẩn VietGAP.
Nhiệm vụ: Tách ĐÚNG NGUYÊN VĂN các chuỗi văn bản (raw text) có trong câu nói vào từng trường tương ứng của JSON object.
LƯU Ý QUAN TRỌNG:
- LLM CHỈ TÁCH TEXT NGUYÊN VĂN từ câu nói, TUYỆT ĐỐI KHÔNG cần chuẩn hoá thành dữ liệu chuẩn database (không đổi ngày sang YYYY-MM-DD, không đổi chữ thành số, không quy đổi đơn vị). Bộ Normalizer phía sau sẽ tự động chuyển text sang data, number, date.
- Trường nào câu nói KHÔNG nhắc đến thì để null. Tuyệt đối không tự bịa đặt dữ liệu.

CÁC TRƯỜNG CẦN TRÍCH XUẤT TRONG "harvest":
- harvest_date: Luôn để null (KHÔNG trích xuất ngày thu hoạch từ câu nói; trường này được khóa theo thời gian ghi nhật ký).
- crop_name: Tên loại nông sản thu hoạch như được nói (ví dụ: Sầu riêng Ri6, Bưởi Da Xanh, Cà gai leo...).
- san_luong_thu_hoach: Khối lượng hoặc số lượng thu hoạch kèm đơn vị như được nói (ví dụ: "hai tấn rưỡi", "1500 kg", "50 tạ"...).
- dia_diem_cach_so_che: Địa điểm và quy cách sơ chế như được nói (ví dụ: "tại vườn đóng sọt vận chuyển", "chở về xưởng băm và sấy khô"...).

QUY TẮC BẮT BUỘC:
1. Trường "active_activities": Luôn luôn là ["harvest"].
2. Trường "harvest": Chứa đối tượng với các trường trên. Trường nào không có thông tin thì để null.
3. Chỉ trích xuất thông tin liên quan đến THU HOẠCH.

VÍ DỤ MẪU:
User: Sáng nay thu hoạch được hai tấn rưỡi cà gai leo chở về xưởng băm và sấy khô
Assistant: {"active_activities": ["harvest"], "harvest": {"harvest_date": null, "crop_name": "Cà gai leo", "san_luong_thu_hoach": "hai tấn rưỡi", "dia_diem_cach_so_che": "chở về xưởng băm và sấy khô"}}""".strip()

    def _build_sale_prompt(self) -> str:
        """Prompt chuyên biệt CHỈ dành riêng cho công việc TIÊU THỤ / XUẤT BÁN."""
        return """Bạn là AI trích xuất thực thể văn bản (Text Entity Extractor) từ giọng nói nông dân cho nhật ký TIÊU THỤ / BÁN NÔNG SẢN theo chuẩn VietGAP.
Nhiệm vụ: Tách ĐÚNG NGUYÊN VĂN các chuỗi văn bản (raw text) có trong câu nói vào từng trường tương ứng của JSON object.
LƯU Ý QUAN TRỌNG:
- LLM CHỈ TÁCH TEXT NGUYÊN VĂN từ câu nói, TUYỆT ĐỐI KHÔNG cần chuẩn hoá thành dữ liệu chuẩn database (không đổi ngày sang YYYY-MM-DD, không đổi chữ thành số, không quy đổi đơn vị). Bộ Normalizer phía sau sẽ tự động chuyển text sang data, number, date.
- Trường nào câu nói KHÔNG nhắc đến thì để null. Tuyệt đối không tự bịa đặt dữ liệu.

CÁC TRƯỜNG CẦN TRÍCH XUẤT TRONG "sale":
- sale_date: Luôn để null (KHÔNG trích xuất ngày bán từ câu nói; trường này được khóa theo thời gian ghi nhật ký).
- crop_name: Tên loại nông sản bán như được nói (ví dụ: Bưởi da xanh, Sầu riêng Ri6, Cà gai leo...).
- khoi_luong_tieu_thu: Khối lượng nông sản bán kèm đơn vị như được nói (ví dụ: "năm trăm mười hai ki lô gam", "2 tấn", "1500 kg"...).
- co_so_thu_mua: Tên thương lái, vựa thu mua hoặc công ty nếu có nhắc đến (ví dụ: "Công ty Sandu", "Thương lái Ba Tèo"...).

QUY TẮC BẮT BUỘC:
1. Trường "active_activities": Luôn luôn là ["sale"].
2. Trường "sale": Chứa đối tượng với các trường trên. Trường nào không có thông tin thì để null.
3. Chỉ trích xuất thông tin liên quan đến BÁN / TIÊU THỤ NÔNG SẢN.

VÍ DỤ MẪU:
User: Chiều nay bán năm trăm mười hai ki lô gam cà gai leo cho công ty Sandu
Assistant: {"active_activities": ["sale"], "sale": {"sale_date": null, "crop_name": "Cà gai leo", "khoi_luong_tieu_thu": "năm trăm mười hai ki lô gam", "co_so_thu_mua": "công ty Sandu"}}""".strip()

    def _build_waste_prompt(self) -> str:
        """Prompt chuyên biệt CHỈ dành riêng cho công việc XỬ LÝ BAO BÌ."""
        return """Bạn là AI trích xuất thực thể văn bản (Text Entity Extractor) từ giọng nói nông dân cho nhật ký XỬ LÝ BAO BÌ & RÁC BVTV theo chuẩn VietGAP.
Nhiệm vụ: Tách ĐÚNG NGUYÊN VĂN các chuỗi văn bản (raw text) có trong câu nói vào từng trường tương ứng của JSON object.
LƯU Ý QUAN TRỌNG:
- LLM CHỈ TÁCH TEXT NGUYÊN VĂN từ câu nói, TUYỆT ĐỐI KHÔNG cần chuẩn hoá thành dữ liệu chuẩn database (không đổi ngày sang YYYY-MM-DD, không đổi chữ thành số, không quy đổi đơn vị). Bộ Normalizer phía sau sẽ tự động chuyển text sang data, number, date.
- Trường nào câu nói KHÔNG nhắc đến thì để null. Tuyệt đối không tự bịa đặt dữ liệu.

CÁC TRƯỜNG CẦN TRÍCH XUẤT TRONG "waste":
- treatment_date: Luôn để null (KHÔNG trích xuất ngày thực hiện từ câu nói; trường này được khóa theo thời gian ghi nhật ký).
- material_name: Tên thuốc hoặc vật tư đã sử dụng có bao bì cần xử lý (ví dụ: Regent 800WG, Anvil, Antracol...).
- waste_type: Loại bao bì hoặc chất thải như được nói (ví dụ: "vỏ chai nhựa thuốc BVTV", "bao bì phân bón"...).
- storage_location: Nơi lưu giữ hoặc hố chứa như được nói (ví dụ: "hố chứa bao bì BVTV có nắp đậy"...).
- treatment_method: Phương pháp xử lý như được nói (ví dụ: "súc rửa 3 lần bằng nước sạch rồi bỏ vào hố chứa"...).

QUY TẮC BẮT BUỘC:
1. Trường "active_activities": Luôn luôn là ["waste"].
2. Trường "waste": Chứa đối tượng với các trường trên. Trường nào không có thông tin thì để null.
3. Chỉ trích xuất thông tin liên quan đến XỬ LÝ BAO BÌ.

VÍ DỤ MẪU:
User: Sau khi phun xong tôi thu gom 5 vỏ chai thuốc Anvil súc rửa 3 lần rồi bỏ vào hố chứa bao bì có nắp đậy
Assistant: {"active_activities": ["waste"], "waste": {"treatment_date": null, "material_name": "Anvil", "waste_type": "vỏ chai thuốc", "storage_location": "hố chứa bao bì có nắp đậy", "treatment_method": "súc rửa 3 lần rồi bỏ vào hố chứa"}}""".strip()

    def _build_general_prompt(self) -> str:
        """Prompt chuyên biệt CHỈ dành riêng cho CÔNG VIỆC CANH TÁC KHÁC."""
        return """Bạn là AI trích xuất thực thể văn bản (Text Entity Extractor) từ giọng nói nông dân cho nhật ký CÔNG VIỆC CANH TÁC KHÁC theo chuẩn VietGAP.
Nhiệm vụ: Tách ĐÚNG NGUYÊN VĂN các chuỗi văn bản (raw text) có trong câu nói vào từng trường tương ứng của JSON object.
LƯU Ý QUAN TRỌNG:
- LLM CHỈ TÁCH TEXT NGUYÊN VĂN từ câu nói, TUYỆT ĐỐI KHÔNG cần chuẩn hoá thành dữ liệu chuẩn database (không đổi ngày sang YYYY-MM-DD, không đổi chữ thành số, không quy đổi đơn vị). Bộ Normalizer phía sau sẽ tự động chuyển text sang data, number, date.
- Trường nào câu nói KHÔNG nhắc đến thì để null. Tuyệt đối không tự bịa đặt dữ liệu.

CÁC TRƯỜNG CẦN TRÍCH XUẤT TRONG "general":
- activity_date: Luôn để null (KHÔNG trích xuất ngày hoạt động từ câu nói; trường này được khóa theo thời gian ghi nhật ký).
- cong_viec: Tên công việc cụ thể đã làm như được nói (ví dụ: "dọn cỏ quanh gốc", "lắp ống nước béc tưới", "phay đất", "trải bạt phủ cỏ"...).
- so_luong: Số lượng hoặc số người thực hiện như được nói trong câu (ví dụ: "mười đến mười lăm người", "năm người", "1000 cây", "500 lít nước"... hoặc null nếu không nhắc).
- target_pest: Sinh vật gây hại phát hiện khi làm việc như được nói (ví dụ: "không có sinh vật gây hại", "sạch sâu bệnh", "rầy nâu", "sâu ăn lá"... hoặc null nếu không nhắc).
- growth_stage: Giai đoạn sinh trưởng của cây như được nói, chuẩn hóa về 1 trong 5 giai đoạn: "Sau thu hoạch", "Ra hoa", "Quả non", "Sinh trưởng", "Thu hoạch" (null nếu không nhắc).
- notes: Ghi chú thêm nếu có.

QUY TẮC BẮT BUỘC:
1. Trường "active_activities": Luôn luôn là ["general"].
2. Trường "general": Chứa đối tượng với các trường trên. Trường nào không có thông tin thì để null.
3. Chỉ trích xuất thông tin liên quan đến CÔNG VIỆC CANH TÁC KHÁC.

VÍ DỤ MẪU:
User: Sáng nay làm cỏ mười đến mười lăm người không có sâu bệnh
Assistant: {"active_activities": ["general"], "general": {"activity_date": null, "cong_viec": "làm cỏ", "so_luong": "mười đến mười lăm người", "target_pest": "không có sâu bệnh", "growth_stage": null, "notes": null}}""".strip()

    def _build_prompt(self, job_type: Optional[str] = None) -> str:
        """Lựa chọn prompt tối ưu theo từng loại công việc hoặc prompt đa nhiệm."""
        if job_type == "spray":
            return self._build_spray_prompt()
        elif job_type == "fertilize":
            return self._build_fertilize_prompt()
        elif job_type == "purchase":
            return self._build_purchase_prompt()
        elif job_type == "harvest":
            return self._build_harvest_prompt()
        elif job_type == "sale":
            return self._build_sale_prompt()
        elif job_type == "waste":
            return self._build_waste_prompt()
        elif job_type == "general":
            return self._build_general_prompt()
        return self._build_multi_task_prompt()

    def _get_schema_for_job(self, job_type: Optional[str] = None) -> dict:
        """Tạo JSON Schema tinh gọn chỉ cho loại công việc được chọn để tối ưu tốc độ và độ chính xác của Gemini API."""
        if not job_type or job_type == "auto" or job_type not in self.TIMELINE_ACTIVITIES:
            return self.EXTRACT_SCHEMA

        act_schema = self.EXTRACT_SCHEMA.get("properties", {}).get(job_type)
        if not act_schema:
            return self.EXTRACT_SCHEMA

        return {
            "type": "OBJECT",
            "properties": {
                "active_activities": {
                    "type": "ARRAY",
                    "items": {
                        "type": "STRING",
                        "enum": [job_type]
                    }
                },
                "growth_stage": {
                    "type": "STRING",
                    "nullable": True
                },
                job_type: act_schema
            },
            "required": ["active_activities", "growth_stage", job_type]
        }

    async def extract_info(
        self,
        raw_text: str,
        model: Optional[str] = None,
        job_type: Optional[str] = None,
        log_date: Optional[str] = None,
        farmer_id: Optional[int] = None,
        season_id: Optional[int] = None,
        auto_update_stage: bool = True,
    ) -> Dict[str, Any]:
        """
        Gọi Google Gemini API để trích xuất các hoạt động canh tác trên Timeline.
        Tối ưu hóa: Khi người dùng chọn công việc cụ thể, chỉ gửi prompt và schema của công việc đó.
        """
        api_key = self.api_key or config.GEMINI_API_KEY
        if not api_key:
            raise GeminiEngineError("Thiếu GEMINI_API_KEY trong cấu hình.")

        target_model = model or self.default_model
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent?key={api_key}"

        clean_job = str(job_type or "").strip().lower()
        if clean_job not in self.TIMELINE_ACTIVITIES and clean_job != "auto":
            clean_job = None

        system_instruction = self._build_prompt(job_type=clean_job)
        response_schema = self._get_schema_for_job(job_type=clean_job)

        user_content = f"INPUT: {raw_text.strip()}"
        if clean_job and clean_job in self.TIMELINE_ACTIVITIES:
            user_content += f"\n[CHÚ Ý LOẠI CÔNG VIỆC]: Nông dân đang ghi chép công việc '{clean_job}'. Hãy trích xuất dữ liệu vào hoạt động '{clean_job}'."

        payload = {
            "system_instruction": {
                "parts": [{"text": system_instruction}]
            },
            "contents": [
                {"parts": [{"text": user_content}]}
            ],
            "generationConfig": {
                "response_mime_type": "application/json",
                "response_schema": response_schema,
                "temperature": 0.0,
                "max_output_tokens": 2048,
            },
        }

        should_close = False
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT)
            session = aiohttp.ClientSession(connector=connector)
            should_close = True
        else:
            session = self._session

        t0 = time.perf_counter()
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
            headers = {"Content-Type": "application/json"}
            async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                elapsed_s = time.perf_counter() - t0
                if resp.status != 200:
                    err_body = await resp.text()
                    raise GeminiEngineError(f"Google Gemini API error {resp.status}: {err_body[:300]}")

                resp_json = await resp.json()

                candidates = resp_json.get("candidates", [])
                if not candidates:
                    raise GeminiEngineError("Gemini không trả về candidate nào.")

                parts = candidates[0].get("content", {}).get("parts", [])
                if not parts:
                    raise GeminiEngineError("Gemini không trả về content part nào.")

                content_text = parts[0].get("text", "").strip()

                usage = resp_json.get("usageMetadata", {})
                prompt_tokens = usage.get("promptTokenCount", 0)
                completion_tokens = usage.get("candidatesTokenCount", 0)
                tps = (completion_tokens / elapsed_s) if elapsed_s > 0 and completion_tokens > 0 else 0.0

                try:
                    parsed_dict = json.loads(content_text)
                except json.JSONDecodeError:
                    clean_text = re.sub(r"^```(?:json)?\s*", "", content_text, flags=re.MULTILINE)
                    clean_text = re.sub(r"\s*```$", "", clean_text, flags=re.MULTILINE).strip()
                    try:
                        parsed_dict = json.loads(clean_text)
                    except json.JSONDecodeError:
                        m = re.search(r"\{.*", content_text, re.DOTALL)
                        if m:
                            fixed = m.group(0).strip()
                            if fixed.count('"') % 2 != 0:
                                fixed += '"'
                            open_braces = fixed.count('{') - fixed.count('}')
                            if open_braces > 0:
                                fixed += '}' * open_braces
                            try:
                                parsed_dict = json.loads(fixed)
                            except json.JSONDecodeError:
                                logger.error("Không thể phục hồi JSON từ Gemini. Raw: %s", content_text)
                                raise
                        else:
                            raise

                # 1. Trích xuất các hoạt động timeline theo từng loại công việc
                activities = {}
                for act in self.TIMELINE_ACTIVITIES:
                    val = parsed_dict.get(act)
                    if val and isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
                        val = val[0]
                    if val and isinstance(val, dict):
                        if any(v is not None and str(v).strip() != "" and str(v).strip().lower() != "null" for v in val.values()):
                            activities[act] = val

                # 1b. Trích xuất nếu active_activities trả về danh sách object hoạt động
                raw_active_objs = parsed_dict.get("active_activities") or []
                if isinstance(raw_active_objs, list):
                    for item in raw_active_objs:
                        if isinstance(item, dict):
                            for std_act in self.TIMELINE_ACTIVITIES:
                                if std_act in item and isinstance(item[std_act], dict):
                                    activities[std_act] = item[std_act]

                            act_type = str(item.get("activity_type") or item.get("type") or item.get("loai_hoat_dong") or "").lower()
                            matched = False
                            for std_act in self.TIMELINE_ACTIVITIES:
                                if std_act in act_type and std_act not in activities:
                                    activities[std_act] = item
                                    matched = True
                                    break
                            if not matched:
                                if item.get("ten_thuoc_bvtv") and "spray" not in activities:
                                    activities["spray"] = item
                                elif item.get("ten_phan_bon") and "fertilize" not in activities:
                                    activities["fertilize"] = item
                                elif (item.get("ten_vat_tu_nhap") or item.get("material_name")) and "purchase" not in activities:
                                    activities["purchase"] = item
                                elif item.get("san_luong_thu_hoach") and "harvest" not in activities:
                                    activities["harvest"] = item
                                elif (item.get("khoi_luong_ban") or item.get("khoi_luong_tieu_thu")) and "sale" not in activities:
                                    activities["sale"] = item
                                elif (item.get("waste_type") or item.get("loai_bao_bi")) and "waste" not in activities:
                                    activities["waste"] = item
                                elif (item.get("description") or item.get("cong_viec")) and "general" not in activities:
                                    activities["general"] = item

                # 2. Xử lý trường hợp mô hình trả về dạng phẳng (flat keys) thay vì lồng theo hoạt động
                if not activities:
                    loai_hd = str(parsed_dict.get("loai_hoat_dong") or "").upper()
                    if parsed_dict.get("ten_thuoc_bvtv") or loai_hd in ("PESTICIDE", "SPRAY"):
                        activities["spray"] = {
                            "activity_date": parsed_dict.get("ngay_thuc_hien") or parsed_dict.get("activity_date"),
                            "ten_thuoc_bvtv": parsed_dict.get("ten_thuoc_bvtv"),
                            "target_pest": parsed_dict.get("sinh_vat_gay_hai") or parsed_dict.get("target_pest"),
                            "nong_do_va_luong_dung": parsed_dict.get("lieu_luong_thuoc") or parsed_dict.get("nong_do_va_luong_dung"),
                            "thoi_gian_cach_ly": parsed_dict.get("thoi_gian_cach_ly"),
                            "growth_stage": parsed_dict.get("giai_doan") or parsed_dict.get("growth_stage"),
                        }
                    if parsed_dict.get("ten_phan_bon") or parsed_dict.get("luong_phan_bon") or loai_hd == "FERTILIZING":
                        activities["fertilize"] = {
                            "activity_date": parsed_dict.get("ngay_thuc_hien") or parsed_dict.get("activity_date"),
                            "ten_phan_bon": parsed_dict.get("ten_phan_bon"),
                            "luong_phan_bon": parsed_dict.get("luong_phan_bon"),
                            "phuong_phap_bon": parsed_dict.get("phuong_phap_bon"),
                            "growth_stage": parsed_dict.get("giai_doan") or parsed_dict.get("growth_stage"),
                        }
                    if parsed_dict.get("ten_vat_tu_nhap") or parsed_dict.get("material_name") or loai_hd == "PURCHASE":
                        activities["purchase"] = {
                            "purchase_date": parsed_dict.get("ngay_thuc_hien") or parsed_dict.get("purchase_date"),
                            "material_name": parsed_dict.get("ten_vat_tu_nhap") or parsed_dict.get("material_name"),
                            "quantity": parsed_dict.get("so_luong_vat_tu") or parsed_dict.get("quantity"),
                            "unit": parsed_dict.get("unit"),
                            "supplier_name": parsed_dict.get("noi_mua_vat_tu") or parsed_dict.get("supplier_name"),
                            "expiry_date": parsed_dict.get("han_su_dung") or parsed_dict.get("expiry_date"),
                        }
                    if parsed_dict.get("san_luong_thu_hoach") or loai_hd == "HARVEST":
                        activities["harvest"] = {
                            "harvest_date": parsed_dict.get("ngay_thuc_hien") or parsed_dict.get("harvest_date"),
                            "crop_name": parsed_dict.get("crop_name"),
                            "san_luong_thu_hoach": parsed_dict.get("san_luong_thu_hoach"),
                            "dia_diem_cach_so_che": parsed_dict.get("cach_so_che") or parsed_dict.get("dia_diem_cach_so_che"),
                        }
                    if parsed_dict.get("khoi_luong_ban") or parsed_dict.get("khoi_luong_tieu_thu") or loai_hd == "SALE":
                        activities["sale"] = {
                            "sale_date": parsed_dict.get("ngay_thuc_hien") or parsed_dict.get("sale_date"),
                            "crop_name": parsed_dict.get("crop_name"),
                            "khoi_luong_tieu_thu": parsed_dict.get("khoi_luong_ban") or parsed_dict.get("khoi_luong_tieu_thu"),
                            "co_so_thu_mua": parsed_dict.get("nguoi_mua_thuong_lai") or parsed_dict.get("co_so_thu_mua"),
                        }
                    if parsed_dict.get("loai_bao_bi") or parsed_dict.get("waste_type") or loai_hd == "WASTE":
                        activities["waste"] = {
                            "treatment_date": parsed_dict.get("ngay_thuc_hien") or parsed_dict.get("treatment_date"),
                            "waste_type": parsed_dict.get("loai_bao_bi") or parsed_dict.get("waste_type"),
                            "storage_location": parsed_dict.get("noi_ton_tru") or parsed_dict.get("storage_location"),
                            "treatment_method": parsed_dict.get("cach_xu_ly") or parsed_dict.get("treatment_method"),
                        }
                # 2b. Trích xuất và chuẩn hóa growth_stage (giai đoạn mùa vụ)
                from services.database import detect_growth_stage_from_text, update_season_growth_stage

                top_growth_stage = parsed_dict.get("growth_stage")
                if not top_growth_stage:
                    for a_k, a_v in activities.items():
                        if isinstance(a_v, dict) and a_v.get("growth_stage"):
                            top_growth_stage = a_v.get("growth_stage")
                            break
                if not top_growth_stage:
                    top_growth_stage = detect_growth_stage_from_text(raw_text)
                elif isinstance(top_growth_stage, str):
                    std_stage = detect_growth_stage_from_text(top_growth_stage)
                    if std_stage:
                        top_growth_stage = std_stage

                # Hàm xác định xem một hoạt động có dữ liệu cụ thể thực sự không
                def _is_substantive(act_k: str, act_d: dict, text: str) -> bool:
                    if not isinstance(act_d, dict):
                        return False
                    if act_k == "spray":
                        return bool(act_d.get("ten_thuoc_bvtv") or (act_d.get("nong_do_va_luong_dung") and str(act_d.get("nong_do_va_luong_dung")).strip() not in ("", "null", "None")))
                    if act_k == "fertilize":
                        return bool(act_d.get("ten_phan_bon") or (act_d.get("luong_phan_bon") and str(act_d.get("luong_phan_bon")).strip() not in ("", "null", "None")))
                    if act_k == "purchase":
                        return bool(act_d.get("material_name") or act_d.get("quantity"))
                    if act_k == "harvest":
                        return bool(act_d.get("san_luong_thu_hoach") or (act_d.get("crop_name") and "thu hoạch" in text.lower()))
                    if act_k == "sale":
                        return bool(act_d.get("khoi_luong_tieu_thu") or act_d.get("khoi_luong_ban") or act_d.get("co_so_thu_mua"))
                    if act_k == "waste":
                        return bool(act_d.get("waste_type") or act_d.get("treatment_method"))
                    if act_k == "observation":
                        pest = str(act_d.get("sinh_vat_gay_hai") or act_d.get("target_pest") or "").lower()
                        return bool(pest and not any(k in pest for k in ["không", "sạch", "an toàn", "null", "chưa phát hiện"]))
                    if act_k == "general":
                        cv = str(act_d.get("cong_viec") or act_d.get("description") or "").strip().lower()
                        if not cv or cv in ("null", "none"):
                            return False
                        if top_growth_stage and (top_growth_stage.lower() in cv or cv in top_growth_stage.lower()):
                            return False
                        invented = ["thăm vườn", "thăm đồng", "kiểm tra vườn", "kiểm tra", "quan sát vườn", "quan sát"]
                        if cv in invented and not any(w in text.lower() for w in ["thăm", "kiểm tra", "quan sát"]):
                            return False
                        return True
                    return False

                # Kiểm tra xem có bất kỳ hoạt động thực tế nào không
                has_real_activities = any(_is_substantive(k, v, raw_text) for k, v in activities.items())
                is_stage_only = bool(top_growth_stage and not has_real_activities)

                if is_stage_only:
                    stage_updated = False
                    if auto_update_stage and top_growth_stage:
                        try:
                            update_season_growth_stage(top_growth_stage, farmer_id=farmer_id, season_id=season_id)
                            stage_updated = True
                            logger.info("✅ [GeminiEngine] Đã tự động cập nhật giai đoạn mùa vụ thành '%s' (farmer_id=%s, season_id=%s)", top_growth_stage, farmer_id, season_id)
                        except Exception as db_err:
                            logger.warning("Lỗi tự động cập nhật giai đoạn từ GeminiEngine: %s", db_err)

                    return {
                        "status": "success",
                        "is_stage_only": True,
                        "stage_updated": stage_updated,
                        "growth_stage": top_growth_stage,
                        "message": f"Đã tự động cập nhật giai đoạn mùa vụ thành '{top_growth_stage}'",
                        "activities": {},
                        "active_activities": [],
                        "active_tasks": [],
                        "active_tables": [],
                        "all_active_tables": [],
                        "tables": {},
                        "data": {
                            "growth_stage": top_growth_stage,
                            "giai_doan": top_growth_stage,
                            "giai_doan_sinh_truong": top_growth_stage,
                        },
                        "extracted_fields": {
                            "growth_stage": top_growth_stage,
                            "giai_doan": top_growth_stage,
                            "giai_doan_sinh_truong": top_growth_stage,
                        },
                        "raw_response": content_text,
                        "latency_s": elapsed_s,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "tokens_per_sec": tps,
                        "model": target_model,
                    }

                # Nếu không phải stage-only và chưa có hoạt động nào, tạo general từ thông tin người dùng
                if not activities:
                    if parsed_dict.get("cong_viec") or parsed_dict.get("description"):
                        activities["general"] = {
                            "activity_date": parsed_dict.get("ngay_thuc_hien") or parsed_dict.get("activity_date"),
                            "cong_viec": parsed_dict.get("cong_viec") or parsed_dict.get("description"),
                            "growth_stage": top_growth_stage or parsed_dict.get("growth_stage"),
                            "target_pest": parsed_dict.get("sinh_vat_gay_hai") or parsed_dict.get("target_pest"),
                        }

                # 3. Xác định active_activities
                raw_active = parsed_dict.get("active_activities") or []
                active_activities = []
                if isinstance(raw_active, list):
                    for a in raw_active:
                        if isinstance(a, str) and a in activities and a not in active_activities:
                            active_activities.append(a)
                        elif isinstance(a, dict):
                            for k in a.keys():
                                if k in activities and k not in active_activities:
                                    active_activities.append(k)
                elif isinstance(raw_active, str) and raw_active in activities:
                    active_activities.append(raw_active)

                if not active_activities and activities:
                    active_activities = list(activities.keys())
                if not active_activities:
                    active_activities = ["general"]

                # Chỉ giữ lại các hoạt động thực sự được kích hoạt trong active_activities
                activities = {k: v for k, v in activities.items() if k in active_activities}

                # 4. Điền ngày hoạt động theo thời gian ghi nhật ký (không tự trích xuất từ câu nói)
                target_date = to_iso_date(log_date) if log_date else date.today().isoformat()
                for act_key, act_val in activities.items():
                    if not isinstance(act_val, dict):
                        continue
                    # Gán ngày hoạt động theo thời gian ghi nhật ký đã chọn
                    for date_k in ("activity_date", "purchase_date", "harvest_date", "sale_date", "treatment_date"):
                        act_val[date_k] = target_date

                    main_date_k = "purchase_date" if act_key == "purchase" else ("harvest_date" if act_key == "harvest" else ("sale_date" if act_key == "sale" else ("treatment_date" if act_key == "waste" else "activity_date")))
                    act_val[main_date_k] = target_date

                    # Gán giai đoạn mùa vụ nếu hoạt động chưa có
                    if top_growth_stage and not act_val.get("growth_stage"):
                        act_val["growth_stage"] = top_growth_stage
                    elif act_val.get("growth_stage"):
                        std_act_stage = detect_growth_stage_from_text(act_val["growth_stage"])
                        if std_act_stage:
                            act_val["growth_stage"] = std_act_stage

                    # Chuẩn hóa target_pest nếu không có sâu bệnh
                    if "target_pest" in act_val and act_val["target_pest"]:
                        tp = str(act_val["target_pest"]).strip()
                        if any(k in tp.lower() for k in ["không", "sạch", "an toàn", "chưa phát hiện"]):
                            act_val["target_pest"] = "Không phát hiện"

                # 5. Ánh xạ vào các bảng VietGAP để lưu CSDL MySQL
                raw_tables = {}
                if "purchase" in activities:
                    pur_act = dict(activities["purchase"])
                    pur_act["material_name"] = pur_act.get("material_name") or pur_act.get("ten_vat_tu_nhap")
                    pur_act["ten_vat_tu_nhap"] = pur_act["material_name"]
                    pur_act["so_luong_vat_tu"] = pur_act.get("quantity")
                    pur_act["noi_mua_vat_tu"] = pur_act.get("supplier_name")
                    pur_act["noi_mua"] = pur_act.get("supplier_name")
                    exp_val = pur_act.get("expiry_date") or pur_act.get("han_su_dung") or extract_spoken_expiry_date(raw_text)
                    if exp_val:
                        exp_iso = to_iso_date(exp_val) or exp_val
                        pur_act["expiry_date"] = exp_iso
                        pur_act["han_su_dung"] = exp_iso
                    raw_tables["b1_5_material_purchases"] = pur_act
                    raw_tables["material_purchases"] = pur_act
                    raw_tables["purchase"] = pur_act

                usage_data = {}
                if "spray" in activities:
                    spray_act = dict(activities["spray"])
                    spray_act["ten_phan_thuoc"] = spray_act.get("ten_thuoc_bvtv")
                    spray_act["sinh_vat_gay_hai"] = spray_act.get("target_pest")
                    spray_act["luong_su_dung"] = spray_act.get("nong_do_va_luong_dung")
                    spray_act["lieu_luong_thuoc"] = spray_act.get("nong_do_va_luong_dung")
                    usage_data.update(spray_act)
                    raw_tables["pesticide_applications"] = spray_act
                    raw_tables["spray"] = spray_act

                if "fertilize" in activities:
                    fert_act = dict(activities["fertilize"])
                    fert_act["ten_phan_thuoc"] = fert_act.get("ten_phan_bon")
                    fert_act["luong_su_dung"] = fert_act.get("luong_phan_bon")
                    usage_data.update(fert_act)
                    raw_tables["fertilizer_applications"] = fert_act
                    raw_tables["fertilize"] = fert_act

                if usage_data:
                    raw_tables["b1_6_material_usages"] = usage_data
                    raw_tables["material_usages"] = usage_data

                if "waste" in activities:
                    waste_act = dict(activities["waste"])
                    waste_act["loai_bao_bi"] = waste_act.get("waste_type")
                    waste_act["noi_ton_tru"] = waste_act.get("storage_location")
                    waste_act["cach_xu_ly"] = waste_act.get("treatment_method")
                    raw_tables["b1_7_waste_treatments"] = waste_act
                    raw_tables["waste_treatments"] = waste_act
                    raw_tables["waste"] = waste_act

                if "harvest" in activities:
                    har_act = dict(activities["harvest"])
                    har_act["loai_nong_san"] = har_act.get("crop_name")
                    har_act["cach_so_che"] = har_act.get("dia_diem_cach_so_che")
                    raw_tables["b1_8_harvests"] = har_act
                    raw_tables["harvests"] = har_act
                    raw_tables["harvest_records"] = har_act
                    raw_tables["harvest"] = har_act

                if "sale" in activities:
                    sale_act = dict(activities["sale"])
                    sale_act["loai_nong_san"] = sale_act.get("crop_name")
                    sale_act["khoi_luong_ban"] = sale_act.get("khoi_luong_tieu_thu")
                    sale_act["co_so_thu_mua"] = sale_act.get("co_so_thu_mua")
                    sale_act["nguoi_mua_thuong_lai"] = sale_act.get("co_so_thu_mua")
                    sale_act["noi_mua"] = sale_act.get("co_so_thu_mua")
                    raw_tables["b1_9_sales"] = sale_act
                    raw_tables["sales"] = sale_act
                    raw_tables["sales_records"] = sale_act
                    raw_tables["sale"] = sale_act

                # Gộp b1_8_harvest_sales để tương thích ngược
                if "harvest" in activities or "sale" in activities:
                    hs_combined = {}
                    if "harvest" in activities:
                        hs_combined.update(activities["harvest"])
                    if "sale" in activities:
                        hs_combined.update(activities["sale"])
                    raw_tables["b1_8_harvest_sales"] = hs_combined

                if "general" in activities:
                    gen_act = dict(activities["general"])
                    pest_val = gen_act.get("target_pest") or gen_act.get("sinh_vat_gay_hai")
                    if pest_val and any(k in str(pest_val).lower() for k in ["không", "sạch"]):
                        pest_val = "Không phát hiện"
                    gen_act["target_pest"] = pest_val
                    gen_act["sinh_vat_gay_hai"] = pest_val
                    if "cong_viec" not in gen_act and "description" in gen_act:
                        gen_act["cong_viec"] = gen_act["description"]
                    elif "description" not in gen_act and "cong_viec" in gen_act:
                        gen_act["description"] = gen_act["cong_viec"]
                    gen_act["task_name"] = gen_act.get("cong_viec")
                    qty_val = gen_act.get("so_luong") or gen_act.get("so_luong_lieu_luong") or gen_act.get("quantity")
                    if qty_val:
                        clean_q = str(qty_val).strip()
                        gen_act["so_luong"] = clean_q
                        gen_act["so_luong_lieu_luong"] = clean_q
                        gen_act["quantity"] = clean_q
                    activities["general"] = gen_act
                    raw_tables["cultivation_diaries"] = gen_act
                    raw_tables["cultivation_activities"] = gen_act
                if "observation" in activities:
                    obs_act = dict(activities["observation"])
                    raw_tables["observation"] = obs_act
                    raw_tables["pest_observations"] = obs_act
                    raw_tables["diary_pests"] = obs_act

                for act_key, act_val in activities.items():
                    raw_tables[act_key] = act_val
                raw_tables["raw_text"] = raw_text.strip()

                classified_tables = normalize_vietgap_tables(raw_tables)
                for act_key, act_val in activities.items():
                    if act_key not in classified_tables:
                        classified_tables[act_key] = act_val

                # Cập nhật ngược lại activities để frontend và API nhận dữ liệu đã chuẩn hóa hoàn toàn
                for act_key in list(activities.keys()):
                    if act_key in classified_tables and isinstance(classified_tables[act_key], dict):
                        activities[act_key] = classified_tables[act_key]

                flat_data = tables_to_flat_dict(classified_tables)

                # Bổ sung cong_viec và giai_doan vào flat_data nếu bị thiếu
                if not flat_data.get("cong_viec"):
                    if "general" in activities:
                        flat_data["cong_viec"] = activities["general"].get("cong_viec") or activities["general"].get("description")
                    else:
                        task_labels = {
                            "spray": "Phun thuốc BVTV",
                            "fertilize": "Bón phân",
                            "purchase": "Mua vật tư",
                            "harvest": "Thu hoạch",
                            "sale": "Tiêu thụ / Bán",
                            "waste": "Xử lý bao bì",
                            "general": "Công việc canh tác",
                        }
                        matched_labels = [task_labels[a] for a in active_activities if a in task_labels]
                        if matched_labels:
                            flat_data["cong_viec"] = " và ".join(matched_labels)

                if not flat_data.get("giai_doan_sinh_truong") and flat_data.get("giai_doan"):
                    flat_data["giai_doan_sinh_truong"] = flat_data["giai_doan"]
                elif not flat_data.get("giai_doan") and flat_data.get("giai_doan_sinh_truong"):
                    flat_data["giai_doan"] = flat_data["giai_doan_sinh_truong"]

                # Tổng hợp danh sách bảng kích hoạt
                core_vietgap = [
                    "b1_5_material_purchases",
                    "b1_6_material_usages",
                    "b1_7_waste_treatments",
                    "b1_8_harvests",
                    "b1_9_sales",
                    "cultivation_diaries",
                ]
                active_tables = [t for t in core_vietgap if t in classified_tables]
                if not active_tables:
                    active_tables = ["b1_6_material_usages"]

                # Gộp extracted_fields phẳng để tương thích ngược
                merged_fields = {}
                for act_key, act_val in activities.items():
                    for k, v in act_val.items():
                        if v is not None and str(v).strip() != "" and str(v).strip().lower() != "null":
                            merged_fields[k] = v
                extracted_fields = {**parsed_dict, **merged_fields}

                return {
                    "is_stage_only": False,
                    "stage_updated": False,
                    "growth_stage": top_growth_stage,
                    "activities": activities,
                    "active_activities": active_activities,
                    "active_tasks": active_activities,
                    "active_tables": active_tables,
                    "all_active_tables": active_tables,
                    "tables": classified_tables,
                    "data": flat_data,
                    "extracted_fields": extracted_fields,
                    "raw_response": content_text,
                    "latency_s": elapsed_s,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "tokens_per_sec": tps,
                    "model": target_model,
                }
        finally:
            if should_close:
                await session.close()

    async def post_process_stt(self, raw_text: str, model: Optional[str] = None) -> str:
        """
        Trích xuất và chuẩn hóa văn bản STT qua Gemini, tự động phân loại vào các bảng CSDL theo schema.sql.
        Trả về JSON string chứa 'activities' (theo từng loại công việc), 'tables' và 'data'.
        """
        if not raw_text or not raw_text.strip():
            return raw_text

        try:
            result = await self.extract_info(raw_text, model=model)
            payload = {
                "activities": result["activities"],
                "active_activities": result["active_activities"],
                "active_tasks": result["active_tasks"],
                "tables": result["tables"],
                "data": result["data"],
            }
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.exception("Lỗi Gemini post_process_stt: %s", e)
            return raw_text


# Singleton instance
gemini_engine = GeminiEngine()

