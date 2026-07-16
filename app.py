import os
import csv
import json
import re
import time
import uuid
import threading
import unicodedata
from functools import wraps
from io import StringIO
from pathlib import Path

import requests
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, send_file
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / 'uploads'
PROCESSED_DIR = BASE_DIR / 'processed'
USERS_FILE = BASE_DIR / 'users.json'
UPLOAD_DIR.mkdir(exist_ok=True)
PROCESSED_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-me-in-production')

# Ollama defaults -- overridable via Settings page
OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'https://ollama.com')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'gemma3:27b')
OLLAMA_API_KEY = os.environ.get('OLLAMA_API_KEY', '')

# Microsoft Entra ID SSO defaults
MICROSOFT_CLIENT_ID = os.environ.get('MICROSOFT_CLIENT_ID', '')
MICROSOFT_CLIENT_SECRET = os.environ.get('MICROSOFT_CLIENT_SECRET', '')
MICROSOFT_TENANT_ID = os.environ.get('MICROSOFT_TENANT_ID', '')
MICROSOFT_ALLOWED_DOMAIN = os.environ.get('MICROSOFT_ALLOWED_DOMAIN', '')

# reCAPTCHA defaults
RECAPTCHA_SITE_KEY = os.environ.get('RECAPTCHA_SITE_KEY', '')
RECAPTCHA_SECRET_KEY = os.environ.get('RECAPTCHA_SECRET_KEY', '')

# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------
processing_status = {}            # task_id -> dict of progress info
results_store = {}                # task_id -> path of output CSV
csv_previews = {}                 # task_id -> {'header': [...], 'rows': [[...], ...]}
column_mappings = {}              # task_id -> {'company': 7, 'email': 1, ...}

# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------
def load_users():
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"admin": {"password": "admin123", "name": "Admin"}}

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated(*a, **kw):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*a, **kw)
    return decorated

# ---------------------------------------------------------------------------
# Routes – Auth
# ---------------------------------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        users = load_users()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        # Verify reCAPTCHA if configured
        site_key = session.get('recaptcha_site_key', RECAPTCHA_SITE_KEY)
        secret_key = session.get('recaptcha_secret_key', RECAPTCHA_SECRET_KEY)
        if site_key and secret_key:
            token = request.form.get('g-recaptcha-response', '')
            if not token:
                error = 'Security verification failed. Please try again.'
            else:
                resp = requests.post(
                    'https://www.google.com/recaptcha/api/siteverify',
                    data={'secret': secret_key, 'response': token},
                    timeout=10,
                )
                result = resp.json()
                if not result.get('success') or result.get('score', 0) < 0.5:
                    error = 'Security verification failed. Please try again.'

        if not error:
            if username in users and users[username]['password'] == password:
                session['user'] = username
                flash('Logged in', 'success')
                return redirect(url_for('upload'))
            else:
                error = 'Invalid credentials'

        if error:
            flash(error, 'error')

    return render_template('login.html',
        recaptcha_site_key=session.get('recaptcha_site_key', RECAPTCHA_SITE_KEY))

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))

# ---------------------------------------------------------------------------
# Routes – Settings
# ---------------------------------------------------------------------------
@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        session['ollama_url'] = request.form.get('ollama_url', '').rstrip('/')
        session['ollama_model'] = request.form.get('ollama_model', '')
        session['ollama_api_key'] = request.form.get('ollama_api_key', '')
        session['microsoft_client_id'] = request.form.get('microsoft_client_id', '')
        session['microsoft_client_secret'] = request.form.get('microsoft_client_secret', '')
        session['microsoft_tenant_id'] = request.form.get('microsoft_tenant_id', '')
        session['microsoft_allowed_domain'] = request.form.get('microsoft_allowed_domain', '')
        session['app_url'] = request.form.get('app_url', '').rstrip('/')
        session['software_prompt'] = request.form.get('software_prompt', '')
        session['intent_prompt'] = request.form.get('intent_prompt', '')
        session['tier_prompt'] = request.form.get('tier_prompt', '')
        session['recaptcha_site_key'] = request.form.get('recaptcha_site_key', '')
        session['recaptcha_secret_key'] = request.form.get('recaptcha_secret_key', '')
        session['validation_regions'] = ','.join(request.form.getlist('validation_regions')) or 'uk'
        flash('Settings saved', 'success')
        _init_microsoft_oauth()  # Re-init with new values
        return redirect(url_for('upload'))
    sp = session.get('software_prompt', SOFTWARE_PROMPT)
    ip = session.get('intent_prompt', INTENT_PROMPT)
    tp = session.get('tier_prompt', TIER_PROMPT)
    return render_template('settings.html',
        ollama_url=session.get('ollama_url', OLLAMA_BASE_URL),
        ollama_model=session.get('ollama_model', OLLAMA_MODEL),
        ollama_api_key=session.get('ollama_api_key', OLLAMA_API_KEY),
        microsoft_client_id=session.get('microsoft_client_id', MICROSOFT_CLIENT_ID),
        microsoft_client_secret=session.get('microsoft_client_secret', MICROSOFT_CLIENT_SECRET),
        microsoft_tenant_id=session.get('microsoft_tenant_id', MICROSOFT_TENANT_ID),
        microsoft_allowed_domain=session.get('microsoft_allowed_domain', MICROSOFT_ALLOWED_DOMAIN),
        app_url=session.get('app_url', ''),
        software_prompt=sp, intent_prompt=ip, tier_prompt=tp,
        recaptcha_site_key=session.get('recaptcha_site_key', RECAPTCHA_SITE_KEY),
        recaptcha_secret_key=session.get('recaptcha_secret_key', RECAPTCHA_SECRET_KEY),
        validation_regions=session.get('validation_regions', 'uk'),
        regions=REGIONS)

# ---------------------------------------------------------------------------
# Routes – Upload
# ---------------------------------------------------------------------------
@app.route('/', methods=['GET'])
@login_required
def upload():
    return render_template('upload.html')

@app.route('/upload', methods=['POST'])
@login_required
def handle_upload():
    if 'file' not in request.files:
        flash('No file selected', 'error')
        return redirect(url_for('upload'))
    f = request.files['file']
    if not f.filename:
        flash('No file selected', 'error')
        return redirect(url_for('upload'))
    if not f.filename.endswith('.csv'):
        flash('Only CSV files accepted', 'error')
        return redirect(url_for('upload'))

    # Save uploaded file
    task_id = str(uuid.uuid4())
    in_path = UPLOAD_DIR / f'{task_id}_input.csv'
    f.save(in_path)

    # Read columns and preview rows
    with open(in_path, 'r', encoding='utf-8-sig') as cf:
        reader = csv.reader(cf)
        try:
            header = next(reader)
        except StopIteration:
            flash('CSV file is empty', 'error')
            return redirect(url_for('upload'))
        preview_rows = []
        for _ in range(5):
            try:
                preview_rows.append(next(reader))
            except StopIteration:
                break
    # Store preview for mapping page
    csv_previews[task_id] = {
        'header': header,
        'rows': preview_rows,
        'filepath': str(in_path),
    }

    return redirect(url_for('mapping', task_id=task_id))

# ---------------------------------------------------------------------------
# Routes – Column Mapping
# ---------------------------------------------------------------------------
EXPECTED_FIELDS = [
    {'id': 'company', 'label': 'Account: Account Name', 'required': True,
     'desc': 'Company name used for grouping and Software/Intent research'},
    {'id': 'email', 'label': 'Contact: Email', 'required': False,
     'desc': 'Work email for validation and domain matching'},
    {'id': 'personal_email', 'label': 'Contact: Personal Email', 'required': False,
     'desc': 'Personal email (flagged if personal provider detected)'},
    {'id': 'first_name', 'label': 'Contact: First Name', 'required': False,
     'desc': 'Used for name normalization'},
    {'id': 'last_name', 'label': 'Contact: Last Name', 'required': False,
     'desc': 'Used for name normalization'},
    {'id': 'title', 'label': 'Contact: Title / Job Title', 'required': False,
     'desc': 'Used for Tier classification and ICP validation'},
    {'id': 'mobile', 'label': 'Contact: Mobile', 'required': False,
     'desc': 'UK phone number validation and cleanup'},
    {'id': 'mobile_other', 'label': 'Contact: Mobile Other / Corporate Phone', 'required': False,
     'desc': 'Secondary phone number validation'},
    {'id': 'country', 'label': 'Contact: Mailing Country', 'required': False,
     'desc': 'Used for country validation (UK/Non-UK)'},
    {'id': 'city', 'label': 'Contact: Mailing City', 'required': False,
     'desc': 'Used for location matching'},
    {'id': 'employees', 'label': 'Account: Employees', 'required': False,
     'desc': 'Company size (informational)'},
]

@app.route('/map/<task_id>', methods=['GET', 'POST'])
@login_required
def mapping(task_id):
    preview = csv_previews.get(task_id)
    if not preview:
        flash('Session expired. Please upload again.', 'error')
        return redirect(url_for('upload'))

    header = preview['header']
    rows = preview['rows']

    if request.method == 'POST':
        # Build column mapping from form
        mapping = {}
        errors = []
        for field in EXPECTED_FIELDS:
            val = request.form.get(f'col_{field["id"]}', '').strip()
            if val and val != '__none__':
                idx = int(val) if val.isdigit() else -1
                if 0 <= idx < len(header):
                    mapping[field['id']] = idx
            elif field['required']:
                errors.append(f'"{field["label"]}" is required')

        if errors:
            flash('; '.join(errors), 'error')
            return render_template('mapping.html', task_id=task_id,
                                   header=header, rows=rows,
                                   expected_fields=EXPECTED_FIELDS,
                                   mapping=mapping)

        # Store the mapping
        column_mappings[task_id] = mapping

        # Capture Ollama config and start processing
        ollama_cfg = {
            'url': session.get('ollama_url', OLLAMA_BASE_URL).rstrip('/'),
            'model': session.get('ollama_model', OLLAMA_MODEL),
            'api_key': session.get('ollama_api_key', OLLAMA_API_KEY),
            'software_prompt': session.get('software_prompt', SOFTWARE_PROMPT),
            'intent_prompt': session.get('intent_prompt', INTENT_PROMPT),
            'tier_prompt': session.get('tier_prompt', TIER_PROMPT),
            'validation_regions': session.get('validation_regions', 'uk'),
        }

        threading.Thread(
            target=process_csv,
            args=(task_id, preview['filepath'], ollama_cfg, mapping),
            daemon=True,
        ).start()

        return redirect(url_for('progress', task_id=task_id))

    # Auto-detect mapping
    auto_map = {}
    for field in EXPECTED_FIELDS:
        best = _auto_detect_column(field['id'], header)
        if best is not None:
            auto_map[field['id']] = best

    return render_template('mapping.html', task_id=task_id,
                           header=header, rows=rows,
                           expected_fields=EXPECTED_FIELDS,
                           mapping=auto_map)

def _auto_detect_column(field_id, header):
    """Guess which column index matches a field."""
    patterns = {
        'company': ['account', 'company', 'organization', 'organisation'],
        'email': ['email', 'e-mail', 'mail'],
        'personal_email': ['personal email', 'personal e-mail', 'private email'],
        'first_name': ['first name', 'firstname', 'given name', 'forename', 'contact first'],
        'last_name': ['last name', 'lastname', 'surname', 'family name', 'contact last'],
        'title': ['title', 'job title', 'position', 'designation', 'role', 'contact title'],
        'mobile': ['mobile', 'mobile phone', 'cell', 'phone', 'telephone', 'contact mobile'],
        'mobile_other': ['other phone', 'corporate phone', 'business phone', 'mobile other',
                         'work phone', 'mobile other', 'contact mobile other'],
        'country': ['country', 'nation', 'contact mailing country'],
        'city': ['city', 'town', 'contact mailing city'],
        'employees': ['employees', 'employee count', 'company size', 'headcount'],
    }

    hl = [h.strip().lower() for h in header]
    candidates = patterns.get(field_id, [])
    for i, h in enumerate(hl):
        for pat in candidates:
            if pat in h:
                return i
    return None
# ---------------------------------------------------------------------------
@app.route('/progress/<task_id>')
@login_required
def progress(task_id):
    if task_id not in processing_status:
        flash('Task not found', 'error')
        return redirect(url_for('upload'))
    return render_template('processing.html', task_id=task_id)

@app.route('/api/status/<task_id>')
@login_required
def api_status(task_id):
    status = processing_status.get(task_id)
    if not status:
        return jsonify({'error': 'not found'}), 404
    return jsonify(status)

# ---------------------------------------------------------------------------
# Routes – Download
# ---------------------------------------------------------------------------
@app.route('/download/<task_id>')
@login_required
def download(task_id):
    out_path = results_store.get(task_id)
    if not out_path or not os.path.exists(out_path):
        flash('Result not ready or expired', 'error')
        return redirect(url_for('upload'))
    return send_file(
        out_path,
        as_attachment=True,
        download_name='enriched_leads.csv',
        mimetype='text/csv',
    )

# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------
def _ollama_config():
    """Read Ollama config from session or env."""
    try:
        return {
            'url': session.get('ollama_url', OLLAMA_BASE_URL).rstrip('/'),
            'model': session.get('ollama_model', OLLAMA_MODEL),
            'api_key': session.get('ollama_api_key', OLLAMA_API_KEY),
        }
    except RuntimeError:
        return {
            'url': OLLAMA_BASE_URL.rstrip('/'),
            'model': OLLAMA_MODEL,
            'api_key': OLLAMA_API_KEY,
        }

def _call_ollama(prompt, system_prompt=None, max_retries=2, cfg=None):
    if cfg is None:
        cfg = _ollama_config()
    url = f"{cfg['url']}/api/chat"
    headers = {'Content-Type': 'application/json'}
    if cfg['api_key']:
        headers['Authorization'] = f"Bearer {cfg['api_key']}"
    messages = []
    if system_prompt:
        messages.append({'role': 'system', 'content': system_prompt})
    messages.append({'role': 'user', 'content': prompt})

    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json={
                'model': cfg['model'],
                'messages': messages,
                'stream': False,
                'options': {'temperature': 0.1},
            }, headers=headers, timeout=180)
            if resp.status_code == 200:
                content = resp.json()['message']['content']
                return content
            else:
                print(f'Ollama error (attempt {attempt+1}): {resp.status_code} {resp.text[:200]}')
        except Exception as e:
            print(f'Ollama exception (attempt {attempt+1}): {e}')
        time.sleep(2)
    return ''

def _extract_json(text):
    """Extract JSON object from LLM response (handles ```json ... ``` wrapping)."""
    text = text.strip()
    # Try parsing whole thing first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Extract from code fence
    m = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Fallback: try to find {...} or {...} with braces
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None

# ---------------------------------------------------------------------------
# Microsoft Entra ID SSO (Azure AD)
# ---------------------------------------------------------------------------
from authlib.integrations.flask_client import OAuth

_microsoft_oauth = None
_microsoft_cfg = {}

def _init_microsoft_oauth():
    global _microsoft_oauth, _microsoft_cfg
    cfg = {
        'client_id': os.environ.get('MICROSOFT_CLIENT_ID', ''),
        'client_secret': os.environ.get('MICROSOFT_CLIENT_SECRET', ''),
        'tenant_id': os.environ.get('MICROSOFT_TENANT_ID', ''),
        'allowed_domain': os.environ.get('MICROSOFT_ALLOWED_DOMAIN', ''),
    }
    # Override from session if available (set via settings page)
    try:
        cfg['client_id'] = session.get('microsoft_client_id', cfg['client_id'])
        cfg['client_secret'] = session.get('microsoft_client_secret', cfg['client_secret'])
        cfg['tenant_id'] = session.get('microsoft_tenant_id', cfg['tenant_id'])
        cfg['allowed_domain'] = session.get('microsoft_allowed_domain', cfg['allowed_domain'])
    except RuntimeError:
        pass
    _microsoft_cfg = cfg

    if cfg['client_id'] and cfg['client_secret'] and cfg['tenant_id']:
        _microsoft_oauth = OAuth(app)
        _microsoft_oauth.register(
            name='microsoft',
            client_id=cfg['client_id'],
            client_secret=cfg['client_secret'],
            server_metadata_url=f'https://login.microsoftonline.com/{cfg["tenant_id"]}/v2.0/.well-known/openid-configuration',
            client_kwargs={
                'scope': 'openid email profile',
                'code_challenge_method': 'S256',
            },
        )
        return True
    return False

_init_microsoft_oauth()

@app.route('/login/microsoft')
def login_microsoft():
    if not _microsoft_oauth:
        flash('Microsoft SSO is not configured. Ask your admin to set it up in Settings.', 'error')
        return redirect(url_for('login'))
    redirect_uri = url_for('authorize_microsoft', _external=True)
    return _microsoft_oauth.microsoft.authorize_redirect(redirect_uri)

@app.route('/login/microsoft/callback')
def authorize_microsoft():
    if not _microsoft_oauth:
        flash('SSO not configured', 'error')
        return redirect(url_for('login'))
    try:
        token = _microsoft_oauth.microsoft.authorize_access_token()
        userinfo = token.get('userinfo', {})
        if not userinfo:
            userinfo = _microsoft_oauth.microsoft.parse_id_token(token)

        email = userinfo.get('email') or userinfo.get('preferred_username', '')
        name = userinfo.get('name', email.split('@')[0] if '@' in email else email)

        # Domain restriction
        allowed_domain = _microsoft_cfg.get('allowed_domain', '')
        if allowed_domain and '@' in email:
            user_domain = email.split('@')[1].lower()
            if user_domain != allowed_domain.lower():
                flash(f'Access denied. Only @{allowed_domain} accounts are allowed.', 'error')
                return redirect(url_for('login'))

        # Auto-create local user on first login
        users = load_users()
        if email not in users:
            users[email] = {'password': None, 'name': name, 'sso': True}
            save_users(users)

        session['user'] = email
        session['sso_user'] = True
        flash(f'Signed in as {name}', 'success')
        return redirect(url_for('upload'))
    except Exception as e:
        flash(f'SSO login failed: {e}', 'error')
        return redirect(url_for('login'))

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------
SOFTWARE_PROMPT = """You are an Enterprise Technology Intelligence Agent.

Your task is to identify the enterprise software used by the following company.

Company: {company_name}
Company Domain: {company_domain}

Research the company using the following sources in order of priority:
1. Company careers page
2. Job descriptions on LinkedIn Jobs, Indeed, Glassdoor, Greenhouse, Lever, Workday Careers, SmartRecruiters
3. Official company documentation
4. BuiltWith technology data
5. Other reliable public sources

Identify only the following categories:
- ERP
- CRM
- Accounting & Finance
- HRIS / HCM
- Payroll
- Procurement
- Manufacturing
- Business Intelligence (BI)
- Analytics

Rules:
- Only include technologies explicitly mentioned or strongly supported by reliable evidence.
- Do NOT guess or infer technologies.
- Remove duplicate technologies.
- Use official product names (e.g., SAP S/4HANA, Salesforce, Oracle Fusion Cloud ERP, Microsoft Dynamics 365, Workday, Power BI).
- If no technology is found for a category, return an empty array.
- Return ONLY valid JSON. Do not include explanations, reasoning, or markdown.

Return the response in exactly this format:
{{"ERP": [], "CRM": [], "Accounting & Finance": [], "HRIS / HCM": [], "Payroll": [], "Procurement": [], "Manufacturing": [], "Business Intelligence (BI)": [], "Analytics": []}}"""

INTENT_PROMPT = """You are a B2B Sales Intelligence Agent.

Your task is to determine whether the following company has demonstrated financial capacity or intent to invest in enterprise software such as ERP, CRM, HRIS, Finance, Analytics, or other digital transformation initiatives.

Company: {company_name}
Company Domain: {company_domain}

Research the company using reliable public sources including:
- Crunchbase, PitchBook (public), Company press releases, Company Newsroom
- TechCrunch, Business Wire, PR Newswire, Reuters, Bloomberg
- SEC filings, London Stock Exchange / NYSE / NASDAQ announcements
- Venture Capital announcements, Private Equity announcements
- Official company website, Other reliable financial news sources

Identify whether the company has any of the following:
- Seed Funding, Angel Investment, Series A, Series B, Series C, Series D+
- Venture Capital Funding, Growth Equity Investment, Private Equity Investment
- Strategic Investment, IPO, Public Listing, Acquisition, Merger
- Major Expansion, Significant Capital Investment
- Recent Funding Announcement (within the last 5 years)

Rules:
- Only use verified public information. Do not guess.
- If multiple funding rounds exist, return the most recent significant funding event.
- Include the funding type, announced amount (if public), and year.
- If no funding information can be found, set "Funding Status" to "No Public Funding Found".
- Return ONLY valid JSON. Do not include explanations, reasoning, or markdown.

Return as a JSON object like this:
{{"Intent": {{"Funding Status": "", "Funding Type": "", "Funding Amount": "", "Funding Year": "", "Investors": [], "IPO / Public Company": "", "Acquisition / Merger": "", "Expansion": "", "Digital Transformation": "", "Overall Intent": ""}}}}"""

TIER_PROMPT = """You are a B2B Lead Tiering Agent.

Your task is to classify the following job titles into the correct Tier based on the buying authority and influence for FP&A / Finance software.

TIER DEFINITIONS:
- Tier 1 (Decision Maker): C-level executives and the most senior finance leaders who have budget authority and final decision power. Examples: CFO, Chief Financial Officer, Finance Director, Group Finance Director, VP Finance, Head of Finance.
- Tier 2 (Senior Finance Stakeholder): Senior finance professionals who strongly influence decisions and manage finance teams. Examples: Head of FP&A, FP&A Director, Finance Transformation Director, Director of Finance, Financial Controller, Group Financial Controller, Head of Commercial Finance.
- Tier 3 (Manager / Influencer): Managers who evaluate solutions and influence purchasing decisions. Examples: FP&A Manager, Finance Manager, Commercial Finance Manager, Reporting Manager, BI Manager.
- Tier 4 (Technical / Delivery): Individual contributors, analysts, and technical staff who implement and support solutions. Examples: FP&A Analyst, Financial Analyst, Business Analyst, Data Analyst, BI Analyst, Developer, Consultant.
- Unknown: Titles that do not fit any tier or are not finance-related.

TITLES TO CLASSIFY:
{titles}

Rules:
- Analyze each title carefully based on the tier definitions above.
- Use the actual job title, not just generic keywords — consider seniority and domain.
- Tier 1 requires clear C-level or most senior finance leadership.
- Tier 2 is for senior/director-level roles in finance-adjacent functions.
- Tier 3 is for manager-level roles.
- Tier 4 is for analyst/associate/individual-contributor/technical roles.
- If a title includes "VP" and "Finance", it is likely Tier 1.
- If a title includes "Director" and is finance-related, it is likely Tier 2.
- If a title includes "Manager" and is finance-related, it is likely Tier 3.
- If a title is clearly non-finance (e.g., Sales, Marketing, HR, Legal, Operations), classify as "Unknown".
- Return ONLY valid JSON. No explanations, reasoning, or markdown.

Return a JSON object mapping each title to its tier:
{{"title 1": "Tier 1", "title 2": "Tier 2", ...}}"""

VALIDATION_PROMPT = """You are a B2B lead data validation and enrichment assistant.

Process ONE record and return ONLY valid JSON.
Do not include explanations, markdown, or any text outside the JSON.

VALIDATION RULES

1. Names: Remove only special/accent characters (é->e, ñ->n, etc.). Do NOT spell check.

2. Email Validation:
   - If email domain is a personal provider (gmail.com, yahoo.com, hotmail.com, outlook.com, live.com, icloud.com, aol.com, protonmail.com, proton.me) -> "To be Removed": ["Personal email"]
   - If email domain does NOT match the company website -> add issue: "Email domain does not match company"

3. Phone Validation: Check "Mobile Phone" and "Corporate Phone".
   Valid UK numbers: +44, 0044, 0xxxxxxxxxx. If valid convert to +44XXXXXXXXXX. If invalid/non-UK return null.

4. Country Validation: If Country is not UK or United Kingdom -> add "Non-UK location"

5. Contact Location Validation: If contact location does not match company's operating country -> add "Location mismatch"

6. Company Validation: If company appears invalid, generic, duplicated, fake -> add "Company issue"

7. Job Title Validation: Check if title fits FP&A/Finance software ICP.
   Valid: Finance, FP&A, Financial Planning, Commercial Finance, Business Intelligence, Finance Systems, Corporate Performance, EPM, Planning, Reporting, Controlling, Transformation
   If clearly unrelated (Sales, HR, Marketing, Legal, Operations etc.) -> add "Outside ICP"

8. Job Title Tier Classification:
   Tier 1 (Decision Maker): CFO, Chief Financial Officer, Finance Director, Group Finance Director, VP Finance, Head of Finance
   Tier 2 (Senior Finance Stakeholders): Head of FP&A, FP&A Director, Finance Transformation Director, Director of Finance, Financial Controller, Group Financial Controller, Head of Commercial Finance, Head of Financial Planning, Head of Business Performance, Finance Systems Manager, Finance Manager (Senior)
   Tier 3 (Manager / Influencers): FP&A Manager, Finance Manager, Commercial Finance Manager, Reporting Manager, BI Manager, Business Performance Manager, Finance Transformation Manager, Planning Manager, EPM Manager, Analytics Manager
   Tier 4 (Technical / Delivery): FP&A Analyst, Finance Analyst, Financial Analyst, Business Analyst, Data Analyst, BI Analyst, Power BI Developer, SQL Developer, Analytics Developer, Finance Systems Analyst, EPM Consultant, TM1 Developer, Anaplan Model Builder, Jedox Consultant, Developer, Consultant, Centre of Excellence, CoE, Solution Architect
   If no suitable tier exists: "Unknown"

9. Obvious Data Issues: Flag missing names, invalid email format, invalid phone, dummy values, missing company, suspicious characters.

Collect ALL issues.

INPUT RECORD:
Company: {company}
First Name: {first_name}
Last Name: {last_name}
Email: {email}
Personal Email: {personal_email}
Mobile Phone: {mobile}
Corporate Phone: {mobile_other}
Job Title: {title}
Country: {country}
Location City: {city}
Company Employees: {employees}

OUTPUT format (return ONLY this JSON):
{{"First Name": "", "Last Name": "", "Email": "", "Mobile Phone": "", "Corporate Phone": "", "Company": "", "Job Title": "", "Tier": "", "To be Removed": [], "Issues": []}}"""

# ---------------------------------------------------------------------------
# Deterministic fallback for Prompt 3 (much faster than LLM)
# ---------------------------------------------------------------------------
PERSONAL_DOMAINS = {
    'gmail.com','yahoo.com','yahoo.co.uk','hotmail.com','hotmail.co.uk',
    'outlook.com','live.com','live.co.uk','icloud.com','aol.com','aol.co.uk',
    'protonmail.com','proton.me','googlemail.com','btinternet.com',
    'btopenworld.com','talktalk.net','virginmedia.com','sky.com','msn.com',
    'ymail.com','mail.com','inbox.com','gmx.com','yandex.com','fastmail.com','zoho.com',
}

TIER_KEYWORDS = [
    (1, ['cfo','chief financial officer','finance director','group finance director',
         'vp finance','head of finance','chief financial','group cfo',
         'deputy chief financial','international cfo','financial director']),
    (2, ['financial controller','group financial controller','director of finance',
         'director of strategic finance','director of corporate finance',
         'director of financial planning','finance transformation director',
         'fp&a director','group fp&a director','global fp&a',
         'finance systems manager','senior finance manager',
         'head of commercial finance','head of financial planning',
         'head of business performance','head of group financial',
         'head of finance systems','head of financial controls',
         'head of group finance','head of fp&a','head of integration',
         'head of financial planning & analysis','regional finance director',
         'business finance director','senior director finance',
         'financial controls director','financial planning and analysis director']),
    (3, ['fp&a manager','finance manager','commercial finance manager',
         'reporting manager','bi manager','business performance manager',
         'finance transformation manager','planning manager','epm manager',
         'analytics manager','senior fp&a manager','senior financial planning',
         'senior finance systems','group finance manager','senior manager',
         'fp&a senior manager','affluent fp&a manager',
         'group financial planning & analysis senior lead']),
    (4, ['fp&a analyst','finance analyst','financial analyst','business analyst',
         'data analyst','bi analyst','power bi developer','sql developer',
         'analytics developer','finance systems analyst','epm consultant',
         'tm1 developer','anaplan model builder','jedox consultant','developer',
         'consultant','centre of excellence','coe','solution architect',
         'senior analyst','senior financial analyst']),
]

# ---------------------------------------------------------------------------
# Region configurations for validation (phone, country, ICP)
# ---------------------------------------------------------------------------
REGIONS = {
    'uk': {
        'label': 'UK / Ireland',
        'country_codes': {'44', '44'},
        'valid_countries': {'uk','united kingdom','england','scotland','wales','northern ireland','ireland','eire'},
        'country_issue': 'Non-UK/IE location',
        'phone_prefix': '+44',
    },
    'us': {
        'label': 'US / Canada / NA',
        'country_codes': {'1'},
        'valid_countries': {'us','usa','united states','united states of america','canada','mexico','america'},
        'country_issue': 'Non-NA location',
        'phone_prefix': '+1',
    },
    'nordic': {
        'label': 'Nordics (SE, NO, DK, FI)',
        'country_codes': {'46', '47', '45', '358'},
        'valid_countries': {'sweden','norway','denmark','finland','iceland','se','no','dk','fi','is'},
        'country_issue': 'Non-Nordic location',
        'phone_prefix': '+46',
    },
    'europe': {
        'label': 'Europe (broad)',
        'country_codes': {'44', '33', '49', '39', '34', '31', '32', '41', '43', '46', '47', '45', '358', '48', '353', '351', '30', '45'},
        'valid_countries': set(),
        'country_issue': 'Non-European location',
        'phone_prefix': '+44',
    },
    'global': {
        'label': 'Global (skip location checks)',
        'country_codes': set(),
        'valid_countries': set(),
        'country_issue': '',
        'phone_prefix': '',
    },
}

def normalize_name(s):
    s = unicodedata.normalize('NFKD', str(s))
    return s.encode('ascii', 'ignore').decode('ascii')

def normalize_phone(val, regions=None):
    if not val:
        return None
    if regions is None:
        regions = ['uk']
    if isinstance(regions, str):
        regions = [regions]
    v = str(val).strip().replace(' ', '').replace('-','').replace('(','').replace(')','')

    # Try each selected region
    for r in regions:
        cfg = REGIONS.get(r, REGIONS['uk'])
        if cfg['phone_prefix'] and v.startswith(cfg['phone_prefix']):
            return cfg['phone_prefix'] + v[len(cfg['phone_prefix']):]
        for cc in cfg['country_codes']:
            if v.startswith(cc) and len(v) >= len(cc) + 6:
                return '+' + v
            if v.startswith('0') and cc in ('44',):
                return '+44' + v[1:]

    # If already has +, preserve it
    if v.startswith('+'):
        return v
    return None

def classify_tier_deterministic(title):
    if not title:
        return 'Unknown'
    t = title.lower().strip()
    for tier, keywords in TIER_KEYWORDS:
        for kw in keywords:
            if kw in t:
                return tier
    if 'director' in t and any(k in t for k in ['finance','corporate finance','strategic finance','fp&a','transformation']):
        return 2
    if t.startswith('associate director') and any(k in t for k in ['finance','fp&a','financial','sponsor']):
        return 2
    if 'vice president' in t and 'finance' in t:
        return 2
    if 'manager' in t and any(k in t for k in ['finance','fp&a','financial','planning','reporting']):
        return 3
    if 'analyst' in t:
        return 4
    return 'Unknown'

def company_domain_mapping(company):
    d = company.lower().strip()
    m = {
        'aegon': 'aegon.com', 'broadridge': 'broadridge.com',
        'brooks macdonald': 'brooksmacdonald.com', 'df capital bank': 'dfcapital.bank',
        'gatehouse bank plc': 'gatehousebank.com', 'lv=': 'lv.com',
        'leeds building society': 'leedsbuildingsociety.co.uk',
        'legal & general': 'landg.com', 'm&g plc': 'mandg.com', 'mha': 'mha.co.uk',
        'mufg investor services': 'mfsadmin.com', 'marsh': 'marsh.com',
        'masthaven finance limited': 'springfinance.co.uk', 'mattioli woods': 'mattioliwoods.com',
        'mercer': 'mercer.com', 'miller insurance services llp': 'miller-insurance.com',
        'morningstar': 'morningstar.com', 'newcastle building society': 'newcastle.co.uk',
        'nottingham building society': 'thenottingham.com', 'nucleus financial': 'nucleusfinancial.com',
        'osb group': 'osb.co.uk', 'pension insurance corporation plc': 'pensioncorporation.com',
        'pepper money uk': 'pepper.money', 'phoenix group': 'thephoenixgroup.com',
        'principality building society': 'principality.co.uk', 'quilter': 'quilter.com',
        'royal london': 'royallondon.com', 'schroders': 'schroders.com',
        'secure trust bank': 'securetrustbank.co.uk', 'shawbrook': 'shawbrook.co.uk',
        'skipton building society': 'skipton.co.uk', "st james's place": 'sjp.co.uk',
        'succession wealth': 'successionwealth.co.uk', 'the ardonagh group': 'ardonagh.com',
        'the co-operative bank plc': 'co-operativebank.co.uk',
        'the openwork partnership': 'theopenworkpartnership.com', 'utmost group': 'utmostgroup.com',
        'vanquis banking group': 'vanquis.com', 'virgin money': 'virginmoney.com',
        'vitality': 'vitality.co.uk', 'wesleyan': 'wesleyan.co.uk',
        'west one loans': 'westoneloans.co.uk', 'yorkshire building society': 'ybs.co.uk',
    }
    return m.get(d, '')

def get_company_domain(company):
    """Get best-guess domain for a company."""
    cd = company_domain_mapping(company)
    if cd:
        return cd
    # Fallback: slugify company name
    slug = company.lower().replace(' ', '').replace("'", '').replace('&', 'and')
    return f'{slug}.com'

def validate_row_deterministic(row_dict, regions=None):
    """Run Prompt 3 logic deterministically (much faster than LLM)."""
    if regions is None:
        regions = ['uk']
    if isinstance(regions, str):
        regions = [regions]

    issues = []
    to_remove = []

    first = row_dict.get('first_name', '').strip()
    last = row_dict.get('last_name', '').strip()
    email = row_dict.get('email', '').strip()
    personal_email = row_dict.get('personal_email', '').strip()
    mobile = row_dict.get('mobile', '').strip()
    mobile_other = row_dict.get('mobile_other', '').strip()
    title = row_dict.get('title', '').strip()
    company = row_dict.get('company', '').strip()
    country = row_dict.get('country', '').strip()

    first_clean = normalize_name(first)
    last_clean = normalize_name(last)

    # Email checks
    email_to_check = email or personal_email
    if email_to_check:
        domain = email_to_check.split('@')[-1].lower() if '@' in email_to_check else ''
        if domain in PERSONAL_DOMAINS:
            to_remove.append('Personal email')
        if company and domain:
            comp_domain = get_company_domain(company)
            domain_slug = domain.lower().replace('-','').replace('.','')
            comp_slug = company.lower().replace(' ','').replace('-','').replace("'",'').replace('.','')
            if not domain.endswith(comp_domain) and comp_slug not in domain_slug:
                issues.append('Email domain does not match company')
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email_to_check):
            issues.append('Invalid email format')
    else:
        issues.append('Missing email')

    # Phone — try each region until one matches
    mob_clean = normalize_phone(mobile, regions)
    mob_other_clean = normalize_phone(mobile_other, regions)

    # Country — valid if it matches ANY selected region
    country_issues = []
    if country:
        country_lower = country.lower()
        matched = False
        for r in regions:
            rc = REGIONS.get(r, REGIONS['uk'])
            if rc['country_issue'] and country_lower in rc['valid_countries']:
                matched = True
                break
            if not rc['country_issue']:  # global / no check
                matched = True
                break
        if not matched:
            # Use the first region's issue label
            first_rc = REGIONS.get(regions[0], REGIONS['uk'])
            country_issues.append(first_rc['country_issue'])

    # ICP check
    icp_kw = ['finance','fp&a','financial planning','commercial finance','business intelligence',
              'finance systems','corporate performance','epm','planning','reporting',
              'controlling','transformation','financial control','cfo','chief financial',
              'accounting','treasury','audit']
    if title and not any(k in title.lower() for k in icp_kw):
        issues.append('Outside ICP')

    # Tier
    tier = classify_tier_deterministic(title) if title else 'Unknown'

    # Company check
    if company.lower() in ('test','abc','xxx','unknown','','na','n/a','?'):
        issues.append('Company issue')

    # Missing names
    if not first or not last:
        issues.append('Missing first/last name')

    # Collect all removal reasons
    all_issues = list(dict.fromkeys(issues + to_remove + country_issues))  # deduplicate preserving order
    return {
        'first_name_clean': first_clean,
        'last_name_clean': last_clean,
        'mobile_cleaned': mob_clean or '',
        'mobile_other_cleaned': mob_other_clean or '',
        'tier': str(tier),
        'to_be_removed': 'Yes' if all_issues else 'No',
        'issues': '; '.join(all_issues) if all_issues else '',
    }

# ---------------------------------------------------------------------------
# Main processing pipeline (runs in background thread)
# ---------------------------------------------------------------------------
def process_csv(task_id, input_path, ollama_cfg=None, column_map=None):
    status = {
        'state': 'starting',
        'total_companies': 0,
        'current_company': 0,
        'company_name': '',
        'total_rows': 0,
        'current_row': 0,
        'software_phase': 'pending',
        'intent_phase': 'pending',
        'tier_phase': 'pending',
        'validation_phase': 'pending',
        'error': '',
        'progress_pct': 0,
    }
    processing_status[task_id] = status

    if ollama_cfg is None:
        ollama_cfg = {
            'url': OLLAMA_BASE_URL.rstrip('/'),
            'model': OLLAMA_MODEL,
            'api_key': OLLAMA_API_KEY,
            'software_prompt': SOFTWARE_PROMPT,
            'intent_prompt': INTENT_PROMPT,
            'tier_prompt': TIER_PROMPT,
            'validation_regions': 'uk',
        }

    software_prompt_tpl = ollama_cfg.get('software_prompt', SOFTWARE_PROMPT)
    intent_prompt_tpl = ollama_cfg.get('intent_prompt', INTENT_PROMPT)
    tier_prompt_tpl = ollama_cfg.get('tier_prompt', TIER_PROMPT)
    validation_regions = [r.strip() for r in ollama_cfg.get('validation_regions', 'uk').split(',') if r.strip()]

    try:
        # Read input CSV
        with open(input_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            header = next(reader)
            rows = list(reader)

        status['total_rows'] = len(rows)
        status['state'] = 'reading'

        # Use the user's column mapping, or fall back to auto-detect
        col_map = {}
        if column_map:
            col_map = column_map
        else:
            for i, h in enumerate(header):
                hl = h.strip().lower()
                if 'account' in hl and 'name' in hl:
                    col_map['company'] = i
                elif 'first' in hl and 'name' in hl:
                    col_map['first_name'] = i
                elif 'last' in hl and 'name' in hl:
                    col_map['last_name'] = i
                elif hl == 'contact: email' or hl == 'email':
                    col_map['email'] = i
                elif 'personal' in hl and 'email' in hl:
                    col_map['personal_email'] = i
                elif hl == 'contact: mobile' or hl == 'mobile':
                    col_map['mobile'] = i
                elif 'mobile other' in hl:
                    col_map['mobile_other'] = i
                elif 'title' in hl:
                    col_map['title'] = i
                elif 'country' in hl:
                    col_map['country'] = i
                elif 'city' in hl:
                    col_map['city'] = i
                elif 'employees' in hl:
                    col_map['employees'] = i

        if 'company' not in col_map:
            raise ValueError('Could not find "Account: Account Name" column in CSV')

        # Group by company
        companies = {}
        for row in rows:
            c = row[col_map['company']].strip() if col_map['company'] < len(row) else ''
            if c:
                if c not in companies:
                    companies[c] = []
                companies[c].append(row)

        unique_companies = list(companies.keys())
        status['total_companies'] = len(unique_companies)
        status['state'] = 'processing'

        # Phase 1 & 2: Software + Intent per company
        company_software = {}
        company_intent = {}

        for idx, company in enumerate(unique_companies):
            status['current_company'] = idx + 1
            status['company_name'] = company
            status['progress_pct'] = int((idx / len(unique_companies)) * 60)
            processing_status[task_id] = status

            domain = get_company_domain(company)

            # Prompt 1: Software
            status['software_phase'] = f'processing ({company})'
            processing_status[task_id] = status
            sw_prompt = software_prompt_tpl.format(company_name=company, company_domain=domain)
            sw_raw = _call_ollama(sw_prompt, cfg=ollama_cfg)
            sw_data = _extract_json(sw_raw)
            if sw_data:
                company_software[company] = sw_data
            else:
                company_software[company] = {}
            status['software_phase'] = 'done'

            # Prompt 2: Intent
            status['intent_phase'] = f'processing ({company})'
            processing_status[task_id] = status
            in_prompt = intent_prompt_tpl.format(company_name=company, company_domain=domain)
            in_raw = _call_ollama(in_prompt, cfg=ollama_cfg)
            in_data = _extract_json(in_raw)
            if in_data and 'Intent' in in_data:
                company_intent[company] = in_data['Intent']
            elif in_data:
                company_intent[company] = in_data
            else:
                company_intent[company] = {}
            status['intent_phase'] = 'done'

        # Phase 3: Tier classification (batch LLM)
        status['software_phase'] = 'complete'
        status['intent_phase'] = 'complete'
        status['tier_phase'] = 'processing'
        status['progress_pct'] = 60
        processing_status[task_id] = status

        # Collect all unique titles from rows
        unique_titles = set()
        for row in rows:
            title_idx = col_map.get('title')
            if title_idx is not None and title_idx < len(row):
                t = row[title_idx].strip()
                if t:
                    unique_titles.add(t)

        title_tier_map = {}  # title -> tier string
        tier_lookup = {}     # case-insensitive lookup
        if unique_titles:
            titles_list = sorted(unique_titles)
            # Batch classify via LLM
            tier_prompt_body = tier_prompt_tpl.format(titles='\n'.join(titles_list))
            tier_raw = _call_ollama(tier_prompt_body, cfg=ollama_cfg)
            tier_data = _extract_json(tier_raw)
            if isinstance(tier_data, dict):
                for k, v in tier_data.items():
                    if isinstance(v, str):
                        tier_lookup[k.strip().lower()] = v
                title_tier_map = {k.strip(): v for k, v in tier_data.items() if isinstance(v, str)}
            # Fallback for titles the LLM didn't classify
            for t in titles_list:
                if t not in title_tier_map and t.strip().lower() not in tier_lookup:
                    fallback = classify_tier_deterministic(t)
                    # Indicate it was a fallback by appending " (auto)"
                    label = f'{fallback} (auto)' if fallback != 'Unknown' else 'Unknown'
                    title_tier_map[t] = label

        status['tier_phase'] = 'complete'

        # Phase 4: Validation per row
        status['validation_phase'] = 'processing'
        status['progress_pct'] = 65
        processing_status[task_id] = status

        # Build output
        new_header = list(header) + ['Software', 'Intent', 'Tier (Classified)', 'To be Removed', 'Validation Issues']
        output_rows = []

        for idx, row in enumerate(rows):
            status['current_row'] = idx + 1
            status['progress_pct'] = 65 + int((idx / len(rows)) * 32)
            processing_status[task_id] = status

            new_row = list(row)
            while len(new_row) < len(header):
                new_row.append('')

            company = row[col_map['company']].strip() if col_map['company'] < len(row) else ''

            # Software column
            sw = company_software.get(company, {})
            if sw and any(v for v in sw.values() if isinstance(v, list) and v):
                new_row.append(json.dumps(sw))
            else:
                new_row.append('N/A')

            # Intent column
            it = company_intent.get(company, {})
            new_row.append(json.dumps(it) if it else '')

            # Validation
            rd = {
                'first_name': row[col_map.get('first_name', 0)] if col_map.get('first_name', 0) < len(row) else '',
                'last_name': row[col_map.get('last_name', 0)] if col_map.get('last_name', 0) < len(row) else '',
                'email': row[col_map.get('email', 0)] if col_map.get('email', 0) < len(row) else '',
                'personal_email': row[col_map.get('personal_email', 0)] if col_map.get('personal_email', 0) < len(row) else '',
                'mobile': row[col_map.get('mobile', 0)] if col_map.get('mobile', 0) < len(row) else '',
                'mobile_other': row[col_map.get('mobile_other', 0)] if col_map.get('mobile_other', 0) < len(row) else '',
                'title': row[col_map.get('title', 0)] if col_map.get('title', 0) < len(row) else '',
                'company': company,
                'country': row[col_map.get('country', 0)] if col_map.get('country', 0) < len(row) else '',
            }
            v_result = validate_row_deterministic(rd, validation_regions)

            # Use LLM-classified tier when available, fall back to deterministic
            title_val = rd.get('title', '')
            llm_tier = ''
            if title_val:
                llm_tier = title_tier_map.get(title_val) or tier_lookup.get(title_val.strip().lower(), '')
            tier_value = llm_tier or v_result['tier']

            new_row.append(tier_value)
            new_row.append(v_result['to_be_removed'])
            new_row.append(v_result['issues'])

            # Clean phone columns
            mobile_idx = col_map.get('mobile')
            if mobile_idx is not None and normalize_phone(row[mobile_idx]) is None:
                new_row[mobile_idx] = v_result['mobile_cleaned']
            mobile_o_idx = col_map.get('mobile_other')
            if mobile_o_idx is not None and normalize_phone(row[mobile_o_idx]) is None:
                new_row[mobile_o_idx] = v_result['mobile_other_cleaned']

            output_rows.append(new_row)

        # Write output
        out_path = PROCESSED_DIR / f'{task_id}_output.csv'
        with open(out_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(new_header)
            writer.writerows(output_rows)

        results_store[task_id] = str(out_path)
        status['state'] = 'complete'
        status['progress_pct'] = 100
        status['validation_phase'] = 'complete'
        processing_status[task_id] = status

    except Exception as e:
        import traceback
        status['state'] = 'error'
        status['error'] = str(e)
        status['traceback'] = traceback.format_exc()
        processing_status[task_id] = status
        print(f'Error processing {task_id}: {e}')
        traceback.print_exc()

# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------
@app.route('/users', methods=['GET', 'POST'])
@login_required
def manage_users():
    users = load_users()
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'add':
            u = request.form.get('username', '').strip()
            p = request.form.get('password', '')
            n = request.form.get('name', '').strip()
            if u and p:
                users[u] = {'password': p, 'name': n or u}
                save_users(users)
                flash(f'User {u} added', 'success')
        elif action == 'delete':
            u = request.form.get('username', '').strip()
            if u in users and u != request.form.get('current_user'):
                del users[u]
                save_users(users)
                flash(f'User {u} deleted', 'success')
        elif action == 'change_password':
            u = session.get('user', '')
            old = request.form.get('old_password', '')
            new = request.form.get('new_password', '')
            if u in users and users[u]['password'] == old and new:
                users[u]['password'] = new
                save_users(users)
                flash('Password changed', 'success')
    return render_template('users.html', users=users, current_user=session.get('user', ''))

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
