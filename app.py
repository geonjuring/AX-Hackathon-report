import streamlit as st

st.navigation([
    st.Page('pages/1_생산_일간_리포트.py', title='결산 리포트', icon='📊', default=True),
]).run()
