import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import json
import os
from google.oauth2.service_account import Credentials
import gspread
import jdatetime

st.set_page_config(page_title="داشبورد حسابداری هوشمند", layout="wide", page_icon="🏦")
st.title(" داشبورد مدیریتی حسابدار هوشمند")

def connect_sheets():
    creds_json = os.getenv("GOOGLE_CREDS")
    if not creds_json:
        st.error("متغیر محیطی GOOGLE_CREDS تنظیم نشده است!")
        return None
    try:
        creds_dict = json.loads(creds_json)
        # scopes کامل‌تر برای دسترسی به Drive و Sheets
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        return client.open("حسابداری_هوشمند_نهایی").sheet1
    except Exception as e:
        st.error(f"خطا در اتصال به گوگل شیت: {e}")
        return None

sheet = connect_sheets()
if not sheet:
    st.stop()

records = sheet.get_all_values()
if len(records) < 2:
    st.warning("هنوز داده‌ای در حساب ثبت نشده است.")
    st.stop()

df = pd.DataFrame(records[1:], columns=records[0])
df['مبلغ(تومان)'] = pd.to_numeric(df['مبلغ(تومان)'], errors='coerce').fillna(0).astype(int)

df['تاریخ مرتب‌سازی'] = pd.to_datetime(df['تاریخ شمسی'], format='%Y-%m-%d', errors='coerce')
df = df.sort_values('تاریخ مرتب‌سازی', ascending=False)

account_type = st.sidebar.selectbox("نوع حساب را انتخاب کنید:", ["همه", "ویلا_یالبندان", "خانواده"])
if account_type != "همه":
    df = df[df['نوع حساب'] == account_type]

col1, col2 = st.sidebar.columns(2)
with col1:
    start_date = st.date_input("از تاریخ", value=df['تاریخ شمسی'].min())
with col2:
    end_date = st.date_input("تا تاریخ", value=df['تاریخ شمسی'].max())

mask = (df['تاریخ شمسی'] >= str(start_date)) & (df['تاریخ شمسی'] <= str(end_date))
df_filtered = df[mask]

col1, col2, col3, col4 = st.columns(4)
total_income = df_filtered[df_filtered['نوع تراکنش'] == 'درآمد']['مبلغ(تومان)'].sum()
total_expense = df_filtered[df_filtered['نوع تراکنش'] == 'هزینه']['مبلغ(تومان)'].sum()

col1.metric("کل درآمد", f"{total_income:,.0f} تومان")
col2.metric("کل هزینه", f"{total_expense:,.0f} تومان")
col3.metric("مانده", f"{total_income - total_expense:,.0f} تومان")
col4.metric("تعداد تراکنش‌ها", len(df_filtered))

st.subheader("📊 نمودارها")
col1, col2 = st.columns(2)
with col1:
    df_expense = df_filtered[df_filtered['نوع تراکنش'] == 'هزینه']
    if not df_expense.empty:
        fig_pie = px.pie(df_expense, values='مبلغ(تومان)', names='دسته‌بندی', title="توزیع هزینه‌ها")
        st.plotly_chart(fig_pie, use_container_width=True)
    else:
        st.info("هزینه‌ای برای نمایش وجود ندارد.")

with col2:
    daily_summary = df_filtered.groupby(['تاریخ شمسی', 'نوع تراکنش'])['مبلغ(تومان)'].sum().unstack().fillna(0)
    if not daily_summary.empty:
        fig_line = go.Figure()
        if 'درآمد' in daily_summary.columns:
            fig_line.add_trace(go.Scatter(x=daily_summary.index, y=daily_summary['درآمد'], mode='lines+markers', name='درآمد'))
        if 'هزینه' in daily_summary.columns:
            fig_line.add_trace(go.Scatter(x=daily_summary.index, y=daily_summary['هزینه'], mode='lines+markers', name='هزینه'))
        fig_line.update_layout(title="روند درآمد و هزینه", xaxis_title="تاریخ", yaxis_title="مبلغ (تومان)")
        st.plotly_chart(fig_line, use_container_width=True)

st.subheader(" لیست کامل تراکنش‌ها")
st.dataframe(df_filtered[['تاریخ شمسی', 'نوع حساب', 'نوع تراکنش', 'مبلغ(تومان)', 'دسته‌بندی', 'کاربر', 'توضیحات کامل']], use_container_width=True)

csv = df_filtered.to_csv(index=False, encoding='utf-8-sig')
st.download_button(label="📥 دانلود گزارش (CSV)", data=csv, file_name="report.csv", mime="text/csv")
