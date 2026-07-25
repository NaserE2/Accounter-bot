import os
import re
import tempfile
import json
import logging
from datetime import datetime
import pytz
import asyncio
import jdatetime
import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fpdf import FPDF
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
import gspread
from google.oauth2.service_account import Credentials
from PIL import Image
import pytesseract
from pdf2image import convert_from_path
import google.generativeai as genai

# ---------- تنظیمات اولیه ----------
TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("متغیر محیطی GEMINI_API_KEY تنظیم نشده است!")

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash')

# ---------- تنظیمات کاربران مجاز ----------
ALLOWED_USERS = [
    108797218,  # شناسه عددی ناصر
    987654321,  # شناسه عددی کیمیا (لطفاً شناسه واقعی را جایگزین کن)
    456789123   # شناسه عددی مه‌یاس (لطفاً شناسه واقعی را جایگزین کن)
]
ADMIN_USER_ID = 108797218

REMINDER_USERS = [
    108797218,  # فقط ناصر
    987654321   # فقط کیمیا
]

SHEET_NAME = "حسابداری_هوشمند_نهایی"
BACKUP_DIR = "backups"
os.makedirs(BACKUP_DIR, exist_ok=True)
TIMEZONE = pytz.timezone('Asia/Tehran')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------- صف پردازش درخواست‌ها ----------
request_queue = asyncio.Queue()
is_processing = False

# ---------- BUG FIX #1: تبدیل اعداد فارسی/عربی به انگلیسی ----------
# کاراکتر ۴ در رشته اصلی جا افتاده بود!
def normalize_numbers(text):
    persian_nums = '۰۱۲۳۴۵۶۷۸۹'  # FIX: ۴ اضافه شد
    arabic_nums  = '٠١٢٣٤٥٦٧٨٩'
    english_nums = '0123456789'
    table = str.maketrans(persian_nums + arabic_nums, english_nums + english_nums)
    return text.translate(table)

# ---------- BUG FIX #2: اتصال به گوگل شیت با scope کامل ----------
# scope اصلی فقط spreadsheets داشت؛ برای create شیت، drive هم لازمه
def connect_sheets():
    try:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"  # FIX: اضافه شد برای ساخت شیت جدید
        ]
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
        return sheet
    except Exception as e:
        logger.error(f"خطا در اتصال به گوگل شیت: {e}")
        return None

# ---------- تابع پشتیبان‌گیری ----------
def backup_data():
    try:
        sheet = connect_sheets()
        if not sheet: return
        records = sheet.get_all_values()
        if len(records) < 2: return
        df = pd.DataFrame(records[1:], columns=records[0])
        now_shamsi = jdatetime.date.today().strftime("%Y-%m-%d")
        backup_file = os.path.join(BACKUP_DIR, f"backup_{now_shamsi}.json")
        df.to_json(backup_file, orient="records", force_ascii=False)
        logger.info(f"پشتیبان‌گیری انجام شد: {backup_file}")

        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.json')])
        if len(backups) > 30:
            for f in backups[:-30]:
                os.remove(os.path.join(BACKUP_DIR, f))
    except Exception as e:
        logger.error(f"خطا در پشتیبان‌گیری: {e}")

# ---------- Fallback با کلمات کلیدی ----------
def fallback_parse(raw_text):
    text_lower = normalize_numbers(raw_text.lower())
    account, trans_type, category, user = "خانواده", "هزینه", "سایر", "ناصر"
    amount = 0

    villa_keywords    = ["ویلا", "یالبندان", "اجاره", "میهمان", "شبانه", "مسافر", "رزرو"]
    personal_keywords = ["حساب شخصی", "شخصی", "جیب", "توجیبی", "خرج خودم", "من", "ناصر", "کیمیا", "مه یاس", "مهیاس"]

    if any(w in text_lower for w in villa_keywords):
        account = "ویلا_یالبندان"
    elif any(w in text_lower for w in personal_keywords):
        account = "خانواده"

    if any(w in text_lower for w in ["درآمد", "فروش", "دریافت", "واریز شد"]):
        trans_type = "درآمد"

    if "کیمیا" in text_lower:           user = "کیمیا"
    elif "مه یاس" in text_lower or "مهیاس" in text_lower: user = "مه‌یاس"
    elif "همه" in text_lower or "خونه" in text_lower:     user = "همه"

    match = re.search(r"(\d{1,3}(?:,\d{3})*|\d+)\s*(?:تومان|ت)", text_lower)
    if match:
        amount = int(match.group(1).replace(",", ""))

    if   "خواربار" in text_lower:                       category = "خواربار"
    elif "قبض" in text_lower or "برق" in text_lower:   category = "قبوض"
    elif "شارژ" in text_lower:                          category = "شارژ"
    elif "نظافت" in text_lower:                         category = "نظافت"
    elif "تعمیر" in text_lower:                         category = "تعمیرات"

    if amount == 0:
        return None, "⚠️ مبلغی پیدا نشد! لطفاً عدد را به تومان بنویسید (مثال: ۵۰۰۰۰۰ تومان)."

    return {"account": account, "type": trans_type, "amount": amount,
            "category": category, "description": raw_text[:100], "user": user}, None

# ---------- BUG FIX #3: پردازش با جیمینی - JSON parse بهتر ----------
def process_with_gemini(raw_text):
    try:
        prompt = f"""
شما یک دستیار مالی هوشمند هستید. متن زیر را تحلیل کن و فقط یک JSON خالص برگردان (بدون markdown، بدون backtick، بدون توضیح اضافه).

قوانین:
- account: "ویلا_یالبندان" یا "خانواده". اجاره/نظافت ویلا/مسافر -> ویلا_یالبندان. خرج شخصی/خانواده -> خانواده.
- type: "درآمد" یا "هزینه"
- amount: عدد صحیح به تومان. اعداد فارسی مثل ۵۰ یا حروف مثل "پانصد هزار" را به عدد تبدیل کن.
- category: یکی از: اجاره، شارژ، خواربار، قبوض، تفریح، درمان، تعمیرات، نظافت، حمل و نقل، پوشاک، تحصیل، تبلیغات، سایر
- description: خلاصه کوتاه فارسی
- user: "ناصر" یا "کیمیا" یا "مه‌یاس" یا "همه". اگر ویلا بود -> "ناصر"

متن ورودی: "{raw_text}"

خروجی (فقط JSON خالص):
{{"account": "...", "type": "...", "amount": 0, "category": "...", "description": "...", "user": "..."}}
"""
        response = gemini_model.generate_content(prompt)
        raw_response = response.text.strip()

        # FIX: پاک کردن هر نوع markdown block
        clean_json = re.sub(r'```(?:json)?', '', raw_response).replace('```', '').strip()

        # اگر JSON توی متن طولانی باشه، استخراجش می‌کنیم
        json_match = re.search(r'\{.*\}', clean_json, re.DOTALL)
        if json_match:
            clean_json = json_match.group(0)

        data = json.loads(clean_json)
        data['amount'] = int(normalize_numbers(str(data.get('amount', 0))))

        if data.get("amount", 0) == 0:
            return None, "⚠️ مبلغی پیدا نشد! لطفاً عدد را به تومان بنویسید."
        return data, None
    except Exception as e:
        logger.error(f"خطا در جیمینی: {e}. استفاده از Fallback...")
        return fallback_parse(raw_text)

# ---------- BUG FIX #4: تبدیل ویس با Gemini (روش درست) ----------
async def transcribe_voice_with_gemini(ogg_path: str) -> str:
    """
    ارسال فایل صوتی به Gemini برای تبدیل به متن.
    روش قدیم (ارسال raw bytes) کار نمی‌کرد چون MIME type نداشت.
    """
    try:
        # روش 1: آپلود فایل به Gemini Files API (برای فایل‌های بزرگ‌تر از 20MB)
        # روش 2: ارسال inline با MIME type (برای فایل‌های کوچک - ویس تلگرام معمولاً < 1MB)
        with open(ogg_path, "rb") as f:
            audio_bytes = f.read()

        import base64
        audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')

        response = gemini_model.generate_content([
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "audio/ogg",
                            "data": audio_b64
                        }
                    },
                    {
                        "text": "این فایل صوتی را به دقت گوش بده و متن آن را به فارسی برگردان. فقط متن را بنویس بدون توضیح اضافه."
                    }
                ]
            }
        ])
        return response.text.strip()
    except Exception as e:
        logger.error(f"خطا در تبدیل ویس با Gemini: {e}")
        # Fallback: اگر inline_data کار نکرد، upload_file امتحان می‌کنیم
        try:
            uploaded = genai.upload_file(ogg_path, mime_type="audio/ogg")
            response = gemini_model.generate_content([
                uploaded,
                "این فایل صوتی را به دقت گوش بده و متن آن را به فارسی برگردان. فقط متن را بنویس."
            ])
            return response.text.strip()
        except Exception as e2:
            logger.error(f"خطا در fallback upload ویس: {e2}")
            raise

# ---------- توابع تبدیل عکس و PDF ----------
def extract_text_from_image(image_path):
    image = Image.open(image_path)
    return pytesseract.image_to_string(image, lang="fas+eng").strip()

def extract_text_from_pdf(pdf_path):
    images = convert_from_path(pdf_path, dpi=200)
    return "\n".join([pytesseract.image_to_string(img, lang="fas+eng") for img in images]).strip()

# ---------- محاسبه سهم و ذخیره ----------
def calculate_villa_shares(amount, trans_type):
    nasser = amount * (4.5 / 6)
    nahid  = amount * (1.5 / 6)
    sign   = -1 if trans_type == "هزینه" else 1
    return f"ناصر: {sign*nasser:,.0f} / ناهید: {sign*nahid:,.0f}"

def save_to_sheet(sheet, shamsi_date, miladi_date, account, trans_type, amount, category, desc, user, share_info=""):
    records = sheet.get_all_values()
    row = [len(records), shamsi_date, miladi_date, account, trans_type, amount, category, desc, user, share_info]
    sheet.append_row(row)

# ---------- تولید PDF ----------
def generate_villa_pdf(shamsi_month_str, df_villa):
    pdf = FPDF()
    pdf.add_page()

    font_path = "./DejaVuSans.ttf" if os.path.exists("./DejaVuSans.ttf") else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    pdf.add_font('DejaVu', '', font_path, uni=True)
    pdf.set_font('DejaVu', size=16)
    pdf.cell(200, 10, txt="صورتحساب ماهانه - خانه مسافر یالبندان", ln=True, align='C')
    pdf.set_font('DejaVu', size=12)
    pdf.cell(200, 10, txt=f"ماه {shamsi_month_str}", ln=True, align='C')
    pdf.ln(10)

    df_villa = df_villa.copy()
    df_villa['مبلغ(تومان)'] = pd.to_numeric(df_villa['مبلغ(تومان)'], errors='coerce').fillna(0)

    total_in  = df_villa[df_villa['نوع تراکنش'] == 'درآمد']['مبلغ(تومان)'].sum()
    total_out = df_villa[df_villa['نوع تراکنش'] == 'هزینه']['مبلغ(تومان)'].sum()
    net = total_in - total_out

    pdf.set_font('DejaVu', size=11)
    for label, val in [("کل درآمد", total_in), ("کل هزینه", total_out), ("سود خالص", net)]:
        pdf.cell(95, 10, txt=label, border=1)
        pdf.cell(95, 10, txt=f"{val:,.0f} تومان", border=1, ln=True)
    pdf.cell(95, 10, txt="سهم ناصر (۴.۵/۶)", border=1)
    pdf.cell(95, 10, txt=f"{net * (4.5/6):,.0f} تومان", border=1, ln=True)
    pdf.cell(95, 10, txt="سهم ناهید (۱.۵/۶)", border=1)
    pdf.cell(95, 10, txt=f"{net * (1.5/6):,.0f} تومان", border=1, ln=True)
    pdf.ln(10)

    pdf.set_font('DejaVu', size=9)
    for header, w in [("تاریخ", 40), ("نوع", 40), ("مبلغ", 40), ("توضیحات", 70)]:
        pdf.cell(w, 8, txt=header, border=1)
    pdf.ln()
    for _, row in df_villa.iterrows():
        pdf.cell(40, 8, txt=str(row['تاریخ شمسی'])[5:10], border=1)
        pdf.cell(40, 8, txt=str(row['نوع تراکنش']), border=1)
        pdf.cell(40, 8, txt=f"{row['مبلغ(تومان)']:,.0f}", border=1)
        pdf.cell(70, 8, txt=str(row['توضیحات کامل'])[:25], border=1, ln=True)

    temp_pdf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    pdf.output(temp_pdf.name)
    return temp_pdf.name

# ---------- وظایف زمان‌بندی شده ----------
# BUG FIX #5: scheduler jobs باید فقط context بگیرند، نه app
async def send_monthly_villa_report(context: ContextTypes.DEFAULT_TYPE):
    today = jdatetime.date.today()
    days_in_month = jdatetime.date(today.year, today.month, 1).days_in_month
    if today.day != days_in_month:
        return
    sheet = connect_sheets()
    if not sheet: return
    records = sheet.get_all_values()
    if len(records) < 2: return
    df = pd.DataFrame(records[1:], columns=records[0])
    month_str = f"{today.year}-{today.month:02d}"
    df_villa = df[(df['تاریخ شمسی'].str.startswith(month_str)) & (df['نوع حساب'] == 'ویلا_یالبندان')]
    if not df_villa.empty:
        pdf_path = generate_villa_pdf(month_str, df_villa)
        try:
            with open(pdf_path, 'rb') as f:
                await context.bot.send_document(chat_id=ADMIN_USER_ID, document=f,
                                                caption=f"📊 گزارش ماهانه {month_str}")
        finally:
            os.unlink(pdf_path)

async def send_nightly_reminder(context: ContextTypes.DEFAULT_TYPE):
    message = "🌙 یادآوری شبانه: لطفاً اگر هزینه یا درآمدی امروز داشته‌اید، برای حسابدار هوشمند بفرستید."
    for user_id in REMINDER_USERS:
        try:
            await context.bot.send_message(chat_id=user_id, text=message)
        except Exception as e:
            logger.error(f"ارسال یادآوری به {user_id} ناموفق: {e}")

async def auto_backup(context: ContextTypes.DEFAULT_TYPE):
    backup_data()

# ---------- هندلرهای ربات ----------
def is_allowed(user_id):
    return user_id in ALLOWED_USERS

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ شما دسترسی به این ربات ندارید.")
        return
    await update.message.reply_text(
        "🏦 به حسابدار هوشمند خوش آمدید!\n\n"
        "📋 قابلیت‌ها:\n"
        "1️⃣ ارسال ویس، عکس، PDF یا متن برای ثبت خودکار\n"
        "2️⃣ تشخیص هوشمند توسط Gemini (با Fallback دستی)\n"
        "3️⃣ محاسبه خودکار سهام شراکت ویلای یالبندان\n"
        "4️⃣ گزارش‌ها: /report\n"
        "5️⃣ مدیریت: /undo | /edit [ردیف] [فیلد] [مقدار]"
    )

async def process_queue():
    global is_processing
    if is_processing: return
    is_processing = True
    try:
        while not request_queue.empty():
            update, context = await request_queue.get()
            await handle_input_internal(update, context)
    finally:
        is_processing = False

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ شما دسترسی به این ربات ندارید.")
        return
    await request_queue.put((update, context))
    if not is_processing:
        asyncio.create_task(process_queue())

# ---------- BUG FIX #4 (ادامه): handle_input_internal ----------
async def handle_input_internal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = ""
    msg = update.message

    try:
        if msg.voice:
            await msg.reply_text("🎙️ در حال پردازش ویس...")
            voice_file = await msg.voice.get_file()
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                tmp_path = tmp.name
            await voice_file.download_to_drive(tmp_path)
            try:
                raw_text = await transcribe_voice_with_gemini(tmp_path)
            finally:
                os.unlink(tmp_path)

            if not raw_text:
                await msg.reply_text("❌ ویس قابل تبدیل نبود. لطفاً متن بفرستید.")
                return
            await msg.reply_text(f"📝 متن ویس: {raw_text}")

        elif msg.photo:
            await msg.reply_text("🖼️ در حال پردازش تصویر...")
            photo_file = await msg.photo[-1].get_file()
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name
            await photo_file.download_to_drive(tmp_path)
            try:
                raw_text = extract_text_from_image(tmp_path)
            finally:
                os.unlink(tmp_path)

        elif msg.document and msg.document.mime_type == "application/pdf":
            await msg.reply_text("📄 در حال پردازش PDF...")
            pdf_file = await msg.document.get_file()
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = tmp.name
            await pdf_file.download_to_drive(tmp_path)
            try:
                raw_text = extract_text_from_pdf(tmp_path)
            finally:
                os.unlink(tmp_path)

        elif msg.text:
            # فیلتر کردن دستورات (با / شروع می‌شوند)
            if msg.text.startswith("/"):
                return
            raw_text = msg.text

        else:
            await msg.reply_text("❌ نوع ورودی پشتیبانی نمی‌شود. لطفاً متن، ویس، عکس یا PDF بفرستید.")
            return

        if not raw_text.strip():
            await msg.reply_text("❌ محتوایی استخراج نشد. لطفاً دوباره امتحان کنید.")
            return

        await msg.reply_text("⏳ در حال تحلیل با هوش مصنوعی...")
        data, error = process_with_gemini(raw_text)
        if error:
            await msg.reply_text(error)
            return

        # اگر کاربر مشخص نشده بود، سؤال بپرس
        if data['account'] == 'خانواده' and data.get('user') in [None, 'همه', '']:
            keyboard = [
                [InlineKeyboardButton("ناصر", callback_data="user_ناصر"),
                 InlineKeyboardButton("کیمیا", callback_data="user_کیمیا")],
                [InlineKeyboardButton("مه‌یاس", callback_data="user_مه‌یاس"),
                 InlineKeyboardButton("🏠 خونه (همه)", callback_data="user_همه")]
            ]
            context.user_data['pending_data'] = data
            await msg.reply_text(
                "👨‍👩‍👧 این هزینه/درآمد برای کدام یک از اعضای خانواده است؟",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        await save_transaction(update, context, data)

    except Exception as e:
        logger.error(f"خطای غیرمنتظره در پردازش: {e}", exc_info=True)
        await msg.reply_text(f"❌ خطایی رخ داد: {str(e)[:100]}\nلطفاً دوباره تلاش کنید.")

# ---------- BUG FIX #6: save_transaction برای callback_query هم کار کند ----------
async def save_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    # پیام ارسال به کانال درست (message یا callback_query)
    if update.callback_query:
        reply_func = update.callback_query.message.reply_text
    else:
        reply_func = update.message.reply_text

    now_tehran = datetime.now(TIMEZONE)
    sheet = connect_sheets()
    if not sheet:
        await reply_func("❌ خطا در اتصال به گوگل شیت. لطفاً متغیر GOOGLE_CREDS را بررسی کنید.")
        return

    share_text = calculate_villa_shares(data['amount'], data['type']) if data['account'] == 'ویلا_یالبندان' else ""

    save_to_sheet(
        sheet,
        jdatetime.date.today().strftime("%Y-%m-%d"),
        now_tehran.strftime("%Y-%m-%d %H:%M"),
        data['account'], data['type'], data['amount'],
        data['category'], data['description'],
        data.get('user', 'ناصر'), share_text
    )

    reply = (
        f"✅ ثبت شد!\n"
        f"📌 حساب: {data['account']}\n"
        f"{'💵' if data['type'] == 'درآمد' else '💸'} نوع: {data['type']}\n"
        f"💰 مبلغ: {data['amount']:,} تومان\n"
        f"🗂️ دسته: {data.get('category', 'سایر')}\n"
        f"👤 کاربر: {data.get('user', 'ناصر')}"
    )
    if share_text:
        reply += f"\n🔗 سهم‌بندی: {share_text}"
    await reply_func(reply)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    data = context.user_data.get('pending_data')
    if not data:
        await update.callback_query.message.reply_text("❌ اطلاعات منقضی شده. لطفاً دوباره بفرستید.")
        return
    data['user'] = update.callback_query.data.replace("user_", "")
    await save_transaction(update, context, data)
    context.user_data.pop('pending_data', None)

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    sheet = connect_sheets()
    if not sheet:
        await update.message.reply_text("❌ خطا در اتصال به گوگل شیت.")
        return
    records = sheet.get_all_values()
    if len(records) < 2:
        await update.message.reply_text("📭 هنوز ثبتی ندارید.")
        return
    df = pd.DataFrame(records[1:], columns=records[0])
    df['مبلغ(تومان)'] = pd.to_numeric(df['مبلغ(تومان)'], errors='coerce').fillna(0)
    today = jdatetime.date.today().strftime("%Y-%m-%d")
    df_today = df[df['تاریخ شمسی'] == today]

    if df_today.empty:
        await update.message.reply_text(f"📭 امروز ({today}) تراکنشی نداشتید.")
    else:
        total_in  = df_today[df_today['نوع تراکنش'] == 'درآمد']['مبلغ(تومان)'].sum()
        total_out = df_today[df_today['نوع تراکنش'] == 'هزینه']['مبلغ(تومان)'].sum()
        await update.message.reply_text(
            f"📊 گزارش روزانه ({today}):\n"
            f"💵 درآمد: {total_in:,.0f} تومان\n"
            f"💸 هزینه: {total_out:,.0f} تومان\n"
            f"💼 مانده: {total_in - total_out:,.0f} تومان\n"
            f"📋 تعداد تراکنش: {len(df_today)}"
        )

async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    sheet = connect_sheets()
    if not sheet:
        await update.message.reply_text("❌ خطا در اتصال به گوگل شیت.")
        return
    records = sheet.get_all_values()
    if len(records) < 2:
        await update.message.reply_text("📭 هیچ تراکنشی برای لغو وجود ندارد.")
        return
    sheet.delete_rows(len(records))
    await update.message.reply_text("✅ آخرین تراکنش لغو شد.")

async def edit_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("فرمت: /edit [ردیف] [فیلد] [مقدار]\nفیلدها: amount, category, description, user")
        return

    try:
        row_num = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ شماره ردیف باید عدد باشد.")
        return

    field, new_value = args[1], " ".join(args[2:])
    sheet = connect_sheets()
    if not sheet:
        await update.message.reply_text("❌ خطا در اتصال به گوگل شیت.")
        return
    records = sheet.get_all_values()

    if len(records) < row_num + 1:
        await update.message.reply_text("❌ شماره ردیف نامعتبر است.")
        return

    field_map = {"amount": "مبلغ(تومان)", "category": "دسته‌بندی",
                 "description": "توضیحات کامل", "user": "کاربر"}
    if field not in field_map:
        await update.message.reply_text(f"❌ فیلد نامعتبر. فیلدهای معتبر: {', '.join(field_map.keys())}")
        return

    col_index = records[0].index(field_map[field]) + 1
    sheet.update_cell(row_num + 1, col_index, new_value)
    await update.message.reply_text(f"✅ فیلد {field} در ردیف {row_num} به‌روزرسانی شد.")

# ---------- راه‌اندازی اصلی ----------
def main():
    if not TOKEN:
        raise ValueError("متغیر محیطی BOT_TOKEN تنظیم نشده است!")
    if not GOOGLE_CREDS_JSON:
        raise ValueError("متغیر محیطی GOOGLE_CREDS تنظیم نشده است!")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("edit", edit_transaction))
    app.add_handler(MessageHandler(
        filters.VOICE | filters.PHOTO | filters.Document.ALL | filters.TEXT & ~filters.COMMAND,
        handle_input
    ))
    app.add_handler(CallbackQueryHandler(button_callback, pattern="^user_"))

    # BUG FIX #5: scheduler بدون args - context خودکار توسط PTB پاس میشه
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(send_monthly_villa_report, CronTrigger(day="last", hour=23, minute=59))
    scheduler.add_job(send_nightly_reminder, CronTrigger(hour=23, minute=0))
    scheduler.add_job(auto_backup, CronTrigger(hour=0, minute=0))
    scheduler.start()

    logger.info("🤖 ربات حسابداری هوشمند راه‌اندازی شد!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
