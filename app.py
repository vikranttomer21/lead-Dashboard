import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import urllib.parse
import os
import datetime
import re
import engine

st.set_page_config(page_title="Sales Intelligence CRM", layout="wide")

# Custom Clean Styling
st.markdown("""
<style>
    .metric-card {
        background-color: #1E2129;
        border: 1px solid #2E3340;
        border-radius: 6px;
        padding: 16px;
    }
    .stButton>button {
        border-radius: 4px;
    }
    .lead-card {
        border: 1px solid #2E3340;
        background-color: #161922;
        border-radius: 8px;
        padding: 14px;
        margin-bottom: 12px;
    }
</style>
""", unsafe_allow_html=True)

# --- AUTHENTICATION & LOGIN ---
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
    st.session_state.username = None
    st.session_state.user_role = None
    st.session_state.user_display_name = None

def login():
    st.markdown("### Sign In to Lead Platform")
    with st.form("login_form"):
        username_input = st.text_input("Username").strip().lower()
        password_input = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign In")
        
        if submitted:
            users_config = st.secrets.get("users", {})
            if username_input in users_config:
                user_info = users_config[username_input]
                if str(user_info.get("password")) == str(password_input):
                    st.session_state.authenticated = True
                    st.session_state.username = username_input
                    st.session_state.user_role = str(user_info.get("role", "sales_rep")).lower()
                    st.session_state.user_display_name = user_info.get("name", username_input.capitalize())
                    st.rerun()
                else:
                    st.error("Incorrect password.")
            else:
                st.error("Invalid username or password.")

if not st.session_state.authenticated:
    login()
    st.stop()

# --- OPTIMIZED GOOGLE SHEETS CONNECTION & RAM CACHING ---
@st.cache_resource(ttl=3600)
def get_gspread_client():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_dict = dict(st.secrets["gcp_service_account"])
    if "token_uri" not in creds_dict:
        creds_dict["token_uri"] = "https://oauth2.googleapis.com/token"
        
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scope)
    return gspread.authorize(credentials)

@st.cache_resource(ttl=600)
def get_spreadsheet_and_titles():
    client = get_gspread_client()
    sheet_url = st.secrets["spreadsheet"]["sheet_url"]
    spreadsheet = client.open_by_url(sheet_url)
    titles = [ws.title.strip() for ws in spreadsheet.worksheets()]
    return spreadsheet, titles

def col_idx_to_a1(idx: int) -> str:
    letters = ""
    while idx > 0:
        idx, remainder = divmod(idx - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters

# Cache tab data in RAM for 300s (Fast UI navigation)
@st.cache_data(ttl=300, show_spinner=False)
def fetch_cached_tab_data(sheet_url: str, tab_name: str) -> pd.DataFrame:
    client = get_gspread_client()
    spreadsheet = client.open_by_url(sheet_url)
    sheet = spreadsheet.worksheet(tab_name)
    raw_rows = sheet.get_all_values()
    
    if not raw_rows or len(raw_rows) < 2:
        return pd.DataFrame()

    # Dynamic Header Detection (scans up to row 5)
    header_row_idx = 0
    for idx, r in enumerate(raw_rows[:5]):
        cleaned = [str(c).strip().lower() for c in r if str(c).strip()]
        if any(k in cleaned for k in ['name', 'phone_e164', 'phone', 'record_hash', 'city', 'title', 'business_name', 'locality']):
            header_row_idx = idx
            break

    headers = [str(h).strip() for h in raw_rows[header_row_idx]]
    num_cols = len(headers)
    data_rows = raw_rows[header_row_idx + 1:]

    records = []
    for offset, r in enumerate(data_rows):
        # Retain row as long as ANY cell contains valid data
        if not any(bool(str(c).strip()) for c in r):
            continue

        actual_sheet_row = header_row_idx + 2 + offset
        padded_row = r + [""] * (num_cols - len(r))
        row_raw = {headers[i]: str(padded_row[i]).strip() for i in range(num_cols) if headers[i]}

        def get_field(*possible_names, default=""):
            for name in possible_names:
                for k, v in row_raw.items():
                    if k.lower() == name.lower() and v:
                        return v
            return default

        name_val = get_field('Name', 'Business_Name', 'Title', 'Store_Name', 'name', default=f"Lead @ Row {actual_sheet_row}")
        phone_raw = get_field('Phone_E164', 'Phone', 'phone', 'Contact', 'Mobile', default="")
        phone_cleaned = re.sub(r'[^\d+]', '', phone_raw)
        wa_link = get_field('WhatsApp_Click_Link', 'whatsapp_click_link', 'wa_link', default="")
        
        if not phone_cleaned and "wa.me/" in wa_link:
            m = re.search(r'wa\.me/(\d+)', wa_link)
            if m:
                phone_cleaned = m.group(1)

        unique_key = f"{tab_name}::{actual_sheet_row}"

        row_dict = {
            "_lead_unique_key": unique_key,
            "_sheet_row_num": actual_sheet_row,
            "_sheet_tab_name": tab_name,
            "Record_Hash": get_field('Record_Hash', 'record_hash', default=f"HASH_{actual_sheet_row}"),
            "Lead_Tier": get_field('Lead_Tier', 'lead_tier', default=""),
            "Platinum_Score": get_field('Platinum_Score', 'platinum_score', default="0"),
            "Name": name_val,
            "Location": get_field('Location', 'Address', 'locality', 'Locality', default=""),
            "City": get_field('City', 'city', default=""),
            "Phone_E164": phone_cleaned,
            "WhatsApp_Click_Link": wa_link,
            "Website": get_field('Website', 'website', 'Domain', default=""),
            "Infra_Classification": get_field('Infra_Classification', 'infra_classification', default="NO_WEBSITE"),
            "CMS_Tech_Stack": get_field('CMS_Tech_Stack', 'cms_tech_stack', default="None"),
            "Pixels_Tracked": get_field('Pixels_Tracked', 'pixels_tracked', default="None"),
            "Network_Footprint": get_field('Network_Footprint', 'network_footprint', default="Single Location"),
            "Entity_Resolution_Type": get_field('Entity_Resolution_Type', 'entity_resolution_type', default="Independent"),
            
            # Scraped Rich Copy
            "Audit_Triggers": get_field('Audit_Triggers', 'audit_triggers', default=""),
            "Primary_Pitch_Strategy": get_field('Primary_Pitch_Strategy', 'primary_pitch_strategy', default=""),
            "Client_Pain_Point": get_field('Client_Pain_Point', 'client_pain_point', default=""),
            "WhatsApp_DM_Hook": get_field('WhatsApp_DM_Hook', 'whatsapp_dm_hook', default=""),
            "Value_First_Audit_Hook": get_field('Value_First_Audit_Hook', 'value_first_audit_hook', default=""),
            "Competitor_Comparison_Hook": get_field('Competitor_Comparison_Hook', 'competitor_comparison_hook', default=""),
            "HTML_Scorecard_File": get_field('HTML_Scorecard_File', 'html_scorecard_file', default=""),
            
            # CRM Tracking Fields
            "crm_status": get_field('crm_status', default=""),
            "notes": get_field('notes', default=""),
            "next_followup": get_field('next_followup', default=""),
            "last_contacted": get_field('last_contacted', default=""),
            "assigned_to": get_field('assigned_to', default=""),
        }

        scores = engine.calculate_scores(row_dict)
        row_dict.update(scores)
        records.append(row_dict)

    if not records:
        return pd.DataFrame()

    return pd.DataFrame(records).reset_index(drop=True)

def update_lead_in_sheet(spreadsheet, tab_name: str, exact_sheet_row: int, update_dict: dict):
    sheet = spreadsheet.worksheet(tab_name)
    headers = [str(h).strip() for h in sheet.row_values(1)]
    header_modified = False

    for key in update_dict.keys():
        if key not in headers:
            headers.append(key)
            header_modified = True

    if header_modified:
        range_header = f"A1:{col_idx_to_a1(len(headers))}1"
        try:
            sheet.update(values=[headers], range_name=range_header)
        except TypeError:
            sheet.update(range_header, [headers])

    current_row_values = sheet.row_values(exact_sheet_row)
    if len(current_row_values) < len(headers):
        current_row_values.extend([""] * (len(headers) - len(current_row_values)))

    for key, val in update_dict.items():
        if key in headers:
            col_pos = headers.index(key)
            current_row_values[col_pos] = str(val)

    range_update = f"A{exact_sheet_row}:{col_idx_to_a1(len(headers))}{exact_sheet_row}"
    try:
        sheet.update(values=[current_row_values], range_name=range_update)
    except TypeError:
        sheet.update(range_update, [current_row_values])
    
    # Invalidate cache so modifications load immediately
    fetch_cached_tab_data.clear()

# Session state initialization
if "view" not in st.session_state:
    st.session_state.view = "dashboard"
if "selected_lead_name" not in st.session_state:
    st.session_state.selected_lead_name = None
if "selected_lead_hash" not in st.session_state:
    st.session_state.selected_lead_hash = None
if "assigned_batch_keys" not in st.session_state:
    st.session_state.assigned_batch_keys = []

try:
    spreadsheet, tab_names = get_spreadsheet_and_titles()
    sheet_url = st.secrets["spreadsheet"]["sheet_url"]
except Exception as e:
    st.error(f"Failed to connect to Google Sheets: {e}")
    st.stop()

# --- SIDEBAR ---
st.sidebar.markdown(f"**User:** {st.session_state.user_display_name}")
st.sidebar.caption(f"Role: **{st.session_state.user_role.upper()}**")

if st.sidebar.button("🔄 Refresh Data Cache", use_container_width=True):
    fetch_cached_tab_data.clear()
    st.toast("Data cache refreshed!")
    st.rerun()

if st.sidebar.button("Log Out", use_container_width=True):
    st.session_state.authenticated = False
    st.session_state.username = None
    st.session_state.selected_lead_name = None
    st.session_state.selected_lead_hash = None
    st.session_state.assigned_batch_keys = []
    st.rerun()

st.sidebar.divider()

current_user = st.session_state.username
is_admin = (st.session_state.user_role == "admin")

# --- TOP NAVIGATION BAR ---
if is_admin:
    col_nav1, col_tab_sel, col_nav2, col_nav3, col_nav4 = st.columns([2, 2, 1, 1, 1])
else:
    col_nav1, col_tab_sel, col_nav2, col_nav3 = st.columns([2.5, 2.5, 1, 1])

with col_nav1:
    st.markdown("### Lead Intelligence & Outreach")

def reset_tab_state():
    st.session_state.selected_lead_name = None
    st.session_state.selected_lead_hash = None
    st.session_state.view = "dashboard"
    st.session_state.assigned_batch_keys = []

with col_tab_sel:
    selected_tab_name = st.selectbox(
        "Active Campaign Tab", 
        tab_names, 
        key="active_keyword_tab",
        on_change=reset_tab_state
    )

with col_nav2:
    if st.button("Dashboard", use_container_width=True):
        st.session_state.view = "dashboard"
        st.session_state.selected_lead_name = None
        st.session_state.selected_lead_hash = None
        st.rerun()

with col_nav3:
    nav_label = "Lead Master DB" if is_admin else "My Claimed Leads"
    if st.button(nav_label, use_container_width=True):
        st.session_state.view = "database"
        st.session_state.selected_lead_name = None
        st.session_state.selected_lead_hash = None
        st.rerun()

if is_admin:
    with col_nav4:
        if st.button("Rep Analytics", use_container_width=True):
            st.session_state.view = "analytics"
            st.session_state.selected_lead_name = None
            st.session_state.selected_lead_hash = None
            st.rerun()

st.divider()

# Load Tab Data instantly from RAM cache
try:
    df_master = fetch_cached_tab_data(sheet_url, selected_tab_name)
except Exception as e:
    st.error(f"Could not load tab data: {e}")
    st.stop()

if df_master.empty and st.session_state.view != "analytics":
    st.warning(f"No records found in campaign tab '{selected_tab_name}'.")
    st.stop()

def generate_fresh_batch(df_pool):
    unclaimed = df_pool[df_pool['assigned_to'].isin(['', 'Unassigned', None])]
    if unclaimed.empty:
        return []
    sample_size = min(10, len(unclaimed))
    return unclaimed.sample(n=sample_size)['_lead_unique_key'].tolist()

if not is_admin:
    if not st.session_state.assigned_batch_keys and not df_master.empty:
        st.session_state.assigned_batch_keys = generate_fresh_batch(df_master)

# --- VIEW 1: DASHBOARD ---
if st.session_state.view == "dashboard":
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    st.subheader(f"Pipeline Overview — {selected_tab_name}")

    if is_admin:
        due_today_pool = df_master[
            (df_master['next_followup'] != '') & 
            (df_master['next_followup'] <= today_str) & 
            (~df_master['crm_status'].str.upper().isin(['WON', 'LOST']))
        ]
        
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Leads Pool", len(df_master))
        m2.metric("Unassigned Pool", len(df_master[df_master['assigned_to'].isin(['', 'Unassigned', None])]))
        m3.metric("Follow-ups Due Today", len(due_today_pool))
        m4.metric("In-Outreach Active", len(df_master[df_master['crm_status'].str.upper().isin(['CONTACTED', 'CLAIMED', 'NO_ANSWER', 'FOLLOW-UP'])]))
        
        if not due_today_pool.empty:
            with st.expander(f"Action Required: {len(due_today_pool)} Follow-Ups Due Today", expanded=True):
                for idx, row in due_today_pool.iterrows():
                    c1, c2, c3, c4 = st.columns([3, 2, 2, 1])
                    c1.markdown(f"**{row.get('Name', 'Unnamed Business')}**\n*{row.get('Location', '')} ({row.get('City', '')})*")
                    c2.markdown(f"**Assigned Rep:** `{row.get('assigned_to', 'Unassigned')}`\n*Due Date: {row.get('next_followup')}*")
                    c3.markdown(f"**Status:** `{row.get('crm_status') or 'NEW'}`\n*Notes: {str(row.get('notes', ''))[:45]}...*")
                    if c4.button("Open", key=f"due_admin_{idx}"):
                        st.session_state.selected_lead_name = row.get("Name")
                        st.session_state.selected_lead_hash = row.get("Record_Hash")
                        st.session_state.view = "lead_profile"
                        st.rerun()
                    st.divider()

        st.write("---")
        st.markdown("#### High-Priority Outreach Targets (Master View)")
        priority_df = df_master.sort_values(by="opportunity_score", ascending=False).head(10)
        
        for idx, row in priority_df.iterrows():
            c1, c2, c3 = st.columns([3, 2, 1])
            assigned_text = row.get('assigned_to') if row.get('assigned_to') else "Unassigned"
            c1.markdown(f"**{row.get('Name', 'Unnamed Business')}**\n*{row.get('Location', '')}, {row.get('City', '')}*")
            c2.markdown(f"**Opp Score: {row.get('opportunity_score', 100)} / 100**\n`Assigned: {assigned_text}`")
            if c3.button("Inspect", key=f"admin_lead_{idx}"):
                st.session_state.selected_lead_name = row.get("Name")
                st.session_state.selected_lead_hash = row.get("Record_Hash")
                st.session_state.view = "lead_profile"
                st.rerun()
            st.divider()

    else:
        my_claimed = df_master[df_master['assigned_to'] == current_user]
        my_due_today = my_claimed[
            (my_claimed['next_followup'] != '') & 
            (my_claimed['next_followup'] <= today_str) & 
            (~my_claimed['crm_status'].str.upper().isin(['WON', 'LOST']))
        ]
        
        m1, m2, m3 = st.columns(3)
        m1.metric("My Claimed Leads", len(my_claimed))
        m2.metric("Follow-ups Due Today", len(my_due_today))
        m3.metric("Pending Calls", len(my_claimed[my_claimed['crm_status'].isin(['', 'NEW', 'CLAIMED'])]))

        if not my_due_today.empty:
            with st.expander(f"Priority Callbacks: {len(my_due_today)} Scheduled for Today", expanded=True):
                for idx, row in my_due_today.iterrows():
                    c1, c2, c3, c4 = st.columns([3, 2, 2, 1])
                    c1.markdown(f"**{row.get('Name', 'Unnamed Business')}**\n*{row.get('Location', '')} ({row.get('City', '')})*")
                    c2.markdown(f"**Opp Score:** `{row.get('opportunity_score', 100)} / 100`\n*Target: {row.get('next_followup')}*")
                    c3.markdown(f"**Status:** `{row.get('crm_status') or 'NEW'}`\n*Notes: {str(row.get('notes', ''))[:40]}...*")
                    if c4.button("Call Now", key=f"due_rep_{idx}", type="primary"):
                        st.session_state.selected_lead_name = row.get("Name")
                        st.session_state.selected_lead_hash = row.get("Record_Hash")
                        st.session_state.view = "lead_profile"
                        st.rerun()
                    st.divider()

        st.write("---")
        col_deck_title, col_shuffle = st.columns([3, 1])
        with col_deck_title:
            st.markdown("#### Active 10-Lead Discovery Deck")
            st.caption("Review these 10 potential opportunities. Claim high-conviction leads to unlock full dialer details.")
        
        with col_shuffle:
            if st.button("Shuffle & Deal 10 New Leads", type="primary", use_container_width=True):
                st.session_state.assigned_batch_keys = generate_fresh_batch(df_master)
                st.rerun()

        batch_df = df_master[df_master['_lead_unique_key'].isin(st.session_state.assigned_batch_keys)]
        
        if batch_df.empty:
            st.info("No unclaimed leads remaining in this campaign tab. Click 'Shuffle' or select another campaign.")
        else:
            for idx, row in batch_df.iterrows():
                is_claimed_by_me = (row.get('assigned_to') == current_user)
                
                with st.container():
                    c1, c2, c3, c4 = st.columns([3, 2, 1.5, 1])
                    c1.markdown(f"**{row.get('Name', 'Unnamed Business')}**\n*{row.get('Location', '')}, {row.get('City', '')}*")
                    c2.markdown(f"**Opp Score:** `{row.get('opportunity_score', 100)} / 100`\n*{row.get('Infra_Classification', 'NO_WEBSITE')}*")
                    
                    if is_claimed_by_me:
                        c3.markdown("`CLAIMED BY YOU`")
                        if c4.button("Open", key=f"open_deck_{idx}"):
                            st.session_state.selected_lead_name = row.get("Name")
                            st.session_state.selected_lead_hash = row.get("Record_Hash")
                            st.session_state.view = "lead_profile"
                            st.rerun()
                    else:
                        if c3.button("Claim Lead", key=f"claim_deck_{idx}", use_container_width=True):
                            sheet_row_num = int(row['_sheet_row_num'])
                            update_lead_in_sheet(spreadsheet, selected_tab_name, sheet_row_num, {
                                'assigned_to': current_user, 
                                'crm_status': 'CLAIMED'
                            })
                            st.session_state.selected_lead_name = row.get("Name")
                            st.session_state.selected_lead_hash = row.get("Record_Hash")
                            st.session_state.view = "lead_profile"
                            st.rerun()
                        
                        if c4.button("Inspect", key=f"inspect_deck_{idx}"):
                            st.session_state.selected_lead_name = row.get("Name")
                            st.session_state.selected_lead_hash = row.get("Record_Hash")
                            st.session_state.view = "lead_profile"
                            st.rerun()
                    st.divider()

# --- VIEW 2: DIRECTORY / MY LEADS DATABASE & SEARCH ENGINE ---
elif st.session_state.view == "database":
    if is_admin:
        st.subheader(f"Master Lead Database — {selected_tab_name}")
        filtered_df = df_master.copy()
    else:
        st.subheader(f"My Claimed Leads Workspace — {selected_tab_name}")
        filtered_df = df_master[df_master['assigned_to'] == current_user].copy()

    with st.expander("Search, Filter, and Sort Records", expanded=True):
        f1, f2, f3 = st.columns(3)
        search_query = f1.text_input("Live Search (Name, Phone, Locality)", value="", key="search_box").strip()
        
        city_options = sorted([str(c) for c in filtered_df['City'].unique() if str(c).strip()])
        city_filter = f2.multiselect("Filter by City", options=city_options)
        
        tier_options = sorted([str(t) for t in filtered_df['lead_tier'].unique() if str(t).strip()])
        tier_filter = f3.multiselect("Filter by Opportunity Tier", options=tier_options)

        f4, f5, f6 = st.columns(3)
        min_opp_score = f4.slider("Minimum Opportunity Score", 0, 100, 0)
        
        sort_by = f5.selectbox(
            "Sort Order",
            ["Highest Opportunity Score", "Lowest Opportunity Score", "Business Name (A-Z)", "City (A-Z)"]
        )
        
        if is_admin:
            status_filter_options = ["All Records", "Unassigned Only", "Claimed Only"]
            assignment_selection = f6.selectbox("Assignment Scope", status_filter_options)
        else:
            assignment_selection = "All Records"

    if search_query:
        s = search_query.lower()
        filtered_df = filtered_df[
            filtered_df['Name'].astype(str).str.lower().str.contains(s, na=False) |
            filtered_df['Location'].astype(str).str.lower().str.contains(s, na=False) |
            filtered_df['City'].astype(str).str.lower().str.contains(s, na=False) |
            filtered_df['Phone_E164'].astype(str).str.contains(s, na=False) |
            filtered_df['Website'].astype(str).str.lower().str.contains(s, na=False)
        ]

    if city_filter:
        filtered_df = filtered_df[filtered_df['City'].astype(str).isin(city_filter)]

    if tier_filter:
        filtered_df = filtered_df[filtered_df['lead_tier'].astype(str).isin(tier_filter)]
    
    filtered_df = filtered_df[filtered_df['opportunity_score'] >= min_opp_score]

    if is_admin:
        if assignment_selection == "Unassigned Only":
            filtered_df = filtered_df[filtered_df['assigned_to'].isin(['', 'Unassigned', None])]
        elif assignment_selection == "Claimed Only":
            filtered_df = filtered_df[~filtered_df['assigned_to'].isin(['', 'Unassigned', None])]

    if sort_by == "Highest Opportunity Score":
        filtered_df = filtered_df.sort_values(by="opportunity_score", ascending=False)
    elif sort_by == "Lowest Opportunity Score":
        filtered_df = filtered_df.sort_values(by="opportunity_score", ascending=True)
    elif sort_by == "Business Name (A-Z)":
        filtered_df = filtered_df.sort_values(by="Name", ascending=True)
    elif sort_by == "City (A-Z)":
        filtered_df = filtered_df.sort_values(by="City", ascending=True)

    head_count_col, export_col = st.columns([3, 1])
    with head_count_col:
        st.markdown(f"**Records Displayed: {len(filtered_df)}**")

    if is_admin and not filtered_df.empty:
        with export_col:
            export_data = filtered_df.drop(columns=['_sheet_row_num', '_lead_unique_key', '_sheet_tab_name'], errors='ignore')
            csv_payload = export_data.to_csv(index=False).encode('utf-8')
            timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M")
            export_filename = f"leads_{selected_tab_name.lower().replace(' ', '_')}_{timestamp_str}.csv"
            st.download_button(
                label="Export CSV Segment",
                data=csv_payload,
                file_name=export_filename,
                mime="text/csv",
                use_container_width=True
            )

    if filtered_df.empty:
        st.info("No matching records found.")
    else:
        display_subset = filtered_df.head(50)
        for idx, row in display_subset.iterrows():
            cols = st.columns([2, 3, 2, 1.5, 1.5, 1])
            assigned_badge = f"Claimed ({row.get('assigned_to')})" if row.get('assigned_to') else "Unassigned"
            cols[0].write(f"`{assigned_badge}`")
            cols[1].markdown(f"**{row.get('Name', 'N/A')}**")
            cols[2].write(f"{row.get('Location', '')} ({row.get('City', '')})")
            cols[3].write(f"Opp: **{row.get('opportunity_score', 100)}**")
            cols[4].write(f"Status: `{row.get('crm_status') or 'NEW'}`")
            if cols[5].button("Open", key=f"db_lead_{idx}"):
                st.session_state.selected_lead_name = row.get("Name")
                st.session_state.selected_lead_hash = row.get("Record_Hash")
                st.session_state.view = "lead_profile"
                st.rerun()

# --- VIEW 3: LEAD PROFILE & DIALER WORKSPACE ---
elif st.session_state.view == "lead_profile":
    target_name = st.session_state.get("selected_lead_name")
    target_hash = st.session_state.get("selected_lead_hash")

    lead_matches = df_master[
        (df_master["Name"] == target_name) & 
        (df_master["Record_Hash"] == target_hash)
    ]

    if lead_matches.empty:
        lead_matches = df_master[df_master["Name"] == target_name]

    if lead_matches.empty:
        st.error(f"Lead '{target_name}' not found.")
        if st.button("Back to Dashboard"):
            st.session_state.view = "dashboard"
            st.rerun()
        st.stop()

    lead = lead_matches.iloc[0].to_dict()
    sheet_row_num = int(lead.get('_sheet_row_num', 2))
    assigned_rep = lead.get('assigned_to', '')
    is_claimed_by_me = (assigned_rep == current_user)
    is_unclaimed = (assigned_rep in ['', 'Unassigned', None])

    brief = engine.generate_sales_brief(lead)

    nav_back_target = "database" if is_claimed_by_me else "dashboard"
    st.button(f"Back to {nav_back_target.capitalize()}", on_click=lambda: setattr(st.session_state, 'view', nav_back_target))

    head_col1, head_col2 = st.columns([3, 1])
    with head_col1:
        st.title(lead.get('Name', 'Unnamed Business'))
        st.caption(f"{lead.get('Location', '')} · {lead.get('City', '')} · {brief.get('network_scope', '')}")
    with head_col2:
        if is_claimed_by_me:
            st.success(f"Claimed by You ({st.session_state.user_display_name})")
        elif is_unclaimed:
            st.warning("Unclaimed Lead")
            if st.button("Claim This Lead Now", type="primary", use_container_width=True):
                update_lead_in_sheet(spreadsheet, selected_tab_name, sheet_row_num, {'assigned_to': current_user, 'crm_status': 'CLAIMED'})
                st.success("Lead claimed successfully.")
                st.rerun()
        else:
            st.info(f"Assigned to: {assigned_rep}")

    st.divider()
    col_left, col_right = st.columns([1, 1])

    with col_left:
        st.markdown("#### Diagnostic Summary")
        tier_title = lead.get("Lead_Tier") or brief.get("category_title", "Standard Profile")
        st.info(f"**Classification:** {tier_title}")

        st.markdown("#### Identified Pain Points & Commercial Leaks")
        pain_triggers = lead.get("Audit_Triggers") or lead.get("Client_Pain_Point")
        if pain_triggers:
            for p in str(pain_triggers).split("|"):
                if p.strip():
                    st.markdown(f"- {p.strip()}")
        else:
            for p in brief.get("pain_points", []):
                st.markdown(f"- {p}")

        st.markdown(f"#### Recommended Package: {lead.get('Primary_Pitch_Strategy') or brief.get('package_name')}")
        for scope_item in brief.get("package_scope", []):
            st.markdown(f"- {scope_item}")

        st.markdown("#### Core Pitch Angle")
        st.write(lead.get("Client_Pain_Point") or brief.get("closer_hook"))

        with st.expander("Objection Handling Battlecards", expanded=False):
            for obj in brief.get("objections", []):
                st.markdown(f"**Prospect:** \"{obj.get('objection')}\"")
                st.markdown(f"**Your Rebuttal:** {obj.get('rebuttal')}")
                st.write("---")

        # --- EMBEDDED HTML AUDIT SCORECARD PREVIEW ---
        html_file_name = str(lead.get("HTML_Scorecard_File", "")).strip()
        record_hash = str(lead.get("Record_Hash", "")).strip()

        html_path = None
        if html_file_name:
            candidate = os.path.join("audit_reports_html", html_file_name)
            if os.path.exists(candidate):
                html_path = candidate

        if not html_path and record_hash:
            candidate_hash = os.path.join("audit_reports_html", f"{record_hash}.html")
            if os.path.exists(candidate_hash):
                html_path = candidate_hash

        if html_path and os.path.exists(html_path):
            st.markdown("#### Generated Audit Scorecard")
            with open(html_path, "r", encoding="utf-8") as f:
                html_content = f.read()
            st.components.v1.html(html_content, height=450, scrolling=True)

    with col_right:
        st.markdown("#### Action Triggers & Dialing")
        phone_num = str(lead.get('Phone_E164', 'N/A')).strip()
        can_contact = is_admin or is_claimed_by_me
        
        if not can_contact:
            masked_phone = phone_num[:4] + "******" + phone_num[-2:] if len(phone_num) > 6 else "Locked"
            st.write(f"**Direct Phone:** `{masked_phone}` *(Claim lead to unlock)*")
        else:
            st.write(f"**Direct Phone:** `{phone_num}`")

        raw_phone = re.sub(r'\D', '', phone_num)
        wa_link = lead.get('WhatsApp_Click_Link', '')
        if not wa_link and raw_phone:
            wa_link = f"https://wa.me/{raw_phone}"

        a1, a2 = st.columns(2)
        if can_contact and raw_phone:
            a1.link_button("Direct Phone Call", f"tel:{raw_phone}", use_container_width=True)
            a2.link_button("Launch WhatsApp Pitch", wa_link, use_container_width=True)
        else:
            a1.button("Direct Phone Call (Claim to Unlock)", disabled=True, use_container_width=True)
            a2.button("WhatsApp (Claim to Unlock)", disabled=True, use_container_width=True)

        st.markdown("#### Ready-to-Send Outreach Script")
        direct_hook = lead.get('WhatsApp_DM_Hook', '').strip()
        if direct_hook and can_contact:
            st.code(direct_hook, language=None)
        elif "text=" in str(wa_link) and can_contact:
            try:
                decoded_pitch = urllib.parse.unquote_plus(str(wa_link).split("text=")[1])
                st.code(decoded_pitch, language=None)
            except Exception:
                pass
        elif can_contact:
            fallback_text, _, _ = engine.get_outreach_pitch(
                lead.get('Name', ''),
                selected_tab_name,
                lead.get('City', '')
            )
            st.code(fallback_text, language=None)

        # --- CALL OUTCOMES ---
        st.markdown("#### Quick Call Outcomes")
        if can_contact:
            q1, q2, q3 = st.columns(3)
            now_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            
            if q1.button("No Answer", use_container_width=True):
                next_date = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                update_lead_in_sheet(spreadsheet, selected_tab_name, sheet_row_num, {
                    'crm_status': 'NO_ANSWER',
                    'notes': f"[{now_ts}] Called - No answer / Busy.",
                    'next_followup': next_date,
                    'last_contacted': now_ts,
                    'assigned_to': current_user
                })
                st.success("Tagged: No Answer")
                st.rerun()

            if q2.button("Gatekeeper Refused", use_container_width=True):
                next_date = (datetime.date.today() + datetime.timedelta(days=3)).strftime("%Y-%m-%d")
                update_lead_in_sheet(spreadsheet, selected_tab_name, sheet_row_num, {
                    'crm_status': 'GATEKEEPER_BLOCKED',
                    'notes': f"[{now_ts}] Reached reception/staff. Owner unavailable.",
                    'next_followup': next_date,
                    'last_contacted': now_ts,
                    'assigned_to': current_user
                })
                st.info("Tagged: Gatekeeper Blocked")
                st.rerun()

            if q3.button("Call Back Later", use_container_width=True):
                next_date = (datetime.date.today() + datetime.timedelta(days=2)).strftime("%Y-%m-%d")
                update_lead_in_sheet(spreadsheet, selected_tab_name, sheet_row_num, {
                    'crm_status': 'FOLLOW-UP',
                    'notes': f"[{now_ts}] Requested callback.",
                    'next_followup': next_date,
                    'last_contacted': now_ts,
                    'assigned_to': current_user
                })
                st.info("Tagged: Call Back Scheduled")
                st.rerun()

            q4, q5 = st.columns(2)
            if q4.button("Meeting / Demo Booked", type="primary", use_container_width=True):
                next_date = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                update_lead_in_sheet(spreadsheet, selected_tab_name, sheet_row_num, {
                    'crm_status': 'INTERESTED',
                    'notes': f"[{now_ts}] Meeting / Demo pitch scheduled.",
                    'next_followup': next_date,
                    'last_contacted': now_ts,
                    'assigned_to': current_user
                })
                st.success("Tagged: Meeting Booked.")
                st.rerun()

            if q5.button("Not Interested / Lost", use_container_width=True):
                update_lead_in_sheet(spreadsheet, selected_tab_name, sheet_row_num, {
                    'crm_status': 'LOST',
                    'notes': f"[{now_ts}] Lead declined offer.",
                    'last_contacted': now_ts,
                    'assigned_to': current_user
                })
                st.warning("Tagged: Not Interested.")
                st.rerun()

# --- VIEW 4: ADMIN ANALYTICS ---
elif st.session_state.view == "analytics" and is_admin:
    st.subheader("Sales Representative Performance & Pipeline Analytics")

    with st.spinner("Aggregating sales activity across all 34 campaign sheets..."):
        all_leads = []
        progress_bar = st.progress(0)
        
        for i, t_name in enumerate(tab_names):
            try:
                sheet_df = fetch_cached_tab_data(sheet_url, t_name)
                if not sheet_df.empty:
                    sheet_df['Campaign_Tab'] = t_name
                    all_leads.append(sheet_df)
            except Exception as tab_err:
                st.warning(f"Tab '{t_name}' could not be loaded: {tab_err}")
            
            progress_bar.progress((i + 1) / len(tab_names))

        progress_bar.empty()

    if not all_leads:
        st.warning("No campaign lead data available to analyze.")
        st.stop()

    df_analytics = pd.concat(all_leads, ignore_index=True)
    claimed_mask = ~df_analytics['assigned_to'].isin(['', 'Unassigned', None])
    df_claimed = df_analytics[claimed_mask].copy()

    top_m1, top_m2, top_m3, top_m4 = st.columns(4)
    top_m1.metric("Total Lead Inventory", len(df_analytics))
    top_m2.metric("Total Claimed Leads", len(df_claimed))
    top_m3.metric("Total Meetings Booked", len(df_claimed[df_claimed['crm_status'].str.upper().isin(['INTERESTED', 'WON', 'PROPOSAL'])]))
    top_m4.metric("Active Follow-ups", len(df_claimed[df_claimed['crm_status'].str.upper() == 'FOLLOW-UP']))

    st.divider()
    st.markdown("#### Rep Performance Scorecard")

    if not df_claimed.empty:
        rep_groups = df_claimed.groupby('assigned_to')
        scorecard_rows = []

        for rep_name, group in rep_groups:
            claimed_count = len(group)
            contacted_count = len(group[group['crm_status'].str.upper() != 'CLAIMED'])
            meetings_booked = len(group[group['crm_status'].str.upper().isin(['INTERESTED', 'WON', 'PROPOSAL'])])
            followups = len(group[group['crm_status'].str.upper() == 'FOLLOW-UP'])
            lost_count = len(group[group['crm_status'].str.upper() == 'LOST'])
            conv_rate = round((meetings_booked / claimed_count) * 100, 1) if claimed_count > 0 else 0.0

            scorecard_rows.append({
                "Sales Rep": rep_name,
                "Claimed Leads": claimed_count,
                "Leads Touched": contacted_count,
                "Meetings Booked": meetings_booked,
                "Follow-ups Pending": followups,
                "Lost / Dropped": lost_count,
                "Conversion Rate (%)": f"{conv_rate}%"
            })

        scorecard_df = pd.DataFrame(scorecard_rows).sort_values(by="Meetings Booked", ascending=False)
        st.dataframe(scorecard_df, use_container_width=True, hide_index=True)