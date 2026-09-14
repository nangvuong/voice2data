-- =========================================================
-- VIETGAP FARM DIARY DATABASE
-- Cấu trúc theo nhóm: Trang trại / Danh mục / Nhật ký / AI / Đồng bộ
-- =========================================================

CREATE DATABASE IF NOT EXISTS vietgap_diary
CHARACTER SET utf8mb4
COLLATE utf8mb4_unicode_ci;

USE vietgap_diary;

-- =========================================================
-- 0. USERS (hạ tầng dùng chung — không có trong danh sách gốc
--    nhưng cần để gán created_by / reviewed_by / chủ trang trại)
-- =========================================================

CREATE TABLE users (
    user_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(100) NOT NULL UNIQUE,
    password VARCHAR(255) NOT NULL,
    full_name VARCHAR(150),
    phone VARCHAR(20),
    role ENUM('ADMIN','FARM_OWNER','WORKER','TECHNICIAN') NOT NULL DEFAULT 'WORKER',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- =========================================================
-- 1. TRANG TRẠI
-- =========================================================

CREATE TABLE farm (
    farm_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    owner_user_id BIGINT NULL,

    name VARCHAR(200) NOT NULL,
    farm_code VARCHAR(50) UNIQUE,              -- mã vùng trồng (traceability code)
    address TEXT,
    phone VARCHAR(20),
    email VARCHAR(150),

    certification_body VARCHAR(200),           -- đơn vị chứng nhận VietGAP
    certification_no VARCHAR(100),
    certification_date DATE,
    certification_expiry DATE,
    target_market TEXT,                        -- cơ sở / đối tác tiêu thụ

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_farm_owner FOREIGN KEY (owner_user_id)
        REFERENCES users(user_id) ON DELETE SET NULL ON UPDATE CASCADE
);

CREATE TABLE plots (
    plot_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    farm_id BIGINT NOT NULL,

    name VARCHAR(150) NOT NULL,                -- "Lô A2"
    plot_code VARCHAR(100) UNIQUE,
    crop_name VARCHAR(150),                    -- cây trồng hiện tại
    variety VARCHAR(150),

    area_size DECIMAL(12,2),
    area_unit VARCHAR(20) DEFAULT 'm2',

    latitude DECIMAL(10,7),
    longitude DECIMAL(10,7),

    soil_type VARCHAR(100),
    water_source VARCHAR(150),
    land_use_history TEXT,                     -- lịch sử sử dụng đất (VietGAP yêu cầu)

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_plot_farm FOREIGN KEY (farm_id)
        REFERENCES farm(farm_id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE crop_seasons (
    season_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    plot_id BIGINT NOT NULL,

    season_name VARCHAR(100) NOT NULL,         -- "Vụ 2026"
    crop_name VARCHAR(150),
    variety VARCHAR(150),
    seed_source VARCHAR(200),                  -- nguồn giống (truy xuất nguồn gốc)

    start_date DATE,
    expected_harvest_date DATE,
    actual_end_date DATE,

    expected_yield DECIMAL(15,2),
    actual_yield DECIMAL(15,2),
    yield_unit VARCHAR(20) DEFAULT 'kg',
    target_market TEXT,                        -- cơ sở / đối tác tiêu thụ

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_season_plot FOREIGN KEY (plot_id)
        REFERENCES plots(plot_id) ON DELETE CASCADE ON UPDATE CASCADE
);

-- =========================================================
-- 2. DANH MỤC
-- =========================================================

CREATE TABLE suppliers (
    supplier_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    address TEXT,
    phone VARCHAR(20),
    email VARCHAR(150),
    supplier_type VARCHAR(100),                -- thuốc / phân / giống / khác
    business_license VARCHAR(150),             -- chứng từ pháp lý nguồn cung
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE pesticides (
    pesticide_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(200) NOT NULL,                -- "ABC 50EC"
    active_ingredient VARCHAR(255),            -- hoạt chất
    formulation VARCHAR(50),                   -- EC, SC, WP...
    registration_no VARCHAR(100),              -- số đăng ký lưu hành (bắt buộc VietGAP)
    allowed_crops TEXT,                        -- cây trồng được phép dùng
    min_isolation_days INT,                    -- cách ly tối thiểu theo nhãn thuốc
    max_concentration VARCHAR(255),            -- liều lượng nồng độ pha chế tối đa
    toxicity_group VARCHAR(50),                -- nhóm độc I/II/III/IV, GHS
    is_banned BOOLEAN NOT NULL DEFAULT FALSE,  -- cờ cảnh báo thuốc cấm/hết hạn lưu hành
    manufacturer VARCHAR(255),                 -- nhà sản xuất / đăng ký / phân phối
    rag_knowledge JSON,                        -- dữ liệu tri thức kỹ thuật RAG (cách dùng, đối tượng hại, lưu ý)
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE fertilizers (
    fertilizer_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(200) NOT NULL,                -- "NPK 16-16-8"
    fertilizer_type VARCHAR(100),              -- hữu cơ / vô cơ / vi sinh / vi lượng
    npk_ratio TEXT,                            -- tỷ lệ NPK hoặc hàm lượng dinh dưỡng chi tiết
    registration_no VARCHAR(100),              -- số lưu hành / MSPB
    manufacturer VARCHAR(255),                 -- nhà sản xuất / phân phối
    rag_knowledge JSON,                        -- dữ liệu tri thức kỹ thuật RAG (cách dùng, đối tượng, lưu ý)
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE materials (
    material_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(200) NOT NULL,                -- giống, dụng cụ, màng phủ...
    material_type VARCHAR(100),
    unit VARCHAR(30),
    description TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- =========================================================
-- 3. NHẬT KÝ
--    cultivation_activities = bảng gốc (mọi hoạt động ghi nhận theo ngày/giờ,
--    có ảnh + ghi âm dùng chung). Mỗi loại hoạt động chi tiết mở rộng
--    bằng bảng con 1-1 (pesticide_applications, fertilizer_applications...).
--    Cách này tránh lặp cột ngày/giờ/ảnh/voice ở từng bảng con,
--    và cho phép timeline hiển thị mọi loại hoạt động bằng 1 query duy nhất.
-- =========================================================

CREATE TABLE cultivation_activities (
    activity_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    season_id BIGINT NOT NULL,

    activity_type ENUM(
        'PESTICIDE','FERTILIZER','PEST_OBSERVATION',
        'IRRIGATION','HARVEST','OTHER'
    ) NOT NULL,

    activity_date DATE NOT NULL,
    activity_time TIME NULL,                   -- "08:15" — tách riêng ngày/giờ

    growth_stage VARCHAR(150),
    description TEXT,
    weather_condition VARCHAR(100),            -- nắng/mưa/gió — an toàn khi phun thuốc

    performed_by VARCHAR(150),                 -- người trực tiếp thực hiện
    created_by BIGINT NULL,                    -- user ghi nhận (có thể là AI service account)

    source ENUM('MANUAL','VOICE_AI','IMPORTED') NOT NULL DEFAULT 'MANUAL',
    is_confirmed BOOLEAN NOT NULL DEFAULT TRUE, -- FALSE nếu AI trích xuất chưa được xác nhận

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_activity_season FOREIGN KEY (season_id)
        REFERENCES crop_seasons(season_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_activity_user FOREIGN KEY (created_by)
        REFERENCES users(user_id) ON DELETE SET NULL ON UPDATE CASCADE
);

CREATE TABLE activity_photos (
    photo_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    activity_id BIGINT NOT NULL,
    photo_url TEXT NOT NULL,
    taken_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_photo_activity FOREIGN KEY (activity_id)
        REFERENCES cultivation_activities(activity_id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE activity_voice_records (
    voice_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    activity_id BIGINT NOT NULL,

    audio_url TEXT NOT NULL,
    audio_duration INT NULL,
    file_size BIGINT NULL,
    mime_type VARCHAR(100),

    stt_text TEXT,                             -- văn bản sau nhận diện giọng nói
    stt_raw_response JSON NULL,

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_voice_activity FOREIGN KEY (activity_id)
        REFERENCES cultivation_activities(activity_id) ON DELETE CASCADE ON UPDATE CASCADE
);

-- ---- Phun thuốc ----
CREATE TABLE pesticide_applications (
    application_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    activity_id BIGINT NOT NULL UNIQUE,
    pesticide_id BIGINT NOT NULL,
    supplier_id BIGINT NULL,

    concentration_value DECIMAL(10,4),         -- 0.2
    concentration_unit VARCHAR(20),            -- "%", "ppm"
    dose_quantity DECIMAL(15,3),               -- 20
    dose_unit VARCHAR(20),                     -- "lít"

    application_method VARCHAR(150),           -- phun sương, phun đẫm...
    target_pest VARCHAR(200),                  -- đối tượng phòng trừ

    isolation_days INT NOT NULL,               -- cách ly thực tế ghi nhận
    safe_harvest_date DATE,                    -- = activity_date + isolation_days

    batch_no VARCHAR(100),
    note TEXT,

    CONSTRAINT fk_pest_app_activity FOREIGN KEY (activity_id)
        REFERENCES cultivation_activities(activity_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_pest_app_pesticide FOREIGN KEY (pesticide_id)
        REFERENCES pesticides(pesticide_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_pest_app_supplier FOREIGN KEY (supplier_id)
        REFERENCES suppliers(supplier_id) ON DELETE SET NULL ON UPDATE CASCADE
);

-- ---- Bón phân ----
CREATE TABLE fertilizer_applications (
    application_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    activity_id BIGINT NOT NULL UNIQUE,
    fertilizer_id BIGINT NOT NULL,
    supplier_id BIGINT NULL,

    quantity DECIMAL(15,3) NOT NULL,
    unit VARCHAR(20),
    application_method VARCHAR(150),           -- bón gốc, bón lá...
    batch_no VARCHAR(100),
    note TEXT,

    CONSTRAINT fk_fert_app_activity FOREIGN KEY (activity_id)
        REFERENCES cultivation_activities(activity_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_fert_app_fertilizer FOREIGN KEY (fertilizer_id)
        REFERENCES fertilizers(fertilizer_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_fert_app_supplier FOREIGN KEY (supplier_id)
        REFERENCES suppliers(supplier_id) ON DELETE SET NULL ON UPDATE CASCADE
);

-- ---- Phát hiện sâu bệnh ----
CREATE TABLE pest_observations (
    observation_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    activity_id BIGINT NOT NULL UNIQUE,

    pest_name VARCHAR(200) NOT NULL,
    pest_type VARCHAR(100),                    -- sâu / bệnh / cỏ dại...
    severity VARCHAR(50),                      -- nhẹ / trung bình / nặng
    affected_area_percent DECIMAL(5,2),
    action_taken TEXT,
    note TEXT,

    CONSTRAINT fk_pest_obs_activity FOREIGN KEY (activity_id)
        REFERENCES cultivation_activities(activity_id) ON DELETE CASCADE ON UPDATE CASCADE
);

-- ---- Tưới ----
CREATE TABLE irrigation_records (
    irrigation_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    activity_id BIGINT NOT NULL UNIQUE,

    water_source VARCHAR(150),                 -- giếng khoan, sông, hồ...
    method VARCHAR(100),                       -- nhỏ giọt, phun mưa, tưới tràn
    duration_minutes INT,
    quantity DECIMAL(15,3) NULL,               -- m3 nếu đo được
    note TEXT,

    CONSTRAINT fk_irrigation_activity FOREIGN KEY (activity_id)
        REFERENCES cultivation_activities(activity_id) ON DELETE CASCADE ON UPDATE CASCADE
);

-- ---- Mua vật tư (không gắn với 1 hoạt động cụ thể mà gắn theo mùa vụ) ----
CREATE TABLE input_purchases (
    purchase_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    season_id BIGINT NOT NULL,
    supplier_id BIGINT NULL,

    item_type ENUM('PESTICIDE','FERTILIZER','MATERIAL') NOT NULL,
    pesticide_id BIGINT NULL,
    fertilizer_id BIGINT NULL,
    material_id BIGINT NULL,

    purchase_date DATE NOT NULL,
    quantity DECIMAL(15,3) NOT NULL,
    unit VARCHAR(30),
    unit_price DECIMAL(15,2),

    batch_no VARCHAR(100),
    expiry_date DATE,
    invoice_no VARCHAR(100),                   -- hóa đơn, chứng từ truy xuất nguồn gốc
    note TEXT,

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_purchase_season FOREIGN KEY (season_id)
        REFERENCES crop_seasons(season_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_purchase_supplier FOREIGN KEY (supplier_id)
        REFERENCES suppliers(supplier_id) ON DELETE SET NULL ON UPDATE CASCADE,
    CONSTRAINT fk_purchase_pesticide FOREIGN KEY (pesticide_id)
        REFERENCES pesticides(pesticide_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_purchase_fertilizer FOREIGN KEY (fertilizer_id)
        REFERENCES fertilizers(fertilizer_id) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT fk_purchase_material FOREIGN KEY (material_id)
        REFERENCES materials(material_id) ON DELETE RESTRICT ON UPDATE CASCADE
);

-- ---- Thu hoạch ----
CREATE TABLE harvest_records (
    harvest_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    activity_id BIGINT NOT NULL UNIQUE,
    season_id BIGINT NOT NULL,

    quantity DECIMAL(15,3) NOT NULL,
    unit VARCHAR(20) DEFAULT 'kg',
    processing_location VARCHAR(255),          -- điểm sơ chế
    harvester_name VARCHAR(150),
    note TEXT,

    CONSTRAINT fk_harvest_activity FOREIGN KEY (activity_id)
        REFERENCES cultivation_activities(activity_id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_harvest_season FOREIGN KEY (season_id)
        REFERENCES crop_seasons(season_id) ON DELETE CASCADE ON UPDATE CASCADE
);

-- ---- Bán sản phẩm ----
CREATE TABLE sales_records (
    sale_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    harvest_id BIGINT NOT NULL,

    buyer_name VARCHAR(200),
    buyer_contact VARCHAR(150),
    sale_date DATE NOT NULL,

    quantity DECIMAL(15,3) NOT NULL,
    unit VARCHAR(20) DEFAULT 'kg',
    unit_price DECIMAL(15,2),
    total_amount DECIMAL(18,2),
    note TEXT,

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_sale_harvest FOREIGN KEY (harvest_id)
        REFERENCES harvest_records(harvest_id) ON DELETE CASCADE ON UPDATE CASCADE
);

-- ---- Xử lý bao bì / chất thải ----
CREATE TABLE waste_disposals (
    disposal_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    season_id BIGINT NOT NULL,

    disposal_date DATE NOT NULL,
    waste_type VARCHAR(150),                   -- bao bì thuốc, chai lọ, tàn dư...
    quantity DECIMAL(15,3),
    unit VARCHAR(30),
    storage_location VARCHAR(255),             -- bể chứa/khu tập kết
    treatment_method TEXT,
    note TEXT,

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_waste_season FOREIGN KEY (season_id)
        REFERENCES crop_seasons(season_id) ON DELETE CASCADE ON UPDATE CASCADE
);

-- =========================================================
-- 4. AI
-- =========================================================

CREATE TABLE ai_extraction_records (
    extraction_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    activity_id BIGINT NULL,                   -- gắn sau khi được xác nhận & tạo activity
    voice_id BIGINT NULL,

    raw_transcript TEXT NOT NULL,              -- văn bản thô từ STT
    ai_json_result JSON NOT NULL,              -- kết quả AI trích xuất có cấu trúc
    time_process DECIMAL(15,3),

    extraction_status ENUM('PENDING','APPLIED','REJECTED','NEEDS_REVIEW')
        NOT NULL DEFAULT 'PENDING',

    reviewed_by BIGINT NULL,
    reviewed_at DATETIME NULL,

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT fk_extraction_activity FOREIGN KEY (activity_id)
        REFERENCES cultivation_activities(activity_id) ON DELETE SET NULL ON UPDATE CASCADE,
    CONSTRAINT fk_extraction_voice FOREIGN KEY (voice_id)
        REFERENCES activity_voice_records(voice_id) ON DELETE SET NULL ON UPDATE CASCADE,
    CONSTRAINT fk_extraction_reviewer FOREIGN KEY (reviewed_by)
        REFERENCES users(user_id) ON DELETE SET NULL ON UPDATE CASCADE
);

-- =========================================================
-- 5. ĐỒNG BỘ (offline-first mobile)
-- =========================================================

CREATE TABLE sync_queue (
    queue_id BIGINT AUTO_INCREMENT PRIMARY KEY,

    device_id VARCHAR(100) NOT NULL,
    local_uuid VARCHAR(36) NOT NULL,           -- UUID sinh ở client, dùng để map khi server trả id thật
    table_name VARCHAR(100) NOT NULL,
    record_id BIGINT NULL,                     -- id thật trên server sau khi đồng bộ xong

    operation ENUM('INSERT','UPDATE','DELETE') NOT NULL,
    payload JSON NOT NULL,

    sync_status ENUM('PENDING','SYNCING','SYNCED','FAILED') NOT NULL DEFAULT 'PENDING',
    error_message TEXT NULL,
    retry_count INT NOT NULL DEFAULT 0,

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    synced_at DATETIME NULL,

    UNIQUE KEY uq_sync_local (device_id, local_uuid)
);

-- =========================================================
-- 6. INDEX
-- =========================================================

CREATE INDEX idx_plot_farm ON plots(farm_id);
CREATE INDEX idx_season_plot ON crop_seasons(plot_id);

CREATE INDEX idx_activity_season ON cultivation_activities(season_id);
CREATE INDEX idx_activity_date ON cultivation_activities(activity_date);
CREATE INDEX idx_activity_type ON cultivation_activities(activity_type);

CREATE INDEX idx_photo_activity ON activity_photos(activity_id);
CREATE INDEX idx_voice_activity ON activity_voice_records(activity_id);

CREATE INDEX idx_pest_app_pesticide ON pesticide_applications(pesticide_id);
CREATE INDEX idx_pest_app_safe_date ON pesticide_applications(safe_harvest_date);

CREATE INDEX idx_fert_app_fertilizer ON fertilizer_applications(fertilizer_id);

CREATE INDEX idx_purchase_season ON input_purchases(season_id);
CREATE INDEX idx_harvest_season ON harvest_records(season_id);
CREATE INDEX idx_sale_harvest ON sales_records(harvest_id);
CREATE INDEX idx_waste_season ON waste_disposals(season_id);

CREATE INDEX idx_extraction_status ON ai_extraction_records(extraction_status);
CREATE INDEX idx_sync_status ON sync_queue(sync_status);
CREATE INDEX idx_sync_table_record ON sync_queue(table_name, record_id);