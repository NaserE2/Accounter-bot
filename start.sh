#!/bin/bash
apt-get update && apt-get install -y \
    tesseract-ocr-fas \
    poppler-utils \
    fonts-dejavu \
    fontconfig
# کپی فونت به مسیر مناسب
cp /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf ./
python bot.py