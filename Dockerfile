FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-fas \
    poppler-utils \
    fonts-dejavu \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=C.UTF-8
# تعریف متغیر محیطی پورت برای سازگاری بهتر با Render
ENV PORT=8501

WORKDIR /app

RUN pip install --no-cache-dir --upgrade "pip<24.1" "setuptools<70" wheel

COPY requirements.txt .

RUN pip install --no-cache-dir --no-build-isolation -r requirements.txt

COPY . .

EXPOSE 8501

# اجرای همزمان: داشبورد در پیش‌زمینه و ربات در پس‌زمینه
CMD streamlit run dashboard.py --server.port=$PORT --server.address=0.0.0.0 & python bot.py
