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

latest_file = max(list_of_files, key=os.path.getctime)

with open(latest_file, 'r') as f:
    data = json.load(f)

st.subheader("Ringkasan Komputasi Terakhir")
col1, col2, col3 = st.columns(3)
col1.metric("Waktu Eksekusi (UTC)", data.get("run_time", "N/A"))
col2.metric("Rekomendasi BUY Ditemukan", data.get("actionable_recommendations", 0))
col3.metric("Data Bersumber", latest_file.split('\\')[-1])

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