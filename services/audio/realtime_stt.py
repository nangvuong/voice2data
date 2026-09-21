"""
Realtime STT Engine for Vietnamese Agriculture & Data Logging.
Sử dụng mô hình sherpa-onnx (Offline Transducer) kết hợp Silero VAD để streaming nhận diện:
- Hỗ trợ audio chunking (100ms - 200ms) PCM16 mono 16kHz.
- Nhận diện tức thời (Partial events mỗi ~250-350ms) trong lúc người dùng đang nói.
- Nhận diện chốt câu (Final events) ngay khi phát hiện khoảng lặng (VAD endpoint).
- Tích hợp 100% RealtimeNumberUnitNormalizer tự động chuẩn hoá số, phân số, đơn vị nông nghiệp và trích xuất thực thể số lượng theo thời gian thực.
- Hỗ trợ đa phiên đồng thời (Multi-session streaming).
- Tích hợp WebSocket Server độc lập và API REST streaming cho WebApp.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple, Union

import numpy as np
import sherpa_onnx

import config

logger = logging.getLogger("realtime-stt")


@dataclass
class RealtimeSTTEvent:
    """Sự kiện phát ra từ Realtime STT Engine (thuần nhận diện âm thanh thô)."""
    event_type: str  # "partial", "final", "complete"
    text: str
    segment_id: int
    duration_sec: float
    is_endpoint: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event": self.event_type,
            "text": self.text,
            "raw_text": self.text,
            "normalized_text": self.text,
            "quantities": [],
            "segment_id": self.segment_id,
            "duration_sec": round(self.duration_sec, 2),
            "is_endpoint": self.is_endpoint,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class StreamingSTTSession:
    """
    Một phiên nhận diện streaming độc lập cho một luồng microphone / websocket.
    Mỗi session sở hữu trạng thái VAD riêng biệt, dùng chung Transducer Recognizer.
    """

    def __init__(
        self,
        session_id: str,
        recognizer: sherpa_onnx.OfflineRecognizer,
        recognizer_lock: threading.Lock,
        vad_model_path: str = config.VAD_MODEL_PATH,
        sample_rate: int = config.TARGET_SAMPLE_RATE,
        vad_threshold: float = 0.38,
        min_silence_sec: float = 0.35,
        min_speech_sec: float = 0.25,
        max_speech_sec: float = 15.0,
        partial_interval_sec: float = 0.25,
    ):
        self.session_id = session_id
        self._recognizer = recognizer
        self._recognizer_lock = recognizer_lock
        self.sample_rate = sample_rate
        self.partial_interval_samples = int(partial_interval_sec * sample_rate)

        # Cấu hình Silero VAD riêng biệt cho session
        vad_config = sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model = vad_model_path
        vad_config.silero_vad.threshold = vad_threshold
        vad_config.silero_vad.min_silence_duration = min_silence_sec
        vad_config.silero_vad.min_speech_duration = min_speech_sec
        vad_config.silero_vad.max_speech_duration = max_speech_sec
        vad_config.silero_vad.window_size = 512
        vad_config.sample_rate = sample_rate
        vad_config.num_threads = 1

        if not vad_config.validate():
            raise RuntimeError(f"VAD config không hợp lệ với model {vad_model_path}")

        self._vad = sherpa_onnx.VoiceActivityDetector(
            vad_config, buffer_size_in_seconds=max_speech_sec + 10.0
        )

        self._window_size = 512
        self._incoming_buffer: List[float] = []
        self._all_samples: List[float] = []
        self._segment_idx = 0
        self._last_partial_samples = 0
        self._total_samples = 0
        self._finalized_segments: List[str] = []
        self._is_finished = False

    def feed_audio(
        self,
        chunk: Union[bytes, np.ndarray, List[float]],
        preserve_case: bool = True,
    ) -> List[RealtimeSTTEvent]:
        """
        Nạp một chunk audio vào session.
        - chunk: PCM16 mono 16kHz (dạng bytes) hoặc float32 ndarray trong khoảng [-1.0, 1.0].
        Trả về danh sách các sự kiện STT mới phát sinh (nếu có): partial hoặc final (chữ nguyên bản).
        """
        if self._is_finished:
            return []

        # Chuyển đổi dữ liệu chunk về float32
        if isinstance(chunk, bytes):
            int16_arr = np.frombuffer(chunk, dtype=np.int16)
            float_samples = int16_arr.astype(np.float32) / 32768.0
        elif isinstance(chunk, np.ndarray):
            if chunk.dtype == np.int16:
                float_samples = chunk.astype(np.float32) / 32768.0
            else:
                float_samples = chunk.astype(np.float32)
        elif isinstance(chunk, list):
            float_samples = np.array(chunk, dtype=np.float32)
        else:
            raise ValueError(f"Định dạng chunk không hợp lệ: {type(chunk)}")

        self._incoming_buffer.extend(float_samples.tolist())
        self._all_samples.extend(float_samples.tolist())
        self._total_samples += len(float_samples)

        events: List[RealtimeSTTEvent] = []

        # Xử lý theo từng window size 512 (32ms tại 16kHz)
        while len(self._incoming_buffer) >= self._window_size:
            window = np.array(self._incoming_buffer[: self._window_size], dtype=np.float32)
            self._incoming_buffer = self._incoming_buffer[self._window_size :]
            self._vad.accept_waveform(window)

            # 1. Kiểm tra đoạn đang nói để phát sinh PARTIAL event (văn bản thô)
            if self._vad.is_speech_detected():
                curr_samples = self._vad.current_segment.samples
                curr_len = len(curr_samples)

                if (
                    curr_len >= 4000
                    and (curr_len - self._last_partial_samples) >= self.partial_interval_samples
                ):
                    self._last_partial_samples = curr_len
                    partial_text = self._decode_samples(curr_samples)

                    if partial_text:
                        events.append(
                            RealtimeSTTEvent(
                                event_type="partial",
                                text=partial_text,
                                segment_id=self._segment_idx + 1,
                                duration_sec=curr_len / self.sample_rate,
                                is_endpoint=False,
                            )
                        )

            # 2. Kiểm tra đoạn đã kết thúc (người dùng ngắt giọng) để phát sinh FINAL event
            while not self._vad.empty():
                seg = self._vad.front
                seg_samples = seg.samples
                self._vad.pop()
                self._last_partial_samples = 0
                self._segment_idx += 1

                final_text = self._decode_samples(seg_samples)
                if final_text:
                    self._finalized_segments.append(final_text)
                    events.append(
                        RealtimeSTTEvent(
                            event_type="final",
                            text=final_text,
                            segment_id=self._segment_idx,
                            duration_sec=len(seg_samples) / self.sample_rate,
                            is_endpoint=True,
                        )
                    )

        return events

    def finish(self, preserve_case: bool = True) -> List[RealtimeSTTEvent]:
        """
        Kết thúc luồng streaming:
        - Xả toàn bộ buffer còn lại trong VAD (flush).
        - Trả về final segment còn sót lại (nếu có).
        - Phát ra sự kiện COMPLETE tổng hợp toàn bộ câu nói thô từ STT.
        """
        if self._is_finished:
            return []

        self._is_finished = True
        events: List[RealtimeSTTEvent] = []

        # Đẩy nốt buffer còn trong incoming_buffer vào VAD nếu có
        if self._incoming_buffer:
            pad_len = (self._window_size - (len(self._incoming_buffer) % self._window_size)) % self._window_size
            if pad_len > 0:
                self._incoming_buffer.extend([0.0] * pad_len)
            while len(self._incoming_buffer) >= self._window_size:
                win = np.array(self._incoming_buffer[: self._window_size], dtype=np.float32)
                self._incoming_buffer = self._incoming_buffer[self._window_size :]
                self._vad.accept_waveform(win)

        # Xả VAD
        self._vad.flush()
        while not self._vad.empty():
            seg = self._vad.front
            seg_samples = seg.samples
            self._vad.pop()
            self._segment_idx += 1

            final_text = self._decode_samples(seg_samples)
            if final_text:
                self._finalized_segments.append(final_text)
                events.append(
                    RealtimeSTTEvent(
                        event_type="final",
                        text=final_text,
                        segment_id=self._segment_idx,
                        duration_sec=len(seg_samples) / self.sample_rate,
                        is_endpoint=True,
                    )
                )

        # Fallback: nếu VAD không bắt được segment nào nhưng có dữ liệu âm thanh đủ dài (>= 0.3s)
        if not self._finalized_segments and len(self._all_samples) >= 4800:
            fallback_text = self._decode_samples(self._all_samples)
            if fallback_text:
                self._finalized_segments.append(fallback_text)
                events.append(
                    RealtimeSTTEvent(
                        event_type="final",
                        text=fallback_text,
                        segment_id=self._segment_idx + 1,
                        duration_sec=len(self._all_samples) / self.sample_rate,
                        is_endpoint=True,
                    )
                )

        # Tạo sự kiện COMPLETE tổng kết văn bản âm thanh thô
        full_raw_text = " ".join(self._finalized_segments).strip()
        events.append(
            RealtimeSTTEvent(
                event_type="complete",
                text=full_raw_text,
                segment_id=self._segment_idx,
                duration_sec=self._total_samples / self.sample_rate,
                is_endpoint=True,
            )
        )

        return events

    def reset(self):
        """Khởi động lại trạng thái session."""
        self._vad.reset()
        self._incoming_buffer.clear()
        self._all_samples.clear()
        self._segment_idx = 0
        self._last_partial_samples = 0
        self._total_samples = 0
        self._finalized_segments.clear()
        self._is_finished = False

    def _decode_samples(self, samples: Union[np.ndarray, List[float]]) -> str:
        """Hàm nội bộ decode mảng float32 thông qua OfflineRecognizer thread-safe."""
        if len(samples) == 0:
            return ""

        samples_arr = np.asarray(samples, dtype=np.float32)

        with self._recognizer_lock:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(self.sample_rate, samples_arr)
            self._recognizer.decode_stream(stream)
            text = stream.result.text.strip()
            return text


class RealtimeSTTEngine:
    """
    Quản lý toàn bộ engine Realtime STT:
    - Quản lý phiên streaming (Sessions).
    - Tái sử dụng mô hình Recognizer chung giúp tối ưu RAM.
    - Hỗ trợ server WebSocket độc lập.
    """

    def __init__(self, recognizer: Optional[sherpa_onnx.OfflineRecognizer] = None):
        self._recognizer_lock = threading.Lock()

        if recognizer is not None:
            self._recognizer = recognizer
            logger.info("RealtimeSTTEngine: Sử dụng recognizer được cấp sẵn.")
        else:
            # Khởi tạo Recognizer từ config nếu chưa có
            logger.info("RealtimeSTTEngine: Đang khởi tạo mô hình Transducer...")
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                tokens=config.TOKENS_PATH,
                encoder=config.ENCODER_PATH,
                decoder=config.DECODER_PATH,
                joiner=config.JOINER_PATH,
                num_threads=config.NUM_THREADS,
                sample_rate=config.TARGET_SAMPLE_RATE,
                feature_dim=80,
            )
            logger.info("RealtimeSTTEngine: Transducer recognizer đã sẵn sàng.")

        self._sessions: Dict[str, StreamingSTTSession] = {}
        self._sessions_lock = threading.Lock()

    def create_session(
        self,
        session_id: Optional[str] = None,
        vad_threshold: float = 0.38,
        min_silence_sec: float = 0.35,
    ) -> StreamingSTTSession:
        """Tạo một phiên streaming mới."""
        s_id = session_id or str(uuid.uuid4())
        session = StreamingSTTSession(
            session_id=s_id,
            recognizer=self._recognizer,
            recognizer_lock=self._recognizer_lock,
            vad_threshold=vad_threshold,
            min_silence_sec=min_silence_sec,
        )
        with self._sessions_lock:
            self._sessions[s_id] = session
        return session

    def get_session(self, session_id: str) -> Optional[StreamingSTTSession]:
        """Lấy phiên streaming theo ID."""
        with self._sessions_lock:
            return self._sessions.get(session_id)

    def close_session(self, session_id: str):
        """Đóng và dọn dẹp phiên streaming."""
        with self._sessions_lock:
            if session_id in self._sessions:
                del self._sessions[session_id]

    async def transcribe_stream(
        self,
        audio_stream: AsyncIterator[bytes],
        preserve_case: bool = True,
    ) -> AsyncIterator[RealtimeSTTEvent]:
        """
        Async Generator: Nhận luồng audio bytes và liên tục yield các RealtimeSTTEvent.
        """
        session = self.create_session()
        try:
            async for chunk in audio_stream:
                events = session.feed_audio(chunk, preserve_case=preserve_case)
                for event in events:
                    yield event

            # Xả luồng khi kết thúc
            final_events = session.finish(preserve_case=preserve_case)
            for event in final_events:
                yield event
        finally:
            self.close_session(session.session_id)

    def start_websocket_server(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        ssl_cert: Optional[str] = None,
        ssl_key: Optional[str] = None,
    ):
        """
        Khởi động WebSocket Server cho các client streaming trực tiếp.
        Client gửi binary PCM16 16kHz mono, server trả JSON events.
        Hỗ trợ SSL (WSS) nếu cấp ssl_cert và ssl_key.
        """
        import ssl
        import websockets

        ssl_context = None
        if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(ssl_cert, ssl_key)
            logger.info("Realtime STT WebSocket: Đã bật TLS/SSL với cert=%s", ssl_cert)

        async def handler(websocket):
            session = self.create_session()
            logger.info(f"WebSocket client kết nối: session={session.session_id}")
            try:
                # Gửi thông báo session ready
                await websocket.send(
                    json.dumps({
                        "event": "ready",
                        "session_id": session.session_id,
                        "sample_rate": config.TARGET_SAMPLE_RATE,
                    })
                )

                async for message in websocket:
                    # Nếu là message binary: chunk audio
                    if isinstance(message, bytes):
                        events = session.feed_audio(message, preserve_case=True)
                        for ev in events:
                            await websocket.send(ev.to_json())
                    # Nếu là message text JSON: lệnh điều khiển
                    elif isinstance(message, str):
                        try:
                            cmd = json.loads(message)
                            action = cmd.get("action")
                            if action == "finish":
                                final_events = session.finish(preserve_case=True)
                                for ev in final_events:
                                    await websocket.send(ev.to_json())
                            elif action == "reset":
                                session.reset()
                                await websocket.send(
                                    json.dumps({"event": "reset", "session_id": session.session_id})
                                )
                            elif action == "ping":
                                await websocket.send(json.dumps({"event": "pong"}))
                        except json.JSONDecodeError:
                            pass
            except websockets.exceptions.ConnectionClosed:
                logger.info(f"WebSocket client ngắt kết nối: session={session.session_id}")
            except Exception as e:
                logger.warning(f"WebSocket lỗi phiên session={session.session_id}: {e}")
            finally:
                self.close_session(session.session_id)

        def process_request(connection, request):
            if request.headers.get("Upgrade", "").lower() != "websocket":
                return connection.respond(
                    200,
                    "Realtime STT WebSocket Service is Active.\n"
                )
            return None

        async def main():
            server = await websockets.serve(
                handler,
                host,
                port,
                ssl=ssl_context,
                max_size=10 * 1024 * 1024,
                process_request=process_request,
            )
            protocol = "wss" if ssl_context else "ws"
            logger.info(f"🚀 Realtime STT WebSocket Server đang chạy tại {protocol}://{host}:{port}")
            await server.wait_closed()

        asyncio.run(main())

    def start_websocket_server_in_background(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        ssl_cert: Optional[str] = None,
        ssl_key: Optional[str] = None,
    ) -> threading.Thread:
        """Khởi động WebSocket Server trong một background daemon thread (non-blocking)."""
        thread = threading.Thread(
            target=self.start_websocket_server,
            args=(host, port, ssl_cert, ssl_key),
            daemon=True,
            name="stt-websocket-server",
        )
        thread.start()
        return thread


# Khởi tạo singleton dùng chung recognizer từ stt_engine nếu đã có
def _get_shared_engine() -> RealtimeSTTEngine:
    try:
        from services.engines.stt_engine import stt_engine
        if hasattr(stt_engine, "_recognizer") and stt_engine._recognizer is not None:
            return RealtimeSTTEngine(recognizer=stt_engine._recognizer)
    except Exception:
        pass
    return RealtimeSTTEngine()


realtime_stt_engine = _get_shared_engine()


if __name__ == "__main__":
    import argparse
    import wave

    parser = argparse.ArgumentParser(description="Test Realtime STT Engine")
    parser.add_argument("--test-file", type=str, default="tmp/test_A1.wav", help="Đường dẫn file WAV 16k mono")
    parser.add_argument("--ws-server", action="store_true", help="Chạy WebSocket server")
    parser.add_argument("--port", type=int, default=8765, help="Port WebSocket server")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host WebSocket server")
    parser.add_argument("--ssl-cert", type=str, default=None, help="Đường dẫn SSL cert.pem (tuỳ chọn)")
    parser.add_argument("--ssl-key", type=str, default=None, help="Đường dẫn SSL key.pem (tuỳ chọn)")
    args = parser.parse_args()

    if args.ws_server:
        print(f"Bắt đầu WebSocket Server trên {args.host}:{args.port} (SSL: {bool(args.ssl_cert)})...")
        realtime_stt_engine.start_websocket_server(
            host=args.host,
            port=args.port,
            ssl_cert=args.ssl_cert,
            ssl_key=args.ssl_key,
        )
    else:
        if not os.path.exists(args.test_file):
            print(f"File {args.test_file} không tồn tại.")
            sys.exit(1)

        print(f"--- Mô phỏng Streaming STT với file: {args.test_file} ---")
        with wave.open(args.test_file, "rb") as wf:
            raw_bytes = wf.readframes(wf.getnframes())

        session = realtime_stt_engine.create_session()
        chunk_bytes_len = 3200  # 100ms PCM16 = 1600 samples * 2 bytes = 3200 bytes

        start_time = time.time()
        for i in range(0, len(raw_bytes), chunk_bytes_len):
            chunk = raw_bytes[i : i + chunk_bytes_len]
            events = session.feed_audio(chunk, preserve_case=True)
            for ev in events:
                print(f"[{ev.event_type.upper()}] (seg {ev.segment_id}, {ev.duration_sec}s): {ev.text}")
            time.sleep(0.01)  # Giả lập streaming

        finish_events = session.finish(preserve_case=True)
        for ev in finish_events:
            print(f"[{ev.event_type.upper()}] (seg {ev.segment_id}, {ev.duration_sec}s): {ev.text}")

        print(f"Hoàn thành trong {time.time() - start_time:.2f}s.")
