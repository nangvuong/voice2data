"""
Module Normalizer - Chuẩn hóa dữ liệu nhật ký hoạt động canh tác.
"""

from services.normalizer.dates import (
    parse_date,
    resolve_spoken_date,
    strip_expiry_phrase,
    extract_spoken_expiry_date,
)
from services.normalizer.activity_normalizer import (
    normalize_activity_json,
    normalize_activity_list,
    normalize_for_webapp,
    normalize_relative_date,
    normalize_unit,
)

from services.normalizer.core.tokenizer import Tokenizer
from services.normalizer.core.parser import NumberParser
from services.normalizer.db_classifier import (
    classify_into_tables,
    normalize_db_tables,
    tables_to_flat_dict,
    to_iso_date,
)
from services.normalizer.vietgap_normalizer import (
    normalize_vietgap_tables,
    normalize_quantity_and_unit,
    clean_material_name,
    clean_work_name,
)

def normalize_text(text: str) -> str:
    """Chuẩn hóa văn bản (vd: chuyển chữ số tiếng Việt thành số Ả Rập và chuẩn hóa đơn vị)."""
    if not text:
        return text
    tokens = Tokenizer.tokenize(text)
    return NumberParser.evaluate_number_tokens(tokens)

__all__ = [
    "parse_date",
    "resolve_spoken_date",
    "normalize_activity_json",
    "normalize_activity_list",
    "normalize_for_webapp",
    "normalize_relative_date",
    "normalize_unit",
    "normalize_text",
    "normalize_quantity_and_unit",
    "clean_material_name",
    "clean_work_name",
    "classify_into_tables",
    "normalize_db_tables",
    "normalize_vietgap_tables",
    "tables_to_flat_dict",
    "to_iso_date",
    "strip_expiry_phrase",
    "extract_spoken_expiry_date",
]


