# Thư mục Tri Thức Production (Production Knowledge Base)

Thư mục này chứa toàn bộ dữ liệu tri thức đã được trích xuất, làm sạch, loại bỏ bìa/mục lục/nhiễu hành chính và cắt chunk tối ưu sẵn sàng đưa vào **Production RAG**.

## 1. Cấu trúc thư mục

```text
production-knowledge/
├── retrieval_chunks.jsonl  # 1.681 chunks - FILE CHÍNH ĐỂ NẠP VÀO VECTOR DB
├── knowledge.md            # Toàn bộ nội dung tri thức định dạng Markdown trực quan
├── documents.jsonl         # Danh mục 33 tài liệu nguồn
├── graph.json              # Đồ thị tri thức phân cấp (1670 nodes, 2355 edges)
├── manifest.json           # Thống kê và thông số kỹ thuật bộ dữ liệu
└── README.md               # Tài liệu hướng dẫn sử dụng
```

## 2. Thông số kỹ thuật

- **Mô hình Embedding tối ưu:** `intfloat/multilingual-e5-small` (kích thước vector: 384 dimensions, max tokens: 512).
- **Tổng số chunk:** 1681 chunks.
- **Độ dài chunk:** Trung bình 857.0 ký tự (tối đa 999 ký tự).
- **Tỷ lệ token an toàn:** 100% chunk < 512 tokens (không bị cắt cụt/truncate).

## 3. Cách sử dụng với SentenceTransformers

```python
import json
from sentence_transformers import SentenceTransformer

# 1. Khởi tạo mô hình
model = SentenceTransformer("intfloat/multilingual-e5-small")

# 2. Đọc dữ liệu
with open("production-knowledge/retrieval_chunks.jsonl", "r", encoding="utf-8") as f:
    chunks = [json.loads(line) for line in f]

# 3. Tạo Vector Embeddings cho tài liệu (đã có sẵn prefix 'passage: ')
texts = [c["embedding_text"] for c in chunks]
embeddings = model.encode(texts, batch_size=64, normalize_embeddings=True)

# 4. Tìm kiếm với câu hỏi người dùng (BẮT BUỘC thêm prefix 'query: ')
user_query = "cách phòng trừ rầy chổng cánh hại bưởi"
query_vec = model.encode(f"query: {user_query}", normalize_embeddings=True)
```

## 4. Metadata và Citation nguồn

Mỗi chunk đều có thông tin định danh:
- `source_file`: Tên tài liệu gốc
- `source_pages`: Số trang trong tài liệu
- `section_title`: Đề mục
- `metadata`: Các trường thực thể (`crop`, `disease`, `pest`, `product_name`, `active_ingredient`) dùng để pre-filter metadata trong Vector DB.
