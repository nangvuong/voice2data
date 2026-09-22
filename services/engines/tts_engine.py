"""
TTS Engine - load model VITS Piper (vi_VN-vais1000-medium) MỘT LẦN khi khởi động,
sau đó xử lý tất cả request tuần tự thông qua một hàng đợi (asyncio.Queue).

Kiến trúc tương tự stt_engine.py:
- Chỉ có 1 tts instance dùng chung cho toàn bộ app.
- Một worker duy nhất (background task) liên tục lấy job từ queue,
  chạy inference trong ThreadPoolExecutor (1 worker), rồi trả kết quả về thông qua Future.
- Nhiều request đến cùng lúc (từ Telegram bot, Web API, CLI) sẽ tự động được xếp hàng,
  xử lý lần lượt, không có race condition, không làm nghẽn CPU/RAM trên Odroid RK3588.
- Model sử dụng: /mnt/data/vits-piper-vi_VN-vais1000-medium
- Hỗ trợ 2 backend:
  + 'sherpa': sherpa-onnx (C++ runtime siêu nhanh, mặc định)
  + 'piper': piper-tts (thư viện gốc của Piper chạy trên ONNX Runtime)
- Hỗ trợ xuất định dạng: WAV, MP3 (libmp3lame), OGG/Opus (chuẩn voice Telegram).
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import io
import logging
import os
import re
import subprocess
import time
from typing import List, Literal, Optional, Tuple, Union
import wave

import numpy as np
import sherpa_onnx

try:
    from piper import PiperVoice
    from piper.config import SynthesisConfig
    PIPER_AVAILABLE = True
except ImportError:
    PIPER_AVAILABLE = False
    PiperVoice = None
    SynthesisConfig = None

import config

logger = logging.getLogger(__name__)

# Thư mục chứa model mặc định
DEFAULT_MODEL_DIR = getattr(config, "TTS_MODEL_DIR", "/mnt/data/vits-piper-vi_VN-vais1000-medium")
DEFAULT_TIMEOUT_SEC = getattr(config, "TTS_TIMEOUT_SEC", 120)


class TTSError(Exception):
    """Lỗi phát sinh trong quá trình chuyển văn bản thành giọng nói (TTS)."""


@dataclass
class AudioResult:
    """Kết quả sinh âm thanh từ TTS."""
    samples: np.ndarray      # Mảng float32 [-1.0, 1.0]
    sample_rate: int         # Tần số lấy mẫu (22050 Hz)
    duration_sec: float      # Thời lượng âm thanh (giây)
    elapsed_sec: float       # Thời gian xử lý inference (giây)
    rtf: float               # Real-Time Factor (thấp hơn 1.0 là nhanh hơn thời gian thực)
    engine_name: str         # 'sherpa-onnx' hoặc 'piper-tts'

    def to_int16(self) -> np.ndarray:
        """Chuyển mảng samples về dạng int16 PCM."""
        arr = np.asarray(self.samples)
        if arr.dtype == np.int16:
            return arr
        clipped = np.clip(arr, -1.0, 1.0)
        return (clipped * 32767).astype(np.int16)

    def to_wav_bytes(self) -> bytes:
        """Xuất audio ra bytes chuẩn WAV PCM 16-bit Mono."""
        int16_data = self.to_int16()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(int16_data.tobytes())
        return buf.getvalue()

    def save(self, output_path: str, format: Optional[str] = None) -> str:
        """Lưu audio ra file với định dạng wav, mp3, hoặc ogg (Opus)."""
        ext = format.lower() if format else os.path.splitext(output_path)[1].lstrip(".").lower()
        if not ext:
            ext = "wav"

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        if ext == "wav":
            int16_data = self.to_int16()
            with wave.open(output_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
                wf.writeframes(int16_data.tobytes())
            return output_path

        # Chuyển đổi định dạng qua ffmpeg
        wav_bytes = self.to_wav_bytes()
        cmd = ["ffmpeg", "-y", "-i", "pipe:0"]
        if ext == "mp3":
            cmd.extend(["-c:a", "libmp3lame", "-b:a", "64k", output_path])
        elif ext in ("ogg", "opus"):
            cmd.extend(["-c:a", "libopus", "-b:a", "32k", output_path])
        else:
            cmd.extend([output_path])

        proc = subprocess.run(
            cmd,
            input=wav_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="ignore")
            raise TTSError(f"ffmpeg convert thất bại sang .{ext}: {err}")

        return output_path


@dataclass
class _Job:
    """Một job trong hàng đợi: văn bản, tham số + Future để trả kết quả."""
    text: str
    speed: float
    sid: int
    engine: str
    output_path: Optional[str]
    output_format: str
    return_bytes: bool
    future: "asyncio.Future[Union[str, bytes, AudioResult]]"


# ==============================================================================
# Helper functions: Tiền xử lý & Chuẩn hoá văn bản tiếng Việt
# ==============================================================================

UNIT_MAPPINGS = [
    (r"\b(\d+)\s*(?:kg|kilogram|kí|ký)\b", r"\1 ki-lô-gam"),
    (r"\b(\d+)\s*g\b", r"\1 gam"),
    (r"\b(\d+)\s*(?:ha|hecta)\b", r"\1 héc-ta"),
    (r"\b(\d+)\s*(?:m2|m²)\b", r"\1 mét vuông"),
    (r"\b(\d+)\s*(?:m3|m³)\b", r"\1 mét khối"),
    (r"\b(\d+)\s*km\b", r"\1 ki-lô-mét"),
    (r"\b(\d+)\s*m\b", r"\1 mét"),
    (r"\b(\d+)\s*cm\b", r"\1 xen-ti-mét"),
    (r"\b(\d+)\s*(?:ml|mililit)\b", r"\1 mi-li-lít"),
    (r"\b(\d+)\s*(?:l|lit|lít)\b", r"\1 lít"),
    (r"\b(\d+)\s*(?:°C|oC|độ C)\b", r"\1 độ C"),
    (r"(\d+)\s*%", r"\1 phần trăm"),
    (r"\b(\d+)\s*(?:đ|vnd|vnđ|đồng)\b", r"\1 đồng"),
    (r"\b(\d+)\s*k\b", r"\1 nghìn"),
]

ABBREVIATIONS = [
    (r"\bTS\.\s*", "Tiến sĩ "),
    (r"\bThS\.\s*", "Thạc sĩ "),
    (r"\bBS\.\s*", "Bác sĩ "),
    (r"\bTP\.\s*", "Thành phố "),
    (r"\bTX\.\s*", "Thị xã "),
    (r"\bTT\.\s*", "Thị trấn "),
    (r"\bH\.\s*", "Huyện "),
    (r"\bX\.\s*", "Xã "),
    (r"\bBVTV\b", "Bảo vệ thực vật"),
    (r"\bVietGAP\b", "Việt Gáp"),
]


def normalize_text(text: str) -> str:
    """Làm sạch markdown, mở rộng từ viết tắt và chuẩn hoá đơn vị đo lường."""
    if not text:
        return ""

    # Loại bỏ URL, markdown, html tags
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`[^`]*`", "", text)
    text = re.sub(r"[*_~#>]+", "", text)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)

    # Viết tắt & đơn vị
    for p, r in ABBREVIATIONS:
        text = re.sub(p, r, text, flags=re.IGNORECASE)
    for p, r in UNIT_MAPPINGS:
        text = re.sub(p, r, text, flags=re.IGNORECASE)

    # Bỏ emoji & ký tự lạ
    text = re.sub(r"[\U00010000-\U0010ffff]", "", text)

    # Chuẩn hoá dấu câu lặp
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"!{2,}", "!", text)
    text = re.sub(r"\?{2,}", "?", text)
    text = re.sub(r",+", ",", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)

    return text.strip()


def split_sentences(text: str, max_chars: int = 150) -> List[str]:
    """Tách đoạn văn thành các câu ngắn để inference mượt mà và ngắt nghỉ tự nhiên."""
    text = normalize_text(text)
    if not text:
        return []

    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    sentence_pattern = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-Ỹ0-9])")
    sentences = []

    for paragraph in paragraphs:
        for s in sentence_pattern.split(paragraph):
            s = s.strip()
            if not s:
                continue
            if len(s) > max_chars:
                parts = re.split(r"(?<=[,;])\s+", s)
                chunk = ""
                for part in parts:
                    if len(chunk) + len(part) < max_chars:
                        chunk = (chunk + " " + part).strip()
                    else:
                        if chunk:
                            sentences.append(chunk)
                        chunk = part
                if chunk:
                    sentences.append(chunk)
            else:
                sentences.append(s)

    final_sentences = []
    for s in sentences:
        s = s.strip()
        if s and s[-1] not in ".!?":
            s += "."
        if s:
            final_sentences.append(s)

    return final_sentences


# ==============================================================================
# Lớp TTSEngine chính
# ==============================================================================

class TTSEngine:
    """
    TTSEngine quản lý mô hình giọng nói tiếng Việt vi_VN-vais1000-medium.
    Load model 1 lần khi khởi động, tiếp nhận các tác vụ qua asyncio.Queue
    và thực thi tuần tự trong ThreadPoolExecutor 1 worker.
    """

    def __init__(
        self,
        model_dir: Optional[str] = None,
        default_engine: str = "sherpa",
        num_threads: int = 4,
    ):
        self._model_dir = model_dir or DEFAULT_MODEL_DIR
        self._default_engine = getattr(config, "TTS_ENGINE", default_engine).lower().strip()
        self._num_threads = getattr(config, "TTS_NUM_THREADS", getattr(config, "NUM_THREADS", num_threads))

        self._model_path = os.path.join(self._model_dir, "vi_VN-vais1000-medium.onnx")
        self._tokens_path = os.path.join(self._model_dir, "tokens.txt")
        self._espeak_dir = os.path.join(self._model_dir, "espeak-ng-data")
        self._config_path = os.path.join(self._model_dir, "vi_VN-vais1000-medium.onnx.json")

        self._sherpa_tts: Optional[sherpa_onnx.OfflineTts] = None
        self._piper_voice: Optional[PiperVoice] = None

        logger.info("Khởi tạo TTSEngine (model_dir: %s, default_engine: %s)...", self._model_dir, self._default_engine)
        self._load_sherpa()

        # Hàng đợi job & worker thread
        self._queue: "asyncio.Queue[_Job]" = asyncio.Queue()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-worker")
        self._worker_task: "Optional[asyncio.Task]" = None

    def _load_sherpa(self):
        """Khởi tạo engine sherpa-onnx."""
        if not os.path.exists(self._model_path):
            raise FileNotFoundError(f"Không tìm thấy model TTS ONNX: {self._model_path}")

        t0 = time.time()
        vits_config = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=self._model_path,
            tokens=self._tokens_path,
            data_dir=self._espeak_dir,
            noise_scale=0.667,
            noise_scale_w=0.8,
            length_scale=1.0,
        )
        model_config = sherpa_onnx.OfflineTtsModelConfig(
            vits=vits_config,
            num_threads=self._num_threads,
            debug=0,
        )
        tts_config = sherpa_onnx.OfflineTtsConfig(model=model_config, max_num_sentences=1)
        if not tts_config.validate():
            raise TTSError("Cấu hình sherpa-onnx TTS không hợp lệ.")

        self._sherpa_tts = sherpa_onnx.OfflineTts(tts_config)
        self.sample_rate = self._sherpa_tts.sample_rate
        logger.info("Đã nạp SherpaOnnx TTS thành công (%.3fs, %dHz).", time.time() - t0, self.sample_rate)

    def _load_piper(self):
        """Khởi tạo engine piper-tts (lazy load khi có yêu cầu)."""
        if self._piper_voice is not None:
            return
        if not PIPER_AVAILABLE:
            raise TTSError("Thư viện piper-tts chưa được cài đặt trong môi trường.")

        t0 = time.time()
        self._piper_voice = PiperVoice.load(self._model_path, config_path=self._config_path)
        logger.info("Đã nạp Piper-TTS thành công (%.3fs).", time.time() - t0)

    # --------------------------------------------------------------------------
    # Lifecycle Management
    # --------------------------------------------------------------------------

    def start(self):
        """Khởi động background worker xử lý hàng đợi TTS."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())
            logger.info("TTS worker đã khởi động, sẵn sàng nhận job.")

    async def stop(self):
        """Dừng worker khi tắt ứng dụng."""
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        self._executor.shutdown(wait=False)
        logger.info("TTS worker đã dừng.")

    # --------------------------------------------------------------------------
    # Worker Loop
    # --------------------------------------------------------------------------

    async def _worker_loop(self):
        """Vòng lặp chạy nền lấy job từ hàng đợi và xử lý TUẦN TỰ trong ThreadPoolExecutor."""
        while True:
            job = await self._queue.get()
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    self._executor, self._process_job_sync, job
                )
                if not job.future.cancelled():
                    job.future.set_result(result)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Lỗi khi xử lý TTS job: '%s...'", job.text[:50])
                if not job.future.cancelled():
                    job.future.set_exception(TTSError(str(exc)))
            finally:
                self._queue.task_done()

    def _process_job_sync(self, job: _Job) -> Union[str, bytes, AudioResult]:
        """Thực thi job đồng bộ trong thread riêng của executor."""
        audio_res = self.synthesize_sync(
            text=job.text,
            speed=job.speed,
            sid=job.sid,
            engine=job.engine,
        )

        if job.output_path:
            audio_res.save(job.output_path, format=job.output_format)
            return job.output_path

        if job.return_bytes:
            if job.output_format.lower() == "wav":
                return audio_res.to_wav_bytes()

            # Nén sang mp3/ogg qua ffmpeg
            wav_bytes = audio_res.to_wav_bytes()
            fmt = job.output_format.lower()
            cmd = ["ffmpeg", "-y", "-i", "pipe:0"]
            if fmt == "mp3":
                cmd.extend(["-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"])
            elif fmt in ("ogg", "opus"):
                cmd.extend(["-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1"])
            else:
                cmd.extend(["-f", fmt, "pipe:1"])

            proc = subprocess.run(
                cmd,
                input=wav_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if proc.returncode != 0:
                raise TTSError(f"ffmpeg convert bytes lỗi: {proc.stderr.decode(errors='ignore')}")
            return proc.stdout

        return audio_res

    # --------------------------------------------------------------------------
    # Inference core (Synchronous)
    # --------------------------------------------------------------------------

    def synthesize_sync(
        self,
        text: str,
        speed: float = 1.0,
        sid: int = 0,
        engine: Optional[str] = None,
        pause_sec: float = 0.15,
    ) -> AudioResult:
        """
        Sinh âm thanh đồng bộ từ văn bản.
        Tự động tách câu và chèn khoảng lặng tự nhiên giữa các câu.
        """
        clean_text = normalize_text(text)
        if not clean_text:
            raise TTSError("Văn bản sau chuẩn hoá bị rỗng.")

        sentences = split_sentences(clean_text)
        if not sentences:
            sentences = [clean_text]

        eng_choice = (engine or self._default_engine).lower()
        t0 = time.time()
        audio_chunks: List[np.ndarray] = []
        silence_samples = np.zeros(int(pause_sec * self.sample_rate), dtype=np.float32)

        if "piper" in eng_choice:
            self._load_piper()
            eff_speed = max(0.2, min(5.0, speed))
            syn_cfg = SynthesisConfig(speaker_id=sid, length_scale=1.0 / eff_speed)

            for i, s in enumerate(sentences):
                chunks = list(self._piper_voice.synthesize(s, syn_config=syn_cfg))
                if chunks:
                    raw_bytes = b"".join(c.audio_int16_bytes for c in chunks)
                    int16_arr = np.frombuffer(raw_bytes, dtype=np.int16)
                    float32_arr = int16_arr.astype(np.float32) / 32767.0
                    if len(float32_arr) > 0:
                        if i > 0 and len(audio_chunks) > 0:
                            audio_chunks.append(silence_samples)
                        audio_chunks.append(float32_arr)
            engine_name = "piper-tts"
        else:
            # Dùng sherpa-onnx (mặc định)
            for i, s in enumerate(sentences):
                gen = self._sherpa_tts.generate(s, sid=sid, speed=speed)
                chunk_arr = np.asarray(gen.samples, dtype=np.float32)
                if len(chunk_arr) > 0:
                    if i > 0 and len(audio_chunks) > 0:
                        audio_chunks.append(silence_samples)
                    audio_chunks.append(chunk_arr)
            engine_name = "sherpa-onnx"

        if not audio_chunks:
            raise TTSError("Không tạo được âm thanh từ văn bản đầu vào.")

        combined = np.concatenate(audio_chunks) if len(audio_chunks) > 1 else audio_chunks[0]
        elapsed = time.time() - t0
        duration = len(combined) / self.sample_rate
        rtf = elapsed / duration if duration > 0 else 0.0

        return AudioResult(
            samples=combined,
            sample_rate=self.sample_rate,
            duration_sec=duration,
            elapsed_sec=elapsed,
            rtf=rtf,
            engine_name=engine_name,
        )

    # --------------------------------------------------------------------------
    # Public Asynchronous Interface
    # --------------------------------------------------------------------------

    async def synthesize(
        self,
        text: str,
        output_path: Optional[str] = None,
        speed: float = 1.0,
        sid: int = 0,
        format: str = "wav",
        engine: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SEC,
    ) -> Union[str, AudioResult]:
        """
        Cổng vào public chính: Đẩy job TTS vào queue và đợi kết quả.
        - Nếu có output_path: Lưu ra file và trả về đường dẫn file.
        - Nếu không có output_path: Trả về đối tượng AudioResult.
        """
        self.start()

        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Union[str, bytes, AudioResult]]" = loop.create_future()

        job = _Job(
            text=text,
            speed=speed,
            sid=sid,
            engine=engine or self._default_engine,
            output_path=output_path,
            output_format=format,
            return_bytes=False,
            future=future,
        )

        await self._queue.put(job)
        logger.info("Đã nhận TTS job: '%s...' (hàng đợi: %d)", text[:30], self._queue.qsize())

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            future.cancel()
            raise TTSError(f"TTS xử lý quá thời gian cho phép ({timeout}s).")

    async def synthesize_to_bytes(
        self,
        text: str,
        format: str = "wav",
        speed: float = 1.0,
        sid: int = 0,
        engine: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SEC,
    ) -> bytes:
        """Đẩy job vào hàng đợi và nhận lại dữ liệu dạng bytes (wav, mp3, ogg)."""
        self.start()

        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Union[str, bytes, AudioResult]]" = loop.create_future()

        job = _Job(
            text=text,
            speed=speed,
            sid=sid,
            engine=engine or self._default_engine,
            output_path=None,
            output_format=format,
            return_bytes=True,
            future=future,
        )

        await self._queue.put(job)

        try:
            res = await asyncio.wait_for(future, timeout=timeout)
            return res  # bytes
        except asyncio.TimeoutError:
            future.cancel()
            raise TTSError(f"TTS xử lý quá thời gian cho phép ({timeout}s).")


# Singleton dùng chung toàn ứng dụng (tương tự stt_engine trong services/stt_engine.py)
tts_engine = TTSEngine()
