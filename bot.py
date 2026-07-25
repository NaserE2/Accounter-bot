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
        
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.json')])
        if len(backups) > 30:
            for f in backups[:-30]:
                os.remove(os.path.join(BACKUP_DIR, f))
    except Exception as e:
        logger.error(f"خطا در پشتیبان‌گیری: {e}")

# ---------- Fallback با کلمات کلیدی ----------
def fallback_parse(raw_text):
    text_lower = raw_text.lower()
    account, trans_type, category, user = "خانواده", "هزینه", "سایر", "ناصر"
    amount = 0
    
    if any(w in text_lower for w in ["ویلا", "یالبندان", "اجاره", "میهمان", "شبانه"]):
        account = "ویلا_یالبندان"
    if any(w in text_lower for w in ["درآمد", "فروش", "دریافت"]):
        trans_type = "درآمد"
    if "کیمیا" in text_lower: user = "کیمیا"
    elif "مه یاس" in text_lower or "مهیاس" in text_lower: user = "مه‌یاس"
    elif "همه" in text_lower or "خونه" in text_lower: user = "همه"
    
    match = re.search(r"(\d{1,3}(?:,\d{3})*|\d+)\s*(?:تومان|ت)", text_lower)
    if match:
        amount = int(match.group(1).replace(",", ""))
    
    if "خواربار" in text_lower: category = "خواربار"
    elif "قبض" in text_lower or "برق" in text_lower: category = "قبوض"
    elif "شارژ" in text_lower: category = "شارژ"
    elif "نظافت" in text_lower: category = "نظافت"
    elif "تعمیر" in text_lower: category = "تعمیرات"
    
    if amount == 0:
        return None, "⚠️ مبلغی در متن پیدا نشد! لطفاً عدد را به تومان وارد کنید."
    
    return {"account": account, "type": trans_type, "amount": amount, "category": category, "description": raw_text[:100], "user": user}, None

# ---------- تابع پردازش با جیمینی ----------
def process_with_gemini(raw_text):
    try:
        prompt = f"""
        شما یک دستیار مالی هوشمند هستید. متن زیر را تحلیل کن و فقط یک JSON معتبر برگردان.
        قوانین:
        - account: "ویلا_یالبندان" یا "خانواده"
        - type: "درآمد" یا "هزینه"
        - amount: عدد به تومان (اگر نبود 0)
        - category: مثلاً "اجاره", "شارژ", "خواربار", "قبوض", "تفریح", "درمان", "تعمیرات", "نظافت"
        - description: خلاصه مطلب
        - user: "ناصر", "کیمیا", "مه‌یاس" یا "همه" (اگر ویلا بود، "ناصر")
        متن: "{raw_text}"
        فرمت خروجی: {{"account": "...", "type": "...", "amount": 0, "category": "...", "description": "...", "user": "..."}}
        """
        response = gemini_model.generate_content(prompt)
        clean_json = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        if data.get("amount", 0) == 0:
            return None, "⚠️ مبلغی در متن پیدا نشد! لطفاً عدد را به تومان وارد کنید."
        return data, None
    except Exception as e:
        logger.error(f"خطا در جیمینی: {e}. استفاده از Fallback...")
        return fallback_parse(raw_text)

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
    nahid = amount * (1.5 / 6)
    sign = -1 if trans_type == "هزینه" else 1
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

    total_in = df_villa[df_villa['نوع تراکنش'] == 'درآمد']['مبلغ(تومان)'].sum()
    total_out = df_villa[df_villa['نوع تراکنش'] == 'هزینه']['مبلغ(تومان)'].sum()
    net = total_in - total_out
    
    pdf.set_font('DejaVu', size=11)
    pdf.cell(95, 10, txt="کل درآمد", border=1)
    pdf.cell(95, 10, txt=f"{total_in:,.0f} تومان", border=1, ln=True)
    pdf.cell(95, 10, txt="کل هزینه", border=1)
    pdf.cell(95, 10, txt=f"{total_out:,.0f} تومان", border=1, ln=True)
    pdf.cell(95, 10, txt="سود خالص", border=1)
    pdf.cell(95, 10, txt=f"{net:,.0f} تومان", border=1, ln=True)
    pdf.ln(5)
    pdf.cell(95, 10, txt="سهم ناصر (۴.۵/)", border=1)
    pdf.cell(95, 10, txt=f"{net * (4.5/6):,.0f} تومان", border=1, ln=True)
    pdf.cell(95, 10, txt="سهم ناهید (۱.۵/۶)", border=1)
    pdf.cell(95, 10, txt=f"{net * (1.5/6):,.0f} تومان", border=1, ln=True)
    pdf.ln(10)

    pdf.set_font('DejaVu', size=9)
    pdf.cell(40, 8, txt="تاریخ", border=1)
    pdf.cell(40, 8, txt="نوع", border=1)
    pdf.cell(40, 8, txt="مبلغ", border=1)
    pdf.cell(70, 8, txt="توضیحات", border=1, ln=True)
    for _, row in df_villa.iterrows():
        pdf.cell(40, 8, txt=str(row['تاریخ شمسی'])[5:10], border=1)
        pdf.cell(40, 8, txt=str(row['نوع تراکنش']), border=1)
        pdf.cell(40, 8, txt=f"{row['مبلغ(تومان)']:,.0f}", border=1)
        pdf.cell(70, 8, txt=str(row['توضیحات کامل'])[:25], border=1, ln=True)

    temp_pdf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    pdf.output(temp_pdf.name)
    return temp_pdf.name

# ---------- وظایف زمان‌بندی شده ----------
async def send_monthly_villa_report(context: ContextTypes.DEFAULT_TYPE):
    today = jdatetime.date.today()
    if today.day == jdatetime.date(today.year, today.month, 1).days_in_month:
        sheet = connect_sheets()
        records = sheet.get_all_values()
        if len(records) < 2: return
        df = pd.DataFrame(records[1:], columns=records[0])
        month_str = f"{today.year}-{today.month:02d}"
        df_villa = df[(df['تاریخ شمسی'].str.startswith(month_str)) & (df['نوع حساب'] == 'ویلا_یالبندان')]
        if not df_villa.empty:
            pdf_path = generate_villa_pdf(month_str, df_villa)
            try:
                with open(pdf_path, 'rb') as f:
                    await context.bot.send_document(chat_id=ADMIN_USER_ID, document=f, caption=f"📊 گزارش ماهانه {month_str}")
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
        "🌟 قابلیت‌ها:\n"
        "1️ ارسال ویس، عکس، PDF یا متن برای ثبت خودکار\n"
        "2️⃣ تشخیص هوشمند توسط Gemini (با Fallback دستی)\n"
        "3️ محاسبه خودکار سهام شراکت ویلای یالبندان\n"
        "4️⃣ گزارش‌ها: /report, /monthly, /yearly 1403, /status\n"
        "5️⃣ مدیریت: /undo, /edit [ردیف] [فیلد] [مقدار]"
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

async def handle_input_internal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = ""
    
    try:
        if update.message.voice:
            # دانلود ویس و ارسال مستقیم به Gemini برای تبدیل به متن و استخراج اطلاعات
            voice_file = await update.message.voice.get_file()
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                await voice_file.download_to_drive(tmp.name)
                
                # استفاده از قابلیت چندوجهی Gemini برای پردازش مستقیم ویس
                file_obj = genai.upload_file(tmp.name, mime_type="audio/ogg")
                response = gemini_model.generate_content(file_obj)
                raw_text = response.text.strip()
                
            os.unlink(tmp.name)
            
        elif update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                await photo_file.download_to_drive(tmp.name)
                raw_text = extract_text_from_image(tmp.name)
            os.unlink(tmp.name)
            
        elif update.message.document and update.message.document.mime_type == "application/pdf":
            pdf_file = await update.message.document.get_file()
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                await pdf_file.download_to_drive(tmp.name)
                raw_text = extract_text_from_pdf(tmp.name)
            os.unlink(tmp.name)
            
        elif update.message.text:
            raw_text = update.message.text
            
        else:
            await update.message.reply_text("❌ نوع ورودی پشتیبانی نمی‌شود.")
            return

        if not raw_text.strip():
            await update.message.reply_text("❌ محتوایی استخراج نشد.")
            return

        data, error = process_with_gemini(raw_text)
        if error:
            await update.message.reply_text(error)
            return

        if data['account'] == 'خانواده' and data.get('user') in [None, 'همه', '']:
            keyboard = [
                [InlineKeyboardButton("ناصر", callback_data="user_ناصر"), InlineKeyboardButton("کیمیا", callback_data="user_کیمیا")],
                [InlineKeyboardButton("مه‌یاس", callback_data="user_مه‌یاس"), InlineKeyboardButton("خونه (همه)", callback_data="user_همه")]
            ]
            context.user_data['pending_data'] = data
            await update.message.reply_text("👨‍👩‍👧 این هزینه/درآمد برای کدام یک از اعضای خانواده است؟", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        await save_transaction(update, context, data)
    except Exception as e:
        logger.error(f"خطای غیرمنتظره در پردازش: {e}")
        await update.message.reply_text("❌ خطایی رخ داد. لطفاً دوباره تلاش کنید.")

async def save_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE, data):
    now_tehran = datetime.now(TIMEZONE)
    sheet = connect_sheets()
    share_text = calculate_villa_shares(data['amount'], data['type']) if data['account'] == 'ویلا_یالبندان' else ""
    
    save_to_sheet(sheet, jdatetime.date.today().strftime("%Y-%m-%d"), now_tehran.strftime("%Y-%m-%d %H:%M"),
                  data['account'], data['type'], data['amount'], data['category'], data['description'], data.get('user', 'ناصر'), share_text)
    
    reply = f"✅ ثبت شد!\n📌 حساب: {data['account']}\n💰 مبلغ: {data['amount']:,} تومان\n👤 کاربر: {data.get('user', 'ناصر')}"
    if share_text: reply += f"\n🔗 سهم: {share_text}"
    await update.message.reply_text(reply)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    data = context.user_data.get('pending_data')
    if data:
        data['user'] = update.callback_query.data.replace("user_", "")
        await save_transaction(update, context, data)
        context.user_data.pop('pending_data', None)

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    sheet = connect_sheets()
    records = sheet.get_all_values()
    if len(records) < 2:
        await update.message.reply_text("📭 هنوز ثبت‌ای ندارید.")
        return
    df = pd.DataFrame(records[1:], columns=records[0])
    today = jdatetime.date.today().strftime("%Y-%m-%d")
    df_today = df[df['تاریخ شمسی'] == today]
    
    if df_today.empty:
        await update.message.reply_text(f"📭 امروز {today} تراکنشی نداشتید.")
    else:
        total_in = df_today[df_today['نوع تراکنش'] == 'درآمد']['مبلغ(تومان)'].sum()
        total_out = df_today[df_today['نوع تراکنش'] == 'هزینه']['مبلغ(تومان)'].sum()
        await update.message.reply_text(f"📊 گزارش روزانه ({today}):\n💵 درآمد: {total_in:,.0f}\n💸 هزینه: {total_out:,.0f}\n📈 مانده: {total_in - total_out:,.0f}")

async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    sheet = connect_sheets()
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
    
    row_num, field, new_value = int(args[0]), args[1], " ".join(args[2:])
    sheet = connect_sheets()
    records = sheet.get_all_values()
    
    if len(records) < row_num + 1:
        await update.message.reply_text("❌ شماره ردیف نامعتبر است.")
        return
    
    field_map = {"amount": "مبلغ(تومان)", "category": "دسته‌بندی", "description": "توضیحات کامل", "user": "کاربر"}
    if field not in field_map:
        await update.message.reply_text("❌ فیلد نامعتبر.")
        return
    
    col_index = records[0].index(field_map[field]) + 1
    sheet.update_cell(row_num + 1, col_index, new_value)
    await update.message.reply_text(f"✅ فیلد {field} به‌روزرسانی شد.")

# ---------- راه‌اندازی اصلی ----------
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("edit", edit_transaction))
    app.add_handler(MessageHandler(filters.VOICE | filters.PHOTO | filters.Document.ALL | filters.TEXT, handle_input))
    app.add_handler(CallbackQueryHandler(button_callback, pattern="^user_"))

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(send_monthly_villa_report, CronTrigger(day="last", hour=23, minute=59), args=[app])
    scheduler.add_job(send_nightly_reminder, CronTrigger(hour=23, minute=0), args=[app])
    scheduler.add_job(auto_backup, CronTrigger(hour=0, minute=0), args=[app])
    scheduler.start()

    logger.info("🤖 ربات حسابداری هوشمند (نسخه سبک) راه‌اندازی شد!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
