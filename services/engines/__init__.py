"""AI Inference Engines Package (NPU / ONNX / Local / Cloud)."""

from .stt_engine import STTEngine, STTError, stt_engine, update_onnx_hotwords
from .tts_engine import TTSEngine, TTSError, AudioResult, tts_engine
from .ocr_engine import OCREngine, OCRError, OCRLine, OCRResult, ocr_engine
from .vlm_engine import (
    VLMEngine,
    VLMEngineError,
    VLMAnalysisResult,
    PestDiseaseDiagnosis,
    PesticideDetection,
    FertilizerDetection,
    vlm_engine,
)
from .llm_engine import LLMEngine, LLMError, llm_engine
from .gemini_engine import GeminiEngine, GeminiEngineError, gemini_engine
from .embedding_engine import EmbeddingEngine

__all__ = [
    "STTEngine",
    "STTError",
    "stt_engine",
    "update_onnx_hotwords",
    "TTSEngine",
    "TTSError",
    "AudioResult",
    "tts_engine",
    "OCREngine",
    "OCRError",
    "OCRLine",
    "OCRResult",
    "ocr_engine",
    "VLMEngine",
    "VLMEngineError",
    "VLMAnalysisResult",
    "PestDiseaseDiagnosis",
    "PesticideDetection",
    "FertilizerDetection",
    "vlm_engine",
    "LLMEngine",
    "LLMError",
    "llm_engine",
    "GeminiEngine",
    "GeminiEngineError",
    "gemini_engine",
    "EmbeddingEngine",
]
