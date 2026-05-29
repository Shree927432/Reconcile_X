import streamlit as st
import pandas as pd
import recordlinkage 
import re
import io

# --- 1. WEB UI SETUP ---
st.set_page_config(page_title="ReconcileX", layout="wide")
st.title("⚡ReconcileX")
st.markdown("Upload your Book and Portal data to automatically match invoices using weighted AI scoring.")

# --- 2. UPLOAD ZONE ---
col1, col2 = st.columns(2)
with col1:
    book_file = st.file_uploader("Upload Book Excel (Tally)", type=["xlsx", "xls"])
with col2:
    portal_file = st.file_uploader("Upload Portal Excel (GSTR-2B)", type=["xlsx", "xls"])

# --- 3. CLEANING FUNCTIONS ---
def clean_invoice(number):
    if pd.isna(number):
        return ""
    text = str(number).upper()
    items = r"2025-26|25-26|24-25|23-24|/|-|\.|\b0"
    clean = re.sub(items, "", text)
    clean = re.sub(r"[A-Z]", "", clean)
    return clean.lstrip("0")

def clean_client_name(name):
    if pd.isna(name): return ""
    text = str(name).strip().upper()
    text = re.sub(r"[.-]", " ", text)
    suffix_list = r"\b(LLP|ENTERPRISES|ENTERPRISE|ENTERPRIES|PVT|LTD|NEW|CO|GUJARAT|PRIVATE|LIMITED|AGENCIES|CLOTHING|TRADERS|M\/S|INC|FASHIONS|FASHION)\b"
    return re.sub(suffix_list, "", text).lower().replace(" ", "")

def clean_and_round_number(value):
    if pd.isna(value): return 0.0
    clean_text = str(value).strip().replace(',', '')
    try:
        return round(float(clean_text), 2)
    except ValueError:
        return 0.0

def clean_gstin(gst):
    if pd.isna(gst): return ""
    return str(gst).strip().upper()

# --- 4. EXECUTION ENGINE ---
if book_file and portal_file:
    if st.button("Run Reconciliation Engine", type="primary", use_container_width=True):
     with st.spinner("Cleaning data and running RecordLinkage Engine..."):
            
           # --- 4.1 Load Data & ARMOR THE INDICES ---
            book_df = pd.read_excel(book_file, skiprows=6)
            book_df = book_df.iloc[:-1] 
            book_df = book_df.reset_index(drop=True) 
            
            # --- THE FIX: Flexible Sheet Reader ---
            portal_xls = pd.ExcelFile(portal_file)
            if "B2B" in portal_xls.sheet_names:
                portal_df = pd.read_excel(portal_xls, sheet_name="B2B", skiprows=5)
            else:
                # If "B2B" is missing, just grab the first sheet available
                fallback_sheet = portal_xls.sheet_names[0]
                portal_df = pd.read_excel(portal_xls, sheet_name=fallback_sheet, skiprows=5)
                st.warning(f"⚠️ Note: Could not find a sheet named 'B2B'. We used '{fallback_sheet}' instead.")
            
            portal_df = portal_df.rename(columns={'Unnamed: 0':'Supplier GSTIN', 'Unnamed: 1':'Trade/Legal Name'})
            portal_df = portal_df.reset_index(drop=True)
         
            # Select Relevant Columns
            book_df = book_df[['Particulars', 'Supplier Invoice No.', 'GSTIN/UIN', 'Gross Total']] 
            portal_df = portal_df[['Supplier GSTIN', 'Trade/Legal Name', 'Invoice number', 'Invoice Value(₹)']]
            
            # --- 4.2 Apply Cleaning & BULLETPROOF STRINGS ---
            book_df["Invoice_Clean"] = book_df['Supplier Invoice No.'].apply(clean_invoice).astype(str)
            portal_df["Invoice_Clean"] = portal_df['Invoice number'].apply(clean_invoice).astype(str)
            
            book_df["Name_Clean"] = book_df['Particulars'].apply(clean_client_name).astype(str)
            portal_df["Name_Clean"] = portal_df['Trade/Legal Name'].apply(clean_client_name).astype(str)
            
            book_df["Total_Clean"] = book_df['Gross Total'].apply(clean_and_round_number)
            portal_df["Total_Clean"] = portal_df['Invoice Value(₹)'].apply(clean_and_round_number)
            
            book_df["GST_Clean"] = book_df['GSTIN/UIN'].apply(clean_gstin).astype(str)
            portal_df["GST_Clean"] = portal_df['Supplier GSTIN'].apply(clean_gstin).astype(str)
            
            # --- 4.3 Create Blocking Keys (THE FIX) ---
            # This extracts the first letter of the clean name to act as the "Room" 
            book_df["Name_Initial"] = book_df['Name_Clean'].str[0].fillna('')
            portal_df["Name_Initial"] = portal_df['Name_Clean'].str[0].fillna('')
            
            # --- 4.4 Indexing (Blocking) ---
            indexer = recordlinkage.Index()
            indexer.block("Name_Initial") 
            candidate_links = indexer.index(book_df, portal_df)

            book_df['Name_Clean'] = book_df['Name_Clean'].fillna("").astype(str)
            portal_df['Name_Clean'] = portal_df['Name_Clean'].fillna("").astype(str)
            
            book_df['Invoice_Clean'] = book_df['Invoice_Clean'].fillna("").astype(str)
            portal_df['Invoice_Clean'] = portal_df['Invoice_Clean'].fillna("").astype(str)
            
            book_df['GST_Clean'] = book_df['GST_Clean'].fillna("").astype(str)
            portal_df['GST_Clean'] = portal_df['GST_Clean'].fillna("").astype(str)

            
            # --- 4.5 Compare Engine ---
            compare_cl = recordlinkage.Compare()
            compare_cl.string("Name_Clean", "Name_Clean", method="jarowinkler", threshold=0.75, label="Name_Match")
            compare_cl.exact("Invoice_Clean", "Invoice_Clean", label="Invoice_Match")
            compare_cl.numeric("Total_Clean", "Total_Clean", method="step", offset=1, label="Amount_Match")
            compare_cl.exact("GST_Clean", "GST_Clean", label="GSTIN_Match")
            
            features = compare_cl.compute(candidate_links, book_df, portal_df)
            
            
            # 4.5 Weighted Cascading System
            features['Total_Score'] = (
                features['Name_Match'] * 1.0 +
                features['Invoice_Match'] * 3.0 +  
                features['Amount_Match'] * 2.0 +   
                features['GSTIN_Match'] * 1.0      
            )
            
            # --- TIER 1: PERFECT MATCHES ---
            perfect_matches = features[features['Total_Score'] == 7.0]
            book_perfect_indices = perfect_matches.index.get_level_values(0)
            portal_perfect_indices = perfect_matches.index.get_level_values(1)
            
            # --- TIER 2: STRONG MATCHES ---
            remaining_for_strong = features[
                ~features.index.get_level_values(0).isin(book_perfect_indices) & 
                ~features.index.get_level_values(1).isin(portal_perfect_indices)
            ]
            strong_matches = remaining_for_strong[(remaining_for_strong['Total_Score'] >= 5.0) & (remaining_for_strong['Total_Score'] <= 6.0)]
            book_strong_indices = strong_matches.index.get_level_values(0)
            portal_strong_indices = strong_matches.index.get_level_values(1)
            
            # --- TIER 3: PROBABLE MATCHES ---
            all_used_book_ids = book_perfect_indices.append(book_strong_indices)
            all_used_portal_ids = portal_perfect_indices.append(portal_strong_indices)
            
            remaining_for_probable = features[
                ~features.index.get_level_values(0).isin(all_used_book_ids) & 
                ~features.index.get_level_values(1).isin(all_used_portal_ids)
            ]
            probable_matches = remaining_for_probable[(remaining_for_probable['Total_Score'] >= 3.0) & (remaining_for_probable['Total_Score'] <= 4.0)]
            book_probable_indices = probable_matches.index.get_level_values(0)
            portal_probable_indices = probable_matches.index.get_level_values(1)
            
            # Retrieve data subsets
            perfect_match_book   = book_df.loc[book_perfect_indices, ['Particulars', 'Supplier Invoice No.','GSTIN/UIN', 'Gross Total']].reset_index(drop=True)
            perfect_match_portal = portal_df.loc[portal_perfect_indices, ['Trade/Legal Name', 'Invoice number','Supplier GSTIN', 'Invoice Value(₹)']].reset_index(drop=True)
            
            strong_match_book    = book_df.loc[book_strong_indices, ['Particulars', 'Supplier Invoice No.','GSTIN/UIN','Gross Total']].reset_index(drop=True)
            strong_match_portal  = portal_df.loc[portal_strong_indices, ['Trade/Legal Name', 'Invoice number','Supplier GSTIN','Invoice Value(₹)']].reset_index(drop=True)
            
            probable_match_book  = book_df.loc[book_probable_indices, ['Particulars', 'Supplier Invoice No.','GSTIN/UIN','Gross Total']].reset_index(drop=True)
            probable_match_portal = portal_df.loc[portal_probable_indices, ['Trade/Legal Name', 'Invoice number','Supplier GSTIN', 'Invoice Value(₹)']].reset_index(drop=True)
            
            # Glue subsets side-by-side
            perfect_reconciliation  = pd.concat([perfect_match_book, perfect_match_portal], axis=1)
            strong_reconciliation   = pd.concat([strong_match_book, strong_match_portal], axis=1)
            probable_reconciliation = pd.concat([probable_match_book, probable_match_portal], axis=1)
            
            # Isolate unique leftover indices
            all_matched_book_idx = pd.Index(book_perfect_indices.append(book_strong_indices).append(book_probable_indices)).unique()
            all_matched_portal_idx = pd.Index(portal_perfect_indices.append(portal_strong_indices).append(portal_probable_indices)).unique()
            
            unmatched_book = book_df.drop(index=all_matched_book_idx, errors='ignore')
            unmatched_portal = portal_df.drop(index=all_matched_portal_idx, errors='ignore')
            
            unmatched_book = unmatched_book[['Particulars', 'Supplier Invoice No.', 'GSTIN/UIN', 'Gross Total']]
            unmatched_portal = unmatched_portal[['Supplier GSTIN', 'Trade/Legal Name', 'Invoice number', 'Invoice Value(₹)']]
            
            st.success(f"✅ Processing Complete! Found {len(perfect_reconciliation)} perfect matches.")
            
            # --- 5. EXPORT TO MEMORY ---
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                perfect_reconciliation.to_excel(writer, sheet_name="Perfect_Matches", index=False)
                strong_reconciliation.to_excel(writer, sheet_name="Strong_Matches", index=False)
                probable_reconciliation.to_excel(writer, sheet_name="Probable_Matches", index=False)
                unmatched_book.to_excel(writer, sheet_name="Unmatched_Books", index=False)
                unmatched_portal.to_excel(writer, sheet_name="Unmatched_Portal", index=False)
            
            excel_data = output.getvalue()
            
            # Show download button
            st.download_button(
                label="📥 Download Final Reconciliation Report",
                data=excel_data,
                file_name="AI_GST_Reconciliation_Report.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary"
            )
