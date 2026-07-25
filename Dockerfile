FROM python:3.10-slim

# نصب ابزارهای سیستمی مورد نیاز
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-fas \
    poppler-utils \
    fonts-dejavu \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=C.UTF-8

WORKDIR /app

# نصب نسخه پایدار setuptools که شامل pkg_resources است و جلوگیری از آپدیت خودکار آن
RUN pip install --no-cache-dir --upgrade "pip<24.1" "setuptools<70" wheel

COPY requirements.txt .

# نصب پکیج‌ها بدون ایزوله‌سازی بیلد تا از setuptools سراسری استفاده کند
RUN pip install --no-cache-dir --no-build-isolation -r requirements.txt

COPY . .

EXPOSE 8501

# اجرای همزمان داشبورد (برای زنده نگه داشتن سرویس) و ربات
CMD streamlit run dashboard.py --server.port=8501 --server.address=0.0.0.0 & python bot.py
