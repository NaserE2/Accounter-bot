FROM python:3.10-slim

# جلوگیری از کش کردن و به‌روزرسانی مخازن
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-fas \
    poppler-utils \
    fonts-dejavu \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

# تنظیم متغیرهای محیطی برای فونت
ENV LANG=C.UTF-8

# کپی فایل‌های پروژه
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# پورت پیش‌فرض استریم‌لیت
EXPOSE 8501

# اجرای همزمان ربات (در پس‌زمینه) و داشبورد (در پیش‌زمینه برای زنده نگه داشتن سرویس رندر)
CMD streamlit run dashboard.py --server.port=8501 --server.address=0.0.0.0 & python bot.py