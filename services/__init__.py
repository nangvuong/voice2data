"""Services package for Voice-to-Data VietGAP.

Organized into subpackages:
- services.engines: Core AI inference engines (STT, TTS, OCR, VLM, LLM, Gemini, Embedding)
- services.audio: Realtime audio processing, streaming STT, audio conversion, dictionary
- services.rag: Agricultural knowledge retrieval, RAG engine, safety checker
- services.vietgap: Domain rules, material matching, regex parser
- services.normalizer: Normalization pipeline (dates, numbers, entities, tables)
- services.exporters: Export VietGAP diaries to Excel/PDF/Word
- services.database: MySQL/SQLite persistence layer
"""

# 1. AI Engines
from .engines import (
    stt_engine,
    STTEngine,
    STTError,
    tts_engine,
    TTSEngine,
    TTSError,
    ocr_engine,
    OCREngine,
    OCRError,
    OCRLine,
    OCRResult,
    vlm_engine,
    VLMEngine,
    VLMEngineError,
    VLMAnalysisResult,
    PestDiseaseDiagnosis,
    PesticideDetection,
    FertilizerDetection,
    llm_engine,
    LLMEngine,
    LLMError,
    gemini_engine,
    GeminiEngine,
    GeminiEngineError,
    EmbeddingEngine,
)

# 2. Audio & Speech Processing
from .audio import (
    realtime_stt_engine,
    RealtimeSTTEngine,
    resample_to_16k_mono_pcm16,
    AudioConversionError,
    hotwords_manager,
    HotwordsManager,
    stt_records,
    STTRecords,
    tts_app,
)

# 3. RAG & Knowledge
from .rag import (
    rag_engine,
    RAGEngine,
    rag_bp,
    RAGKnowledgeBase,
    safety_checker,
    SafetyChecker,
    SafetyQueryResult,
)

# 4. VietGAP Domain
from .vietgap import (
    material_matcher,
    MaterialMatcher,
    MaterialMatchResult,
    check_banned_ingredient,
    parse_with_regex,
)

# 5. Database Layer
from .database import (
    get_db_connection,
    save_diary_to_mysql,
    get_diary_records,
)

__all__ = [
    # Engines
    "stt_engine",
    "STTEngine",
    "STTError",
    "tts_engine",
    "TTSEngine",
    "TTSError",
    "ocr_engine",
    "OCREngine",
    "OCRError",
    "OCRLine",
    "OCRResult",
    "vlm_engine",
    "VLMEngine",
    "VLMEngineError",
    "VLMAnalysisResult",
    "PestDiseaseDiagnosis",
    "PesticideDetection",
    "FertilizerDetection",
    "llm_engine",
    "LLMEngine",
    "LLMError",
    "gemini_engine",
    "GeminiEngine",
    "GeminiEngineError",
    "EmbeddingEngine",
    # Audio
    "realtime_stt_engine",
    "RealtimeSTTEngine",
    "resample_to_16k_mono_pcm16",
    "AudioConversionError",
    "hotwords_manager",
    "HotwordsManager",
    "stt_records",
    "STTRecords",
    "tts_app",
    # RAG
    "rag_engine",
    "RAGEngine",
    "rag_bp",
    "RAGKnowledgeBase",
    "safety_checker",
    "SafetyChecker",
    "SafetyQueryResult",
    # VietGAP
    "material_matcher",
    "MaterialMatcher",
    "MaterialMatchResult",
    "check_banned_ingredient",
    "parse_with_regex",
    # Database
    "get_db_connection",
    "save_diary_to_mysql",
    "get_diary_records",
]
