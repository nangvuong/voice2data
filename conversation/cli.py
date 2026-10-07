"""
Conversation CLI - Giao diện dòng lệnh tương tác trực tiếp cho Nông Ký AI.
Cho phép nông dân và kỹ thuật viên:
- Đối thoại đa lượt bằng văn bản hoặc giọng nói (STT).
- Gửi ảnh hiện trường để:
    [1] Ghi nhật ký VietGAP tự động
    [2] Nhận tư vấn thông tin về thuốc BVTV, phân bón, sâu bệnh hại
- Tự động sinh câu hỏi gợi mở điền khuyết bằng Template.
- Tự động sinh âm thanh câu trả lời bằng TTS tiếng Việt.
- Xem trạng thái Slot trực tiếp và lưu CSDL MySQL an toàn.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid

# Đảm bảo đường dẫn gốc của project nằm trong sys.path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from conversation.manager import conversation_manager
from conversation.state import DialogState, session_store
import config

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger("conversation.cli")


# Mã màu ANSI hiển thị trên Terminal
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_BLUE = "\033[94m"
C_CYAN = "\033[96m"
C_RED = "\033[91m"
C_MAGENTA = "\033[95m"


def print_banner(session_id: str, enable_tts: bool):
    """In giao diện banner chào mừng."""
    print(C_CYAN + "=" * 80 + C_RESET)
    print(C_BOLD + C_GREEN + "   🌾 NÔNG KÝ AI - CONVERSATION CLI (VIETGAP MULTI-TURN ASSISTANT) 🌾" + C_RESET)
    print(C_CYAN + "=" * 80 + C_RESET)
    tts_status = f"{C_GREEN}BẬT (Piper VITS){C_RESET}" if enable_tts else f"{C_YELLOW}TẮT{C_RESET}"
    print(f"Phiên làm việc: {C_BOLD}{session_id}{C_RESET} | Nông hộ: {C_BOLD}nongdan1{C_RESET} | Giọng nói (TTS): {tts_status}")
    print(C_CYAN + "-" * 80 + C_RESET)
    print(C_BOLD + "Hướng dẫn sử dụng:" + C_RESET)
    print(f"  • {C_GREEN}> [câu nói]{C_RESET}        : Gõ nội dung hoạt động canh tác (ví dụ: 'Sáng nay phun thuốc Nano Ecotech')")
    print(f"  • {C_GREEN}/image <file>{C_RESET}     : Gửi ảnh (Hỗ trợ 2 hướng: [1] Ghi nhật ký | [2] Tư vấn thuốc, phân, bệnh)")
    print(f"  • {C_GREEN}/audio <file>{C_RESET}     : Gửi file ghi âm âm thanh (.wav, .ogg, .mp3)")
    print(f"  • {C_GREEN}/state{C_RESET}            : Xem chi tiết các trường (slots) hiện có trong phiên")
    print(f"  • {C_GREEN}/tts on|off{C_RESET}      : Bật / Tắt đọc câu trả lời bằng giọng nói tiếng Việt")
    print(f"  • {C_GREEN}/reset{C_RESET}            : Bắt đầu lại một phiên ghi chép mới")
    print(f"  • {C_GREEN}/test{C_RESET}             : Chạy bộ kiểm thử kịch bản tự động (Automated Scenarios)")
    print(f"  • {C_GREEN}/help{C_RESET}             : Xem lại trợ giúp")
    print(f"  • {C_GREEN}/exit{C_RESET} hoặc {C_GREEN}/quit{C_RESET} : Thoát chương trình")
    print(C_CYAN + "=" * 80 + C_RESET + "\n")


def print_assistant_reply(res: dict, enable_tts: bool):
    """In câu trả lời của trợ lý với định dạng đẹp mắt."""
    dialog_state = res.get("dialog_state", "IDLE")
    state_color = {
        "IDLE": C_BLUE,
        "COLLECTING": C_YELLOW,
        "CLARIFYING": C_YELLOW,
        "CONFIRMING": C_MAGENTA,
        "IMAGE_CHOICE": C_CYAN,
        "ADVISORY": C_GREEN,
        "COMMITTED": C_GREEN,
        "CANCELLED": C_RED,
    }.get(dialog_state, C_RESET)

    print("\n" + C_BOLD + C_CYAN + "🤖 TRỢ LÝ NÔNG KÝ AI:" + C_RESET)
    print(f"   [{C_BOLD}TRẠNG THÁI: {state_color}{dialog_state}{C_RESET}]")
    print(C_CYAN + "   " + "-" * 70 + C_RESET)

    reply_text = res.get("response_text", "")
    for line in reply_text.split("\n"):
        print(f"   {line}")
    print(C_CYAN + "   " + "-" * 70 + C_RESET)

    # Hiển thị tóm tắt slot nếu có
    slots = res.get("accumulated_slots")
    if slots:
        print(f"   {C_BOLD}📦 Dữ liệu đã thu thập ({len(slots)} trường):{C_RESET}")
        for k, v in slots.items():
            print(f"      • {C_YELLOW}{k}{C_RESET}: {v}")

    missing = res.get("missing_slots")
    if missing:
        missing_labels = [m["label"] for m in missing]
        print(f"   {C_RED}⚠️ Còn thiếu {len(missing)} trường bắt buộc: {', '.join(missing_labels)}{C_RESET}")

    # Nếu có audio sinh ra
    audio_path = res.get("audio_file")
    if audio_path and os.path.exists(audio_path):
        print(f"   {C_GREEN}🔊 Âm thanh phản hồi (TTS): {audio_path}{C_RESET}")
        # Thử phát âm thanh nếu có aplay
        try:
            if shutil.which("aplay") and os.environ.get("ENABLE_APLAY") == "1":
                subprocess.Popen(["aplay", "-q", audio_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    print()


async def run_automated_test_suite():
    """Chạy kịch bản tự động để kiểm thử đa lượt."""
    print("\n" + C_BOLD + C_YELLOW + "🧪 BẮT ĐẦU CHẠY BỘ KIỂM THỬ KỊCH BẢN ĐA LƯỢT (AUTOMATED TEST SUITE)..." + C_RESET)
    print("=" * 80)

    # -------------------------------------------------------------------------
    # Kịch bản 1: Phun thuốc BVTV điền khuyết 2 lượt
    # -------------------------------------------------------------------------
    print(C_BOLD + C_CYAN + "\n[TEST 1] Kịch bản Phun thuốc BVTV (Lượt 1 thiếu liều lượng & cách ly -> Lượt 2 bổ sung -> Lưu)" + C_RESET)
    t1_sess = f"test_{uuid.uuid4().hex[:6]}"
    
    # Turn 1
    print(f"{C_BOLD}👨‍🌾 Nông dân (Turn 1):{C_RESET} Sáng nay tôi phun thuốc sinh học Nano Ecotech cho đám cà gai leo.")
    r1 = await conversation_manager.handle_text(t1_sess, "Sáng nay tôi phun thuốc sinh học Nano Ecotech cho đám cà gai leo.")
    print(f"🤖 Trợ lý: {r1['response_text']}")
    print(f"   Trạng thái FSM: {r1['dialog_state']} | Missing slots: {len(r1['missing_slots'])}")
    assert r1["dialog_state"] == DialogState.CLARIFYING.value, "Lượt 1 phải ở trạng thái CLARIFYING"

    # Turn 2
    print(f"\n{C_BOLD}👨‍🌾 Nông dân (Turn 2):{C_RESET} Trị rầy nâu, pha 2 lít vào 500 lít nước, cách ly 14 ngày nhé.")
    r2 = await conversation_manager.handle_text(t1_sess, "Trị rầy nâu, pha 2 lít vào 500 lít nước, cách ly 14 ngày nhé.")
    print(f"🤖 Trợ lý: {r2['response_text']}")
    print(f"   Trạng thái FSM: {r2['dialog_state']} | Missing slots: {len(r2['missing_slots'])}")
    assert r2["dialog_state"] == DialogState.CONFIRMING.value, "Lượt 2 phải ở trạng thái CONFIRMING"

    # Turn 3
    print(f"\n{C_BOLD}👨‍🌾 Nông dân (Turn 3):{C_RESET} Đồng ý lưu đi cháu.")
    r3 = await conversation_manager.handle_text(t1_sess, "Đồng ý lưu đi cháu.")
    print(f"🤖 Trợ lý: {r3['response_text']}")
    print(f"   Trạng thái FSM: {r3['dialog_state']}")
    assert r3["dialog_state"] == DialogState.COMMITTED.value, "Lượt 3 phải ở trạng thái COMMITTED"

    print(C_BOLD + C_GREEN + "✅ TEST 1 THÀNH CÔNG!" + C_RESET)

    # -------------------------------------------------------------------------
    # Kịch bản 2: Bón phân đính chính (Correction)
    # -------------------------------------------------------------------------
    print(C_BOLD + C_CYAN + "\n[TEST 2] Kịch bản Bón phân có đính chính số lượng (Turn 1: 3 bao -> Turn 2 sửa: 5 bao -> Lưu)" + C_RESET)
    t2_sess = f"test_{uuid.uuid4().hex[:6]}"

    # Turn 1
    print(f"{C_BOLD}👨‍🌾 Nông dân (Turn 1):{C_RESET} Hôm nay bón thúc 3 bao phân NPK cho vườn.")
    r2_1 = await conversation_manager.handle_text(t2_sess, "Hôm nay bón thúc 3 bao phân NPK cho vườn.")
    print(f"🤖 Trợ lý: {r2_1['response_text']}")
    print(f"   Trạng thái FSM: {r2_1['dialog_state']}")

    # Turn 2: Đính chính
    print(f"\n{C_BOLD}👨‍🌾 Nông dân (Turn 2):{C_RESET} À nhầm, 5 bao chứ không phải 3 bao nhé.")
    r2_2 = await conversation_manager.handle_text(t2_sess, "À nhầm, 5 bao chứ không phải 3 bao nhé.")
    print(f"🤖 Trợ lý: {r2_2['response_text']}")
    print(f"   Lượng phân sau khi sửa: {r2_2['accumulated_slots'].get('luong_phan_bon')}")

    # Turn 3: Xác nhận
    print(f"\n{C_BOLD}👨‍🌾 Nông dân (Turn 3):{C_RESET} Lưu luôn.")
    r2_3 = await conversation_manager.handle_text(t2_sess, "Lưu luôn.")
    print(f"🤖 Trợ lý: {r2_3['response_text']}")
    print(f"   Trạng thái FSM: {r2_3['dialog_state']}")
    assert r2_3["dialog_state"] == DialogState.COMMITTED.value, "Lượt 3 phải ở trạng thái COMMITTED"

    print(C_BOLD + C_GREEN + "✅ TEST 2 THÀNH CÔNG!" + C_RESET)

    # -------------------------------------------------------------------------
    # Kịch bản 3: Gửi ảnh khi CHƯA ghi nhật ký (Tư vấn -> Hỏi ghi nhật ký -> Đồng ý)
    # -------------------------------------------------------------------------
    sample_photo = "/mnt/data/photos/photo_1570_0.jpg"
    if os.path.exists(sample_photo):
        print(C_BOLD + C_CYAN + f"\n[TEST 3] Kịch bản Gửi ảnh khi CHƯA ghi nhật ký: Trả lời thông tin + Hỏi ghi nhật ký -> Nông dân đồng ý" + C_RESET)
        t3_sess = f"test_{uuid.uuid4().hex[:6]}"

        # Turn 1: Gửi ảnh khi chưa có phiên nhật ký nào
        print(f"{C_BOLD}👨‍🌾 Nông dân (Turn 1):{C_RESET} [Gửi ảnh: {sample_photo}]")
        r3_1 = await conversation_manager.handle_image(t3_sess, sample_photo)
        print(f"🤖 Trợ lý:\n{r3_1['response_text']}")
        print(f"   Trạng thái FSM: {r3_1['dialog_state']}")
        assert r3_1["dialog_state"] == DialogState.ADVISORY.value, "Phải ở trạng thái ADVISORY"

        # Turn 2: Nông dân đồng ý ghi nhật ký dựa trên ảnh
        print(f"\n{C_BOLD}👨‍🌾 Nông dân (Turn 2):{C_RESET} Có, ghi vào sổ nhật ký giúp tôi.")
        r3_2 = await conversation_manager.handle_text(t3_sess, "Có, ghi vào sổ nhật ký giúp tôi.")
        print(f"🤖 Trợ lý:\n{r3_2['response_text']}")
        print(f"   Trạng thái FSM: {r3_2['dialog_state']}")
        assert r3_2["dialog_state"] in (DialogState.CLARIFYING.value, DialogState.CONFIRMING.value), "Phải chuyển sang luồng ghi nhật ký"

        print(C_BOLD + C_GREEN + "✅ TEST 3 THÀNH CÔNG!" + C_RESET)

        # ---------------------------------------------------------------------
        # Kịch bản 4: Gửi ảnh khi ĐANG trong luồng ghi nhật ký
        # ---------------------------------------------------------------------
        print(C_BOLD + C_CYAN + f"\n[TEST 4] Kịch bản Gửi ảnh khi ĐANG ghi nhật ký: Trả lời thông tin + Tự động đắp vào nhật ký đang ghi" + C_RESET)
        t4_sess = f"test_{uuid.uuid4().hex[:6]}"

        # Turn 1: Bắt đầu khai báo hoạt động
        print(f"{C_BOLD}👨‍🌾 Nông dân (Turn 1):{C_RESET} Hôm nay tôi nhập một số vật tư.")
        r4_1 = await conversation_manager.handle_text(t4_sess, "Hôm nay tôi nhập một số vật tư.")
        print(f"🤖 Trợ lý: {r4_1['response_text']}")
        print(f"   Trạng thái FSM: {r4_1['dialog_state']}")
        assert r4_1["dialog_state"] == DialogState.CLARIFYING.value, "Phải ở trạng thái CLARIFYING"

        # Turn 2: Gửi ảnh bổ sung
        print(f"\n{C_BOLD}👨‍🌾 Nông dân (Turn 2):{C_RESET} [Gửi ảnh: {sample_photo}]")
        r4_2 = await conversation_manager.handle_image(t4_sess, sample_photo)
        print(f"🤖 Trợ lý:\n{r4_2['response_text']}")
        print(f"   Trạng thái FSM: {r4_2['dialog_state']}")
        assert r4_2["dialog_state"] in (DialogState.CLARIFYING.value, DialogState.CONFIRMING.value), "Phải tiếp tục trong luồng ghi nhật ký"

        print(C_BOLD + C_GREEN + "✅ TEST 4 THÀNH CÔNG!" + C_RESET)

    print("\n" + C_BOLD + C_GREEN + "🎉 TOÀN BỘ CÁC BÀI TEST TỰ ĐỘNG ĐÃ VƯỢT QUA XUẤT SẮC!" + C_RESET)
    print("=" * 80 + "\n")


async def interactive_loop(session_id: str, enable_tts: bool):
    """Vòng lặp tương tác dòng lệnh với người dùng."""
    print_banner(session_id, enable_tts)

    while True:
        try:
            prompt_str = f"{C_BOLD}{C_GREEN}👨‍🌾 Nông dân>{C_RESET} "
            user_input = input(prompt_str).strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{C_YELLOW}Tạm biệt bác! Chúc bác một vụ mùa bội thu.{C_RESET}")
            break

        if not user_input:
            continue

        cmd_lower = user_input.lower()

        # 1. Lệnh thoát
        if cmd_lower in ("/exit", "/quit", "exit", "quit"):
            print(f"{C_YELLOW}Tạm biệt bác! Chúc bác một vụ mùa bội thu.{C_RESET}")
            break

        # 2. Lệnh trợ giúp
        elif cmd_lower in ("/help", "help", "/?"):
            print_banner(session_id, enable_tts)
            continue

        # 3. Lệnh đặt lại phiên
        elif cmd_lower in ("/reset", "reset"):
            state = session_store.get_or_create(session_id)
            state.reset()
            print(f"{C_GREEN}🔄 Đã làm mới phiên làm việc. Mời bác bắt đầu ghi chép mới!{C_RESET}\n")
            continue

        # 4. Lệnh xem trạng thái
        elif cmd_lower in ("/state", "state"):
            state = session_store.get_or_create(session_id)
            print(f"\n{C_BOLD}📊 THÔNG TIN TRẠNG THÁI PHIÊN [{session_id}]:{C_RESET}")
            print(json.dumps(state.get_summary(), ensure_ascii=False, indent=2) + "\n")
            continue

        # 5. Lệnh bật/tắt TTS
        elif cmd_lower.startswith("/tts"):
            parts = user_input.split()
            if len(parts) > 1 and parts[1].lower() in ("off", "0", "tat"):
                enable_tts = False
                print(f"{C_YELLOW}🔊 Đã TẮT tính năng đọc giọng nói phản hồi (TTS).{C_RESET}\n")
            else:
                enable_tts = True
                print(f"{C_GREEN}🔊 Đã BẬT tính năng đọc giọng nói phản hồi (TTS Piper VITS).{C_RESET}\n")
            continue

        # 6. Lệnh chạy kiểm thử tự động
        elif cmd_lower in ("/test", "test"):
            await run_automated_test_suite()
            continue

        # 7. Lệnh gửi ảnh
        elif cmd_lower.startswith("/image"):
            parts = user_input.split(maxsplit=1)
            if len(parts) < 2:
                print(f"{C_RED}❌ Vui lòng nhập đường dẫn file ảnh. Ví dụ: /image /mnt/data/photos/photo_1570_0.jpg{C_RESET}\n")
                continue
            img_path = parts[1].strip()
            print(f"{C_YELLOW}⏳ Đang phân tích ảnh qua Gemini VLM Engine, vui lòng chờ trong giây lát...{C_RESET}")
            t0 = time.time()
            res = await conversation_manager.handle_image(session_id, img_path, enable_tts=enable_tts)
            print(f"{C_CYAN}(Thời gian xử lý VLM: {time.time() - t0:.2f}s){C_RESET}")
            print_assistant_reply(res, enable_tts)
            continue

        # 8. Lệnh gửi file âm thanh
        elif cmd_lower.startswith("/audio"):
            parts = user_input.split(maxsplit=1)
            if len(parts) < 2:
                print(f"{C_RED}❌ Vui lòng nhập đường dẫn file âm thanh. Ví dụ: /audio /mnt/data/audio/sample.wav{C_RESET}\n")
                continue
            audio_path = parts[1].strip()
            print(f"{C_YELLOW}⏳ Đang xử lý file ghi âm qua Sherpa-ONNX STT, vui lòng chờ...{C_RESET}")
            t0 = time.time()
            res = await conversation_manager.handle_audio(session_id, audio_path, enable_tts=enable_tts)
            print(f"{C_CYAN}(Thời gian nhận diện STT + Phân tích: {time.time() - t0:.2f}s){C_RESET}")
            if "stt_transcript" in res:
                print(f"{C_BOLD}🎙️ Nội dung nhận diện được:{C_RESET} \"{res['stt_transcript']}\"")
            print_assistant_reply(res, enable_tts)
            continue

        # 9. Văn bản trò chuyện thông thường
        else:
            t0 = time.time()
            res = await conversation_manager.handle_text(session_id, user_input, enable_tts=enable_tts)
            print_assistant_reply(res, enable_tts)


async def cleanup():
    """Đóng an toàn các session HTTP aiohttp."""
    from conversation.gemini_helper import gemini_conv_helper
    from services.engines.vlm_engine import vlm_engine
    try:
        await gemini_conv_helper.close()
    except Exception:
        pass
    try:
        await vlm_engine.close()
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Nông Ký AI - Conversation CLI (VietGAP Multi-turn Assistant)")
    parser.add_argument("--session", "-s", type=str, default=None, help="Mã phiên hội thoại (session ID)")
    parser.add_argument("--tts", action="store_true", help="Bật đọc câu trả lời bằng giọng nói tiếng Việt (TTS)")
    parser.add_argument("--test", action="store_true", help="Chạy bộ kiểm thử kịch bản tự động rồi thoát")
    parser.add_argument("--image", "-i", type=str, default=None, help="Phân tích ảnh trực tiếp qua CLI")
    parser.add_argument("--choice", type=int, choices=[1, 2], default=None, help="Chọn ngay hướng khi phân tích ảnh: 1 (Nhật ký) hoặc 2 (Tư vấn)")
    parser.add_argument("--text", "-t", type=str, default=None, help="Chạy một câu nói đơn lẻ rồi in kết quả ra JSON")

    args = parser.parse_args()

    session_id = args.session or f"cli_{uuid.uuid4().hex[:8]}"

    try:
        if args.test:
            asyncio.run(run_automated_test_suite())
            return

        if args.image:
            async def run_single_image():
                print(f"Đang xử lý ảnh: {args.image}...")
                res = await conversation_manager.handle_image(
                    session_id, args.image, enable_tts=args.tts
                )
                print_assistant_reply(res, args.tts)
            asyncio.run(run_single_image())
            return

        if args.text:
            async def run_single_text():
                res = await conversation_manager.handle_text(session_id, args.text, enable_tts=args.tts)
                print(json.dumps(res, ensure_ascii=False, indent=2))
            asyncio.run(run_single_text())
            return

        # Chạy interactive loop mặc định
        asyncio.run(interactive_loop(session_id, enable_tts=args.tts))
    finally:
        asyncio.run(cleanup())


if __name__ == "__main__":
    main()
