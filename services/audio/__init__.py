"""Audio & Realtime Speech Streaming Processing Package."""

from .audio_converter import AudioConversionError, resample_to_16k_mono_pcm16
from .realtime_stt import RealtimeSTTEngine, realtime_stt_engine
from .hotwords_manager import HotwordsManager, hotwords_manager
from .stt_records import STTRecords, stt_records
from .tts_server import app as tts_app

__all__ = [
    "AudioConversionError",
    "resample_to_16k_mono_pcm16",
    "RealtimeSTTEngine",
    "realtime_stt_engine",
    "HotwordsManager",
    "hotwords_manager",
    "STTRecords",
    "stt_records",
    "tts_app",
]
