"""
Package exporters: Cung cấp các dịch vụ xuất báo cáo nhật ký canh tác và hồ sơ VietGAP
ra 3 định dạng: Excel (.xlsx), Word (.docx) và PDF (.pdf).

Gồm 2 exporter chính:
1. cultivation_diary_exporter: Xuất sổ Nhật ký canh tác (theo mẫu vườn cà gai leo: 11 trường thông tin chung + bảng 8 cột theo ngày).
2. vietgap_table_exporter: Xuất toàn bộ biểu mẫu VietGAP (Bìa 1.1-1.2, Mua vật tư 1.5, Sử dụng phân thuốc 1.6, Xử lý bao bì 1.7, Thu hoạch & Tiêu thụ 1.8).
"""

from services.exporters.cultivation_diary_exporter import (
    get_available_seasons,
    get_cultivation_diary_export_data,
    build_cultivation_diary_workbook,
    build_cultivation_diary_docx,
    build_cultivation_diary_pdf,
    export_cultivation_diary_to_bytes,
    export_cultivation_diary_to_file,
    export_cultivation_diary_to_docx_bytes,
    export_cultivation_diary_to_docx_file,
    export_cultivation_diary_to_pdf_bytes,
    export_cultivation_diary_to_pdf_file,
    # Aliases
    export_cultivation_diary_to_excel,
    export_cultivation_diary_to_excel_bytes,
    export_cultivation_diary_to_excel_file,
    export_cultivation_diary_to_docx,
    export_cultivation_diary_to_pdf,
    build_cultivation_diary_excel_workbook,
    build_cultivation_diary_docx_document,
    build_cultivation_diary_pdf_document,
    _init_fonts,
    NumberedCanvas,
)

from services.exporters.vietgap_table_exporter import (
    get_vietgap_full_tables_data,
    get_vietgap_tables_export_data,
    build_vietgap_tables_workbook,
    build_vietgap_tables_docx,
    build_vietgap_tables_pdf,
    export_vietgap_tables_excel_bytes,
    export_vietgap_tables_excel_file,
    export_vietgap_tables_docx_bytes,
    export_vietgap_tables_docx_file,
    export_vietgap_tables_pdf_bytes,
    export_vietgap_tables_pdf_file,
    # Aliases
    export_vietgap_tables_to_excel,
    export_vietgap_tables_to_excel_bytes,
    export_vietgap_tables_to_excel_file,
    export_vietgap_tables_to_docx,
    export_vietgap_tables_to_docx_bytes,
    export_vietgap_tables_to_docx_file,
    export_vietgap_tables_to_pdf,
    export_vietgap_tables_to_pdf_bytes,
    export_vietgap_tables_to_pdf_file,
    build_vietgap_excel_workbook,
    build_vietgap_docx_document,
    build_vietgap_pdf_document,
)

__all__ = [
    # Cultivation Diary
    "get_available_seasons",
    "get_cultivation_diary_export_data",
    "build_cultivation_diary_workbook",
    "build_cultivation_diary_docx",
    "build_cultivation_diary_pdf",
    "export_cultivation_diary_to_bytes",
    "export_cultivation_diary_to_file",
    "export_cultivation_diary_to_docx_bytes",
    "export_cultivation_diary_to_docx_file",
    "export_cultivation_diary_to_pdf_bytes",
    "export_cultivation_diary_to_pdf_file",
    "export_cultivation_diary_to_excel",
    "export_cultivation_diary_to_excel_bytes",
    "export_cultivation_diary_to_excel_file",
    "export_cultivation_diary_to_docx",
    "export_cultivation_diary_to_pdf",
    "build_cultivation_diary_excel_workbook",
    "build_cultivation_diary_docx_document",
    "build_cultivation_diary_pdf_document",
    "_init_fonts",
    "NumberedCanvas",
    # VietGAP
    "get_vietgap_full_tables_data",
    "get_vietgap_tables_export_data",
    "build_vietgap_tables_workbook",
    "build_vietgap_tables_docx",
    "build_vietgap_tables_pdf",
    "export_vietgap_tables_excel_bytes",
    "export_vietgap_tables_excel_file",
    "export_vietgap_tables_docx_bytes",
    "export_vietgap_tables_docx_file",
    "export_vietgap_tables_pdf_bytes",
    "export_vietgap_tables_pdf_file",
    "export_vietgap_tables_to_excel",
    "export_vietgap_tables_to_excel_bytes",
    "export_vietgap_tables_to_excel_file",
    "export_vietgap_tables_to_docx",
    "export_vietgap_tables_to_docx_bytes",
    "export_vietgap_tables_to_docx_file",
    "export_vietgap_tables_to_pdf",
    "export_vietgap_tables_to_pdf_bytes",
    "export_vietgap_tables_to_pdf_file",
    "build_vietgap_excel_workbook",
    "build_vietgap_docx_document",
    "build_vietgap_pdf_document",
]
