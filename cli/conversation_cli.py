#!/usr/bin/env python3
"""
Entrypoint chạy Conversation CLI cho Nông Ký AI.
Cách dùng:
  python conversation_cli.py
  python conversation_cli.py --test
  python conversation_cli.py --tts
  python conversation_cli.py --image /mnt/data/photos/photo_1570_0.jpg
"""

import sys
import os

# Thêm thư mục gốc vào path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from conversation.cli import main

if __name__ == "__main__":
    main()
