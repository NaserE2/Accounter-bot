import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import json
import os
from google.oauth2.service_account import Credentials
import gspread
import jdatetime

# تنظیمات صفحه
st.set_page_config(page_title="داشبورد حسابداری هوشمند", layout="wide")
st.title("🏦 داشبورد مدیریتی حسابدار هوشمند")

# اتصال به شیت
def connect_sheets():
    creds_json = os.getenv("GOOGLE_CREDS")
    if not creds_json:
        st.error("متغیر محیطی GOOGLE_CREDS تنظیم نشده است!")
        return None
    creds_dict = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open("حسابداری_هوشمند_نهایی").sheet1
    return sheet

sheet = connect_sheets()
if not sheet:
    st.stop()

# خواندن داده‌ها
records = sheet.get_all_values()
if len(records) < 2:
    st.warning("هنوز داده‌ای ثبت نشده است.")
    st.stop()

df = pd.DataFrame(records[1:], columns=records[0])
df['مبلغ(تومان)'] = df['مبلغ(تومان)'].astype(int)

# تبدیل تاریخ شمسی به میلادی برای نمودارها
df['تاریخ شمسی'] = pd.to_datetime(df['تاریخ شمسی'], format='%Y-%m-%d')

# انتخاب نوع حساب
account_type = st.sidebar.selectbox("نوع حساب را انتخاب کنید:", ["همه", "ویلا_یالبندان", "خانواده"])
if account_type != "همه":
    df = df[df['نوع حساب'] == account_type]

# فیلتر تاریخ
col1, col2 = st.sidebar.columns(2)
with col1:
    start_date = st.date_input("از تاریخ", value=df['تاریخ شمسی'].min())
with col2:
    end_date = st.date_input("تا تاریخ", value=df['تاریخ شمسی'].max())
mask = (df['تاریخ شمسی'] >= pd.to_datetime(start_date)) & (df['تاریخ شمسی'] <= pd.to_datetime(end_date))
df_filtered = df[mask]

# متریک‌ها
col1, col2, col3, col4 = st.columns(4)
total_income = df_filtered[df_filtered['نوع تراکنش'] == 'درآمد']['مبلغ(تومان)'].sum()
total_expense = df_filtered[df_filtered['نوع تراکنش'] == 'هزینه']['مبلغ(تومان)'].sum()
net_balance = total_income - total_expense

col1.metric("کل درآمد", f"{total_income:,.0f}")
col2.metric("کل هزینه", f"{total_expense:,.0f}")
col3.metric("مانده", f"{net_balance:,.0f}")
col4.metric("تعداد تراکنش‌ها", len(df_filtered))

# نمودارها
col1, col2 = st.columns(2)

with col1:
    # نمودار دایره‌ای دسته‌بندی هزینه‌ها
    df_expense = df_filtered[df_filtered['نوع تراکنش'] == 'هزینه']
    if not df_expense.empty:
        fig_pie = px.pie(df_expense, values='مبلغ(تومان)', names='دسته‌بندی', title="توزیع هزینه‌ها بر اساس دسته‌بندی")
        st.plotly_chart(fig_pie, use_container_width=True)
    else:
        st.info("هزینه‌ای برای نمایش وجود ندارد.")

with col2:
    # نمودار خطی روند درآمد و هزینه
    daily_summary = df_filtered.groupby(['تاریخ شمسی', 'نوع تراکنش'])['مبلغ(تومان)'].sum().unstack().fillna(0)
    if not daily_summary.empty:
        fig_line = go.Figure()
        if 'درآمد' in daily_summary.columns:
            fig_line.add_trace(go.Scatter(x=daily_summary.index, y=daily_summary['درآمد'], mode='lines+markers', name='درآمد'))
        if 'هزینه' in daily_summary.columns:
            fig_line.add_trace(go.Scatter(x=daily_summary.index, y=daily_summary['هزینه'], mode='lines+markers', name='هزینه'))
        fig_line.update_layout(title="روند درآمد و هزینه", xaxis_title="تاریخ", yaxis_title="مبلغ (تومان)")
        st.plotly_chart(fig_line, use_container_width=True)

# جدول کامل تراکنش‌ها
st.subheader("📋 لیست کامل تراکنش‌ها")
st.dataframe(df_filtered[['تاریخ شمسی', 'نوع حساب', 'نوع تراکنش', 'مبلغ(تومان)', 'دسته‌بندی', 'کاربر', 'توضیحات کامل']], use_container_width=True)

# دکمه دانلود
csv = df_filtered.to_csv(index=False, encoding='utf-8-sig')
st.download_button(label="📥 دانلود گزارش (Excel/CSV)", data=csv, file_name="report.csv", mime="text/csv")