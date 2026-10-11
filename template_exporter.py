"""Export processed results in the "Salesforce Data Request Master Template" format.

The template is a 65-column (A..BM) workbook with two header rows:
  row 1: section labels (TO BE PROVIDED / ACCOUNT DETAILS / CONTACT DETAILS)
  row 2: field names

One output row is written per company. Cells with no result are left blank.
Contact Source / Contact Campaign Type default to "uncontacted".
"""
import json

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# Row 2: the 65 field names in column order (A..BM).
COLUMN_NAMES = [
    "Owner ID", "Account ID", "Contact ID", "Account Owner Name", "Contact Owner",
    "Account Client Owner", "Contact Client Owner", "Account Name", "Account Phone",
    "Employee Count", "Website", "Company LinkedIn URL", "Industry", "Other Industry",
    "Parent Account", "Account Type", "X-Factor / Target Flag", "Account Description",
    "Company Overview / Pitch Intel", "Product Focus", "Intent Data", "EPM Tool",
    "ERP Tool", "Data Tools", "Business Intelligence Software", "CRM Platform",
    "Cloud", "Other", "Billing Street", "Billing City", "Billing County/State",
    "Billing Postcode", "Billing Country", "Shipping Street", "Shipping City",
    "Shipping County/State", "Shipping Postcode", "Shipping Country", "Salutation",
    "First Name", "Last Name", "Title", "Phone", "Mobile", "Other Phone", "Email",
    "Personal Email", "LinkedIn Profile URL", "Function", "Tier", "Lead Score",
    "Contact Description", "Contact Intel", "Contact Campaign Type", "Campaign Name",
    "Contact Source", "Previous Contact Owner", "Previous Lead Score", "Last Activity",
    "Source Links", "Partner", "Technology Information", "Notes on Implementation",
    "Data Sources", "Match Method",
]

# Row 1: section labels spanning the corresponding columns.
SECTION_LABELS = [(0, 6, "TO BE PROVIDED"), (7, 47, "ACCOUNT DETAILS"), (48, 64, "CONTACT DETAILS")]

DEFAULT_LEAD_SOURCE = "uncontacted"

# Master software category -> template column index
SOFTWARE_COLUMNS = {
    "EPM / CPM": 21,               # V - EPM Tool
    "ERP": 22,                     # W - ERP Tool
    "Data Tools": 23,              # X - Data Tools
    "Business Intelligence (BI)": 24,  # Y - BI Software
    "CRM": 25,                     # Z - CRM Platform
    "Cloud": 26,                   # AA - Cloud
    "Other": 27,                   # AB - Other
}


def _join_multi(v):
    """Join a jsonb list/string into a readable cell value."""
    if not v:
        return ""
    if isinstance(v, (list, tuple)):
        return "; ".join(str(x).strip() for x in v if str(x).strip())
    return str(v).strip()


def _cn(d, def_keys):
    """First raw value present among the given keys."""
    for k in def_keys:
        v = _join_multi((d.get('raw') or {}).get(k))
        if v:
            return v
    return ""


def build_template_row(account, enrichment=None):
    """Return a 65-cell row from an account dict + enrichment dict."""
    enrichment = enrichment or {}
    software = enrichment.get('software') or account.get('software') or {}
    intent = enrichment.get('intent') or account.get('intent') or {}
    raw = account.get('raw') or {}
    row = [""] * len(COLUMN_NAMES)

    def setc(i, val):
        if val:
            row[i] = val

    # -- Account / ID / owner (TO BE PROVIDED block) --
    setc(1, raw.get('Account ID'))                             # B Account ID
    setc(3, raw.get('Account Owner'))                          # D Account Owner Name

    # -- Account details --
    setc(7, account.get('account_name'))                       # H Account Name
    setc(10, account.get('website_url'))                       # K Website
    setc(11, account.get('linkedin_url'))                      # L Company LinkedIn URL
    setc(12, account.get('industry'))                          # M Industry
    setc(15, account.get('account_type'))                      # P Account Type
    loc = account.get('location')
    if loc:
        setc(29, loc)                                          # AD Billing City
    setc(20, json.dumps(intent) if intent else "")             # U Intent Data

    # -- Technology (from master database) --
    for cat, col in SOFTWARE_COLUMNS.items():
        setc(col, _join_multi(software.get(cat)))
    setc(61, json.dumps(software) if software else "")         # BJ Technology Information

    # -- Contact block (contact level is blank for company rows) --
    setc(49, enrichment.get('tier') or "")                     # AX Tier
    setc(53, DEFAULT_LEAD_SOURCE)                              # BB Contact Campaign Type
    setc(55, DEFAULT_LEAD_SOURCE)                              # BD Contact Source

    # -- Audit / provenance fields --
    setc(58, raw.get('Last Activity'))                         # BG Last Activity
    setc(59, raw.get('Source Links'))                          # BH Source Links
    setc(60, account.get('partner'))                           # BI Partner
    setc(62, raw.get('Notes on Implementation'))               # BK Notes on Implementation
    setc(63, enrichment.get('source') or "")                   # BL Data Sources
    setc(64, raw.get('Match Method') or enrichment.get('source') or "")  # BM Match Method

    return row


def write_template_xlsx(data_rows, out_path, sheet_name="Data Request"):
    """Write the two-header-row template workbook."""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name

    # Row 1: section labels
    for start, end, label in SECTION_LABELS:
        c = ws.cell(row=1, column=start + 1, value=label)
        c.font = Font(bold=True, size=11)
        c.fill = PatternFill("solid", fgColor="DCE6F1")
        ws.merge_cells(start_row=1, start_column=start + 1,
                       end_row=1, end_column=end + 1)

    # Row 2: field names
    for i, name in enumerate(COLUMN_NAMES, start=1):
        c = ws.cell(row=2, column=i, value=name)
        c.font = Font(bold=True)
        c.alignment = Alignment(vertical="top", wrap_text=True)
        c.fill = PatternFill("solid", fgColor="F2F2F2")

    # Data rows
    for r_idx, row in enumerate(data_rows, start=3):
        for c_idx, val in enumerate(row, start=1):
            ws.cell(row=r_idx, column=c_idx, value=val)

    ws.freeze_panes = "C3"
    ws.auto_filter.ref = f"A2:{get_column_letter(len(COLUMN_NAMES))}2"
    for i in range(1, len(COLUMN_NAMES) + 1):
        ws.column_dimensions[get_column_letter(i)].width = 18

    wb.save(out_path)
    return out_path