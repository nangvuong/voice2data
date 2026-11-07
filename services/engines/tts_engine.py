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
- Tích hợp từ điển phiên âm tiếng Việt tự động cho toàn bộ danh mục thuốc BVTV,
  phân bón, hoạt chất và công thức hóa học trong CSDL MySQL (vietgap_diary).
- Tích hợp DSP Audio Post-Processing:
  + Parametric EQ giảm chói (cắt dải 2.8-4.2 kHz & sibilance 5.8 kHz, tăng ấm 210 Hz, cắt sub-bass < 80 Hz).
  + Kiểm tra clipping gốc & chống clipping do khuếch đại bằng Lookahead Envelope Limiter + Soft-Knee Ceiling (-1.5 dBFS).
  + Chuẩn hóa âm lượng (Loudness Normalization) theo K-weighted active speech LUFS (-19.0 LUFS), tránh âm lượng quá lớn.
  + Kiểm tra sample rate và resampling băng thông giới hạn (Band-limited Sinc Resampling 22050 Hz -> 44100 Hz + libsoxr).
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import io
import json
import logging
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple, Union
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
DEFAULT_MODEL_SAMPLE_RATE = int(getattr(config, "TTS_SAMPLE_RATE", 22050))
DEFAULT_OUTPUT_SAMPLE_RATE = int(getattr(config, "TTS_OUTPUT_SAMPLE_RATE", 44100))
DEFAULT_TARGET_LUFS = float(getattr(config, "TTS_TARGET_LUFS", -19.0))
DEFAULT_PEAK_CEILING_DB = float(getattr(config, "TTS_PEAK_CEILING_DB", -1.5))
DEFAULT_EQ_ENABLED = bool(getattr(config, "TTS_EQ_ENABLED", True))


class TTSError(Exception):
    """Lỗi phát sinh trong quá trình chuyển văn bản thành giọng nói (TTS)."""


@dataclass
class AudioResult:
    """Kết quả sinh âm thanh từ TTS kèm thông số kiểm định chất lượng âm thanh."""
    samples: np.ndarray                 # Mảng float32 [-1.0, 1.0] đã qua hậu xử lý DSP
    sample_rate: int                    # Tần số lấy mẫu đầu ra (mặc định 44100 Hz)
    duration_sec: float                 # Thời lượng âm thanh (giây)
    elapsed_sec: float                  # Thời gian xử lý inference + DSP (giây)
    rtf: float                          # Real-Time Factor (thấp hơn 1.0 là nhanh hơn thời gian thực)
    engine_name: str                    # 'sherpa-onnx' hoặc 'piper-tts'
    raw_sample_rate: int = 22050        # Tần số lấy mẫu gốc của model VITS (22050 Hz)
    peak_dbfs: float = -1.5             # Đỉnh biên độ sau chuẩn hóa (dBFS, luôn <= ceiling)
    loudness_lufs: float = -19.0        # Độ lớn âm lượng cảm nhận (K-weighted LUFS)
    gain_applied_db: float = 0.0        # Mức khuếch đại / giảm âm lượng đã áp dụng (dB)
    raw_clipped_samples: int = 0        # Số mẫu bị chạm ngưỡng clip trước xử lý
    post_clipped_samples: int = 0       # Số mẫu bị clip sau xử lý (luôn = 0)

    def to_int16(self) -> np.ndarray:
        """
        Chuyển mảng samples float32 về dạng int16 PCM có TPDF dithering nhẹ
        và đảm bảo tuyệt đối không bị tràn số (hard-clipping).
        """
        arr = np.asarray(self.samples)
        if arr.dtype == np.int16:
            return arr

        arr_f32 = arr.astype(np.float32, copy=False)
        # TPDF dither ở mức +/- 0.5 LSB (1 / 65536) giúp âm thanh đuôi câu tự nhiên, không bị méo lượng tử
        rng = np.random.default_rng(seed=42)
        dither = (rng.random(len(arr_f32), dtype=np.float32) - rng.random(len(arr_f32), dtype=np.float32)) * (1.0 / 65536.0)
        dithered = np.clip(arr_f32 + dither, -0.999969, 0.999969)
        return np.round(dithered * 32767.0).astype(np.int16)

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

    def save_wav(self, output_path: str) -> str:
        """Lưu trực tiếp ra file WAV PCM 16-bit Mono."""
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        int16_data = self.to_int16()
        with wave.open(output_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(int16_data.tobytes())
        return output_path

    def export(self, output_path: str, format: Optional[str] = None) -> str:
        """Alias tương thích cho tts_server.py để xuất ra định dạng bất kỳ."""
        return self.save(output_path, format=format)

    def save(self, output_path: str, format: Optional[str] = None) -> str:
        """Lưu audio ra file với định dạng wav, mp3, hoặc ogg (Opus) với bộ lấy mẫu soxr chất lượng cao."""
        ext = format.lower() if format else os.path.splitext(output_path)[1].lstrip(".").lower()
        if not ext:
            ext = "wav"

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        if ext == "wav":
            return self.save_wav(output_path)

        # Chuyển đổi định dạng qua ffmpeg sử dụng libsoxr resampler để giữ nguyên độ tự nhiên
        wav_bytes = self.to_wav_bytes()
        cmd = ["ffmpeg", "-y", "-i", "pipe:0"]
        if ext == "mp3":
            cmd.extend([
                "-af", "aresample=44100:resampler=soxr:precision=28:dither_method=triangular",
                "-ar", "44100",
                "-c:a", "libmp3lame",
                "-q:a", "2",
                output_path,
            ])
        elif ext in ("ogg", "opus"):
            # libopus hoạt động chuẩn ở 48000 Hz
            cmd.extend([
                "-af", "aresample=48000:resampler=soxr:precision=28",
                "-ar", "48000",
                "-c:a", "libopus",
                "-b:a", "48k",
                "-vbr", "on",
                "-application", "voip",
                output_path,
            ])
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
# 1. TỪ ĐIỂN PHIÊN ÂM THUỐC BVTV, PHÂN BÓN & HOẠT CHẤT TRONG CSDL (VIETGAP_DIARY)
# ==============================================================================
# Lưu ý kỹ thuật: Model vi_VN-vais1000-medium sử dụng espeak-ng (voice 'vi').
# Khi gặp tên tiếng Anh / Latinh hoặc từ viết hoa (như Confidor, NOFARA, OGRANIC,
# Chlorfenapyr, Diafenthiuron), espeak-ng sẽ đánh vần từng chữ cái (C-O-N-F-I-D-O-R)
# hoặc sinh âm vị tiếng Anh (ɹ, θ, əʊ, ɒ) không khớp tập dữ liệu huấn luyện vais1000,
# gây méo tiếng và chói gắt. Ngoài ra, các âm tiết tiếng Việt cần cách nhau bằng
# khoảng trắng (thay vì dấu gạch ngang) và tách cụm phụ âm 'pr', 'cr', 'tr', 'dr', 'st'
# thành 'pờ rô', 'cờ rô', 'sờ' để espeak-ng sinh đúng mã âm vị tiếng Việt tự nhiên.

# 1A. Phiên âm tên thương phẩm đầy đủ của 24 thuốc BVTV & 16 phân bón trong CSDL
DB_PRODUCT_FULL_PHONETICS: List[Tuple[str, str]] = [
    # --- 24 Thuốc BVTV trong bảng `pesticides` ---
    (r"\bAnvil\s+5\s*SC\b", "An vin 5 ét xê"),
    (r"\bChess\s+50\s*WG\b", "Chét 50 vê kép gờ"),
    (r"\bRidomil\s+Gold\s+68\s*WG\b", "Ri đô min Gôn 68 vê kép gờ"),
    (r"\bConfidor\s+200\s*SL\b", "Con phi đo 200 ét lờ"),
    (r"\bSpendora\s+690\s*WP\b", "Sờ pen đô ra 690 vê kép pê"),
    (r"\bEpolists\s+85\s*WP\b", "Ê pô lít 85 vê kép pê"),
    (r"\bKasuduc\s+3\s*SL\b", "Ka su đức 3 ét lờ"),
    (r"\bAmity\s+Top\s+500\s*SC\b", "A mi ti Tóp 500 ét xê"),
    (r"\bTilbest\s+super\s+300\s*EC\b", "Tin bét su pơ 300 ê xê"),
    (r"\bFORTHANE\s+80\s*WP\b", "Pho than 80 vê kép pê"),
    (r"\bMexyl\s+MZ\s+72\s*WP\s+Xanh\s+Gold\b", "Mê xin mờ dét 72 vê kép pê Xanh Gôn"),
    (r"\bMexyl\s+MZ\s+72\s*WP\b", "Mê xin mờ dét 72 vê kép pê"),
    (r"\bINCOPPER\s+85\s*WP\b", "In cốp pơ 85 vê kép pê"),
    (r"\bDosay\s+45\s*WP\b", "Đô say 45 vê kép pê"),
    (r"\bLION\s+RAY\s+200\s*SC\b", "Lai ơn Rây 200 ét xê"),
    (r"Mãng\s+Xà\s*\(\s*Masterpyr\s+240\s*SC\s*\)", "Mãng Xà Mát tơ pi 240 ét xê"),
    (r"\bMasterpyr\s+240\s*SC\b", "Mát tơ pi 240 ét xê"),
    (r"\bMovento\s+150\s*OD\b", "Mô ven tô 150 ô đê"),
    (r"\bNOFARA\s+35\s*WG\b", "Nô pha ra 35 vê kép gờ"),
    (r"Rệp\s+Vương\s*\(\s*Acetat\s+250\s*EC\s*\)", "Rệp Vương A xê tát 250 ê xê"),
    (r"\bAcetat\s+250\s*EC\b", "A xê tát 250 ê xê"),
    (r"\bPesieu\s+500\s*SC\b", "Pê siêu 500 ét xê"),
    (r"\bBP\.?\s*Newone\s+500\s*SC\b", "Bê Pê Niu oăn 500 ét xê"),
    (r"\bDầu\s+khoáng\s+Ds\s+98[.,]8\s*EC\b", "Dầu khoáng Đê ét 98 phẩy 8 ê xê"),
    (r"\bDs\s+98[.,]8\s*EC\b", "Đê ét 98 phẩy 8 ê xê"),
    (r"\bBILLADEN\s+50\s*WG\b", "Bin la đen 50 vê kép gờ"),
    (r"\bTumazine\s+50\s*WP\b", "Tu ma din 50 vê kép pê"),

    # --- 16 Phân bón trong bảng `fertilizers` ---
    (r"\bNPK\s+16\s*-\s*16\s*-\s*8\s+Đầu\s+Trâu\b", "Nờ Pê Ka 16 16 8 Đầu Trâu"),
    (r"\bDAP\s+18\s*-\s*46\s*-\s*0\b", "Đê A Pê 18 46 0"),
    (r"\bNPK\s+20\s*-\s*20\s*-\s*15\s+TE\b", "Nờ Pê Ka 20 20 15 Tê E"),
    (r"Canxi\s+Bo\s*-\s*Silic\s*\(\s*FUNO\s*-\s*PN\s*01\s*\)", "Can xi Bo Si líc Phu nô Pê Nờ không một"),
    (r"\bCanxi\s+Bo\s*-\s*Silic\b", "Can xi Bo Si líc"),
    (r"\bFUNO\s*-\s*PN\s*01\b", "Phu nô Pê Nờ không một"),
    (r"CANXIBO\s+HỮU\s+CƠ\s*\(\s*V\s*-\s*CABO\s+O[GR]+ANIC\s*\)", "Can xi bo Hữu Cơ Vê Ca Bo Óc ga níc"),
    (r"\bV\s*-\s*CABO\s+O[GR]+ANIC\b", "Vê Ca Bo Óc ga níc"),
    (r"Bơ\s+Max\s*\(\s*BO\s+NSG\s+1\s*\)", "Bo Mắc Bo Nờ Ét Gờ 1"),
    (r"\bBơ\s+Max\b", "Bo Mắc"),
    (r"\bBO\s+NSG\s+1\b", "Bo Nờ Ét Gờ 1"),
    (r"\bPROFARM\s*-\s*TOPMICRO\b", "Pờ rô pham Tóp mai cờ rô"),
    (r"\bFarmproka\s*-\s*V\s*3\s+16\s*-\s*8\s*-\s*34\b", "Pham pờ rô ka Vê 3 16 8 34"),
    (r"\bFarmproka\s*-\s*V\s*3\b", "Pham pờ rô ka Vê 3"),
    (r"\bDeltamicro\s+Hydro\b", "Đen ta mai cờ rô Hi đờ rô"),
    (r"\bSIAMB\s*12\b", "Si am bê 12"),
    (r"\bSUPER\s+LÂN\b", "Su pe Lân"),
    (r"\bTOBA\s+NET\b", "Tô ba Nét"),
]

# 1B. Phiên âm từng từ khóa thuốc BVTV, phân bón, hoạt chất và chất cấm
AGRI_TERM_PHONETICS: List[Tuple[str, str]] = [
    # Tên riêng các loại thuốc & phân bón khi đứng độc lập
    (r"\bRidomil\s+Gold\b", "Ri đô min Gôn"),
    (r"\bAmity\s+Top\b", "A mi ti Tóp"),
    (r"\bAmistar\s+Top\b", "A mi sờ ta Tóp"),
    (r"\bTilbest\s+super\b", "Tin bét su pơ"),
    (r"\bLION\s+RAY\b", "Lai ơn Rây"),
    (r"\bBP\.?\s+Newone\b", "Bê Pê Niu oăn"),
    (r"\bNewone\b", "Niu oăn"),
    (r"\bAnvil\b", "An vin"),
    (r"\bChess\b", "Chét"),
    (r"\bRidomil\b", "Ri đô min"),
    (r"\bConfidor\b", "Con phi đo"),
    (r"\bSpendora\b", "Sờ pen đô ra"),
    (r"\bEpolists\b", "Ê pô lít"),
    (r"\bKasuduc\b", "Ka su đức"),
    (r"\bAmity\b", "A mi ti"),
    (r"\bTilbest\b", "Tin bét"),
    (r"\bForthane\b", "Pho than"),
    (r"\bMexyl\b", "Mê xin"),
    (r"\bIncopper\b", "In cốp pơ"),
    (r"\bDosay\b", "Đô say"),
    (r"\bMasterpyr\b", "Mát tơ pi"),
    (r"\bMovento\b", "Mô ven tô"),
    (r"\bNofara\b", "Nô pha ra"),
    (r"\bAcetat\b", "A xê tát"),
    (r"\bPesieu\b", "Pê siêu"),
    (r"\bBilladen\b", "Bin la đen"),
    (r"\bTumazine\b", "Tu ma din"),
    (r"\bCanxibo\b", "Can xi bo"),
    (r"\bCanxi\b", "Can xi"),
    (r"\bSilic\b", "Si líc"),
    (r"\bFuno\b", "Phu nô"),
    (r"\bOgranic\b", "Óc ga níc"),
    (r"\bOrganic\b", "Óc ga níc"),
    (r"\bProfarm\b", "Pờ rô pham"),
    (r"\bTopmicro\b", "Tóp mai cờ rô"),
    (r"\bFarmproka\b", "Pham pờ rô ka"),
    (r"\bDeltamicro\b", "Đen ta mai cờ rô"),
    (r"\bHydro\b", "Hi đờ rô"),
    (r"\bSiamb\b", "Si am bê"),
    (r"\bToba\b", "Tô ba"),
    (r"\bSuper\b", "Su pơ"),
    (r"\bGold\b", "Gôn"),
    (r"\bMax\b", "Mắc"),

    # Hoạt chất thuốc BVTV trong CSDL & phổ biến
    (r"\bCopper\s+oxychloride\b", "Cốp pơ ô xi cờ lô rua"),
    (r"\bEmamectin\s+benzoate\b", "Ê ma méc tin ben dô át"),
    (r"\bPetroleum\s+Spray\s+Oil\b", "Dầu khoáng Pê trô li um"),
    (r"\bLambda\s*-\s*cyhalothrin\b", "Lam đa xi ha lô thờ rin"),
    (r"\bMetalaxyl\s+M\b", "Mê ta la xin Mờ"),
    (r"\bHexaconazole\b", "Hê xa cô na dôn"),
    (r"\bPymetrozine\b", "Pi mê trô din"),
    (r"\bMetalaxyl\b", "Mê ta la xin"),
    (r"\bMancozeb\b", "Man cô dép"),
    (r"\bImidacloprid\b", "I mi đa cờ lô pờ rít"),
    (r"\bDimethomorph\b", "Đi mê thô móp"),
    (r"\bKasugamycin\b", "Ka su ga mi xin"),
    (r"\bAzoxystrobin\b", "A dốc xi sờ trô bin"),
    (r"\bDifenoconazole\b", "Đi phê nô cô na dôn"),
    (r"\bPropiconazole\b", "Pờ rô pi cô na dôn"),
    (r"\bCymoxanil\b", "Xi mốc xa nin"),
    (r"\bZineb\b", "Di nép"),
    (r"\bChlorfenapyr\b", "Cờ lo phê na pi"),
    (r"\bSpirotetramat\b", "Sờ pi rô tê tra mát"),
    (r"\bThiamethoxam\b", "Thi a mê thốc xam"),
    (r"\bAcetamiprid\b", "A xê ta mi pờ rít"),
    (r"\bDiafenthiuron\b", "Đi a phen thi u ron"),
    (r"\bEmamectin\b", "Ê ma méc tin"),
    (r"\bCyromazine\b", "Xi rô ma din"),
    (r"\bAbamectin\b", "A ba méc tin"),
    (r"\bPaclobutrazol\b", "Pác lô bu tra dôn"),
    (r"\bValidamycin\b", "Va li đa mi xin"),
    (r"\bStreptomycin\b", "Sờ trép tô mi xin"),
    (r"\bTrichoderma\b", "Tri cô đéc ma"),
    (r"\bChaetomium\b", "Chê tô mi um"),
    (r"\bActara\b", "Ắc ta ra"),
    (r"\bRadiant\b", "Ra đi ăng"),
    (r"\bAliette\b", "A li ét"),
    (r"\bAntracol\b", "An tra con"),
    (r"\bNativo\b", "Na ti vô"),
    (r"\bAmistar\b", "A mi sờ ta"),
    (r"\bSyngenta\b", "Xin ghen ta"),
    (r"\bBayer\b", "Bay ơ"),

    # Hoạt chất cấm (Thông tư 75 / Luật BVTV)
    (r"\bChlorpyrifos\s+Ethyl\b", "Cờ lo pi ri phốt Ê tin"),
    (r"\bChlorpyrifos\b", "Cờ lo pi ri phốt"),
    (r"\bMethyl\s+Parathion\b", "Mê thin Pa ra thi on"),
    (r"\bParathion\s+Ethyl\b", "Pa ra thi on Ê tin"),
    (r"\bParathion\b", "Pa ra thi on"),
    (r"\bMethamidophos\b", "Mê tha mi đô phốt"),
    (r"\bMonocrotophos\b", "Mô nô cờ rô tô phốt"),
    (r"\bPhosphamidon\b", "Phốt pha mi đon"),
    (r"\bCarbofuran\b", "Các bô phu ran"),
    (r"\bFuradan\b", "Phu ra đan"),
    (r"\bCuraterr\b", "Cu ra te"),
    (r"\bEndosulfan\b", "En đô sun phan"),
    (r"\bThiodan\b", "Thi ô đan"),
    (r"\bTrichlorfon\b", "Tri cờ lo phon"),
    (r"\bChlorophos\b", "Cờ lo rô phốt"),
    (r"\bDipterex\b", "Đíp tê rếch"),
    (r"\bFipronil\b", "Phi pờ rô nin"),
    (r"\bRegent\b", "Rê ghen"),
    (r"\bGlyphosate\b", "Gờ li phô sát"),
    (r"\bRoundup\b", "Rao đắp"),
    (r"\bParaquat\b", "Pa ra quát"),
    (r"\bGramoxone\b", "Gờ ra mốc xôn"),
    (r"\bWofatox\b", "Vô pha tốc"),
    (r"\bVofatox\b", "Vô pha tốc"),
    (r"\bAzodrin\b", "A dô đờ rin"),
    (r"\bMonitor\b", "Mô ni tơ"),
    (r"\bCaptan\b", "Cáp tan"),
    (r"\bCaptafol\b", "Cáp ta phon"),
    (r"\bLindane\b", "Lin đan"),
    (r"\bDieldrin\b", "Đi en đờ rin"),
    (r"\bAldrin\b", "An đờ rin"),
    (r"\bEndrin\b", "En đờ rin"),
    (r"\bHeptachlor\b", "Hép ta cờ lo"),
    (r"\bChlordane\b", "Cờ lo đan"),
    (r"\bDDT\b", "Đê Đê Tê"),
    (r"\bBHC\b", "Bê Hát Xê"),
    (r"\b2,4,5-T\b", "2 phẩy 4 phẩy 5 Tê"),
    (r"\b2,4-D\b", "2 phẩy 4 Đê"),

    # Phân bón, nguyên tố dinh dưỡng & ký hiệu nông nghiệp
    (r"\bNPK\b", "Nờ Pê Ka"),
    (r"\bDAP\b", "Đê A Pê"),
    (r"\bP2O5\b", "Pê 2 Ô 5"),
    (r"\bK2O\b", "Ka 2 Ô"),
    (r"\bCaO\b", "Can xi ô xít"),
    (r"\bMgO\b", "Ma giê ô xít"),
    (r"\bSiO2\b", "Si líc"),
    (r"\bNAA\b", "Nờ A A"),
    (r"\bGA3\b", "Gờ A 3"),
    (r"\bMolipđen\b", "Mô líp đen"),
    (r"\bMolipden\b", "Mô líp đen"),
    (r"\bMagie\b", "Ma giê"),
    (r"\bCoban\b", "Cô ban"),
    (r"\bCôban\b", "Cô ban"),
    (r"\bHumic\b", "Hu míc"),
    (r"\bFulvic\b", "Phun víc"),
    (r"\bChelate\b", "Chê lát"),
    (r"\bAmino\b", "A mi nô"),
]

# 1C. Dạng chế phẩm thuốc BVTV (Formulations) đi kèm số hoặc độc lập
FORMULATION_MAPPINGS: List[Tuple[str, str]] = [
    (r"\b(\d+(?:[.,]\d+)?)\s*SC\b", r"\1 ét xê"),
    (r"\b(\d+(?:[.,]\d+)?)\s*WG\b", r"\1 vê kép gờ"),
    (r"\b(\d+(?:[.,]\d+)?)\s*WP\b", r"\1 vê kép pê"),
    (r"\b(\d+(?:[.,]\d+)?)\s*SL\b", r"\1 ét lờ"),
    (r"\b(\d+(?:[.,]\d+)?)\s*EC\b", r"\1 ê xê"),
    (r"\b(\d+(?:[.,]\d+)?)\s*OD\b", r"\1 ô đê"),
    (r"\b(\d+(?:[.,]\d+)?)\s*EW\b", r"\1 ê vê kép"),
    (r"\b(\d+(?:[.,]\d+)?)\s*SP\b", r"\1 ét pê"),
    (r"\b(\d+(?:[.,]\d+)?)\s*GR\b", r"\1 gờ rờ"),
    (r"\b(\d+(?:[.,]\d+)?)\s*DF\b", r"\1 đê ép"),
    (r"\b(\d+(?:[.,]\d+)?)\s*FS\b", r"\1 ép ét"),
    (r"\b(\d+(?:[.,]\d+)?)\s*CS\b", r"\1 xê ét"),
    (r"\b(\d+(?:[.,]\d+)?)\s*ME\b", r"\1 mờ ê"),
    (r"\b(\d+(?:[.,]\d+)?)\s*SE\b", r"\1 ét ê"),
    (r"\b(\d+(?:[.,]\d+)?)\s*TE\b", r"\1 Tê E"),
    (r"\bMZ\s+(\d+)\b", r"mờ dét \1"),
]

# 1D. Bộ nhớ đệm danh mục động từ CSDL MySQL (pesticides & fertilizers)
_DB_DYNAMIC_PHONETICS: List[Tuple[re.Pattern, str]] = []
_DB_PHONETICS_LOADED_AT: float = 0.0
_DB_PHONETICS_TTL_SEC: float = 600.0  # Tự động làm mới mỗi 10 phút

_VIET_DIACRITIC_RE = re.compile(r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.IGNORECASE)

_LETTER_SPELL_MAP = {
    "A": "a", "B": "bê", "C": "xê", "D": "đê", "E": "e", "F": "ép",
    "G": "gờ", "H": "hát", "I": "i", "J": "gi", "K": "ka", "L": "lờ",
    "M": "mờ", "N": "nờ", "O": "ô", "P": "pê", "Q": "quy", "R": "rờ",
    "S": "ét", "T": "tê", "U": "u", "V": "vê", "W": "vê kép", "X": "ích",
    "Y": "i", "Z": "dét",
}


def _auto_transcribe_foreign_token(token: str) -> Optional[str]:
    """
    Chuyển đổi tự động một từ tiếng Anh/Latinh nông nghiệp mới (chưa có trong từ điển tĩnh)
    sang âm tiết tiếng Việt để espeak-ng không đánh vần từng chữ cái.
    """
    clean = token.strip("()[]{}.,;:-_/\\")
    if not clean or len(clean) < 2:
        return None
    # Nếu đã có dấu tiếng Việt thì giữ nguyên
    if _VIET_DIACRITIC_RE.search(clean):
        return None
    # Nếu là số thuần túy
    if clean.isdigit():
        return None

    # Nếu là từ viết tắt in hoa ngắn không có nguyên âm (vd: NSG, VNT, TLC, PN)
    if clean.isupper() and len(clean) <= 4 and not re.search(r"[AEIOU]", clean):
        return " ".join(_LETTER_SPELL_MAP.get(ch, ch.lower()) for ch in clean)

    # Kiểm tra các từ tiếng Việt không dấu phổ biến không cần chuyển đổi
    common_vn_no_accent = {
        "phan", "bon", "thuoc", "cay", "hoa", "qua", "trai", "la", "re",
        "xanh", "vang", "do", "den", "trang", "tim", "nau", "hong",
        "sieu", "lan", "dam", "voi", "bot", "nuoc", "dau", "khoang",
        "sinh", "hoc", "huu", "co", "vi", "luong", "bo", "kem", "sat", "dong",
    }
    if clean.lower() in common_vn_no_accent:
        return None

    # Quy tắc phiên âm thực nghiệm Latinh/Anh -> Việt cho tên thuốc/hoạt chất mới
    w = clean.lower()
    w = re.sub(r"ph", "f", w)
    w = re.sub(r"th", "t", w)
    w = re.sub(r"sh", "s", w)
    w = re.sub(r"ch", "c", w)
    w = re.sub(r"ck", "c", w)
    w = re.sub(r"qu", "qu", w)
    w = re.sub(r"^x", "s", w)
    w = re.sub(r"x", "c s", w)
    w = re.sub(r"z", "d", w)
    w = re.sub(r"j", "gi", w)
    w = re.sub(r"w", "v", w)
    # Tách cụm phụ âm đầu tiếng Anh để espeak-ng không đọc âm 'ɹ' tiếng Anh
    w = re.sub(r"\bpr", "pờ r", w)
    w = re.sub(r"\bcr", "cờ r", w)
    w = re.sub(r"\bbr", "bờ r", w)
    w = re.sub(r"\bdr", "đờ r", w)
    w = re.sub(r"\bgr", "gờ r", w)
    w = re.sub(r"\bfr", "phờ r", w)
    w = re.sub(r"\bpl", "pờ l", w)
    w = re.sub(r"\bcl", "cờ l", w)
    w = re.sub(r"\bfl", "phờ l", w)
    w = re.sub(r"\bgl", "gờ l", w)
    w = re.sub(r"\bsp", "sờ p", w)
    w = re.sub(r"\bst", "sờ t", w)
    w = re.sub(r"\bsk", "sờ k", w)
    w = re.sub(r"azole\b", "a dôn", w)
    w = re.sub(r"mycin\b", "mi xin", w)
    w = re.sub(r"strobin\b", "sờ trô bin", w)
    w = re.sub(r"mectin\b", "méc tin", w)
    w = re.sub(r"uron\b", "u ron", w)
    w = re.sub(r"phos\b", "phốt", w)
    w = re.sub(r"zine\b", "din", w)
    w = re.sub(r"nil\b", "nin", w)
    w = re.sub(r"prid\b", "pờ rít", w)
    w = re.sub(r"pyr\b", "pi", w)
    w = re.sub(r"yl\b", "in", w)
    return w


def load_db_pronunciations(force: bool = False) -> List[Tuple[re.Pattern, str]]:
    """
    Đọc danh mục thuốc BVTV (bảng `pesticides`) và phân bón (bảng `fertilizers`)
    trực tiếp từ CSDL MySQL `vietgap_diary` để xây dựng bộ quy tắc phiên âm động
    cho bất kỳ tên vật tư hoặc hoạt chất mới nào được thêm vào CSDL.
    """
    global _DB_DYNAMIC_PHONETICS, _DB_PHONETICS_LOADED_AT
    now = time.time()
    if not force and _DB_PHONETICS_LOADED_AT > 0 and (now - _DB_PHONETICS_LOADED_AT < _DB_PHONETICS_TTL_SEC):
        return _DB_DYNAMIC_PHONETICS

    _DB_PHONETICS_LOADED_AT = now
    dynamic_rules: List[Tuple[re.Pattern, str]] = []

    # Tập hợp các mẫu đã có trong từ điển tĩnh
    known_patterns = set()
    for pat_str, _ in DB_PRODUCT_FULL_PHONETICS + AGRI_TERM_PHONETICS:
        cleaned_pat = re.sub(r"\\b|\\s\*|\\s\+|[\^\$\(\)\[\]\?\.\*\\]", " ", pat_str).lower()
        for tok in cleaned_pat.split():
            if len(tok) >= 2:
                known_patterns.add(tok)

    try:
        import pymysql
        conn_kwargs: Dict[str, Any] = {
            "user": config.MYSQL_USER,
            "password": config.MYSQL_PASSWORD,
            "database": getattr(config, "MYSQL_DATABASE", "vietgap_diary"),
            "charset": "utf8mb4",
            "cursorclass": pymysql.cursors.DictCursor,
            "connect_timeout": 2,
        }
        if getattr(config, "MYSQL_SOCKET", None) and os.path.exists(config.MYSQL_SOCKET):
            conn_kwargs["unix_socket"] = config.MYSQL_SOCKET
        else:
            conn_kwargs["host"] = config.MYSQL_HOST
            conn_kwargs["port"] = config.MYSQL_PORT

        conn = pymysql.connect(**conn_kwargs)
        db_terms: List[str] = []
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT name, active_ingredient FROM pesticides;")
                for r in cur.fetchall():
                    if r.get("name"):
                        db_terms.append(str(r["name"]))
                    if r.get("active_ingredient"):
                        db_terms.append(str(r["active_ingredient"]))

                cur.execute("SELECT name FROM fertilizers;")
                for r in cur.fetchall():
                    if r.get("name"):
                        db_terms.append(str(r["name"]))
        finally:
            conn.close()

        seen_tokens = set()
        for raw_term in db_terms:
            for tok in re.findall(r"[A-Za-zÀ-ỹ]{3,}", raw_term):
                tok_low = tok.lower()
                if tok_low in known_patterns or tok_low in seen_tokens:
                    continue
                if _VIET_DIACRITIC_RE.search(tok):
                    continue
                phonetic = _auto_transcribe_foreign_token(tok)
                if phonetic and phonetic != tok_low:
                    seen_tokens.add(tok_low)
                    dynamic_rules.append((re.compile(r"\b" + re.escape(tok) + r"\b", re.IGNORECASE), phonetic))

        _DB_DYNAMIC_PHONETICS = dynamic_rules
        logger.info(
            "Đã đồng bộ danh mục thuốc & phân bón từ CSDL cho TTS (%d mục gốc, %d quy tắc động bổ sung).",
            len(db_terms),
            len(dynamic_rules),
        )
    except Exception as exc:
        logger.debug("Không thể nạp danh mục phiên âm động từ MySQL (dùng từ điển chuẩn): %s", exc)

    return _DB_DYNAMIC_PHONETICS


# ==============================================================================
# 2. TIỀN XỬ LÝ & CHUẨN HÓA VĂN BẢN TIẾNG VIỆT CHO TTS
# ==============================================================================

UNIT_MAPPINGS = [
    # Đơn vị tỷ lệ pha / nồng độ có dấu gạch chéo
    (r"\b(\d+(?:[.,]\d+)?)\s*g\s*/\s*(?:l|lit|lít)\b", r"\1 gam trên lít"),
    (r"\b(\d+(?:[.,]\d+)?)\s*g\s*/\s*(?:kg|kí|ký)\b", r"\1 gam trên ki lô gam"),
    (r"\b(\d+(?:[.,]\d+)?)\s*mg\s*/\s*(?:l|lit|lít)\b", r"\1 mi li gam trên lít"),
    (r"\b(\d+(?:[.,]\d+)?)\s*ml\s*/\s*(?:l|lit|lít)\b", r"\1 mi li lít trên lít"),
    (r"\b(\d+(?:[.,]\d+)?)\s*ml\s*/\s*bình\b", r"\1 mi li lít trên bình"),
    (r"\b(\d+(?:[.,]\d+)?)\s*g\s*/\s*bình\b", r"\1 gam trên bình"),
    (r"\b(\d+(?:[.,]\d+)?)\s*(?:l|lít)\s*/\s*ha\b", r"\1 lít trên héc ta"),
    (r"\b(\d+(?:[.,]\d+)?)\s*kg\s*/\s*ha\b", r"\1 ki lô gam trên héc ta"),
    (r"\b(\d+(?:[.,]\d+)?)\s*tấn\s*/\s*ha\b", r"\1 tấn trên héc ta"),
    (r"\b(\d+(?:[.,]\d+)?)\s*tấn\s*/\s*năm\b", r"\1 tấn trên năm"),
    (r"\b(\d+(?:[.,]\d+)?)\s*(?:l|lít)\s*/\s*h\b", r"\1 lít trên giờ"),
    (r"\b(\d+)\s*-\s*(\d+)\s*ngày\s*/\s*lần\b", r"\1 đến \2 ngày một lần"),
    (r"\b(\d+)\s*ngày\s*/\s*lần\b", r"\1 ngày một lần"),
    (r"\b(\d+)\s*-\s*(\d+)\s*lần\s*/\s*vụ\b", r"\1 đến \2 lần một vụ"),
    (r"\b(\d+)\s*lần\s*/\s*vụ\b", r"\1 lần một vụ"),
    (r"\bw\s*/\s*w\b", ""),
    (r"\bw\s*/\s*v\b", ""),

    # Đơn vị đo lường chuẩn (dùng khoảng trắng thay vì gạch nối để espeak-ng đọc tách âm tự nhiên)
    (r"\b(\d+(?:[.,]\d+)?)\s*ppm\b", r"\1 pê pê mờ"),
    (r"\bppm\b", "pê pê mờ"),
    (r"\b(\d+(?:[.,]\d+)?)\s*(?:kg|kilogram|kí|ký)\b", r"\1 ki lô gam"),
    (r"\b(\d+(?:[.,]\d+)?)\s*(?:g|gam|gram)\b", r"\1 gam"),
    (r"\b(\d+(?:[.,]\d+)?)\s*mg\b", r"\1 mi li gam"),
    (r"\b(\d+(?:[.,]\d+)?)\s*(?:ha|hecta)\b", r"\1 héc ta"),
    (r"\b(\d+(?:[.,]\d+)?)\s*(?:m2|m²)\b", r"\1 mét vuông"),
    (r"\b(\d+(?:[.,]\d+)?)\s*(?:m3|m³)\b", r"\1 mét khối"),
    (r"\b(\d+(?:[.,]\d+)?)\s*km\b", r"\1 ki lô mét"),
    (r"\b(\d+(?:[.,]\d+)?)\s*cm\b", r"\1 xen ti mét"),
    (r"\b(\d+(?:[.,]\d+)?)\s*mm\b", r"\1 mi li mét"),
    (r"\b(\d+(?:[.,]\d+)?)\s*m\b", r"\1 mét"),
    (r"\b(\d+(?:[.,]\d+)?)\s*(?:ml|mililit|cc)\b", r"\1 mi li lít"),
    (r"\b(\d+(?:[.,]\d+)?)\s*(?:l|lit|lít)\b", r"\1 lít"),
    (r"\b(\d+(?:[.,]\d+)?)\s*(?:°C|oC|độ C)\b", r"\1 độ Xê"),
    (r"(\d+(?:[.,]\d+)?)\s*%", r"\1 phần trăm"),
    (r"\b(\d+(?:[.,]\d+)?)\s*(?:đ|vnd|vnđ|đồng)\b", r"\1 đồng"),
    (r"\b(\d+)\s*k\b", r"\1 nghìn"),
]

ABBREVIATIONS = [
    (r"\bTS\.\s*", "Tiến sĩ "),
    (r"\bThS\.\s*", "Thạc sĩ "),
    (r"\bKS\.\s*", "Kỹ sư "),
    (r"\bBS\.\s*", "Bác sĩ "),
    (r"\bTP\.\s*HCM\b", "Thành phố Hồ Chí Minh"),
    (r"\bTP\.\s*", "Thành phố "),
    (r"\bTX\.\s*", "Thị xã "),
    (r"\bTT\.\s*", "Thị trấn "),
    (r"\bH\.\s*", "Huyện "),
    (r"\bX\.\s*", "Xã "),
    (r"\bHTX\b", "Hợp tác xã"),
    (r"\bBVTV\b", "Bảo vệ thực vật"),
    (r"\bVTNN\b", "Vật tư nông nghiệp"),
    (r"\bVietGAP\b", "Việt Gáp"),
    (r"\bGlobalGAP\b", "Gờ lô bồ Gáp"),
    (r"\bMSPB\b", "Mã số phân bón"),
    (r"\bNSX\b", "Ngày sản xuất"),
    (r"\bHSD\b", "Hạn sử dụng"),
    (r"\bpH\b", "pê hát"),
]


def apply_agricultural_phonetics(text: str) -> str:
    """
    Áp dụng bộ từ điển phiên âm thuốc BVTV, phân bón, hoạt chất và công thức NPK
    từ CSDL MySQL (vietgap_diary) lên văn bản trước khi đưa vào bộ tổng hợp giọng nói.
    """
    if not text:
        return ""

    # 1. Thay thế tên thương phẩm đầy đủ (ưu tiên cụm dài trước)
    for pattern, replacement in DB_PRODUCT_FULL_PHONETICS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # 2. Chuẩn hóa tỷ lệ NPK 3 số (vd: 16-16-8 -> 16 16 8, 20-20-15 -> 20 20 15, 16-8-34 -> 16 8 34)
    text = re.sub(r"\b(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\b", r"\1 \2 \3", text)

    # 3. Chuẩn hóa mã chế phẩm thuốc đi kèm số (5SC -> 5 ét xê, 68WG -> 68 vê kép gờ, 85WP -> 85 vê kép pê...)
    for pattern, replacement in FORMULATION_MAPPINGS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # 4. Thay thế từng tên thuốc, phân bón, hoạt chất, chất cấm độc lập
    for pattern, replacement in AGRI_TERM_PHONETICS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # 5. Áp dụng quy tắc phiên âm động từ CSDL MySQL (nếu có sản phẩm mới thêm vào bảng pesticides/fertilizers)
    for dyn_regex, dyn_repl in load_db_pronunciations():
        text = dyn_regex.sub(dyn_repl, text)

    return text


def normalize_text(text: str) -> str:
    """
    Làm sạch markdown, mở rộng từ viết tắt, phiên âm danh mục thuốc BVTV & phân bón,
    chuẩn hóa số thập phân / hàng nghìn và đơn vị đo lường tiếng Việt.
    """
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

    # 1. Phiên âm tên thuốc BVTV, phân bón và hoạt chất từ CSDL (chạy trước khi tách dấu chấm câu để xử lý 'BP. Newone', 'Ds 98.8EC')
    text = apply_agricultural_phonetics(text)

    # 2. Mở rộng từ viết tắt (riêng chữ viết hoa PHI: bỏ lặp nếu đi sau 'thời gian cách ly', còn đứng độc lập mới đọc 'thời gian cách ly')
    text = re.sub(r"(thời\s+gian\s+cách\s+ly)\s*\(\s*PHI\s*\)", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"\bPHI\b", "thời gian cách ly", text)
    for p, r in ABBREVIATIONS:
        text = re.sub(p, r, text, flags=re.IGNORECASE)

    # 3. Xử lý phân cách hàng nghìn (vd: 2.000, 15.000, 117.800 -> 2000, 15000, 117800)
    # Lưu ý: 3 chữ số sau dấu chấm (như .000, .500) là hàng nghìn trong tiếng Việt
    text = re.sub(r"\b(\d{1,3})\.(\d{3})\.(\d{3})\b", r"\1\2\3", text)
    text = re.sub(r"\b(\d{1,3})\.(\d{3})\b", r"\1\2", text)

    # 4. Mở rộng đơn vị đo lường
    for p, r in UNIT_MAPPINGS:
        text = re.sub(p, r, text, flags=re.IGNORECASE)

    # 5. Chuyển số thập phân (1-2 chữ số sau dấu chấm hoặc dấu phẩy, vd: 98.8, 2,5, 8.6) thành "phẩy"
    text = re.sub(r"\b(\d+)[.,](\d{1,2})\b", r"\1 phẩy \2", text)

    # 6. Khoảng số (vd: 7-10 ngày, 15-20 đến...) -> "đến"
    text = re.sub(r"\b(\d+)\s*-\s*(\d+)\b", r"\1 đến \2", text)

    # Bỏ dấu ngoặc đơn/kép dư thừa để giữ nhịp ngắt tự nhiên bằng dấu phẩy
    text = re.sub(r"\(\s*([^)]+?)\s*\)", r", \1, ", text)
    text = re.sub(r"[\[\]{}\"'“”‘’]", " ", text)

    # Bỏ emoji & ký tự lạ
    text = re.sub(r"[\U00010000-\U0010ffff]", "", text)
    text = re.sub(r"[━─═✅⚠️💡🌿🐛🧪🌱🌾🧾📷🔍📝📦⚖️⏳🎯✓✕🔊🔇●👉🚜🏪💰📍🤝🛡️♻️⚒️⛔🚨]", "", text)

    # Chuẩn hoá dấu câu lặp
    text = re.sub(r"\s+([,.;!?])", r"\1", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"!{2,}", "!", text)
    text = re.sub(r"\?{2,}", "?", text)
    text = re.sub(r",\s*,+", ",", text)
    text = re.sub(r",\s*([.!?])", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)

    return text.strip(" ,")


def split_sentences(text: str, max_chars: int = 160) -> List[str]:
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
        s = s.strip(" ,;:-")
        if not s:
            continue
        if s[-1] not in ".!?":
            s += "."
        final_sentences.append(s)

    return final_sentences


# ==============================================================================
# 3. DSP AUDIO POST-PROCESSING: EQ GIẢM CHÓI, CHỐNG CLIPPING, LOUDNESS & RESAMPLING
# ==============================================================================

def check_and_declip_raw(samples: np.ndarray, clip_threshold: float = 0.985) -> Tuple[np.ndarray, int]:
    """
    Kiểm tra xem tín hiệu thô từ mô hình VITS có mẫu nào bị chạm ngưỡng clipping hay không,
    loại bỏ DC offset và làm mềm (cubic de-clip / attenuation) các đỉnh vượt ngưỡng.
    """
    if len(samples) == 0:
        return samples, 0

    arr = np.asarray(samples, dtype=np.float32)
    # 1. Loại bỏ DC offset
    dc_offset = float(np.mean(arr))
    if abs(dc_offset) > 1e-6:
        arr = arr - dc_offset

    # 2. Đếm số mẫu chạm ngưỡng clip (>= 0.985)
    abs_arr = np.abs(arr)
    clipped_mask = abs_arr >= clip_threshold
    raw_clipped_count = int(np.sum(clipped_mask))

    peak = float(np.max(abs_arr))
    if peak >= clip_threshold:
        # Hạ nhẹ toàn cục về vùng tuyến tính an toàn (0.85) và làm mượt đỉnh bị bẹt bằng bộ lọc trung bình cục bộ 3 điểm
        arr = arr * (0.85 / max(peak, 1e-6))
        if raw_clipped_count > 0 and len(arr) >= 3:
            smoothed = 0.25 * arr[:-2] + 0.5 * arr[1:-1] + 0.25 * arr[2:]
            idx = np.where(clipped_mask[1:-1])[0] + 1
            arr[idx] = smoothed[idx - 1]

    return arr, raw_clipped_count


def apply_fade_edges(samples: np.ndarray, sample_rate: int, fade_ms: float = 8.0) -> np.ndarray:
    """
    Áp dụng đường cong raised-cosine fade-in / fade-out (mặc định 8ms) ở hai đầu đoạn âm thanh
    để triệt tiêu hoàn toàn tiếng 'bụp/click' ở điểm cắt ghép giữa các câu.
    """
    n = len(samples)
    fade_len = min(int(round(fade_ms * 0.001 * sample_rate)), n // 4)
    if fade_len <= 1:
        return samples

    out = samples.copy()
    ramp = (0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, fade_len, dtype=np.float32)))).astype(np.float32)
    out[:fade_len] *= ramp
    out[-fade_len:] *= ramp[::-1]
    return out


def apply_natural_eq_and_resample(
    samples: np.ndarray,
    orig_sr: int,
    target_sr: int,
    enable_eq: bool = True,
) -> np.ndarray:
    """
    Kết hợp Parametric EQ pha tuyến tính (Zero-phase Frequency-Domain EQ) và
    Band-limited Sinc Resampling (nội suy băng thông giới hạn không răng cưa):

    Cấu hình EQ tinh chỉnh riêng cho model vi_VN-vais1000-medium (22050 Hz):
    1. High-Pass Filter (< 80 Hz, -24 dB/oct): Loại bỏ tạp âm tần số thấp (sub-bass rumble) & DC drift.
    2. Low-Mid Warmth Boost (210 Hz, +1.5 dB, BW=0.65 oct): Tăng độ ấm và dày tự nhiên của giọng đọc.
    3. Upper-Mid Anti-Harshness Cut (3150 Hz, -3.2 dB, BW=0.55 oct): Giảm cảm giác chói gắt ở vùng nhạy cảm nhất của tai người (2.5 - 4.2 kHz).
    4. De-Esser Cut (5800 Hz, -2.8 dB, BW=0.40 oct): Làm dịu âm xuýt (s, x, ch, tr, ph) bị sắc.
    5. High-Shelf & Anti-Imaging Low-Pass (> 6800 Hz -3.0 dB; > 9500 Hz roll-off -18 dB):
       Loại bỏ tiếng xì kim loại (vocoder fizz) của HiFi-GAN và ngăn chặn hiện tượng
       răng cưa phổ (spectral imaging) quanh tần số Nyquist (11025 Hz) khi phát trên trình duyệt/thiết bị.
    """
    n_in = len(samples)
    if n_in == 0:
        return samples

    # Biến đổi RFFT tại tần số gốc (orig_sr)
    spec = np.fft.rfft(samples)
    freqs = np.fft.rfftfreq(n_in, d=1.0 / float(orig_sr)).astype(np.float32)
    safe_freqs = np.maximum(freqs, 10.0)

    eq_db = np.zeros_like(freqs, dtype=np.float32)

    if enable_eq:
        # 1. High-pass filter dưới 80 Hz
        hp_mask = freqs < 80.0
        eq_db[hp_mask] -= 24.0 * ((80.0 - freqs[hp_mask]) / 80.0) ** 2

        # 2. Low-mid warmth bell (+1.5 dB tại 210 Hz)
        eq_db += 1.5 * np.exp(-0.5 * (np.log2(safe_freqs / 210.0) / 0.65) ** 2)

        # 3. Anti-harshness presence cut (-3.2 dB tại 3150 Hz - vùng gây chói tai nhất)
        eq_db -= 3.2 * np.exp(-0.5 * (np.log2(safe_freqs / 3150.0) / 0.50) ** 2)

        # 4. De-esser sibilance cut (-2.8 dB tại 5800 Hz)
        eq_db -= 2.8 * np.exp(-0.5 * (np.log2(safe_freqs / 5800.0) / 0.38) ** 2)

        # 5. High-shelf giảm độ xì tần số cao (-3.0 dB trên 6800 Hz)
        hs_curve = 0.5 * (1.0 + np.tanh((freqs - 6800.0) / 900.0))
        eq_db -= 3.0 * hs_curve

    # 6. Bộ lọc thông thấp chống răng cưa (Anti-aliasing / Anti-imaging Low-Pass)
    # Đảm bảo suy hao mượt trước ngưỡng Nyquist của tần số nhỏ hơn giữa orig_sr và target_sr
    nyquist_min = 0.5 * float(min(orig_sr, target_sr))
    cutoff_hz = min(9600.0, nyquist_min * 0.90)
    transition_hz = max(350.0, nyquist_min * 0.045)
    lp_curve = 0.5 * (1.0 + np.tanh((freqs - cutoff_hz) / transition_hz))
    eq_db -= 24.0 * lp_curve

    gain_linear = (10.0 ** (eq_db / 20.0)).astype(np.float32)
    spec_filtered = spec * gain_linear

    # Resampling băng thông giới hạn (Band-limited Sinc Resampling qua miền tần số)
    if target_sr != orig_sr and target_sr > 0:
        n_out = int(round(n_in * float(target_sr) / float(orig_sr)))
        out = np.fft.irfft(spec_filtered, n=n_out).astype(np.float32) * (float(n_out) / float(n_in))
    else:
        out = np.fft.irfft(spec_filtered, n=n_in).astype(np.float32)

    return out


def resample_audio(samples: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """
    Hàm tiện ích đổi tần số lấy mẫu (Resampling) chất lượng cao với bộ lọc chống răng cưa.
    """
    if orig_sr == target_sr or len(samples) == 0 or target_sr <= 0:
        return np.asarray(samples, dtype=np.float32)
    return apply_natural_eq_and_resample(samples, orig_sr=orig_sr, target_sr=target_sr, enable_eq=False)


def compute_active_speech_lufs(samples: np.ndarray, sample_rate: int, gate_db: float = -45.0) -> float:
    """
    Đo độ lớn âm lượng cảm nhận (Perceptual Active-Speech Loudness - xấp xỉ ITU-R BS.1770 LUFS):
    - Áp dụng trọng số tần số K-weighting (nhấn nhẹ dải tần cao > 1.5 kHz bằng sai phân bậc 1)
      để phản ánh đúng độ nhạy tai người.
    - Chia theo các khung 50ms và loại bỏ các khung khoảng lặng dưới ngưỡng `gate_db` (-45 dBFS)
      để khoảng nghỉ giữa câu không làm sai lệch phép đo âm lượng lời nói.
    """
    if len(samples) == 0:
        return -70.0

    # xấp xỉ K-weighting pre-filter (tăng ~+3.5 dB ở tần số cao)
    diff = np.diff(samples, prepend=samples[0])
    k_weighted = samples + 0.35 * diff

    frame_len = max(1, int(0.050 * sample_rate))  # Khung 50ms
    num_frames = len(k_weighted) // frame_len
    if num_frames >= 1:
        framed = k_weighted[: num_frames * frame_len].reshape(num_frames, frame_len)
        frame_ms = np.mean(framed * framed, axis=1)
        gate_linear = 10.0 ** (gate_db / 10.0)
        active_frames = frame_ms[frame_ms > gate_linear]
        if len(active_frames) > 0:
            # Gating tương đối (Relative gate -10 LU giống chuẩn BS.1770-4)
            mean_active = float(np.mean(active_frames))
            rel_gate = mean_active * 0.1  # -10 dB
            gated_frames = active_frames[active_frames >= rel_gate]
            mean_sq = float(np.mean(gated_frames)) if len(gated_frames) > 0 else mean_active
        else:
            mean_sq = float(np.mean(k_weighted * k_weighted))
    else:
        mean_sq = float(np.mean(k_weighted * k_weighted))

    if mean_sq <= 1e-12:
        return -70.0

    return float(-0.691 + 10.0 * np.log10(mean_sq))


def normalize_loudness_and_limit(
    samples: np.ndarray,
    sample_rate: int,
    target_lufs: float = DEFAULT_TARGET_LUFS,
    ceiling_db: float = DEFAULT_PEAK_CEILING_DB,
    max_gain_db: float = 5.5,
) -> Tuple[np.ndarray, float, float, float]:
    """
    Chuẩn hóa âm lượng (Loudness Normalization) và Chống Clipping do khuếch đại (Anti-Amplification Clipping):
    1. Đo độ lớn giọng đọc thực tế (K-weighted Active Speech LUFS).
    2. Tính hệ số khuếch đại đưa về `target_lufs` (mặc định -19.0 LUFS, âm lượng vừa phải, ấm áp,
       không bao giờ quá lớn gây giật mình). Giới hạn mức khuếch đại tối đa `max_gain_db` (+5.5 dB).
    3. Áp dụng bộ giới hạn đỉnh bao hình (Lookahead Envelope Peak Limiter) kết hợp Soft-Knee Saturation
       với trần tuyệt đối `ceiling_db` (mặc định -1.5 dBFS ~ 0.8414):
       - Ngưỡng bắt đầu nén mềm (knee threshold): ceiling - 2.0 dB (-3.5 dBFS ~ 0.6683).
       - Đảm bảo 100% không có bất kỳ mẫu nào vượt quá `ceiling_linear` hay bị xén phẳng (hard-clip).
    Trả về: (processed_samples, final_lufs, final_peak_dbfs, gain_applied_db)
    """
    if len(samples) == 0:
        return samples, -70.0, -70.0, 0.0

    current_lufs = compute_active_speech_lufs(samples, sample_rate)
    if current_lufs <= -65.0:
        return samples, current_lufs, -70.0, 0.0

    # 1. Tính độ lợi (gain) cần thiết để đạt target_lufs (-19.0 LUFS)
    desired_gain_db = target_lufs - current_lufs
    # Tránh khuếch đại quá đà đối với câu quá ngắn/nhỏ, nhưng cho phép giảm thoải mái nếu âm lượng quá lớn
    applied_gain_db = float(np.clip(desired_gain_db, -18.0, max_gain_db))
    gain_linear = float(10.0 ** (applied_gain_db / 20.0))

    out = (samples * gain_linear).astype(np.float32)

    # 2. Kiểm tra đỉnh sau khuếch đại & Chống clipping do khuếch đại (Lookahead Envelope Limiter + Soft Knee)
    ceiling_linear = float(10.0 ** (ceiling_db / 20.0))          # -1.5 dBFS -> 0.8414
    knee_linear = float(10.0 ** ((ceiling_db - 2.0) / 20.0))     # -3.5 dBFS -> 0.6683

    peak_after_gain = float(np.max(np.abs(out)))
    if peak_after_gain > knee_linear:
        # 2a. Lookahead Envelope Follower theo các cửa sổ nhỏ 2ms, làm mượt 12ms để giảm gain tự nhiên
        win_len = max(1, int(0.002 * sample_rate))
        n_wins = (len(out) + win_len - 1) // win_len
        padded = np.pad(np.abs(out), (0, n_wins * win_len - len(out)), mode="edge")
        win_peaks = np.max(padded.reshape(n_wins, win_len), axis=1)

        # Mở rộng lookahead 1 cửa sổ hai bên + trung bình trượt 5 cửa sổ (~10ms)
        if len(win_peaks) >= 3:
            win_peaks = np.maximum.reduce([
                np.roll(win_peaks, 1),
                win_peaks,
                np.roll(win_peaks, -1),
            ])
            win_peaks[0] = max(win_peaks[0], win_peaks[1])
            win_peaks[-1] = max(win_peaks[-1], win_peaks[-2])
            kernel = np.array([0.1, 0.2, 0.4, 0.2, 0.1], dtype=np.float32)
            win_peaks = np.convolve(win_peaks, kernel, mode="same")

        # Tính hệ số nén bao hình cho các vùng vượt ngưỡng knee
        target_env = np.where(
            win_peaks > knee_linear,
            knee_linear + (ceiling_linear - knee_linear) * np.tanh((win_peaks - knee_linear) / max(ceiling_linear - knee_linear, 1e-4)),
            win_peaks,
        )
        env_gain = np.where(win_peaks > 1e-6, target_env / np.maximum(win_peaks, 1e-6), 1.0).astype(np.float32)
        env_gain = np.clip(env_gain, 0.1, 1.0)

        # Nội suy mượt hệ số gain về từng mẫu
        centers = np.linspace(0, len(out) - 1, num=len(env_gain), dtype=np.float32)
        sample_idx = np.arange(len(out), dtype=np.float32)
        smooth_gain = np.interp(sample_idx, centers, env_gain).astype(np.float32)
        out = out * smooth_gain

        # 2b. Soft-knee hyperbolic tangent safety ceiling (đảm bảo 100% không mẫu nào chạm/vượt ceiling_linear)
        abs_out = np.abs(out)
        over_mask = abs_out > knee_linear
        if np.any(over_mask):
            headroom = max(ceiling_linear - knee_linear, 1e-4)
            excess = abs_out[over_mask] - knee_linear
            compressed = knee_linear + headroom * np.tanh(excess / headroom)
            out[over_mask] = np.sign(out[over_mask]) * compressed

    final_peak = float(np.max(np.abs(out)))
    final_peak_dbfs = float(20.0 * np.log10(max(final_peak, 1e-9)))
    final_lufs = compute_active_speech_lufs(out, sample_rate)

    return out, final_lufs, final_peak_dbfs, applied_gain_db


# ==============================================================================
# 4. LỚP TTSENGINE CHÍNH
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
        self._output_sample_rate = int(getattr(config, "TTS_OUTPUT_SAMPLE_RATE", DEFAULT_OUTPUT_SAMPLE_RATE))
        self._target_lufs = float(getattr(config, "TTS_TARGET_LUFS", DEFAULT_TARGET_LUFS))
        self._peak_ceiling_db = float(getattr(config, "TTS_PEAK_CEILING_DB", DEFAULT_PEAK_CEILING_DB))
        self._eq_enabled = bool(getattr(config, "TTS_EQ_ENABLED", DEFAULT_EQ_ENABLED))

        self._model_path = os.path.join(self._model_dir, "vi_VN-vais1000-medium.onnx")
        self._tokens_path = os.path.join(self._model_dir, "tokens.txt")
        self._espeak_dir = os.path.join(self._model_dir, "espeak-ng-data")
        self._config_path = os.path.join(self._model_dir, "vi_VN-vais1000-medium.onnx.json")

        self._sherpa_tts: Optional[sherpa_onnx.OfflineTts] = None
        self._piper_voice: Optional[PiperVoice] = None
        self.model_sample_rate: int = DEFAULT_MODEL_SAMPLE_RATE
        self.sample_rate: int = self._output_sample_rate

        logger.info(
            "Khởi tạo TTSEngine (model_dir: %s, default_engine: %s, output_sr: %dHz, target_lufs: %.1f)...",
            self._model_dir,
            self._default_engine,
            self._output_sample_rate,
            self._target_lufs,
        )
        self._load_sherpa()
        # Nạp danh mục thuốc & phân bón từ CSDL để sẵn sàng phiên âm
        load_db_pronunciations()

        # Hàng đợi job & worker thread
        self._queue: "asyncio.Queue[_Job]" = asyncio.Queue()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-worker")
        self._worker_task: "Optional[asyncio.Task]" = None

    def _load_sherpa(self):
        """Khởi tạo engine sherpa-onnx với noise_scale tối ưu giảm độ xì kim loại."""
        if not os.path.exists(self._model_path):
            raise FileNotFoundError(f"Không tìm thấy model TTS ONNX: {self._model_path}")

        t0 = time.time()
        # Giảm nhẹ noise_scale (0.667 -> 0.60) và noise_scale_w (0.8 -> 0.72) để giọng đọc sạch, đỡ chói và ổn định nhịp điệu hơn
        vits_config = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=self._model_path,
            tokens=self._tokens_path,
            data_dir=self._espeak_dir,
            noise_scale=0.60,
            noise_scale_w=0.72,
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
        self.model_sample_rate = int(self._sherpa_tts.sample_rate)
        if self._output_sample_rate <= 0:
            self._output_sample_rate = self.model_sample_rate
        self.sample_rate = self._output_sample_rate
        logger.info(
            "Đã nạp SherpaOnnx TTS thành công (%.3fs, model_sr=%dHz -> output_sr=%dHz).",
            time.time() - t0,
            self.model_sample_rate,
            self.sample_rate,
        )

    def _load_piper(self):
        """Khởi tạo engine piper-tts (lazy load khi có yêu cầu)."""
        if self._piper_voice is not None:
            return
        if not PIPER_AVAILABLE:
            raise TTSError("Thư viện piper-tts chưa được cài đặt trong môi trường.")

        t0 = time.time()
        self._piper_voice = PiperVoice.load(self._model_path, config_path=self._config_path)
        logger.info(
            "Đã nạp Piper-TTS thành công (%.3fs, model_sr=%dHz).",
            time.time() - t0,
            self._piper_voice.config.sample_rate,
        )

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

            # Nén sang mp3/ogg qua ffmpeg với bộ resampler soxr chất lượng cao
            wav_bytes = audio_res.to_wav_bytes()
            fmt = job.output_format.lower()
            cmd = ["ffmpeg", "-y", "-i", "pipe:0"]
            if fmt == "mp3":
                cmd.extend([
                    "-af", "aresample=44100:resampler=soxr:precision=28:dither_method=triangular",
                    "-ar", "44100",
                    "-c:a", "libmp3lame",
                    "-q:a", "2",
                    "-f", "mp3",
                    "pipe:1",
                ])
            elif fmt in ("ogg", "opus"):
                cmd.extend([
                    "-af", "aresample=48000:resampler=soxr:precision=28",
                    "-ar", "48000",
                    "-c:a", "libopus",
                    "-b:a", "48k",
                    "-vbr", "on",
                    "-application", "voip",
                    "-f", "ogg",
                    "pipe:1",
                ])
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
    # Inference core + DSP Post-Processing (Synchronous)
    # --------------------------------------------------------------------------

    def synthesize_sync(
        self,
        text: str,
        speed: float = 1.0,
        sid: int = 0,
        engine: Optional[str] = None,
        pause_sec: float = 0.18,
        target_sample_rate: Optional[int] = None,
        enable_eq: Optional[bool] = None,
        target_lufs: Optional[float] = None,
    ) -> AudioResult:
        """
        Sinh âm thanh đồng bộ từ văn bản với pipeline xử lý âm thanh hoàn chỉnh:
        1. Chuẩn hóa văn bản & phiên âm tên thuốc BVTV / phân bón từ CSDL.
        2. Tách câu tự nhiên, tổng hợp từng câu ở tần số mẫu gốc (22050 Hz),
           tắt normalize_audio thô của Piper để tránh clipping từng câu.
        3. Kiểm tra clipping gốc, khử DC offset, cân bằng RMS giữa các câu và làm mượt biên (fade edges).
        4. Điều chỉnh Parametric EQ giảm chói & Resampling băng thông giới hạn (22050 Hz -> 44100 Hz).
        5. Chuẩn hóa độ lớn âm lượng (Loudness Normalization -19.0 LUFS) kèm bộ giới hạn đỉnh
           Lookahead Envelope Limiter + Soft-Knee Ceiling (-1.5 dBFS) chống clipping do khuếch đại.
        """
        clean_text = normalize_text(text)
        if not clean_text:
            raise TTSError("Văn bản sau chuẩn hoá bị rỗng.")

        sentences = split_sentences(clean_text)
        if not sentences:
            sentences = [clean_text]

        eng_choice = (engine or self._default_engine).lower()
        t0 = time.time()
        raw_sentence_chunks: List[np.ndarray] = []
        total_raw_clipped = 0

        if "piper" in eng_choice:
            self._load_piper()
            native_sr = int(self._piper_voice.config.sample_rate)
            eff_speed = max(0.2, min(5.0, speed))
            # QUAN TRỌNG: Đặt normalize_audio=False để Piper KHÔNG tự động kích biên độ mỗi câu lên 0.0 dBFS (1.0)
            syn_cfg = SynthesisConfig(
                speaker_id=sid,
                length_scale=1.0 / eff_speed,
                noise_scale=0.60,
                noise_w_scale=0.72,
                normalize_audio=False,
            )

            for s in sentences:
                chunks = list(self._piper_voice.synthesize(s, syn_config=syn_cfg))
                if chunks:
                    # Lấy trực tiếp mảng float32 từ VITS thay vì qua audio_int16_bytes để tránh méo lượng tử kép
                    float32_arr = np.concatenate([np.asarray(c.audio_float_array, dtype=np.float32) for c in chunks])
                    if len(float32_arr) > 0:
                        declipped, n_clip = check_and_declip_raw(float32_arr)
                        total_raw_clipped += n_clip
                        raw_sentence_chunks.append(apply_fade_edges(declipped, native_sr, fade_ms=8.0))
            engine_name = "piper-tts"
        else:
            # Dùng sherpa-onnx (mặc định)
            native_sr = self.model_sample_rate
            for s in sentences:
                gen = self._sherpa_tts.generate(s, sid=sid, speed=speed)
                chunk_arr = np.asarray(gen.samples, dtype=np.float32)
                if len(chunk_arr) > 0:
                    declipped, n_clip = check_and_declip_raw(chunk_arr)
                    total_raw_clipped += n_clip
                    raw_sentence_chunks.append(apply_fade_edges(declipped, native_sr, fade_ms=8.0))
            engine_name = "sherpa-onnx"

        if not raw_sentence_chunks:
            raise TTSError("Không tạo được âm thanh từ văn bản đầu vào.")

        # Cân bằng nhẹ độ lớn giữa các câu (nếu đoạn văn có nhiều câu) để tránh câu to câu nhỏ
        if len(raw_sentence_chunks) > 1:
            chunk_rms = [float(np.sqrt(np.mean(c * c) + 1e-12)) for c in raw_sentence_chunks]
            valid_rms = [r for r in chunk_rms if r > 1e-4]
            if valid_rms:
                median_rms = float(np.median(valid_rms))
                for idx, (c, r) in enumerate(zip(raw_sentence_chunks, chunk_rms)):
                    if r > 1e-4:
                        # Chỉ điều chỉnh nhẹ trong phạm vi +/- 3.0 dB (0.71x .. 1.41x) để giữ ngữ điệu tự nhiên
                        ratio = float(np.clip(median_rms / r, 0.71, 1.41))
                        raw_sentence_chunks[idx] = c * ratio

        # Ghép các câu với khoảng nghỉ tự nhiên (pause_sec)
        silence_samples = np.zeros(int(round(pause_sec * native_sr)), dtype=np.float32)
        interleaved: List[np.ndarray] = []
        for idx, chunk in enumerate(raw_sentence_chunks):
            if idx > 0:
                interleaved.append(silence_samples)
            interleaved.append(chunk)

        combined_native = np.concatenate(interleaved) if len(interleaved) > 1 else interleaved[0]

        # Bước DSP 1: Điều chỉnh EQ giảm chói & Resampling băng thông giới hạn sang tần số mục tiêu
        out_sr = int(target_sample_rate or self._output_sample_rate or native_sr)
        use_eq = self._eq_enabled if enable_eq is None else bool(enable_eq)
        eq_resampled = apply_natural_eq_and_resample(
            combined_native,
            orig_sr=native_sr,
            target_sr=out_sr,
            enable_eq=use_eq,
        )

        # Bước DSP 2: Chuẩn hóa Loudness (-19.0 LUFS) & Chống Clipping do khuếch đại (Ceiling -1.5 dBFS)
        eff_lufs = float(target_lufs if target_lufs is not None else self._target_lufs)
        processed, final_lufs, final_peak_dbfs, gain_db = normalize_loudness_and_limit(
            eq_resampled,
            sample_rate=out_sr,
            target_lufs=eff_lufs,
            ceiling_db=self._peak_ceiling_db,
        )

        # Làm mượt 6ms đầu/cuối toàn file audio
        processed = apply_fade_edges(processed, out_sr, fade_ms=6.0)
        post_clipped = int(np.sum(np.abs(processed) >= 0.995))

        elapsed = time.time() - t0
        duration = len(processed) / float(out_sr)
        rtf = elapsed / duration if duration > 0 else 0.0

        return AudioResult(
            samples=processed,
            sample_rate=out_sr,
            duration_sec=duration,
            elapsed_sec=elapsed,
            rtf=rtf,
            engine_name=engine_name,
            raw_sample_rate=native_sr,
            peak_dbfs=round(final_peak_dbfs, 2),
            loudness_lufs=round(final_lufs, 2),
            gain_applied_db=round(gain_db, 2),
            raw_clipped_samples=total_raw_clipped,
            post_clipped_samples=post_clipped,
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
