import streamlit as st

st.set_page_config(
    page_title="TrafficFlow Command Center",
    page_icon="🚔",
    layout="wide",
    initial_sidebar_state="collapsed"
)

base = "http://localhost:8000/dashboard/"
page = st.query_params.get("page", "dashboard")
DASHBOARD_URL = base if page == "dashboard" else f"{base}{page}"

st.markdown(
    f'<iframe src="{DASHBOARD_URL}" style="position:fixed;top:0;left:0;width:100vw;height:100vh;border:none;z-index:999999;"></iframe>',
    unsafe_allow_html=True,
)
