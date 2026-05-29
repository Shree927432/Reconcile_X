import streamlit as st
import pandas as pd
import recordlinkage 
import re
import io

# --- 1. WEB UI SETUP ---
st.set_page_config(page_title="GST Reconciliation Engine", layout="wide")
st.title("⚡ ReconcileX")
st.markdown("Upload your Book and Portal data to automatically match invoices using weighted AI scoring.")

# --- 2. UPLOAD ZONE ---
col1, col2 = st.columns(2)
with col1:
    book_file = st.file_uploader("Upload Book Excel (Tally)", type=["xlsx", "xls"])
with col2:
    portal_file = st.file_uploader("Upload Portal Excel (GSTR-2B)", type=["xlsx", "xls"])

# --- 3. BULLETPROOF CLEANING FUNCTIONS ---
# We no longer return empty strings (""). We return "UNKNOWN" to prevent Cython backend crashes.
def clean_invoice(number):
    if pd.isna(number): return "UNKNOWN"
    text = str(number).upper().strip()
    if not text or text == "NAN": return "UNKNOWN"
    items = r"2025-26|25-26|24-25|23-24|/|-|\.|\b0"
    clean = re.sub(items, "", text)
    clean = re.sub(r"[A-Z]", "", clean)
    clean = clean.lstrip("0")
    return clean if clean else "UNKNOWN"

def clean_client_name(name):
    if pd.isna(name): return "UNKNOWN"
    text = str(name).strip().upper()
    if not text or text == "NAN": return "UNKNOWN"
    text = re.sub(r"[.-]", " ", text)
    suffix_list = r"\b(LLP|ENTERPRISES|ENTERPRISE|ENTERPRIES|PVT|LTD|NEW|CO|GUJARAT|PRIVATE|LIMITED|AGENCIES|CLOTHING|TRADERS|M\/S|INC|FASHIONS|FASHION)\b"
    cleaned = re.sub(suffix_list, "", text).lower().replace(" ", "")
    return cleaned if cleaned else "UNKNOWN"

def clean_and_round_number(value):
    if pd.isna(value): return 0.0
    clean_text = str(value).strip().replace(',', '')
    if not clean_text or clean_text.upper() == "NAN": return 0.0
    try:
        return round(float(clean_text), 2)
    except ValueError:
        return 0.0

def clean_gstin(gst):
    if pd.isna(gst): return "UNKNOWN"
    text = str(gst).strip().upper()
    return text if text and text != "NAN" else "UNKNOWN"

# --- 4. EXECUTION ENGINE ---
if book_file and portal_file:
    if st.button("Run Reconciliation Engine", type="primary", use_container_width=True):
        with st.spinner("Cleaning data and running RecordLinkage Engine..."):
            try:
                # 4.1 Load Data & ARMOR THE INDICES
                book_df = pd.read_excel(book_file, skiprows=6)
                # Ensure we don't slice an empty dataframe
                if len(book_df) > 0:
                    book_df = book_df.iloc[:-1] 
                book_df = book_df.reset_index(drop=True) 
                
                # Flexible Sheet Reader for Portal Data
                portal_xls = pd.ExcelFile(portal_file)
                if "B2B" in portal_xls.sheet_names:
                    portal_df = pd.read_excel(portal_xls, sheet_name="B2B", skiprows=5)
                else:
                    fallback_sheet = portal_xls.sheet_names[0]
                    portal_df = pd.read_excel(portal_xls, sheet_name=fallback_sheet, skiprows=5)
                    st.warning(f"⚠️ Note: Could not find a sheet named 'B2B'. We used '{fallback_sheet}' instead.")
                
                portal_df = portal_df.rename(columns={'Unnamed: 0':'Supplier GSTIN', 'Unnamed: 1':'Trade/Legal Name'})
                portal_df = portal_df.reset_index(drop=True) 
                
                # Check if essential columns exist before proceeding
                book_cols_needed = ['Particulars', 'Supplier Invoice No.', 'GSTIN/UIN', 'Gross Total']
                portal_cols_needed = ['Supplier GSTIN', 'Trade/Legal Name', 'Invoice number', 'Invoice Value(₹)']
                
                missing_book = [c for c in book_cols_needed if c not in book_df.columns]
                missing_portal = [c for c in portal_cols_needed if c not in portal_df.columns]
                
                if missing_book:
                    st.error(f"Missing columns in Book file: {missing_book}")
                    st.stop()
                if missing_portal:
                    st.error(f"Missing columns in Portal file: {missing_portal}")
                    st.stop()
                
                # Select Relevant Columns
                book_df = book_df[book_cols_needed].copy()
                portal_df = portal_df[portal_cols_needed].copy()
                
                # 4.2 Apply Cleaning Functions
                book_df["Invoice_Clean"] = book_df['Supplier Invoice No.'].apply(clean_invoice)
                portal_df["Invoice_Clean"] = portal_df['Invoice number'].apply(clean_invoice)
                
                book_df["Name_Clean"] = book_df['Particulars'].apply(clean_client_name)
                portal_df["Name_Clean"] = portal_df['Trade/Legal Name'].apply(clean_client_name)
                
                book_df["Total_Clean"] = book_df['Gross Total'].apply(clean_and_round_number)
                portal_df["Total_Clean"] = portal_df['Invoice Value(₹)'].apply(clean_and_round_number)
                
                book_df["GST_Clean"] = book_df['GSTIN/UIN'].apply(clean_gstin)
                portal_df["GST_Clean"] = portal_df['Supplier GSTIN'].apply(clean_gstin)
                
                # 4.3 Create Blocking Keys
                book_df["Name_Initial"] = book_df['Name_Clean'].str[0]
                portal_df["Name_Initial"] = portal_df['Name_Clean'].str[0]
                
                # 4.4 Indexing (Blocking)
                indexer = recordlinkage.Index()
                indexer.block("Name_Initial")
                candidate_links = indexer.index(book_df, portal_df)
                
                if len(candidate_links) == 0:
                    st.error("No potential matches found during the blocking phase. Check your files.")
                    st.stop()
                
                # 4.5 Compare Engine (With missing_value safety nets)
                compare_cl = recordlinkage.Compare()
                compare_cl.string("Name_Clean", "Name_Clean", method="jarowinkler", threshold=0.75, missing_value=0.0, label="Name_Match")
                compare_cl.exact("Invoice_Clean", "Invoice_Clean", missing_value=0, label="Invoice_Match")
                compare_cl.numeric("Total_Clean", "Total_Clean", method="step", offset=1, missing_value=0.0, label="Amount_Match")
                compare_cl.exact("GST_Clean", "GST_Clean", missing_value=0, label="GSTIN_Match")
                
                features = compare_cl.compute(candidate_links, book_df, portal_df)
                
                # 4.6 Weighted Cascading System
                features['Total_Score'] = (
                    features['Name_Match'] * 1.0 +
                    features['Invoice_Match'] * 3.0 +  
                    features['Amount_Match'] * 2.0 +   
                    features['GSTIN_Match'] * 1.0      
                )
                
                # Filter Matches
                perfect_matches = features[features['Total_Score'] == 7.0]
                book_perfect_indices = perfect_matches.index.get_level_values(0)
                portal_perfect_indices = perfect_matches.index.get_level_values(1)
                
                remaining_for_strong = features[
                    ~features.index.get_level_values(0).isin(book_perfect_indices) & 
                    ~features.index.get_level_values(1).isin(portal_perfect_indices)
                ]
                strong_matches = remaining_for_strong[(remaining_for_strong['Total_Score'] >= 5.0) & (remaining_for_strong['Total_Score'] <= 6.0)]
                book_strong_indices = strong_matches.index.get_level_values(0)
                portal_strong_indices = strong_matches.index.get_level_values(1)
                
                all_used_book_ids = book_perfect_indices.append(book_strong_indices)
                all_used_portal_ids = portal_perfect_indices.append(portal_strong_indices)
                
                remaining_for_probable = features[
                    ~features.index.get_level_values(0).isin(all_used_book_ids) & 
                    ~features.index.get_level_values(1).isin(all_used_portal_ids)
                ]
                probable_matches = remaining_for_probable[(remaining_for_probable['Total_Score'] >= 3.0) & (remaining_for_probable['Total_Score'] <= 4.0)]
                book_probable_indices = probable_matches.index.get_level_values(0)
                portal_probable_indices = probable_matches.index.get_level_values(1)
                
                # Retrieve Subsets
                perfect_reconciliation  = pd.concat([
                    book_df.loc[book_perfect_indices, book_cols_needed].reset_index(drop=True),
                    portal_df.loc[portal_perfect_indices, portal_cols_needed].reset_index(drop=True)
                ], axis=1)
                
                strong_reconciliation   = pd.concat([
                    book_df.loc[book_strong_indices, book_cols_needed].reset_index(drop=True),
                    portal_df.loc[portal_strong_indices, portal_cols_needed].reset_index(drop=True)
                ], axis=1)
                
                probable_reconciliation = pd.concat([
                    book_df.loc[book_probable_indices, book_cols_needed].reset_index(drop=True),
                    portal_df.loc[portal_probable_indices, portal_cols_needed].reset_index(drop=True)
                ], axis=1)
                
                # Unmatched
                all_matched_book_idx = pd.Index(book_perfect_indices.append(book_strong_indices).append(book_probable_indices)).unique()
                all_matched_portal_idx = pd.Index(portal_perfect_indices.append(portal_strong_indices).append(portal_probable_indices)).unique()
                
                unmatched_book = book_df.drop(index=all_matched_book_idx, errors='ignore')[book_cols_needed]
                unmatched_portal = portal_df.drop(index=all_matched_portal_idx, errors='ignore')[portal_cols_needed]
                
                st.success(f"✅ Processing Complete! Found {len(perfect_reconciliation)} perfect matches, {len(strong_reconciliation)} strong matches, and {len(probable_reconciliation)} probable matches.")
                
                # 4.7 EXPORT TO MEMORY
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                    perfect_reconciliation.to_excel(writer, sheet_name="Perfect_Matches", index=False)
                    strong_reconciliation.to_excel(writer, sheet_name="Strong_Matches", index=False)
                    probable_reconciliation.to_excel(writer, sheet_name="Probable_Matches", index=False)
                    unmatched_book.to_excel(writer, sheet_name="Unmatched_Books", index=False)
                    unmatched_portal.to_excel(writer, sheet_name="Unmatched_Portal", index=False)
                
                excel_data = output.getvalue()
                
                st.download_button(
                    label="📥 Download Final Reconciliation Report",
                    data=excel_data,
                    file_name="AI_GST_Reconciliation_Report.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    type="primary"
                )
                
            except Exception as e:
                st.error(f"An unexpected error occurred during processing: {str(e)}")
                st.info("If this keeps happening, ensure your Excel files match the standard Tally and Portal export formats.")
