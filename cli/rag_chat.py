#!/usr/bin/env python3
"""
RAG Chat CLI (cli/rag_chat.py)
==============================
Terminal interactive để hỏi đáp với knowledge base VietGAP bưởi,
sử dụng RAG retrieval (Hybrid E5 + BM25) + Google Gemini (siêu tốc ~1.5s, chuẩn xác).

Pipeline:
  [Câu hỏi] → Safety Guardrail (< 1ms: phát hiện hoạt chất cấm theo Thông tư 75)
            → Hybrid Search (Dense E5 + BM25Okapi + RRF)
            → System prompt + context VietGAP → Google Gemini (hoặc Local Qwen 2B)
            → Câu trả lời chuẩn xác + nguồn trích dẫn [1], [2]

Sử dụng:
    cd /root/voice_to_data
    source .venv/bin/activate
    python3 cli/rag_chat.py                       # Mặc định dùng Gemini
    python3 cli/rag_chat.py --engine local        # Dùng Qwen 2B cục bộ (:8080)
    python3 cli/rag_chat.py --top-k 5 --temperature 0.2
    python3 cli/rag_chat.py --no-llm              # Chỉ xem retrieval (không gọi LLM)

Lệnh trong chat:
    /quit, /exit, /q   — thoát
    /gemini            — chuyển sang Google Gemini (mặc định)
    /local             — chuyển sang Qwen3.5-2B cục bộ
    /engine            — xem engine AI hiện tại
    /info              — thông tin index
    /clear             — xoá lịch sử conversation
    /top <N>           — đổi số kết quả retrieval
    /temp <T>          — đổi temperature LLM
    /nollm             — bật/tắt LLM (chỉ xem retrieval)
    /sources           — bật/tắt hiển thị nguồn
    /last              — xem raw retrieval của câu hỏi vừa rồi
"""

import argparse
import json
import logging
import os
import readline  # noqa: F401 – enables arrow-key history in input()
import sys
import time
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import config

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

# ──────────────────────────────────────────────
# ANSI colors
# ──────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()

def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

GREEN   = lambda t: _c(str(t), "32")
RED     = lambda t: _c(str(t), "31")
YELLOW  = lambda t: _c(str(t), "33")
CYAN    = lambda t: _c(str(t), "36")
BLUE    = lambda t: _c(str(t), "34")
BOLD    = lambda t: _c(str(t), "1")
DIM     = lambda t: _c(str(t), "2")
MAGENTA = lambda t: _c(str(t), "35")


# ──────────────────────────────────────────────
# Local LLM Fallback (Qwen 2B qua RKLLM Server)
# ──────────────────────────────────────────────

LOCAL_LLM_URL   = getattr(config, "LOCAL_AI_BASE_URL", "http://127.0.0.1:8080") + "/v1/chat/completions"
LOCAL_LLM_MODEL = getattr(config, "LOCAL_AI_MODEL", "Qwen3.5-2B_RK3588_w8a8")

def call_local_llm(
    messages: List[Dict[str, str]],
    temperature: float = 0.3,
    max_tokens: int = 512,
    timeout: int = 60,
) -> str:
    """Gọi LLM server cục bộ (Qwen 2B) đồng bộ."""
    payload = {
        "model":       LOCAL_LLM_MODEL,
        "messages":    messages,
        "max_tokens":  max_tokens,
        "temperature": temperature,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        LOCAL_LLM_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"].strip()
    except urllib.error.URLError as e:
        raise RuntimeError(f"Local LLM server không phản hồi: {e}") from e
    except (KeyError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Local LLM response lỗi: {e}") from e


# ──────────────────────────────────────────────
# Format helpers
# ──────────────────────────────────────────────

def _score_bar(score: float, width: int = 10) -> str:
    filled = max(0, min(width, round(score * width)))
    bar = "█" * filled + "░" * (width - filled)
    color = "32" if score >= 0.8 else ("33" if score >= 0.6 else "31")
    return _c(bar, color)


def _wrap(text: str, indent: int = 4, width: int = 80) -> str:
    prefix = " " * indent
    return textwrap.fill(text, width=width, initial_indent=prefix, subsequent_indent=prefix)


def _print_sources(results: List[Dict], show_text: bool = False):
    print(DIM("\n── Nguồn tài liệu tham khảo ────────────────────────"))
    for i, r in enumerate(results, 1):
        score = r.get("score", 0.0)
        score_bar = _score_bar(score)
        level = r.get("level", "chunk")
        level_tag = CYAN(f"[{level}]")
        title = r.get("section_title") or r.get("title") or "Tài liệu kỹ thuật"
        src_file = r.get("source_file", "")
        pages = r.get("source_pages", [])
        page_str = f" trang {pages}" if pages else ""
        print(f"  {BOLD(str(i))}. {score_bar} {score:.3f}  {level_tag}  {title[:55]}")
        if src_file:
            print(DIM(f"     {src_file}{page_str}"))
        if show_text:
            preview = (r.get("text") or "")[:120].replace("\n", " ")
            print(DIM(f"     |{preview}...|"))


# ──────────────────────────────────────────────
# RAG Chat Session
# ──────────────────────────────────────────────

class RAGChatSession:
    def __init__(
        self,
        engine: str = "gemini",
        top_k: int = 5,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        use_llm: bool = True,
        show_sources: bool = True,
    ):
        self.engine       = engine.lower()
        self.top_k        = top_k
        self.temperature  = temperature
        self.max_tokens   = max_tokens
        self.use_llm      = use_llm
        self.show_sources = show_sources
        self._last_results: List[Dict] = []
        self._conv_history: List[Dict] = []

        print("⏳ Khởi tạo RAG Engine & Index ...", end="", flush=True)
        t0 = time.perf_counter()
        from services.rag.rag_engine import rag_engine
        self._rag_engine = rag_engine
        self._kb = rag_engine._get_kb()
        elapsed = time.perf_counter() - t0
        info = self._kb.info()
        print(f"\r✅ RAG loaded: {info['num_vectors']} vectors ({elapsed:.1f}s) | Engine: {self.engine.upper()}   ")

    def ask(self, question: str) -> Optional[str]:
        """Quy trình hỏi đáp RAG với Gemini hoặc Local LLM."""
        # 1. Chốt chặn an toàn hoá chất cấm (< 1ms)
        from services.rag.safety_checker import safety_checker
        safety_ans = safety_checker.route_and_answer_safety_query(question)
        if safety_ans and safety_ans.handled:
            print(DIM(f"\n  [🛡️ Safety Guardrail: < 1ms — Căn cứ: {safety_ans.source_citation}]"))
            if safety_ans.is_banned:
                print(RED(f"  ⛔ CẢNH BÁO PHÁP LÝ: HOẠT CHẤT / THUỐC ĐÃ BỊ CẤM: {safety_ans.entity_name.upper()}"))
            else:
                print(GREEN(f"  ✅ THÔNG TIN HỢP PHÁP / ĐÃ XÁC THỰC: {safety_ans.entity_name}"))
            print()
            self._conv_history.append({"role": "user", "content": question})
            self._conv_history.append({"role": "assistant", "content": safety_ans.markdown_answer})
            return safety_ans.markdown_answer

        # 2. Retrieval nếu chỉ muốn xem tài liệu
        if not self.use_llm:
            t0 = time.perf_counter()
            results = self._kb.search(question, top_k=self.top_k, use_hybrid=True)
            t_ret = (time.perf_counter() - t0) * 1000
            self._last_results = results

            print(DIM(f"\n  [Retrieval only — {t_ret:.0f}ms]"))
            if self.show_sources:
                _print_sources(results, show_text=True)
            return None

        # 3. Phản hồi bằng Google Gemini Engine (Mặc định)
        if self.engine == "gemini":
            print(DIM(f"\n  [RAG Hybrid Retrieval → Google Gemini generating...]"), end="", flush=True)
            t0 = time.perf_counter()
            res = self._rag_engine.ask(
                question=question,
                top_k=self.top_k,
                history=self._conv_history,
                temperature=self.temperature,
            )
            t_total = (time.perf_counter() - t0) * 1000
            self._last_results = res.sources

            print(DIM(f"\r  [{res.retrieval_ms:.0f}ms retrieval + {res.llm_ms:.0f}ms Gemini ({res.model_used})]          "))

            if self.show_sources and res.sources:
                _print_sources(res.sources)

            self._conv_history.append({"role": "user", "content": question})
            self._conv_history.append({"role": "assistant", "content": res.answer})
            return res.answer

        # 4. Fallback: Local LLM Qwen 2B
        else:
            t0 = time.perf_counter()
            results = self._kb.search(question, top_k=self.top_k, use_hybrid=True)
            t_ret = (time.perf_counter() - t0) * 1000
            self._last_results = results

            if not results:
                return RED("Không tìm thấy tài liệu phù hợp trong knowledge base.")

            if self.show_sources:
                _print_sources(results)

            context_parts = [f"[{i}] {r.get('section_title')}\n{r.get('text', '')[:500]}" for i, r in enumerate(results, 1)]
            context_text = "\n\n---\n\n".join(context_parts)
            system_msg = f"Bạn là trợ lý kỹ thuật canh tác cây bưởi VietGAP.\nTÀI LIỆU:\n{context_text}\nHãy trả lời chính xác, trích dẫn [1], [2]."

            history_tail = self._conv_history[-4:] if self._conv_history else []
            messages = [
                {"role": "system", "content": system_msg},
                *history_tail,
                {"role": "user", "content": question},
            ]

            print(DIM(f"\n  [Retrieval {t_ret:.0f}ms → Local Qwen-2B thinking...]"), end="", flush=True)
            t1 = time.perf_counter()
            try:
                answer = call_local_llm(messages, self.temperature, self.max_tokens)
                t_llm = (time.perf_counter() - t1) * 1000
                print(DIM(f"\r  [{t_ret:.0f}ms retrieval + {t_llm:.0f}ms Qwen-2B]          "))
            except RuntimeError as e:
                print(f"\r  {RED(str(e))}")
                return None

            self._conv_history.append({"role": "user", "content": question})
            self._conv_history.append({"role": "assistant", "content": answer})
            return answer

    def handle_command(self, cmd: str) -> bool:
        """Xử lý lệnh terminal bắt đầu bằng /."""
        parts = cmd.strip().split()
        c = parts[0].lower()

        if c in ("/quit", "/exit", "/q"):
            return True
        elif c == "/gemini":
            self.engine = "gemini"
            print(GREEN("✓ Đã chuyển sang model: Google Gemini (Siêu tốc & Chuẩn xác)"))
        elif c == "/local":
            self.engine = "local"
            print(YELLOW(f"✓ Đã chuyển sang model: {LOCAL_LLM_MODEL} (Cục bộ :8080)"))
        elif c == "/engine":
            eng_str = GREEN("Google Gemini") if self.engine == "gemini" else YELLOW("Local Qwen 2B")
            print(f"  AI Engine hiện tại: {eng_str}")
        elif c == "/info":
            info = self._kb.info()
            print(CYAN(json.dumps(info, ensure_ascii=False, indent=2)))
        elif c == "/clear":
            self._conv_history.clear()
            print(GREEN("✓ Đã xoá lịch sử hội thoại."))
        elif c == "/top":
            if len(parts) > 1 and parts[1].isdigit():
                self.top_k = int(parts[1])
                print(GREEN(f"✓ top_k = {self.top_k}"))
            else:
                print(f"  top_k hiện tại: {self.top_k}")
        elif c == "/temp":
            if len(parts) > 1:
                try:
                    self.temperature = float(parts[1])
                    print(GREEN(f"✓ temperature = {self.temperature}"))
                except ValueError:
                    print(RED("Giá trị không hợp lệ."))
            else:
                print(f"  temperature hiện tại: {self.temperature}")
        elif c == "/nollm":
            self.use_llm = not self.use_llm
            state = GREEN("BẬT") if self.use_llm else YELLOW("TẮT")
            print(f"  LLM: {state}")
        elif c == "/sources":
            self.show_sources = not self.show_sources
            state = GREEN("BẬT") if self.show_sources else YELLOW("TẮT")
            print(f"  Hiện nguồn: {state}")
        elif c == "/last":
            if self._last_results:
                _print_sources(self._last_results, show_text=True)
            else:
                print(DIM("  (Chưa có câu hỏi nào.)"))
        elif c == "/help":
            print(DIM("""
  Lệnh điều khiển:
    /quit  /exit  /q  — Thoát chương trình
    /gemini          — Chuyển sang Google Gemini (mặc định)
    /local           — Chuyển sang Qwen 2B cục bộ (:8080)
    /engine          — Xem engine AI hiện tại
    /info            — Thông tin index vector
    /clear           — Xoá lịch sử hội thoại
    /top <N>         — Đổi số kết quả retrieval (top_k)
    /temp <T>        — Đổi temperature LLM (0.0 - 1.0)
    /nollm           — Bật/tắt gọi LLM (chỉ xem retrieval)
    /sources         — Bật/tắt hiển thị nguồn tài liệu
    /last            — Xem chi tiết các chunks vừa tìm được
    /help            — Xem hướng dẫn này
"""))
        else:
            print(DIM(f"  Lệnh không nhận ra: {c} (gõ /help để xem trợ giúp)"))

        return False


# ──────────────────────────────────────────────
# Main CLI Loop
# ──────────────────────────────────────────────

BANNER = """
╔══════════════════════════════════════════════════════════╗
║  🌿  RAG Chat — VietGAP Bưởi Knowledge Base             ║
║      Engine: Google Gemini + Hybrid Search (E5 + BM25)   ║
║      Tích hợp Safety Guardrail chống chất cấm (< 1ms)    ║
║      Gõ /help để xem lệnh, /quit để thoát               ║
╚══════════════════════════════════════════════════════════╝
"""


def main():
    parser = argparse.ArgumentParser(
        description="RAG Chat CLI — VietGAP Bưởi với Google Gemini",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--engine",     type=str,   default="gemini", choices=["gemini", "local"],
                        help="Engine sinh câu trả lời: 'gemini' (mặc định) hoặc 'local' (Qwen 2B)")
    parser.add_argument("--top-k",      type=int,   default=5,   help="Số chunks retrieval (default: 5)")
    parser.add_argument("--temperature", type=float, default=0.2, help="LLM temperature (default: 0.2)")
    parser.add_argument("--max-tokens", type=int,   default=1024, help="LLM max output tokens (default: 1024)")
    parser.add_argument("--no-llm",     action="store_true",     help="Chỉ retrieval, không gọi LLM")
    parser.add_argument("--no-sources", action="store_true",     help="Ẩn nguồn tài liệu")
    parser.add_argument("--question",   type=str,   default=None,
                        help="Hỏi 1 câu rồi thoát (batch mode)")
    args = parser.parse_args()

    print(BOLD(MAGENTA(BANNER)))

    session = RAGChatSession(
        engine      = args.engine,
        top_k       = args.top_k,
        temperature = args.temperature,
        max_tokens  = args.max_tokens,
        use_llm     = not args.no_llm,
        show_sources = not args.no_sources,
    )

    eng_status = GREEN("Google Gemini") if args.engine == "gemini" else YELLOW("Qwen3.5-2B @ :8080")
    print(f"  Engine: {eng_status} | top_k: {args.top_k} | temp: {args.temperature}\n")

    # ── Batch mode: 1 câu hỏi ───────────────────
    if args.question:
        answer = session.ask(args.question)
        if answer:
            print(f"\n{BOLD(GREEN('💬 Trả lời:'))}")
            print(answer)
        return

    # ── Interactive loop ─────────────────────────
    while True:
        try:
            raw = input(BOLD(CYAN("\n🌿 Câu hỏi: "))).strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{DIM('Thoát.')}")
            break

        if not raw:
            continue

        if raw.startswith("/"):
            should_exit = session.handle_command(raw)
            if should_exit:
                print(DIM("Tạm biệt! 👋"))
                break
            continue

        answer = session.ask(raw)
        if answer:
            print(f"\n{BOLD(GREEN('💬 Trả lời:'))}\n")
            print(answer)


if __name__ == "__main__":
    main()
