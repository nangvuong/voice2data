"""
Wrapper gọi ffmpeg (đã cài sẵn trên hệ thống) để resample audio
về định dạng sherpa-onnx yêu cầu: 16000Hz, Mono, PCM 16-bit.
"""

import asyncio
import logging
import os

import config

logger = logging.getLogger(__name__)


class AudioConversionError(Exception):
    """Lỗi khi ffmpeg convert audio thất bại."""


async def resample_to_16k_mono_pcm16(input_path: str, output_path: str) -> str:
    """
    Chạy ffmpeg convert file audio bất kỳ về:
    - sample rate 16000 Hz
    - mono (1 kênh)
    - PCM 16-bit (pcm_s16le)

    Ném AudioConversionError nếu ffmpeg lỗi hoặc timeout.
    """
    if not input_path or not os.path.exists(input_path):
        raise AudioConversionError("File âm thanh đầu vào không tồn tại.")

    file_size = os.path.getsize(input_path)
    if file_size < 100:
        raise AudioConversionError("File âm thanh rỗng hoặc quá ngắn (dưới 100 bytes).")

    cmd = [
        "ffmpeg",
        "-y",  # ghi đè file output nếu đã tồn tại
        "-i", input_path,
        "-ar", str(config.TARGET_SAMPLE_RATE),
        "-ac", "1",
        "-sample_fmt", "s16",
        "-c:a", "pcm_s16le",
        output_path,
    ]

    logger.info("Chạy ffmpeg: %s", " ".join(cmd))

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=config.FFMPEG_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise AudioConversionError("ffmpeg xử lý quá thời gian cho phép (timeout).")

    if process.returncode != 0:
        err_text = stderr.decode(errors="ignore")
        logger.error("ffmpeg lỗi: %s", err_text)
        raise AudioConversionError(f"ffmpeg thất bại (mã lỗi {process.returncode}).")

    return output_path
