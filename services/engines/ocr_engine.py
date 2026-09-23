"""
OCR Engine - Nhận diện chữ từ hình ảnh (Optical Character Recognition) sử dụng thư viện RapidOCR.

Kiến trúc tương tự stt_engine.py và tts_engine.py:
- Quản lý mô hình RapidOCR (chạy trên ONNX Runtime, tối ưu hóa cao cho kiến trúc ARM64/Odroid RK3588).
- Hỗ trợ xử lý bất đồng bộ thông qua hàng đợi tuần tự (asyncio.Queue) kết hợp ThreadPoolExecutor
  (1 worker) nhằm đảm bảo thread-safe, không gây quá tải CPU/RAM trên thiết bị nhúng.
- Đồng thời cung cấp phương thức ocr_sync() trực tiếp cho các tác vụ đồng bộ (CLI, Flask route...).
- Đa dạng kiểu đầu vào: đường dẫn file ảnh (str, Path), mảng numpy (OpenCV), PIL Image, hoặc dữ liệu raw bytes.
- Kết quả đầu ra trả về đối tượng có cấu trúc OCRResult gồm text gộp, danh sách từng dòng (OCRLine) kèm
  tọa độ bounding box polygon, độ tin cậy (confidence score) và thời gian thực thi.
- Hỗ trợ vẽ và xuất ảnh minh họa trực quan (visualization) các vùng chữ nhận diện được.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
import io
import logging
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from rapidocr_onnxruntime import RapidOCR

import config

logger = logging.getLogger(__name__)

# Font mặc định hệ thống cho hiển thị tiếng Việt khi vẽ visualization
DEFAULT_SYSTEM_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
]


class OCRError(Exception):
    """Lỗi phát sinh trong quá trình nhận diện văn bản (OCR)."""


@dataclass
class OCRLine:
    """Biểu diễn một dòng văn bản nhận diện được từ ảnh."""
    text: str
    score: float
    box: List[List[float]]  # 4 đỉnh polygon [[x1, y1], [x2, y2], [x3, y3], [x4, y4]]

    @property
    def rect(self) -> Tuple[float, float, float, float]:
        """Tọa độ hình chữ nhật bao quanh (xmin, ymin, xmax, ymax)."""
        xs = [pt[0] for pt in self.box]
        ys = [pt[1] for pt in self.box]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def center(self) -> Tuple[float, float]:
        """Tọa độ tâm (cx, cy) của dòng chữ."""
        xs = [pt[0] for pt in self.box]
        ys = [pt[1] for pt in self.box]
        return sum(xs) / len(xs), sum(ys) / len(ys)

    def to_dict(self) -> Dict[str, Any]:
        """Chuyển thành dictionary có thể serialize sang JSON."""
        xmin, ymin, xmax, ymax = self.rect
        return {
            "text": self.text,
            "score": round(float(self.score), 4),
            "box": [[round(float(c), 1) for c in pt] for pt in self.box],
            "rect": [round(float(xmin), 1), round(float(ymin), 1), round(float(xmax), 1), round(float(ymax), 1)],
        }


@dataclass
class OCRResult:
    """Kết quả tổng thể của một phiên nhận diện OCR."""
    text: str
    lines: List[OCRLine]
    image_size: Tuple[int, int]  # (width, height)
    total_lines: int
    avg_score: float
    det_elapse: float = 0.0
    cls_elapse: float = 0.0
    rec_elapse: float = 0.0
    total_elapse: float = 0.0
    raw_result: Any = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        """Chuyển đổi toàn bộ kết quả sang dict chuẩn JSON cho Web API."""
        return {
            "text": self.text,
            "lines": [line.to_dict() for line in self.lines],
            "total_lines": self.total_lines,
            "avg_score": round(float(self.avg_score), 4),
            "image_size": {
                "width": self.image_size[0],
                "height": self.image_size[1],
            },
            "timing": {
                "det_elapse": round(self.det_elapse, 4),
                "cls_elapse": round(self.cls_elapse, 4),
                "rec_elapse": round(self.rec_elapse, 4),
                "total_elapse": round(self.total_elapse, 4),
            },
        }

    def filter_by_score(self, min_score: float) -> "OCRResult":
        """Lọc danh sách các dòng văn bản có độ tin cậy >= min_score."""
        filtered_lines = [l for l in self.lines if l.score >= min_score]
        filtered_text = "\n".join([l.text for l in filtered_lines]).strip()
        avg_score = (
            sum(l.score for l in filtered_lines) / len(filtered_lines)
            if filtered_lines
            else 0.0
        )
        return OCRResult(
            text=filtered_text,
            lines=filtered_lines,
            image_size=self.image_size,
            total_lines=len(filtered_lines),
            avg_score=avg_score,
            det_elapse=self.det_elapse,
            cls_elapse=self.cls_elapse,
            rec_elapse=self.rec_elapse,
            total_elapse=self.total_elapse,
            raw_result=self.raw_result,
        )

    def draw_visualization(
        self,
        image_input: Union[str, Path, bytes, np.ndarray, Image.Image],
        font_path: Optional[str] = None,
    ) -> np.ndarray:
        """
        Vẽ bounding box polygon và nhãn văn bản lên ảnh gốc.
        Trả về mảng numpy ảnh màu RGB (có thể dùng cv2.imwrite hoặc PIL để lưu).
        """
        # Đọc ảnh gốc sang PIL Image (RGB)
        pil_img = _to_pil_image(image_input)
        draw = ImageDraw.Draw(pil_img)

        # Tìm font chữ hỗ trợ tiếng Việt
        font = _load_display_font(font_path)

        for line in self.lines:
            pts = [(float(p[0]), float(p[1])) for p in line.box]
            if len(pts) >= 4:
                # Vẽ polygon bounding box viền xanh lá
                draw.polygon(pts, outline=(0, 200, 0), width=2)

                # Vẽ nhãn text nhỏ góc trên bên trái box
                x0, y0 = pts[0]
                label = f"{line.text} ({line.score:.2f})"
                bbox = font.getbbox(label)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
                draw.rectangle([x0, max(0, y0 - th - 4), x0 + tw + 4, max(0, y0 - th - 4) + th + 4], fill=(0, 150, 0))
                draw.text((x0 + 2, max(0, y0 - th - 4) + 1), label, fill=(255, 255, 255), font=font)

        return np.array(pil_img)

    def save_visualization(
        self,
        image_input: Union[str, Path, bytes, np.ndarray, Image.Image],
        output_path: str,
        font_path: Optional[str] = None,
    ) -> str:
        """Vẽ bounding box và lưu ảnh kết quả trực quan ra file."""
        vis_arr = self.draw_visualization(image_input, font_path=font_path)
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        vis_pil = Image.fromarray(vis_arr)
        vis_pil.save(output_path)
        return output_path


@dataclass
class _OCRJob:
    """Một job nhận diện trong hàng đợi bất đồng bộ."""
    img_input: Any
    min_score: float
    use_det: Optional[bool]
    use_cls: Optional[bool]
    use_rec: Optional[bool]
    kwargs: Dict[str, Any]
    future: "asyncio.Future[OCRResult]"


def _find_default_font() -> Optional[str]:
    """Tìm font TTF có sẵn trên hệ điều hành hỗ trợ tiếng Việt."""
    for fp in DEFAULT_SYSTEM_FONTS:
        if os.path.exists(fp):
            return fp
    return None


def _load_display_font(font_path: Optional[str] = None, size: int = 14) -> ImageFont.FreeTypeFont:
    """Tải font hiển thị, nếu không có sẽ dùng load_default font."""
    chosen_path = font_path or _find_default_font()
    if chosen_path and os.path.exists(chosen_path):
        try:
            return ImageFont.truetype(chosen_path, size=size)
        except Exception:
            pass
    return ImageFont.load_default()


def _to_pil_image(image_input: Union[str, Path, bytes, np.ndarray, Image.Image]) -> Image.Image:
    """Chuyển đổi các định dạng ảnh đầu vào đa dạng thành PIL Image RGB."""
    if isinstance(image_input, Image.Image):
        return image_input.convert("RGB")

    if isinstance(image_input, (str, Path)):
        path_str = str(image_input)
        if not os.path.exists(path_str):
            raise FileNotFoundError(f"Không tìm thấy file ảnh: {path_str}")
        return Image.open(path_str).convert("RGB")

    if isinstance(image_input, (bytes, bytearray)):
        return Image.open(io.BytesIO(image_input)).convert("RGB")

    if isinstance(image_input, io.BytesIO):
        image_input.seek(0)
        return Image.open(image_input).convert("RGB")

    if isinstance(image_input, np.ndarray):
        if image_input.ndim == 2:
            return Image.fromarray(image_input).convert("RGB")
        if image_input.ndim == 3:
            # OpenCV BGR -> RGB
            if image_input.shape[2] == 3:
                return Image.fromarray(cv2.cvtColor(image_input, cv2.COLOR_BGR2RGB))
            if image_input.shape[2] == 4:
                return Image.fromarray(cv2.cvtColor(image_input, cv2.COLOR_BGRA2RGBA)).convert("RGB")

    raise OCRError(f"Định dạng đầu vào không được hỗ trợ: {type(image_input)}")


def _to_cv2_image(image_input: Union[str, Path, bytes, np.ndarray, Image.Image]) -> np.ndarray:
    """Chuyển đổi ảnh đầu vào thành mảng numpy ndarray (chuẩn BGR hoặc RGB) cho RapidOCR."""
    if isinstance(image_input, np.ndarray):
        if image_input.size == 0:
            raise OCRError("Ảnh numpy rỗng (size=0).")
        return image_input

    if isinstance(image_input, Image.Image):
        arr = np.array(image_input.convert("RGB"))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

    if isinstance(image_input, (bytes, bytearray)):
        arr = np.frombuffer(image_input, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise OCRError("Không thể giải mã dữ liệu ảnh từ bytes.")
        return img

    if isinstance(image_input, io.BytesIO):
        image_input.seek(0)
        arr = np.frombuffer(image_input.getvalue(), dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise OCRError("Không thể giải mã dữ liệu ảnh từ BytesIO stream.")
        return img

    if isinstance(image_input, (str, Path)):
        path_str = str(image_input)
        if not os.path.exists(path_str):
            raise FileNotFoundError(f"Không tìm thấy file ảnh: {path_str}")
        # Hỗ trợ tốt đường dẫn Unicode tiếng Việt
        img = cv2.imdecode(np.fromfile(path_str, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            # Thử lại với cv2.imread chuẩn
            img = cv2.imread(path_str)
        if img is None:
            raise OCRError(f"Không thể đọc file ảnh: {path_str}")
        return img

    raise OCRError(f"Kiểu dữ liệu ảnh không hợp lệ: {type(image_input)}")


class OCREngine:
    """
    OCREngine quản lý mô hình nhận diện chữ RapidOCR.
    Khởi tạo model 1 lần khi chạy ứng dụng, phục vụ các yêu cầu nhận diện
    từ Telegram Bot, Flask Web API, CLI và các dịch vụ tự động hóa.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        det_model_path: Optional[str] = None,
        rec_model_path: Optional[str] = None,
        cls_model_path: Optional[str] = None,
        rec_keys_path: Optional[str] = None,
        num_threads: Optional[int] = None,
    ):
        t0 = time.time()
        logger.info("Đang khởi tạo RapidOCR Engine...")

        # Đọc cấu hình từ config.py nếu không chỉ định cụ thể
        self._cfg_path = config_path or getattr(config, "OCR_CONFIG_PATH", "") or None
        self._det_model = det_model_path or getattr(config, "OCR_DET_MODEL_PATH", "") or None
        self._rec_model = rec_model_path or getattr(config, "OCR_REC_MODEL_PATH", "") or None
        self._cls_model = cls_model_path or getattr(config, "OCR_CLS_MODEL_PATH", "") or None
        self._rec_keys = rec_keys_path or getattr(config, "OCR_REC_KEYS_PATH", "") or None
        self._num_threads = num_threads or getattr(config, "OCR_NUM_THREADS", 4)

        # Cấu hình tham số khởi tạo cho RapidOCR
        rapidocr_kwargs: Dict[str, Any] = {}
        if self._cfg_path and os.path.exists(self._cfg_path):
            rapidocr_kwargs["config_path"] = self._cfg_path
        if self._det_model and os.path.exists(self._det_model):
            rapidocr_kwargs["det_model_path"] = self._det_model
        if self._rec_model and os.path.exists(self._rec_model):
            rapidocr_kwargs["rec_model_path"] = self._rec_model
        if self._cls_model and os.path.exists(self._cls_model):
            rapidocr_kwargs["cls_model_path"] = self._cls_model
        if self._rec_keys and os.path.exists(self._rec_keys):
            rapidocr_kwargs["rec_keys_path"] = self._rec_keys

        # Khởi tạo instance RapidOCR
        try:
            self._engine = RapidOCR(**rapidocr_kwargs)
            logger.info("RapidOCR Engine đã sẵn sàng (%.3fs, threads=%d).", time.time() - t0, self._num_threads)
        except Exception as e:
            logger.exception("Lỗi khởi tạo RapidOCR Engine: %s", e)
            raise OCRError(f"Không thể khởi tạo RapidOCR: {e}") from e

        # Hàng đợi bất đồng bộ và executor tuần tự (1 worker)
        self._queue: "asyncio.Queue[_OCRJob]" = asyncio.Queue()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr-worker")
        self._worker_task: Optional[asyncio.Task] = None

    # --------------------------------------------------------------------------
    # Lifecycle Management
    # --------------------------------------------------------------------------

    def start(self):
        """Khởi động worker background xử lý hàng đợi OCR (nếu dùng async)."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())
            logger.info("OCR worker đã khởi động, sẵn sàng nhận job.")

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
        logger.info("OCR worker đã dừng.")

    # --------------------------------------------------------------------------
    # Worker Loop (Async Queue)
    # --------------------------------------------------------------------------

    async def _worker_loop(self):
        """Vòng lặp chạy nền liên tục lấy job từ queue và xử lý tuần tự qua ThreadPoolExecutor."""
        while True:
            job = await self._queue.get()
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    self._executor,
                    self._process_job_sync,
                    job,
                )
                if not job.future.cancelled():
                    job.future.set_result(result)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Lỗi khi xử lý OCR job: %s", exc)
                if not job.future.cancelled():
                    job.future.set_exception(OCRError(str(exc)))
            finally:
                self._queue.task_done()

    def _process_job_sync(self, job: _OCRJob) -> OCRResult:
        """Xử lý job OCR đồng bộ trong worker thread."""
        return self.ocr_sync(
            image_input=job.img_input,
            min_score=job.min_score,
            use_det=job.use_det,
            use_cls=job.use_cls,
            use_rec=job.use_rec,
            **job.kwargs,
        )

    # --------------------------------------------------------------------------
    # Synchronous Inference Core
    # --------------------------------------------------------------------------

    def ocr_sync(
        self,
        image_input: Union[str, Path, bytes, np.ndarray, Image.Image],
        min_score: Optional[float] = None,
        use_det: Optional[bool] = None,
        use_cls: Optional[bool] = None,
        use_rec: Optional[bool] = None,
        **kwargs,
    ) -> OCRResult:
        """
        Nhận diện văn bản đồng bộ từ hình ảnh.
        Phù hợp cho Flask API routes đồng bộ, CLI scripts, kiểm thử hoặc tiền xử lý dữ liệu.

        Args:
            image_input: Đường dẫn file ảnh, dữ liệu bytes, numpy ndarray hoặc PIL Image.
            min_score: Ngưỡng lọc độ tin cậy tối thiểu (mặc định lấy từ config.OCR_MIN_SCORE hoặc 0.5).
            use_det: Bật/tắt phát hiện vùng chữ (Text Detection).
            use_cls: Bật/tắt phân loại hướng xoay chữ (Text Angle Classification).
            use_rec: Bật/tắt nhận diện ký tự (Text Recognition).
            **kwargs: Các tùy chọn bổ sung truyền cho RapidOCR.__call__ (ví dụ: box_thresh, unclip_ratio).

        Returns:
            OCRResult: Đối tượng chứa toàn bộ văn bản, danh sách dòng, bounding box và độ tin cậy.
        """
        t_start = time.time()

        # Chuẩn hoá ảnh đầu vào thành ndarray cho RapidOCR
        img_cv2 = _to_cv2_image(image_input)
        h, w = img_cv2.shape[:2]
        if h <= 0 or w <= 0:
            raise OCRError(f"Kích thước ảnh không hợp lệ: {w}x{h}")

        # Xác định các cờ bật/tắt module
        eff_use_det = getattr(config, "OCR_USE_DET", True) if use_det is None else use_det
        eff_use_cls = getattr(config, "OCR_USE_CLS", True) if use_cls is None else use_cls
        eff_use_rec = getattr(config, "OCR_USE_REC", True) if use_rec is None else use_rec
        eff_min_score = getattr(config, "OCR_MIN_SCORE", 0.5) if min_score is None else min_score

        # Thực thi nhận diện với RapidOCR
        try:
            raw_result, elapse = self._engine(
                img_cv2,
                use_det=eff_use_det,
                use_cls=eff_use_cls,
                use_rec=eff_use_rec,
                **kwargs,
            )
        except Exception as e:
            logger.exception("Lỗi trong quá trình inference của RapidOCR: %s", e)
            raise OCRError(f"Lỗi inference RapidOCR: {e}") from e

        # Phân rã thời gian xử lý
        det_elapse = elapse[0] if elapse and len(elapse) > 0 else 0.0
        cls_elapse = elapse[1] if elapse and len(elapse) > 1 else 0.0
        rec_elapse = elapse[2] if elapse and len(elapse) > 2 else 0.0
        total_elapse = time.time() - t_start

        lines: List[OCRLine] = []
        if raw_result:
            for item in raw_result:
                # Cấu trúc của item trong rapidocr: [box_points, text, score]
                box_pts = item[0]
                text_str = str(item[1]).strip()
                score_val = float(item[2])

                if score_val >= eff_min_score and text_str:
                    lines.append(
                        OCRLine(
                            text=text_str,
                            score=score_val,
                            box=[[float(c) for c in pt] for pt in box_pts],
                        )
                    )

        # Ghép toàn bộ các dòng theo thứ tự từ trên xuống dưới
        full_text = "\n".join([line.text for line in lines]).strip()
        avg_score = (
            sum(line.score for line in lines) / len(lines) if lines else 0.0
        )

        logger.info(
            "OCR hoàn tất: tìm thấy %d/%d dòng hợp lệ (avg_score=%.3f, thời gian=%.3fs).",
            len(lines),
            len(raw_result) if raw_result else 0,
            avg_score,
            total_elapse,
        )

        return OCRResult(
            text=full_text,
            lines=lines,
            image_size=(w, h),
            total_lines=len(lines),
            avg_score=avg_score,
            det_elapse=det_elapse,
            cls_elapse=cls_elapse,
            rec_elapse=rec_elapse,
            total_elapse=total_elapse,
            raw_result=raw_result,
        )

    # --------------------------------------------------------------------------
    # Asynchronous Interface (Public)
    # --------------------------------------------------------------------------

    async def ocr(
        self,
        image_input: Union[str, Path, bytes, np.ndarray, Image.Image],
        min_score: Optional[float] = None,
        use_det: Optional[bool] = None,
        use_cls: Optional[bool] = None,
        use_rec: Optional[bool] = None,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> OCRResult:
        """
        Cổng vào nhận diện OCR bất đồng bộ:
        Đẩy yêu cầu vào hàng đợi nội bộ và chờ kết quả một cách an toàn trên event loop.
        Phù hợp cho Telegram Bot handlers, các API bất đồng bộ (aiohttp / FastAPI / Quart).
        """
        self.start()

        eff_timeout = timeout or getattr(config, "OCR_TIMEOUT_SEC", 60)
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[OCRResult]" = loop.create_future()

        job = _OCRJob(
            img_input=image_input,
            min_score=min_score if min_score is not None else getattr(config, "OCR_MIN_SCORE", 0.5),
            use_det=use_det,
            use_cls=use_cls,
            use_rec=use_rec,
            kwargs=kwargs,
            future=future,
        )

        await self._queue.put(job)
        logger.info("Đã nhận OCR job (hàng đợi đang có: %d job)", self._queue.qsize())

        try:
            return await asyncio.wait_for(future, timeout=eff_timeout)
        except asyncio.TimeoutError:
            future.cancel()
            raise OCRError(f"Xử lý OCR quá thời gian cho phép ({eff_timeout}s).")


# Singleton instance dùng chung toàn hệ thống (tương tự stt_engine và tts_engine)
ocr_engine = OCREngine()
