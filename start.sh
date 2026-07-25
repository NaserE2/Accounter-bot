#!/bin/bash

# اجرای Streamlit در پس‌زمینه تا پورت 8501 باز شود و Render سرویس را زنده نگه دارد
streamlit run dashboard.py --server.port=8501 --server.address=0.0.0.0 &

# اجرای ربات تلگرام در پیش‌زمینه
python bot.py