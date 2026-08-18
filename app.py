import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import requests
import urllib.parse
import os
import datetime
import engine

st.set_page_config(page_title="Sales Intelligence CRM", layout="wide")

# Custom Clean Professional Styling
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

# --- GOOGLE SHEETS CONNECTION ---
@st.cache_resource
def get_gspread_client():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_dict = dict(st.secrets["gcp_service_account"])
    if "token_uri" not in creds_dict:
        creds_dict["token_uri"] = "https://oauth2.googleapis.com/token"
        
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scope)
    client = gspread.authorize(credentials)
    return client

@st.cache_data(ttl=30)
def get_all_worksheets():
    client = get_gspread_client()
    sheet_url = st.secrets["spreadsheet"]["sheet_url"]
    spreadsheet = client.open_by_url(sheet_url)
    worksheets = {ws.title.strip(): ws for ws in spreadsheet.worksheets()}
    return worksheets, spreadsheet

def load_tab_data(sheet):
    raw_rows = sheet.get_all_values()
    if not raw_rows or len(raw_rows) < 2:
        return pd.DataFrame()

    header_idx = 0
    known_headers = {'Record_Hash', 'Name', 'Phone_E164', 'Location', 'City', 'Website'}
    for idx, row in enumerate(raw_rows[:15]):
        cleaned_row_cells = {str(cell).strip() for cell in row if str(cell).strip()}
        if known_headers.intersection(cleaned_row_cells):
            header_idx = idx
            break

    headers = [str(h).strip() for h in raw_rows[header_idx]]
    data_rows = raw_rows[header_idx + 1:]
    if not data_rows:
        return pd.DataFrame()

    seen_headers = {}
    clean_headers = []
    for i, h in enumerate(headers):
        if not h:
            h = f"Column_{i+1}"
        if h in seen_headers:
            seen_headers[h] += 1
            clean_headers.append(f"{h}_{seen_headers[h]}")
        else:
            seen_headers[h] = 0
            clean_headers.append(h)

    row_payloads = []
    for offset, r in enumerate(data_rows):
        if any(str(cell).strip() for cell in r):
            actual_sheet_row = header_idx + 2 + offset
            row_dict = {clean_headers[i]: r[i] if i < len(r) else "" for i in range(len(clean_headers))}
            row_dict["_sheet_row_num"] = actual_sheet_row
            row_payloads.append(row_dict)

    if not row_payloads:
        return pd.DataFrame()

    df = pd.DataFrame(row_payloads)

    for col in ['crm_status', 'notes', 'next_followup', 'last_contacted', 'assigned_to']:
        if col not in df.columns:
            df[col] = ""

    if 'Record_Hash' not in df.columns or df['Record_Hash'].eq('').all():
        df['Record_Hash'] = [f"HASH_{i+1}" for i in range(len(df))]

    return df.reset_index(drop=True)

def col_idx_to_a1(idx: int) -> str:
    letters = ""
    while idx > 0:
        idx, remainder = divmod(idx - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters

def update_lead_in_sheet(sheet, exact_sheet_row: int, update_dict: dict):
    headers = [str(h).strip() for h in sheet.row_values(1)]
    header_modified = False

    for key in update_dict.keys():
        if key not in headers:
            headers.append(key)
            header_modified = True

    if header_modified:
        range_header = f"A1:{col_idx_to_a1(len(headers))}1"
        sheet.update(range_name=range_header, values=[headers])

    current_row_values = sheet.row_values(exact_sheet_row)

    if len(current_row_values) < len(headers):
        current_row_values.extend([""] * (len(headers) - len(current_row_values)))

    for key, val in update_dict.items():
        col_pos = headers.index(key)
        current_row_values[col_pos] = str(val)

    range_update = f"A{exact_sheet_row}:{col_idx_to_a1(len(headers))}{exact_sheet_row}"
    sheet.update(range_name=range_update, values=[current_row_values])

# Initialize UI session states
if "view" not in st.session_state:
    st.session_state.view = "dashboard"
if "selected_lead_id" not in st.session_state:
    st.session_state.selected_lead_id = None
if "assigned_batch_hashes" not in st.session_state:
    st.session_state.assigned_batch_hashes = []

try:
    worksheets_dict, spreadsheet = get_all_worksheets()
except Exception as e:
    st.error(f"Failed to connect to Google Sheets: {e}")
    st.stop()

# --- SIDEBAR (USER PROFILE & LOGOUT) ---
st.sidebar.markdown(f"**User:** {st.session_state.user_display_name}")
st.sidebar.caption(f"Role: **{st.session_state.user_role.upper()}**")

if st.sidebar.button("Log Out", use_container_width=True):
    st.session_state.authenticated = False
    st.session_state.username = None
    st.session_state.assigned_batch_hashes = []
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

def reset_lead_selection():
    st.session_state.selected_lead_id = None
    st.session_state.view = "dashboard"
    st.session_state.assigned_batch_hashes = []

with col_tab_sel:
    tab_names = list(worksheets_dict.keys())
    selected_tab_name = st.selectbox(
        "Active Campaign Tab", 
        tab_names, 
        key="active_keyword_tab",
        on_change=reset_lead_selection
    )
    active_sheet = worksheets_dict[selected_tab_name]

with col_nav2:
    if st.button("Dashboard", use_container_width=True):
        st.session_state.view = "dashboard"
        st.session_state.selected_lead_id = None
        st.rerun()

with col_nav3:
    nav_label = "Lead Master DB" if is_admin else "My Claimed Leads"
    if st.button(nav_label, use_container_width=True):
        st.session_state.view = "database"
        st.session_state.selected_lead_id = None
        st.rerun()

if is_admin:
    with col_nav4:
        if st.button("Rep Analytics", use_container_width=True):
            st.session_state.view = "analytics"
            st.session_state.selected_lead_id = None
            st.rerun()

st.divider()

# Load Data for Active Tab
try:
    df_raw = load_tab_data(active_sheet)
except Exception as e:
    st.error(f"Could not load tab data: {e}")
    st.stop()

if df_raw.empty and st.session_state.view != "analytics":
    st.warning(f"No records found in campaign tab '{selected_tab_name}'.")
    st.stop()

# Scoring & Preparation with positional index reset
if not df_raw.empty:
    scores_list = [engine.calculate_scores(row.to_dict()) for _, row in df_raw.iterrows()]
    scores_df = pd.DataFrame(scores_list, index=df_raw.index)
    
    cols_to_drop = [c for c in ['opportunity_score', 'lead_tier', 'platinum_raw', 'priority_level', 'infra_deficiency', 'tracking_deficiency'] if c in df_raw.columns]
    df_clean = df_raw.drop(columns=cols_to_drop).reset_index(drop=True)
    scores_df = scores_df.reset_index(drop=True)
    df_master = pd.concat([df_clean, scores_df], axis=1)
else:
    df_master = pd.DataFrame()

def generate_fresh_batch(df_pool):
    unclaimed = df_pool[df_pool['assigned_to'].isin(['', 'Unassigned', None])]
    if unclaimed.empty:
        return []
    sample_size = min(10, len(unclaimed))
    return unclaimed.sample(n=sample_size)['Record_Hash'].tolist()

if not is_admin:
    if not st.session_state.assigned_batch_hashes and not df_master.empty:
        st.session_state.assigned_batch_hashes = generate_fresh_batch(df_master)

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
                    c3.markdown(f"**Status:** `{row.get('crm_status')}`\n*Notes: {str(row.get('notes', ''))[:45]}...*")
                    if c4.button("Open", key=f"due_admin_{idx}"):
                        st.session_state.selected_lead_id = row.get('Record_Hash')
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
                st.session_state.selected_lead_id = row.get('Record_Hash')
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
                    c3.markdown(f"**Status:** `{row.get('crm_status')}`\n*Notes: {str(row.get('notes', ''))[:40]}...*")
                    if c4.button("Call Now", key=f"due_rep_{idx}", type="primary"):
                        st.session_state.selected_lead_id = row.get('Record_Hash')
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
                st.session_state.assigned_batch_hashes = generate_fresh_batch(df_master)
                st.rerun()

        batch_df = df_master[df_master['Record_Hash'].isin(st.session_state.assigned_batch_hashes)]
        
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
                            st.session_state.selected_lead_id = row.get('Record_Hash')
                            st.session_state.view = "lead_profile"
                            st.rerun()
                    else:
                        if c3.button("Claim Lead", key=f"claim_deck_{idx}", use_container_width=True):
                            sheet_row_num = int(row['_sheet_row_num'])
                            update_lead_in_sheet(active_sheet, sheet_row_num, {
                                'assigned_to': current_user, 
                                'crm_status': 'CLAIMED'
                            })
                            st.session_state.selected_lead_id = row.get('Record_Hash')
                            st.session_state.view = "lead_profile"
                            st.rerun()
                        
                        if c4.button("Inspect", key=f"inspect_deck_{idx}"):
                            st.session_state.selected_lead_id = row.get('Record_Hash')
                            st.session_state.view = "lead_profile"
                            st.rerun()
                    st.divider()

# --- VIEW 2: DIRECTORY / MY LEADS DATABASE & CSV EXPORT ---
elif st.session_state.view == "database":
    if is_admin:
        st.subheader(f"Master Lead Database — {selected_tab_name}")
        filtered_df = df_master.copy()
    else:
        st.subheader(f"My Claimed Leads Workspace — {selected_tab_name}")
        filtered_df = df_master[df_master['assigned_to'] == current_user].copy()

    with st.expander("Filter and Segment Records", expanded=True):
        f1, f2, f3 = st.columns(3)
        search_term = f1.text_input("Search Business, Locality, or Phone")
        city_options = sorted([str(c) for c in filtered_df['City'].unique() if c])
        city_filter = f2.multiselect("City Filter", options=city_options)
        
        tier_options = sorted([str(t) for t in filtered_df['lead_tier'].unique() if t])
        tier_filter = f3.multiselect("Opportunity Tier", options=tier_options)

        f4, f5 = st.columns(2)
        min_opp_score = f4.slider("Minimum Opportunity Score", 0, 100, 0)
        
        if is_admin:
            status_filter_options = ["All Records", "Unassigned Only", "Claimed Only"]
            assignment_selection = f5.selectbox("Assignment Scope", status_filter_options)
        else:
            assignment_selection = "All Records"

    if search_term:
        filtered_df = filtered_df[
            filtered_df['Name'].astype(str).str.contains(search_term, case=False, na=False) |
            filtered_df['Location'].astype(str).str.contains(search_term, case=False, na=False) |
            filtered_df['City'].astype(str).str.contains(search_term, case=False, na=False) |
            filtered_df['Phone_E164'].astype(str).str.contains(search_term, na=False)
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

    head_count_col, export_col = st.columns([3, 1])
    with head_count_col:
        st.markdown(f"**Records Displayed: {len(filtered_df)}**")

    if is_admin and not filtered_df.empty:
        with export_col:
            export_data = filtered_df.drop(columns=['_sheet_row_num'], errors='ignore')
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
        if not is_admin:
            st.info("You have not claimed any leads in this tab yet. Go to the Dashboard to review and claim leads from your 10-lead deck.")
        else:
            st.info("No matching records found.")
    else:
        for idx, row in filtered_df.iterrows():
            cols = st.columns([2, 3, 2, 1.5, 1.5, 1])
            assigned_badge = f"Claimed ({row.get('assigned_to')})" if row.get('assigned_to') else "Unassigned"
            cols[0].write(f"`{assigned_badge}`")
            cols[1].markdown(f"**{row.get('Name', 'N/A')}**")
            cols[2].write(f"{row.get('Location', '')} ({row.get('City', '')})")
            cols[3].write(f"Opp: **{row.get('opportunity_score', 100)}**")
            cols[4].write(f"Status: `{row.get('crm_status') or 'NEW'}`")
            if cols[5].button("Open", key=f"db_lead_{idx}"):
                st.session_state.selected_lead_id = row.get('Record_Hash')
                st.session_state.view = "lead_profile"
                st.rerun()

# --- VIEW 3: LEAD PROFILE & DIALER WORKSPACE ---
elif st.session_state.view == "lead_profile":
    lead_id = st.session_state.selected_lead_id
    lead_matches = df_master[df_master['Record_Hash'].astype(str) == str(lead_id)]
    
    if lead_matches.empty:
        st.warning("Lead profile not found.")
        if st.button("Return to Dashboard"):
            st.session_state.selected_lead_id = None
            st.session_state.view = "dashboard"
            st.rerun()
        st.stop()

    lead_row_idx = lead_matches.index[0]
    lead = df_master.iloc[lead_row_idx].to_dict()
    sheet_row_num = int(lead['_sheet_row_num'])
    assigned_rep = lead.get('assigned_to', '')
    is_claimed_by_me = (assigned_rep == current_user)
    is_unclaimed = (assigned_rep in ['', 'Unassigned', None])

    brief = engine.generate_sales_brief(lead)

    nav_back_target = "database" if is_claimed_by_me else "dashboard"
    st.button(f"Back to {nav_back_target.capitalize()}", on_click=lambda: setattr(st.session_state, 'view', nav_back_target))

    network_text = brief.get('network_scope') or f"{lead.get('Network_Footprint', 'Single Location')} ({lead.get('Entity_Resolution_Type', 'Independent')})"

    head_col1, head_col2 = st.columns([3, 1])
    with head_col1:
        st.title(lead.get('Name', 'Unnamed Business'))
        st.caption(f"{lead.get('Location', '')} · {lead.get('City', '')} · {network_text}")
    with head_col2:
        if is_claimed_by_me:
            st.success(f"Claimed by You ({st.session_state.user_display_name})")
        elif is_unclaimed:
            st.warning("Unclaimed Lead")
            if st.button("Claim This Lead Now", type="primary", use_container_width=True):
                update_lead_in_sheet(active_sheet, sheet_row_num, {'assigned_to': current_user, 'crm_status': 'CLAIMED'})
                st.success("Lead claimed successfully.")
                st.rerun()
        else:
            st.info(f"Assigned to: {assigned_rep}")

    st.divider()
    col_left, col_right = st.columns([1, 1])

    with col_left:
        st.markdown("#### Diagnostic Summary")
        category_title = brief.get('category_title', 'Standard Digital Profile')
        st.info(f"**Classification:** {category_title}")

        st.markdown("#### Identified Pain Points & Commercial Leaks")
        pain_points = brief.get('pain_points', [])
        if pain_points:
            for p in pain_points:
                st.markdown(f"- {p}")
        else:
            for w in brief.get('why_points', ['Digital deficiency identified across technical parameters.']):
                st.markdown(f"- {w}")

        pkg_name = brief.get('package_name') or brief.get('service_recommendation', 'Turnkey Web Optimization')
        st.markdown(f"#### Recommended Package: {pkg_name}")
        
        pkg_scope = brief.get('package_scope', [])
        if pkg_scope:
            for scope_item in pkg_scope:
                st.markdown(f"- {scope_item}")
        else:
            st.write(pkg_name)

        closer_hook = brief.get('closer_hook') or brief.get('sales_angle', '')
        if closer_hook:
            st.markdown("#### Core Pitch Angle")
            st.write(closer_hook)

        objections = brief.get('objections', [])
        if objections:
            with st.expander("Objection Handling Battlecards", expanded=False):
                for obj in objections:
                    st.markdown(f"**Prospect:** \"{obj.get('objection')}\"")
                    st.markdown(f"**Your Rebuttal:** {obj.get('rebuttal')}")
                    st.write("---")

        # HTML Scorecard Preview
        record_hash = str(lead.get('Record_Hash', '')).strip()
        html_report_path = os.path.join("audit_reports_html", f"{record_hash}.html")
        if os.path.exists(html_report_path):
            st.markdown("#### Generated Audit Scorecard")
            with open(html_report_path, "r", encoding="utf-8") as f:
                html_content = f.read()
            st.components.v1.html(html_content, height=400, scrolling=True)

    with col_right:
        st.markdown("#### Action Triggers & Dialing")
        
        phone_num = str(lead.get('Phone_E164', 'N/A'))
        can_contact = is_admin or is_claimed_by_me
        
        if not is_admin and not is_claimed_by_me:
            masked_phone = phone_num[:4] + "******" + phone_num[-2:] if len(phone_num) > 6 else "Locked"
            st.write(f"**Direct Phone:** `{masked_phone}` *(Claim lead to unlock)*")
        else:
            st.write(f"**Direct Phone:** `{phone_num}`")

        raw_phone = str(lead.get('Phone_E164', '')).replace("+", "").replace(" ", "").replace("-", "")
        wa_link = lead.get('WhatsApp_Click_Link', '')

        a1, a2 = st.columns(2)
        if can_contact and raw_phone:
            a1.link_button("Direct Phone Call", f"tel:{raw_phone}", use_container_width=True)
            a2.link_button("Launch WhatsApp Pitch", wa_link, use_container_width=True)
        else:
            a1.button("Direct Phone Call (Claim to Unlock)", disabled=True, use_container_width=True)
            a2.button("WhatsApp (Claim to Unlock)", disabled=True, use_container_width=True)

        st.markdown("#### Ready-to-Send Outreach Script")
        pitch_displayed = False
        if "text=" in str(wa_link) and can_contact:
            try:
                decoded_pitch = urllib.parse.unquote_plus(str(wa_link).split("text=")[1])
                st.code(decoded_pitch, language=None)
                pitch_displayed = True
            except Exception:
                pass
        
        if not pitch_displayed and can_contact:
            scenario_key = brief.get('scenario') or brief.get('pitch_type', 'GENERAL')
            fallback_text, _, _ = engine.get_outreach_pitch(
                scenario_key,
                lead.get('Name', ''),
                selected_tab_name,
                lead.get('City', '')
            )
            st.code(fallback_text, language=None)

        # --- CALL OUTCOME QUICK-TAGS ---
        st.markdown("#### Quick Call Outcomes")
        if can_contact:
            q1, q2, q3 = st.columns(3)
            now_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            
            if q1.button("No Answer", use_container_width=True):
                next_date = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                update_lead_in_sheet(active_sheet, sheet_row_num, {
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
                update_lead_in_sheet(active_sheet, sheet_row_num, {
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
                update_lead_in_sheet(active_sheet, sheet_row_num, {
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
                update_lead_in_sheet(active_sheet, sheet_row_num, {
                    'crm_status': 'INTERESTED',
                    'notes': f"[{now_ts}] Meeting / Demo pitch scheduled.",
                    'next_followup': next_date,
                    'last_contacted': now_ts,
                    'assigned_to': current_user
                })
                st.success("Tagged: Meeting Booked.")
                st.rerun()

            if q5.button("Not Interested / Lost", use_container_width=True):
                update_lead_in_sheet(active_sheet, sheet_row_num, {
                    'crm_status': 'LOST',
                    'notes': f"[{now_ts}] Lead declined offer.",
                    'last_contacted': now_ts,
                    'assigned_to': current_user
                })
                st.warning("Tagged: Not Interested.")
                st.rerun()
        else:
            st.caption("Claim this lead to enable 1-click call logging.")

        st.markdown("#### Manual Activity Entry")
        with st.form("crm_logging_form"):
            current_status = str(lead.get('crm_status', 'NEW')).upper()
            status_options = ['NEW', 'CLAIMED', 'CONTACTED', 'FOLLOW-UP', 'INTERESTED', 'PROPOSAL', 'WON', 'LOST']
            default_index = status_options.index(current_status) if current_status in status_options else 0
            
            new_status = st.selectbox("Status", status_options, index=default_index)
            next_date = st.date_input("Follow-up Date")
            notes = st.text_area("Detailed Notes", value=str(lead.get('notes', '')))
            
            if st.form_submit_button("Save Detailed Activity"):
                update_dict = {
                    'crm_status': new_status,
                    'notes': notes,
                    'next_followup': str(next_date),
                    'last_contacted': datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    'assigned_to': current_user if is_unclaimed else assigned_rep
                }
                update_lead_in_sheet(active_sheet, sheet_row_num, update_dict)
                st.success("Activity saved.")
                st.rerun()

# --- VIEW 4: ADMIN REP PERFORMANCE ANALYTICS ---
elif st.session_state.view == "analytics" and is_admin:
    st.subheader("Sales Representative Performance & Pipeline Analytics")
    st.caption("Multi-campaign aggregated metrics across all team members.")

    with st.spinner("Aggregating sales activity across all campaign sheets..."):
        all_leads = []
        for tab_name, sheet_obj in worksheets_dict.items():
            try:
                sheet_df = load_tab_data(sheet_obj)
                if not sheet_df.empty:
                    sheet_df['Campaign_Tab'] = tab_name
                    all_leads.append(sheet_df)
            except Exception:
                continue

    if not all_leads:
        st.warning("No campaign lead data available to analyze.")
        st.stop()

    df_analytics = pd.concat(all_leads, ignore_index=True)

    claimed_mask = ~df_analytics['assigned_to'].isin(['', 'Unassigned', None])
    df_claimed = df_analytics[claimed_mask].copy()

    total_pool = len(df_analytics)
    total_claimed = len(df_claimed)
    total_meetings = len(df_claimed[df_claimed['crm_status'].str.upper().isin(['INTERESTED', 'WON', 'PROPOSAL'])])
    total_followups = len(df_claimed[df_claimed['crm_status'].str.upper() == 'FOLLOW-UP'])

    top_m1, top_m2, top_m3, top_m4 = st.columns(4)
    top_m1.metric("Total Lead Inventory", total_pool)
    top_m2.metric("Total Claimed Leads", total_claimed)
    top_m3.metric("Total Meetings Booked", total_meetings)
    top_m4.metric("Active Follow-ups", total_followups)

    st.divider()

    st.markdown("#### Rep Performance Scorecard")

    if df_claimed.empty:
        st.info("No leads have been claimed by sales representatives yet.")
    else:
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

    st.divider()

    # Activity Timeline Feed
    st.markdown("#### Recent Team Activity Log")
    df_touched = df_claimed[df_claimed['last_contacted'] != ""].sort_values(by="last_contacted", ascending=False).head(15)

    if df_touched.empty:
        st.caption("No recent outreach activity logged.")
    else:
        for _, row in df_touched.iterrows():
            st.markdown(f"**[{row.get('last_contacted')}]** `{row.get('assigned_to')}` updated **{row.get('Name')}** ({row.get('Campaign_Tab')}) -> Status: `{row.get('crm_status')}`")
            if row.get('notes'):
                st.caption(f"Notes: {row.get('notes')}")
            st.write("---")