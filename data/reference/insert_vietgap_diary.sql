-- ========================================================
-- DỮ LIỆU TỰ ĐỘNG TRÍCH XUẤT CHO VIETGAP FARM DIARY
-- Tạo lúc: 2026-09-30 16:40:53
-- ========================================================
USE vietgap_diary;

-- --------------------------------------------------------
-- 1. BẢNG PESTICIDES (22 bản ghi)
-- --------------------------------------------------------
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Spendora 690WP', 'Dimethomorph 90g/kg, Mancozeb 600g/kg', 'WP', '7949/CNĐKT - BVTV', 'Đậu tương, cà chua, khoai tây, vải, thiều, nhãn, dâu tây, hoa cắt cảnh, tiêu, dưa hấu, thuốc lá, cao su, dưa, bầu bí, hành tỏi, nho', 7, '1.5 - 2.0 kg/ha', 'GHS', FALSE, 'Heze Longge Plant Protection Technology Co., Ltd (Đăng ký và phân phối: Công ty Cổ phần Kỹ thuật Công nghệ Klever)');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Epolists 85WP', 'Copper oxychloride 85% w/w', 'WP', '5289/CNĐKT-BVTV', 'Lúa, Cà phê, Điều', 7, '20g/8 lít nước', 'GHS', FALSE, 'Parikh Enterprises Pvt. Ltd - India / Công ty Cổ phần Thanh Điền');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Kasuduc 3SL', 'Kasugamycin 30g/l', 'SL', '5153/CNĐKT-BVTV', 'Lúa', 7, '15ml/20 lít nước', 'GHS 5', FALSE, 'Công ty TNHH Việt Đức');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Amity Top 500SC', 'Azoxystrobin 250g/l, Difenoconazole 250g/l', 'SC', '4611/CNĐKT-BVTV', 'Lúa, Vải', NULL, 'Chưa ghi rõ trên nhãn', 'Vàng', FALSE, 'Công ty TNHH Việt Đức');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Tilbest super 300EC', 'Difenoconazole 150g/l, Propiconazole 150g/l', 'EC', 'Chưa xác định', 'Lúa, Vải, Nhãn, Táo, Cam, Bưởi, Mận, Na', 14, '10ml/16 lít nước', 'Chưa xác định', FALSE, 'Công ty Cổ phần Green Mê Kông');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Ridomil Gold 68WG', 'Metalaxyl M 40g/kg, Mancozeb 640g/kg', 'WG', '154/CNĐKT-BVTV', 'Ca cao, Điều, Cao su, Thuốc lá, Lạc, Hồ tiêu, Ngô (bắp)', 14, '1000g/phuy 200 lít', 'Nhóm 4', FALSE, 'Công ty TNHH Syngenta Việt Nam');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Anvil 5SC', 'Hexaconazole 50g/l', 'SC', '141/CNĐKT-BVTV', 'Lúa, Cà phê, Lạc, Ngô, Thuốc lá, Cao su, Cam, Thanh long', 7, '0.6%', 'Nhóm 3', FALSE, 'Syngenta Việt Nam');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('FORTHANE 80WP', 'Mancozeb 80% w/w', 'WP', '2556/CNĐKT-BVTV', 'Bắp cải, Lúa', 7, '50g/20-25 lít nước', 'GHS', FALSE, 'Công ty TNHH Thuốc BVTV Hoàng Phát');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Mexyl MZ 72 WP Xanh Gold', 'Metalaxyl 8% w/w, Mancozeb 64% w/w', 'WP', '2706/CNĐKT-BVTV', 'Tiêu, Cao su', 7, '0.45% (300-450g/100 lít nước)', 'GHS', FALSE, 'Công ty Cổ phần Bảo vệ Thực vật Sài Gòn');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('INCOPPER 85WP', 'Copper oxychloride 85% w/w', 'WP', '3790/CNĐKT-BVTV', 'Cà phê, cam, quýt, dưa hấu, cà chua, khoai tây, dưa chuột, đậu đỗ, hành tỏi, thuốc lá', 7, '1kg/600-800 lít nước', 'GHS 4', FALSE, 'Agro Life Science Corporation (Ấn Độ) - Phân phối: Công ty Cổ phần Agrifarm Việt Nam');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Dosay 45WP', 'Cymoxanil 4% + Zineb 12% + Copper Oxychloride 29%', 'WP', '2172/CNĐKT-BVTV', 'Khoai tây, Cao su', 7, '0.3%', 'GHS', FALSE, 'AGRIA S.A. - Bulgaria');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('LION RAY 200SC', 'Imidacloprid 200g/l', 'SC', '8005/CNĐKT-BVTV', 'Rệp sáp bột hồng/sắn', 7, '20ml/25 lít nước', 'GHS', FALSE, 'Công ty TNHH TCT Hà Nội');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Mãng Xà (Masterpyr 240 SC)', 'Chlorfenapyr 240g/l', 'SC', '7587/CNĐKT-BVTV', 'Lạc, chè, hoa, quất, cam, phật thủ, lúa, ngô, rau màu', 7, '15ml/bình 18-20 lít', 'GHS', FALSE, 'Công ty TNHH Agro Việt');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Movento 150 OD', 'Spirotetramat 150g/l', 'OD', 'Đang cập nhật', 'Cây ăn trái, rau màu, cây công nghiệp', 7, '0.04% - 0.05% (pha 20-25ml cho bình 16 lít nước)', 'Nhóm 3', FALSE, 'Bayer Việt Nam');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('NOFARA 35WG', 'Thiamethoxam 35% w/w', 'WG', '669/CNĐKT-BVTV', 'Lúa', 14, '20g/ha (lượng nước 320-500 lít/ha)', 'Nhóm 5 GHS', FALSE, 'Công ty TNHH TM Nông Phát');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Rệp Vương (Acetat 250EC)', 'Acetamiprid 200g/l + Lambda-cyhalothrin 50g/l', 'EC', '7886/CNĐKT-BVTV', 'Mía', 7, '200ml/200-250 lít nước', 'GHS', FALSE, 'Công ty TNHH A2T Việt Nam');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Pesieu 500SC', 'Diafenthiuron 500g/l', 'SC', '63/CNĐKT-BVTV', 'Hoa, hoa hồng, cúc, dưa, rau màu, vải, nhãn, cam', 7, '10-15ml cho bình 16 lít nước', 'GHS', FALSE, 'Công ty TNHH Việt Thắng');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('BP. Newone 500SC', 'Diafenthiuron 500g/l', 'SC', '8180/CNĐKT-BVTV', 'Hoa hồng', 7, '0.8 lít/ha', 'GHS 4', FALSE, 'Công ty TNHH Nông dược Việt Đức');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Dầu khoáng Ds 98.8EC', 'Petroleum Spray Oil 98.8% w/w', 'EC', '5056/CNĐKT-BVTV', 'Cây có múi', 2, '85-125ml/bình 25 lít', 'GHS 5', FALSE, 'Công ty Cổ phần Đồng Xanh');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Dũng sĩ diệt ruồi vàng An Phát', 'Không có (Sản phẩm bẫy dính cơ học)', 'Bẫy dính', 'Không áp dụng', 'Tất cả các loại cây trồng', NULL, 'Không áp dụng', 'Không độc hại', FALSE, 'Công ty TNHH MTV Thương mại Sản xuất An Phát');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('BILLADEN 50WG', 'Emamectin benzoate 50g/kg', 'WG', '3537/CNĐKT-BVTV', 'Cam, bắp cải', 7, '0.03% (cho nhện đỏ), 0.12 kg/ha (cho sâu xanh)', 'GHS', FALSE, 'Công ty CP Nông nghiệp Thanh Xuân');
INSERT INTO pesticides (name, active_ingredient, formulation, registration_no, allowed_crops, min_isolation_days, max_concentration, toxicity_group, is_banned, manufacturer)
VALUES ('Tumazine 50WP', 'Cyromazine 500g/kg', 'WP', '7842/CNĐKT-BVTV', 'Dưa chuột', 7, '18g/25 lít nước', 'GHS 5', FALSE, 'Guangxi Baikal Biochemical Product Co., Ltd');

-- --------------------------------------------------------
-- 2. BẢNG FERTILIZERS (10 bản ghi)
-- --------------------------------------------------------
INSERT INTO fertilizers (name, fertilizer_type, npk_ratio, registration_no, manufacturer)
VALUES ('Canxi Bo - Silic (FUNO-PN01)', 'Phân bón vi lượng', 'Bo: 2.000ppm, Ca: 7, Mg: 3, Zn: 15.000, Fe: 90, Cu: 80, Mn: 80', '16838', 'Công ty TNHH Khoa học Kỹ thuật Asanzo');
INSERT INTO fertilizers (name, fertilizer_type, npk_ratio, registration_no, manufacturer)
VALUES ('CANXIBO HỮU CƠ (V-CABO OGRANIC)', 'Phân bón hữu cơ', 'Chất hữu cơ: 20%, Canxi (Ca): 8.6%, Bo (B): 800ppm', 'MSPB: 28634', 'Công ty TNHH New Vision Vietnamese');
INSERT INTO fertilizers (name, fertilizer_type, npk_ratio, registration_no, manufacturer)
VALUES ('Bơ Max (BO NSG 1)', 'Phân bón lá vi lượng', 'Bo: 117.800 mg/l, N: 5,4%, NAA: 1.200 ppm', 'MSPB: 14669', 'Công ty CPĐT và Phát triển Vũng Phát');
INSERT INTO fertilizers (name, fertilizer_type, npk_ratio, registration_no, manufacturer)
VALUES ('Siêu ra rễ - đẻ nhánh', 'Phân bón lá vi lượng', 'Bo (B): 2,222ppm', '27463', 'Công ty Cổ phần Thương mại Xuất Nhập Khẩu VNT');
INSERT INTO fertilizers (name, fertilizer_type, npk_ratio, registration_no, manufacturer)
VALUES ('PROFARM-TOPMICRO', 'Phân bón vi lượng', 'Mangan: 40.000 ppm, Kẽm: 30.000 ppm, Sắt: 20.000 ppm, Đồng: 10.000 ppm, Bo: 5.000 ppm, Molipđen: 1.000 ppm, Côban: 500 ppm, Lưu huỳnh: 8%, Magie: 7,2%', 'MSPB: 11658', 'Công ty TNHH Nông Việt (Agriviet Co., Ltd)');
INSERT INTO fertilizers (name, fertilizer_type, npk_ratio, registration_no, manufacturer)
VALUES ('Farmproka-V3 16-8-34', 'Phân bón NPK', '16-8-34', '932/QĐ-BVTV-PB', 'Công ty TNHH Nông Việt (Agriviet Co., Ltd.)');
INSERT INTO fertilizers (name, fertilizer_type, npk_ratio, registration_no, manufacturer)
VALUES ('Deltamicro Hydro', 'Phân bón vi lượng', 'Lưu huỳnh: 6.98%, Magie: 1.8%, Sắt: 54000 ppm, Mangan: 50000 ppm, Bo: 35000 ppm, Kẽm: 30000 ppm, Đồng: 3000 ppm, Molipden: 4000 ppm, Coban: 500 ppm', '15626', 'Greendelta Co., Ltd.');
INSERT INTO fertilizers (name, fertilizer_type, npk_ratio, registration_no, manufacturer)
VALUES ('Phân bón vi lượng SIAMB 12', 'Phân bón vi lượng', 'Kẽm (Zn): 15.000ppm', 'MSPB: 08524', 'Công ty TNHH TLC CONTINENTAL');
INSERT INTO fertilizers (name, fertilizer_type, npk_ratio, registration_no, manufacturer)
VALUES ('SUPER LÂN', 'Phân bón vi lượng', 'Bo: 2.000ppm', 'MSP: 14420', 'Công ty TNHH Sản xuất Thương mại Hoàng Thái Nông');
INSERT INTO fertilizers (name, fertilizer_type, npk_ratio, registration_no, manufacturer)
VALUES ('TOBA NET', 'Phân bón hỗn hợp NPK', 'N: 5%, P2O5: 8%, K2O: 8%', 'MSPB: 18640', 'Công ty TNHH Sản xuất - Thương mại Toba');
