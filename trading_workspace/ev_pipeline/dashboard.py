import streamlit as st
import pandas as pd
import json
import glob
import os

st.set_page_config(page_title="EV Betting Dashboard", layout="wide")
st.title("Polymarket EV Analysis")

# Mencari file JSON snapshot terbaru
list_of_files = glob.glob('snapshots/*.json')

if not list_of_files:
    st.warning("Belum ada data snapshot. Jalankan eksekusi utama (ev_pipeline.py) terlebih dahulu.")
    st.stop()

# FIX D3a: getctime di Linux mengembalikan inode change time, bukan creation time.
# getmtime (modification time) konsisten di semua OS.
latest_file = max(list_of_files, key=os.path.getmtime)

with open(latest_file, 'r') as f:
    data = json.load(f)

st.subheader("Ringkasan Komputasi Terakhir")
col1, col2, col3 = st.columns(3)
col1.metric("Waktu Eksekusi (UTC)", data.get("run_time", "N/A"))
# FIX B1: key yang ditulis reporter.py baris 275 adalah "buy_recommendations"
# bukan "actionable_recommendations" — penyebab output selalu = 0
col2.metric("Rekomendasi BUY Ditemukan", data.get("buy_recommendations", 0))
# FIX D3b: split('\\') tidak berfungsi di Linux. os.path.basename() cross-platform.
col3.metric("Data Bersumber", os.path.basename(latest_file))

st.markdown("---")

if data.get('results'):
    df = pd.DataFrame(data['results'])
    
    # Memilih dan mengganti nama kolom metrik utama
    display_df = df[[
        'recommendation', 'sport_event', 'question', 
        'edge_yes', 'edge_no', 'true_prob_yes', 'implied_prob_yes', 'volume_usd'
    ]].copy()
    
    # Format persentase agar lebih mudah dibaca
    for col in ['edge_yes', 'edge_no', 'true_prob_yes', 'implied_prob_yes']:
        display_df[col] = (display_df[col] * 100).map("{:.2f}%".format)
        
    # Format mata uang
    display_df['volume_usd'] = display_df['volume_usd'].map("${:,.0f}".format)

    st.subheader("Data Analisis Peluang (Edge)")
    st.dataframe(display_df, use_container_width=True)
else:
    st.info("Tidak ada transaksi berpeluang positif (Edge) yang ditemukan dalam komputasi ini. Tunggu fluktuasi pasar atau jalankan ulang pipeline.")