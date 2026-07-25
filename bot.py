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
    try:
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

# ---------- تبدیل اعداد فارسی به انگلیسی ----------
def normalize_numbers(text):
    persian_nums = '۰۱۲۳۴۵۶۷۸۹'
    arabic_nums = '٠١٢٣٤٥٦٧٨٩'
    english_nums = '0123456789'
    table = str.maketrans(persian_nums + arabic_nums, english_nums * 2)
    return text.translate(table)

# ---------- Fallback با کلمات کلیدی ----------
def fallback_parse(raw_text):
    text_lower = normalize_numbers(raw_text.lower())
    account, trans_type, category, user = "خانواده", "هزینه", "سایر", "ناصر"
    amount = 0
    
    if any(w in text_lower for w in ["ویلا", "یالبندان", "اجاره", "میهمان", "شبانه"]):
        account = "ویلا_یالبندان"
    if any(w in text_lower for w in ["درآمد", "فروش", "دریافت"]):
        trans_type = "درآمد"
    if "کیمیا" in text_lower: user = "کیمیا"
    elif "مه یاس" in text_lower or "مهیاس" in text_lower: user = "مه‌یاس"
    elif "همه" in text_lower or "خونه" in text_lower: user = "همه"
    
    # پشتیبانی از اعداد فارسی و انگلیسی
    match = re.search(r"(\d{1,3}(?:,\d{3})*|\d+)\s*(?:تومان|ت)", text_lower)
    if match:
        amount = int(match.group(1).replace(",", ""))
    
    if "خواربار" in text_lower: category = "خواربار"
    elif "قبض" in text_lower or "برق" in text_lower: category = "قبوض"
    elif "شارژ" in text_lower: category = "شارژ"
    elif "نظافت" in text_lower: category = "نظافت"
    elif "تعمیر" in text_lower: category = "تعمیرات"
    
    if amount == 0:
        return None, "⚠️ مبلغی در متن پیدا نشد! لطفاً عدد را به صورت انگلیسی وارد کنید (مثلاً 500000)."
    
    return {"account": account, "type": trans_type, "amount": amount, "category": category, "description": raw_text[:100], "user": user}, None

# ---------- تابع پردازش با جیمینی ----------
def process_with_gemini(raw_text):
    try:
        prompt = f"""
        شما یک دستیار مالی هوشمند هستید. متن زیر را تحلیل کن و فقط یک JSON معتبر برگردان.
        قوانین:
        - account: "ویلا_یالبندان" یا "خانواده"
        - type: "درآمد" یا "هزینه"
        - amount: عدد به تومان (اگر نبود 0). حتماً اعداد فارسی را به انگلیسی تبدیل کن.
        - category: مثلاً "اجاره", "شارژ", "خواربار", "قبوض", "تفریح", "درمان", "تعمیرات", "نظافت"
        - description: خلاصه مطلب
        - user: "ناصر", "کیمیا", "مه‌یاس" یا "همه" (اگر ویلا بود، "ناصر")
        متن: "{raw_text}"
        فرمت خروجی: {{"account": "...", "type": "...", "amount": 0, "category": "...", "description": "...", "user": "..."}}
        """
        response = gemini_model.generate_content(prompt)
        clean_json = response.text.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        # تبدیل اعداد فارسی احتمالی در پاسخ جمینای
        data['amount'] = int(normalize_numbers(str(data.get('amount', 0))))
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
    pdf.cell(95, 10, txt="سهم ناصر (۴.۵/۶)", border=1)
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
        pdf.cell(70, 8, txt=str(row['توض
