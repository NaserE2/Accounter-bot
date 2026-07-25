import os
import re
import tempfile
import json
import logging
from datetime import datetime, timedelta
import pytz
import asyncio
import shutil

# کتابخانه‌های جدید
import jdatetime
from fpdf import FPDF
import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes

import gspread
from google.oauth2.service_account import Credentials

from faster_whisper import WhisperModel
from PIL import Image
import pytesseract
from pdf2image import convert_from_path

import google.generativeai as genai

# برای چت صوتی
from gtts import gTTS
import io

# ---------- تنظیمات اولیه ----------
TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("متغیر محیطی GEMINI_API_KEY تنظیم نشده است!")

# تنظیم جیمینی
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash')

# ---------- تنظیمات کاربران مجاز (شناسه عددی) ----------
# برای دریافت شناسه عددی، هر کاربر به ربات @userinfobot پیام دهد.
ALLOWED_USERS = [
    123456789,  # شناسه عددی ناصر ابراهیم زاده (108797218)
    987654321,  # شناسه عددی کیمیا خانعلی‌زاده (جایگزین کن)
    456789123   # شناسه عددی مه‌یاس ابراهیم زاده (جایگزین کن)
]
ADMIN_USER_ID = 108797218  # شناسه ناصر (برای گزارش‌های اختصاصی)

# کاربرانی که پیام یادآوری شبانه دریافت می‌کنند (فقط ناصر و کیمیا)
REMINDER_USERS = [
    108797218,  # ناصر
    987654321   # کیمیا
]

SHEET_NAME = "حسابداری_هوشمند_نهایی"
BACKUP_DIR = "backups"
os.makedirs(BACKUP_DIR, exist_ok=True)
TIMEZONE = pytz.timezone('Asia/Tehran')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- صف پردازش درخواست‌ها ----------
request_queue = asyncio.Queue()
is_processing = False

# ---------- اتصال به گوگل شیت ----------
def connect_sheets():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    try:
        sheet = client.open(SHEET_NAME).sheet1
    except gspread.SpreadsheetNotFound:
        sh = client.create(SHEET_NAME)
        sheet = sh.sheet1
        headers = ["ردیف", "تاریخ شمسی", "تاریخ میلادی", "نوع حساب", "نوع تراکنش", "مبلغ(تومان)", 
                   "دسته‌بندی", "توضیحات کامل", "کاربر", "سهم‌بندی شرکا"]
        sheet.append_row(headers)
        # اشتراک‌گذاری با ایمیل خودتان (اختیاری)
        # sh.share('your_email@gmail.com', perm_type='user', role='writer')
    return sheet

# ---------- تابع پشتیبان‌گیری ----------
def backup_data():
    try:
        sheet = connect_sheets()
        records = sheet.get_all_values()
        if len(records) < 2:
            return
        df = pd.DataFrame(records[1:], columns=records[0])
        now_shamsi = jdatetime.date.today().strftime("%Y-%m-%d")
        backup_file = os.path.join(BACKUP_DIR, f"backup_{now_shamsi}.json")
        df.to_json(backup_file, orient="records", force_ascii=False)
        logger.info(f"پشتیبان‌گیری انجام شد: {backup_file}")
        # نگهداری فقط ۳۰ نسخه آخر
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.json')])
        if len(backups) > 30:
            for f in backups[:-30]:
                os.remove(os.path.join(BACKUP_DIR, f))
    except Exception as e:
        logger.error(f"خطا در پشتیبان‌گیری: {e}")

# ---------- تابع بازیابی از پشتیبان ----------
def restore_from_backup(backup_file):
    try:
        df = pd.read_json(backup_file, orient="records")
        sheet = connect_sheets()
        # پاک کردن شیت
        sheet.clear()
        # نوشتن هدرها
        headers = ["ردیف", "تاریخ شمسی", "تاریخ میلادی", "نوع حساب", "نوع تراکنش", "مبلغ(تومان)", 
                   "دسته‌بندی", "توضیحات کامل", "کاربر", "سهم‌بندی شرکا"]
        sheet.append_row(headers)
        # نوشتن داده‌ها
        for _, row in df.iterrows():
            sheet.append_row(row.tolist())
        logger.info(f"بازیابی از {backup_file} انجام شد.")
        return True
    except Exception as e:
        logger.error(f"خطا در بازیابی: {e}")
        return False

# ---------- Fallback با کلمات کلیدی (در صورت عدم دسترسی به Gemini) ----------
def fallback_parse(raw_text):
    """تشخیص دستی با کلمات کلیدی (راه جایگزین)"""
    text_lower = raw_text.lower()
    account = "خانواده"
    trans_type = "هزینه"
    amount = 0
    category = "سایر"
    user = "ناصر"
    
    # تشخیص حساب
    if any(w in text_lower for w in ["ویلا", "یالبندان", "اجاره", "میهمان", "شبانه"]):
        account = "ویلا_یالبندان"
    
    # تشخیص نوع تراکنش
    if any(w in text_lower for w in ["درآمد", "اجاره", "فروش", "پرداخت شد", "دریافت"]):
        trans_type = "درآمد"
    
    # تشخیص کاربر
    if "کیمیا" in text_lower:
        user = "کیمیا"
    elif "مه یاس" in text_lower or "مهیاس" in text_lower:
        user = "مه‌یاس"
    elif "همه" in text_lower or "خونه" in text_lower:
        user = "همه"
    
    # استخراج مبلغ
    match = re.search(r"(\d{1,3}(?:,\d{3})*|\d+)\s*(?:تومان|ت)", text_lower)
    if match:
        amount_str = match.group(1).replace(",", "")
        amount = int(amount_str)
    
    # دسته‌بندی
    if "خواربار" in text_lower or "میوه" in text_lower:
        category = "خواربار"
    elif "قبض" in text_lower or "برق" in text_lower or "آب" in text_lower:
        category = "قبوض"
    elif "شارژ" in text_lower:
        category = "شارژ"
    elif "نظافت" in text_lower:
        category = "نظافت"
    elif "تعمیر" in text_lower:
        category = "تعمیرات"
    elif "تفریح" in text_lower:
        category = "تفریح"
    elif "پوشاک" in text_lower or "لباس" in text_lower:
        category = "پوشاک"
    elif "درمان" in text_lower or "دکتر" in text_lower:
        category = "درمان"
    
    if amount == 0:
        return None, "⚠️ مبلغی در متن پیدا نشد! لطفاً عدد را به تومان وارد کنید."
    
    data = {
        "account": account,
        "type": trans_type,
        "amount": amount,
        "category": category,
        "description": raw_text[:100],
        "user": user
    }
    return data, None

# ---------- تابع پردازش با جیمینی (با Fallback) ----------
def process_with_gemini(raw_text):
    try:
        prompt = f"""
        شما یک دستیار مالی هوشمند هستید. متن زیر را تحلیل کنید و اطلاعات مالی را دقیقاً به فرمت JSON استخراج کنید.

        قوانین:
        - نوع حساب (account) باید یکی از این دو مقدار باشد: "ویلا_یالبندان" یا "خانواده".
        - اگر مربوط به اجاره شبانه، هزینه‌های ویلای یالبندان، شارژ، نظافت، تعمیرات، تبلیغات و ... است، "ویلا_یالبندان" را انتخاب کن.
        - اگر مربوط به خرج و مخارج زندگی روزمره خانواده (ناصر، کیمیا، مه‌یاس) است، "خانواده" را انتخاب کن.
        - نوع تراکنش (type): "درآمد" یا "هزینه".
        - مبلغ (amount): عدد را به تومان استخراج کن. اگر نبود، ۰ بگذار.
        - دسته‌بندی (category): مثلاً "اجاره", "شارژ", "خواربار", "حمل و نقل", "پوشاک", "قبوض", "تفریح", "درمان", "تحصیل", "تبلیغات", "تعمیرات", "نظافت".
        - توضیحات (description): خلاصه‌ای از کل مطلب.
        - کاربر (user): اگر مشخص است هزینه برای کیست (ناصر، کیمیا، مه‌یاس)، بنویس. اگر برای همه‌ی اعضای خانواده است، "همه" بنویس. اگر مربوط به ویلاست، "ناصر" (مدیر) در نظر بگیر.

        متن ورودی: 
        "{raw_text}"

        خروجی را فقط به صورت یک JSON معتبر برگردان. 
        فرمت: {{"account": "...", "type": "...", "amount": 0, "category": "...", "description": "...", "user": "..."}}
        """
        response = gemini_model.generate_content(prompt)
        clean_json = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        if data["amount"] == 0:
            return None, "⚠️ مبلغی در متن پیدا نشد! لطفاً عدد را به تومان وارد کنید."
        return data, None
    except Exception as e:
        logger.error(f"خطا در جیمینی: {e}. استفاده از Fallback...")
        return fallback_parse(raw_text)

# ---------- توابع تبدیل صدا و عکس ----------
model = None
def voice_to_text(voice_file_path):
    global model
    if model is None:
        model = WhisperModel("base", device="cpu", compute_type="int8")
    
    segments, info = model.transcribe(voice_file_path, language="fa")
    return " ".join([segment.text for segment in segments])

def extract_text_from_image(image_path):
    image = Image.open(image_path)
    text = pytesseract.image_to_string(image, lang="fas+eng")
    return text.strip()

def extract_text_from_pdf(pdf_path):
    images = convert_from_path(pdf_path, dpi=200)
    full_text = ""
    for img in images:
        text = pytesseract.image_to_string(img, lang="fas+eng")
        full_text += text + "\n"
    return full_text.strip()

# ---------- محاسبه سهم شراکت ویلایی ----------
def calculate_villa_shares(amount, trans_type):
    nasser_share = amount * (4.5 / 6)
    nahid_share = amount * (1.5 / 6)
    if trans_type == "هزینه":
        return f"ناصر: {-nasser_share:,.0f} / ناهید: {-nahid_share:,.0f}"
    else:
        return f"ناصر: {nasser_share:,.0f} / ناهید: {nahid_share:,.0f}"

# ---------- ذخیره در شیت ----------
def save_to_sheet(sheet, shamsi_date, miladi_date, account, trans_type, amount, category, desc, user, share_info=""):
    # شماره ردیف خودکار
    records = sheet.get_all_values()
    row_num = len(records)  # شماره ردیف جدید
    row = [row_num, shamsi_date, miladi_date, account, trans_type, amount, category, desc, user, share_info]
    sheet.append_row(row)

# ---------- تولید PDF صورتحساب ویلای یالبندان (با فونت فارسی) ----------
def generate_villa_pdf(shamsi_month_str, df_villa):
    pdf = FPDF()
    pdf.add_page()
    
    # استفاده از فونت DejaVu (که در start.sh نصب می‌شود)
    pdf.add_font('DejaVu', '', 'DejaVuSans.ttf', uni=True)
    pdf.set_font('DejaVu', size=16)
    pdf.cell(200, 10, txt="صورتحساب ماهانه - خانه مسافر یالبندان", ln=True, align='C')
    pdf.set_font('DejaVu', size=12)
    pdf.cell(200, 10, txt=f"ماه {shamsi_month_str}", ln=True, align='C')
    pdf.ln(10)

    total_income = df_villa[df_villa['نوع تراکنش'] == 'درآمد']['مبلغ(تومان)'].sum()
    total_expense = df_villa[df_villa['نوع تراکنش'] == 'هزینه']['مبلغ(تومان)'].sum()
    net_profit = total_income - total_expense
    nasser_share = net_profit * (4.5 / 6)
    nahid_share = net_profit * (1.5 / 6)

    pdf.set_font('DejaVu', size=11)
    pdf.cell(95, 10, txt="کل درآمد ماه", border=1)
    pdf.cell(95, 10, txt=f"{total_income:,.0f} تومان", border=1, ln=True)
    pdf.cell(95, 10, txt="کل هزینه ماه", border=1)
    pdf.cell(95, 10, txt=f"{total_expense:,.0f} تومان", border=1, ln=True)
    pdf.cell(95, 10, txt="سود خالص", border=1)
    pdf.cell(95, 10, txt=f"{net_profit:,.0f} تومان", border=1, ln=True)
    pdf.ln(5)
    pdf.cell(95, 10, txt="سهم ناصر (۴.۵/۶)", border=1)
    pdf.cell(95, 10, txt=f"{nasser_share:,.0f} تومان", border=1, ln=True)
    pdf.cell(95, 10, txt="سهم ناهید (۱.۵/۶)", border=1)
    pdf.cell(95, 10, txt=f"{nahid_share:,.0f} تومان", border=1, ln=True)
    pdf.ln(10)

    pdf.set_font('DejaVu', size=10)
    pdf.cell(200, 10, txt="جزئیات تراکنش‌ها:", ln=True)
    pdf.cell(40, 8, txt="تاریخ", border=1)
    pdf.cell(40, 8, txt="نوع", border=1)
    pdf.cell(40, 8, txt="مبلغ", border=1)
    pdf.cell(70, 8, txt="توضیحات", border=1, ln=True)
    for _, row in df_villa.iterrows():
        pdf.cell(40, 8, txt=row['تاریخ شمسی'][5:10], border=1)
        pdf.cell(40, 8, txt=row['نوع تراکنش'], border=1)
        pdf.cell(40, 8, txt=f"{row['مبلغ(تومان)']:,.0f}", border=1)
        pdf.cell(70, 8, txt=row['توضیحات کامل'][:30], border=1, ln=True)

    temp_pdf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    pdf.output(temp_pdf.name)
    return temp_pdf.name

# ---------- تابع ارسال گزارش ماهانه (فقط برای ناصر) ----------
async def send_monthly_villa_report(context: ContextTypes.DEFAULT_TYPE):
    today_shamsi = jdatetime.date.today()
    if today_shamsi.day == jdatetime.date(today_shamsi.year, today_shamsi.month, 1).days_in_month:
        sheet = connect_sheets()
        records = sheet.get_all_values()
        if len(records) < 2:
            return
        df = pd.DataFrame(records[1:], columns=records[0])
        current_month_str = f"{today_shamsi.year}-{today_shamsi.month:02d}"
        df_villa = df[(df['تاریخ شمسی'].str.startswith(current_month_str)) & 
                      (df['نوع حساب'] == 'ویلا_یالبندان')]
        if df_villa.empty:
            return
        pdf_path = generate_villa_pdf(current_month_str, df_villa)
        await context.bot.send_document(chat_id=ADMIN_USER_ID, document=open(pdf_path, 'rb'), 
                                        caption=f"📊 گزارش ماهانه {current_month_str} - خانه مسافر یالبندان")
        os.unlink(pdf_path)

# ---------- تابع ارسال پیام یادآوری شبانه (فقط برای ناصر و کیمیا) ----------
async def send_nightly_reminder(context: ContextTypes.DEFAULT_TYPE):
    message = "🌙 یادآوری شبانه: لطفاً اگر هزینه یا درآمدی امروز داشته‌اید، برای حسابدار هوشمند بفرستید تا ثبت شود."
    for user_id in REMINDER_USERS:  # فقط ناصر و کیمیا
        try:
            await context.bot.send_message(chat_id=user_id, text=message)
        except Exception as e:
            logger.error(f"ارسال یادآوری به {user_id} ناموفق: {e}")

# ---------- تابع پشتیبان‌گیری خودکار ----------
async def auto_backup(context: ContextTypes.DEFAULT_TYPE):
    backup_data()

# ---------- هندلر دسترسی (چک کردن کاربر) ----------
def is_allowed(user_id):
    return user_id in ALLOWED_USERS

# ---------- هندلر دستورات ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("⛔ شما دسترسی به این ربات ندارید.")
        return
    await update.message.reply_text(
        "🏦 به **حسابدار هوشمند خانه مسافر یالبندان و خانواده ناصر، کیمیا و مه‌یاس** خوش آمدید!\n\n"
        "🌟 قابلیت‌ها:\n"
        "1️⃣ ارسال ویس، عکس، PDF، اسکرین‌شات یا متن برای ثبت خودکار\n"
        "2️⃣ تشخیص هوشمند توسط Gemini (با Fallback دستی)\n"
        "3️⃣ محاسبه خودکار سهام شراکت ویلای یالبندان\n"
        "4️⃣ چت صوتی با بات (ویس بپرسید، پاسخ صوتی بشنوید)\n"
        "5️⃣ دریافت گزارش روزانه: /report\n"
        "6️⃣ دریافت گزارش ماهانه شمسی: /monthly\n"
        "7️⃣ دریافت گزارش سالانه: /yearly 1405\n"
        "8️⃣ گزارش بازه دلخواه: /report_from 1405-01-01 1405-12-29\n"
        "9️⃣ وضعیت مالی لحظه‌ای: /status\n"
        "🔟 لغو آخرین تراکنش: /undo\n"
        "1️⃣1️⃣ ویرایش تراکنش: /edit [شماره ردیف] [فیلد] [مقدار جدید]\n"
        "    فیلدها: amount, category, description, user"
    )

# ---------- پردازش ورودی (با صف) ----------
async def process_queue(app):
    global is_processing
    if is_processing:
        return
    is_processing = True
    try:
        while not request_queue.empty():
            update, context = await request_queue.get()
            await handle_input_internal(update, context)
    finally:
        is_processing = False

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("⛔ شما دسترسی به این ربات ندارید.")
        return
    await request_queue.put((update, context))
    if not is_processing:
        asyncio.create_task(process_queue(context.bot))

async def handle_input_internal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = ""
    is_voice_query = False
    
    if update.message.voice:
        voice_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await voice_file.download_to_drive(tmp.name)
            raw_text = voice_to_text(tmp.name)
        os.unlink(tmp.name)
        is_voice_query = True  # برای چت صوتی
    elif update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            await photo_file.download_to_drive(tmp.name)
            raw_text = extract_text_from_image(tmp.name)
        os.unlink(tmp.name)
    elif update.message.document:
        doc = update.message.document
        if doc.mime_type == "application/pdf":
            pdf_file = await doc.get_file()
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                await pdf_file.download_to_drive(tmp.name)
                raw_text = extract_text_from_pdf(tmp.name)
            os.unlink(tmp.name)
        else:
            await update.message.reply_text("❌ فقط فایل PDF پشتیبانی می‌شود.")
            return
    elif update.message.text:
        raw_text = update.message.text
        # بررسی اینکه آیا سؤال است (برای چت صوتی)
        if any(w in raw_text for w in ["؟", "چقدر", "چند", "کی", "کجا", "چه", "آیا"]):
            is_voice_query = True
    else:
        await update.message.reply_text("❌ نوع ورودی پشتیبانی نمی‌شود.")
        return

    if not raw_text.strip():
        await update.message.reply_text("❌ متنی استخراج نشد.")
        return

    # اگر سؤال بود، پاسخ هوشمند بده
    if is_voice_query and len(raw_text) > 10:
        await handle_voice_query(update, context, raw_text)
        return

    # پردازش با جیمینی (یا Fallback)
    data, error = process_with_gemini(raw_text)
    if error:
        await update.message.reply_text(error)
        return

    # اگر حساب خانواده بود و کاربر مشخص نبود، بپرس
    if data['account'] == 'خانواده' and data.get('user') in [None, 'همه', '']:
        keyboard = [
            [InlineKeyboardButton("ناصر", callback_data="user_ناصر"),
             InlineKeyboardButton("کیمیا", callback_data="user_کیمیا")],
            [InlineKeyboardButton("مه‌یاس", callback_data="user_مه‌یاس"),
             InlineKeyboardButton("خونه (همه)", callback_data="user_همه")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        context.user_data['pending_data'] = data
        await update.message.reply_text("👨‍👩‍👧 این هزینه/درآمد برای کدام یک از اعضای خانواده است؟", reply_markup=reply_markup)
        return

    # اگر همه چیز کامل بود، ذخیره کن
    await save_transaction(update, context, data)

# ---------- چت صوتی با بات (پاسخ به سؤالات) ----------
async def handle_voice_query(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str):
    user_id = update.effective_user.id
    try:
        # تولید پاسخ با Gemini
        prompt = f"""
        شما یک دستیار مالی هوشمند برای خانواده ناصر، کیمیا و مه‌یاس هستید.
        به سؤال زیر بر اساس داده‌های مالی که در اختیار دارید پاسخ دهید.
        اگر اطلاعات دقیقی ندارید، پاسخ کلی بدهید.

        سؤال کاربر: "{query_text}"

        پاسخ را به فارسی و مختصر بدهید.
        """
        response = gemini_model.generate_content(prompt)
        answer_text = response.text.strip()
        
        # ارسال پاسخ متنی
        await update.message.reply_text(f"🤖 {answer_text}")
        
        # تبدیل پاسخ به صدا و ارسال
        try:
            tts = gTTS(text=answer_text, lang='fa', slow=False)
            audio_bytes = io.BytesIO()
            tts.write_to_fp(audio_bytes)
            audio_bytes.seek(0)
            await update.message.reply_voice(voice=audio_bytes, caption="🎧 پاسخ صوتی")
        except Exception as e:
            logger.error(f"خطا در تبدیل متن به صدا: {e}")
            
    except Exception as e:
        logger.error(f"خطا در چت صوتی: {e}")
        await update.message.reply_text("❌ متأسفانه در پردازش سؤال شما خطایی رخ داد. لطفاً دوباره تلاش کنید.")

# ---------- ذخیره تراکنش ----------
async def save_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE, data):
    now_tehran = datetime.now(TIMEZONE)
    miladi_str = now_tehran.strftime("%Y-%m-%d %H:%M")
    shamsi_date = jdatetime.date.today().strftime("%Y-%m-%d")
    sheet = connect_sheets()
    
    share_text = ""
    if data['account'] == 'ویلا_یالبندان':
        share_text = calculate_villa_shares(data['amount'], data['type'])
    
    save_to_sheet(sheet, shamsi_date, miladi_str, data['account'], data['type'], 
                  data['amount'], data['category'], data['description'], 
                  data.get('user', 'ناصر'), share_text)
    
    reply = (f"✅ ثبت شد!\n"
             f"📌 حساب: {data['account']}\n"
             f"📊 نوع: {data['type']}\n"
             f"💰 مبلغ: {data['amount']:,} تومان\n"
             f"📂 دسته: {data['category']}\n"
             f"👤 کاربر: {data.get('user', 'ناصر')}\n"
             f"📝 توضیح: {data['description']}")
    if share_text:
        reply += f"\n🔗 سهم‌بندی: {share_text}"
    await update.message.reply_text(reply)

# ---------- پاسخ به دکمه انتخاب کاربر ----------
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_choice = query.data.replace("user_", "")
    data = context.user_data.get('pending_data')
    if not data:
        await query.edit_message_text("❌ خطا: اطلاعاتی برای ذخیره وجود ندارد.")
        return
    data['user'] = user_choice
    await save_transaction(update, context, data)
    context.user_data.pop('pending_data', None)

# ---------- گزارش روزانه ----------
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("⛔ شما دسترسی به این ربات ندارید.")
        return
    sheet = connect_sheets()
    records = sheet.get_all_values()
    if len(records) < 2:
        await update.message.reply_text("📭 هنوز ثبت‌ای ندارید.")
        return
    df = pd.DataFrame(records[1:], columns=records[0])
    today_shamsi = jdatetime.date.today().strftime("%Y-%m-%d")
    df_today = df[df['تاریخ شمسی'] == today_shamsi]
    if df_today.empty:
        await update.message.reply_text(f"📭 امروز {today_shamsi} هیچ تراکنشی نداشتید.")
        return
    total_in = df_today[df_today['نوع تراکنش'] == 'درآمد']['مبلغ(تومان)'].sum()
    total_out = df_today[df_today['نوع تراکنش'] == 'هزینه']['مبلغ(تومان)'].sum()
    await update.message.reply_text(
        f"📊 گزارش روزانه ({today_shamsi}):\n"
        f"💵 درآمد: {total_in:,.0f}\n"
        f"💸 هزینه: {total_out:,.0f}\n"
        f"📈 مانده: {total_in - total_out:,.0f}"
    )

# ---------- گزارش ماهانه (با جزئیات کامل) ----------
async def monthly_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("⛔ شما دسترسی به این ربات ندارید.")
        return
    sheet = connect_sheets()
    records = sheet.get_all_values()
    if len(records) < 2:
        await update.message.reply_text("📭 ثبت‌ای موجود نیست.")
        return
    df = pd.DataFrame(records[1:], columns=records[0])
    today_shamsi = jdatetime.date.today()
    month_str = f"{today_shamsi.year}-{today_shamsi.month:02d}"
    df_month = df[df['تاریخ شمسی'].str.startswith(month_str)]
    if df_month.empty:
        await update.message.reply_text(f"📭 ماه {month_str} تراکنشی ندارید.")
        return

    # گزارش ویلا (فقط برای ناصر)
    if user_id == ADMIN_USER_ID:
        df_villa = df_month[df_month['نوع حساب'] == 'ویلا_یالبندان']
        if not df_villa.empty:
            pdf_path = generate_villa_pdf(month_str, df_villa)
            await update.message.reply_document(document=open(pdf_path, 'rb'))
            os.unlink(pdf_path)

    # گزارش خانواده (برای همه کاربران مجاز)
    df_family = df_month[df_month['نوع حساب'] == 'خانواده']
    if not df_family.empty:
        total_in = df_family[df_family['نوع تراکنش'] == 'درآمد']['مبلغ(تومان)'].sum()
        total_out = df_family[df_family['نوع تراکنش'] == 'هزینه']['مبلغ(تومان)'].sum()
        msg = f"👨‍👩‍👧 گزارش ماهانه خانواده ({month_str}):\n"
        msg += f"💵 درآمد: {total_in:,.0f}\n"
        msg += f"💸 هزینه: {total_out:,.0f}\n"
        msg += f"📈 مانده: {total_in - total_out:,.0f}\n\n"
        msg += "📋 جزئیات تراکنش‌ها:\n"
        for _, row in df_family.iterrows():
            msg += f"{row['تاریخ شمسی'][5:10]} | {row['نوع تراکنش']} | {row['مبلغ(تومان)']:,.0f} | {row['کاربر']} | {row['توضیحات کامل']}\n"
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text(f"📭 در ماه {month_str} هیچ تراکنش خانوادگی ثبت نشده است.")

# ---------- گزارش بازه دلخواه ----------
async def report_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("⛔ شما دسترسی به این ربات ندارید.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("لطفاً تاریخ شروع و پایان را وارد کنید. مثال: /report_from 1405-01-01 1405-12-29")
        return
    start_date = args[0]
    end_date = args[1]
    try:
        jdatetime.datetime.strptime(start_date, "%Y-%m-%d")
        jdatetime.datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text("❌ فرمت تاریخ اشتباه است. فرمت صحیح: YYYY-MM-DD")
        return
    
    sheet = connect_sheets()
    records = sheet.get_all_values()
    if len(records) < 2:
        await update.message.reply_text("📭 ثبت‌ای موجود نیست.")
        return
    df = pd.DataFrame(records[1:], columns=records[0])
    mask = (df['تاریخ شمسی'] >= start_date) & (df['تاریخ شمسی'] <= end_date)
    df_range = df[mask]
    if df_range.empty:
        await update.message.reply_text(f"📭 در بازه {start_date} تا {end_date} هیچ تراکنشی ندارید.")
        return
    
    total_in = df_range[df_range['نوع تراکنش'] == 'درآمد']['مبلغ(تومان)'].sum()
    total_out = df_range[df_range['نوع تراکنش'] == 'هزینه']['مبلغ(تومان)'].sum()
    
    msg = f"📊 گزارش بازه {start_date} تا {end_date}:\n"
    msg += f"💵 درآمد: {total_in:,.0f}\n"
    msg += f"💸 هزینه: {total_out:,.0f}\n"
    msg += f"📈 مانده: {total_in - total_out:,.0f}\n\n"
    msg += "📋 جزئیات تراکنش‌ها:\n"
    for _, row in df_range.iterrows():
        msg += f"{row['تاریخ شمسی']} | {row['نوع حساب']} | {row['نوع تراکنش']} | {row['مبلغ(تومان)']:,.0f} | {row['کاربر']} | {row['توضیحات کامل']}\n"
    await update.message.reply_text(msg)

# ---------- گزارش سالانه ----------
async def yearly_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("⛔ شما دسترسی به این ربات ندارید.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("لطفاً سال را وارد کنید. مثال: /yearly 1405")
        return
    year = args[0]
    sheet = connect_sheets()
    records = sheet.get_all_values()
    if len(records) < 2:
        await update.message.reply_text("📭 ثبت‌ای موجود نیست.")
        return
    df = pd.DataFrame(records[1:], columns=records[0])
    df_year = df[df['تاریخ شمسی'].str.startswith(year)]
    if df_year.empty:
        await update.message.reply_text(f"📭 سال {year} تراکنشی ندارید.")
        return
    
    # گزارش ویلا (فقط برای ناصر)
    if user_id == ADMIN_USER_ID:
        df_villa = df_year[df_year['نوع حساب'] == 'ویلا_یالبندان']
        if not df_villa.empty:
            total_in = df_villa[df_villa['نوع تراکنش'] == 'درآمد']['مبلغ(تومان)'].sum()
            total_out = df_villa[df_villa['نوع تراکنش'] == 'هزینه']['مبلغ(تومان)'].sum()
            await update.message.reply_text(
                f"📊 گزارش سالانه {year} - ویلا یالبندان:\n"
                f"درآمد: {total_in:,.0f}\n"
                f"هزینه: {total_out:,.0f}\n"
                f"سود: {total_in - total_out:,.0f}"
            )
    
    # گزارش خانواده (برای همه)
    df_family = df_year[df_year['نوع حساب'] == 'خانواده']
    if not df_family.empty:
        total_in = df_family[df_family['نوع تراکنش'] == 'درآمد']['مبلغ(تومان)'].sum()
        total_out = df_family[df_family['نوع تراکنش'] == 'هزینه']['مبلغ(تومان)'].sum()
        await update.message.reply_text(
            f"👨‍👩‍👧 گزارش سالانه {year} - خانواده:\n"
            f"درآمد: {total_in:,.0f}\n"
            f"هزینه: {total_out:,.0f}\n"
            f"مانده: {total_in - total_out:,.0f}"
        )

# ---------- وضعیت مالی لحظه‌ای ----------
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("⛔ شما دسترسی به این ربات ندارید.")
        return
    sheet = connect_sheets()
    records = sheet.get_all_values()
    if len(records) < 2:
        await update.message.reply_text("📭 هنوز ثبت‌ای ندارید.")
        return
    df = pd.DataFrame(records[1:], columns=records[0])
    
    # ویلا
    df_villa = df[df['نوع حساب'] == 'ویلا_یالبندان']
    if not df_villa.empty:
        total_in_villa = df_villa[df_villa['نوع تراکنش'] == 'درآمد']['مبلغ(تومان)'].sum()
        total_out_villa = df_villa[df_villa['نوع تراکنش'] == 'هزینه']['مبلغ(تومان)'].sum()
        msg_villa = f"🏡 ویلا یالبندان: درآمد {total_in_villa:,.0f} - هزینه {total_out_villa:,.0f} = سود {total_in_villa - total_out_villa:,.0f}"
    else:
        msg_villa = "🏡 ویلا یالبندان: هیچ تراکنشی ثبت نشده است."
    
    # خانواده
    df_family = df[df['نوع حساب'] == 'خانواده']
    if not df_family.empty:
        total_in_family = df_family[df_family['نوع تراکنش'] == 'درآمد']['مبلغ(تومان)'].sum()
        total_out_family = df_family[df_family['نوع تراکنش'] == 'هزینه']['مبلغ(تومان)'].sum()
        msg_family = f"👨‍👩‍👧 خانواده: درآمد {total_in_family:,.0f} - هزینه {total_out_family:,.0f} = مانده {total_in_family - total_out_family:,.0f}"
    else:
        msg_family = "👨‍👩‍👧 خانواده: هیچ تراکنشی ثبت نشده است."
    
    await update.message.reply_text(f"📊 وضعیت مالی لحظه‌ای:\n\n{msg_villa}\n\n{msg_family}")

# ---------- لغو آخرین تراکنش ----------
async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("⛔ شما دسترسی به این ربات ندارید.")
        return
    sheet = connect_sheets()
    records = sheet.get_all_values()
    if len(records) < 2:
        await update.message.reply_text("📭 هیچ تراکنشی برای لغو وجود ندارد.")
        return
    sheet.delete_rows(len(records))
    await update.message.reply_text("✅ آخرین تراکنش با موفقیت لغو شد.")

# ---------- ویرایش تراکنش ----------
async def edit_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("⛔ شما دسترسی به این ربات ندارید.")
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("فرمت: /edit [شماره ردیف] [فیلد] [مقدار جدید]\nفیلدها: amount, category, description, user")
        return
    
    row_num = int(args[0])
    field = args[1]
    new_value = " ".join(args[2:])
    
    sheet = connect_sheets()
    records = sheet.get_all_values()
    if len(records) < row_num + 1:
        await update.message.reply_text("❌ شماره ردیف نامعتبر است.")
        return
    
    # پیدا کردن ستون مربوطه
    headers = records[0]
    field_map = {
        "amount": "مبلغ(تومان)",
        "category": "دسته‌بندی",
        "description": "توضیحات کامل",
        "user": "کاربر"
    }
    if field not in field_map:
        await update.message.reply_text("❌ فیلد نامعتبر. فیلدهای مجاز: amount, category, description, user")
        return
    
    col_index = headers.index(field_map[field]) + 1  # +1 برای gspread (شروع از ۱)
    
    # به‌روزرسانی
    sheet.update_cell(row_num + 1, col_index, new_value)
    await update.message.reply_text(f"✅ فیلد {field} با موفقیت به‌روزرسانی شد.")

# ---------- راه‌اندازی اصلی ----------
def main():
    app = Application.builder().token(TOKEN).build()

    # هندلرها
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("monthly", monthly_report))
    app.add_handler(CommandHandler("yearly", yearly_report))
    app.add_handler(CommandHandler("report_from", report_from))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("edit", edit_transaction))
    app.add_handler(MessageHandler(filters.VOICE | filters.PHOTO | filters.Document.ALL | filters.TEXT, handle_input))
    app.add_handler(CallbackQueryHandler(button_callback, pattern="^user_"))

    # زمان‌بندی
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(send_monthly_villa_report, CronTrigger(day="last", hour=23, minute=59), args=[app])
    scheduler.add_job(send_nightly_reminder, CronTrigger(hour=23, minute=0), args=[app])
    scheduler.add_job(auto_backup, CronTrigger(hour=0, minute=0), args=[app])
    scheduler.start()

    print("🤖 ابرربات حسابداری هوشمند سوپر اپدیت با Gemini راه‌اندازی شد!")
    app.run_polling()

if __name__ == "__main__":
    main()
