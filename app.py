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

import db as dbstore
from db import (
    CAT_COLUMN_LABEL,
    CATEGORY_COL,
    MODEL_SOFTWARE_CATEGORIES,
    bootstrap_admin, company_detail, create_upload, create_user, delete_user,
    get_user, list_users, lookup_companies, normalize_company_name,
    row_to_software_dict, save_records, search_companies, set_user_role,
    software_dict_to_row, update_password, upsert_companies,
)
from tabular import SUPPORTED_EXTENSIONS, read_tabular
from template_exporter import build_template_row, write_template_xlsx
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / 'uploads'
PROCESSED_DIR = BASE_DIR / 'processed'
USERS_FILE = BASE_DIR / 'users.json'
UPLOAD_DIR.mkdir(exist_ok=True)
PROCESSED_DIR.mkdir(exist_ok=True)

import config as app_config

app = Flask(__name__)
app.secret_key = app_config.SECRET_KEY

# Ollama defaults -- overridable via Settings page
OLLAMA_BASE_URL = app_config.OLLAMA_BASE_URL
OLLAMA_MODEL = app_config.OLLAMA_MODEL
OLLAMA_API_KEY = app_config.OLLAMA_API_KEY

# Microsoft Entra ID SSO defaults
MICROSOFT_CLIENT_ID = app_config.MICROSOFT_CLIENT_ID
MICROSOFT_CLIENT_SECRET = app_config.MICROSOFT_CLIENT_SECRET
MICROSOFT_TENANT_ID = app_config.MICROSOFT_TENANT_ID
MICROSOFT_ALLOWED_DOMAIN = app_config.MICROSOFT_ALLOWED_DOMAIN

# reCAPTCHA defaults
RECAPTCHA_SITE_KEY = app_config.RECAPTCHA_SITE_KEY
RECAPTCHA_SECRET_KEY = app_config.RECAPTCHA_SECRET_KEY

# Ensure the database schema exists (idempotent; non-fatal if DB is down).
try:
    dbstore.init_schema()
    bootstrap_admin()
except Exception as e:
    print(f'DB init warning: {e}')

# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------
processing_status = {}            # task_id -> dict of progress info
results_store = {}                # task_id -> path of output CSV
template_store = {}               # task_id -> path of template XLSX
csv_previews = {}                 # task_id -> {'header': [...], 'rows': [[...], ...]}
column_mappings = {}              # task_id -> {'company': 7, 'email': 1, ...}

# ---------------------------------------------------------------------------
# User helpers (backed by Postgres; legacy users.json is used only as a
# fallback migration source on first boot)
# ---------------------------------------------------------------------------
def _default_admin_user():
    try:
        with open(USERS_FILE) as f:
            data = json.load(f)
    except Exception:
        return None
    admin = data.get('admin')
    if admin and isinstance(admin, dict):
        return {'username': 'admin', 'password': admin.get('password', 'admin123'),
                'name': admin.get('name', 'Admin')}
    return None


def load_users():
    """Return a dict in the legacy shape for the login flow."""
    db_users = list_users()
    if not db_users:
        legacy = _default_admin_user()
        if legacy:
            create_user(legacy['username'],
                        password_hash=generate_password_hash(legacy['password']),
                        name=legacy['name'], role='admin')
            db_users = list_users()
    return {u['username']: {
        'name': u['name'] or u['username'],
        'email': u['email'],
        'role': u['role'],
        'sso': u['sso'],
        'password_hash': u.get('password_hash'),
    } for u in db_users if u.get('username')}


def save_users(users):
    """No-op keeper; user mutations go through the DB functions directly."""
    pass


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def set_session_from_user(username, name=None, sso=False):
    session['user'] = username
    session['name'] = name or username
    session['sso_user'] = sso
    user = get_user(username=username)
    session['role'] = (user or {}).get('role', 'user')


def current_role():
    return session.get('role', 'user')


def login_required(f):
    @wraps(f)
    def decorated(*a, **kw):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*a, **kw)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*a, **kw):
        if 'user' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            flash('Admin access required', 'error')
            return redirect(url_for('search'))
        return f(*a, **kw)
    return decorated


# ---------------------------------------------------------------------------
# Routes – Auth
# ---------------------------------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
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
            user = get_user(username=username)
            if user and user.get('password_hash') and check_password_hash(
                    user['password_hash'], password):
                set_session_from_user(user['username'], user.get('name'))
                flash('Logged in', 'success')
                return redirect(url_for('search'))
            else:
                error = 'Invalid credentials'

        if error:
            flash(error, 'error')

    return render_template('login.html',
        recaptcha_site_key=session.get('recaptcha_site_key', RECAPTCHA_SITE_KEY))


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        name = request.form.get('name', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')

        if not username or not password:
            flash('Username and password are required', 'error')
        elif len(password) < 6:
            flash('Password must be at least 6 characters', 'error')
        elif password != confirm:
            flash('Passwords do not match', 'error')
        elif get_user(username=username) or get_user(email=email):
            flash('That username or email is already registered', 'error')
        else:
            try:
                create_user(username, password_hash=generate_password_hash(password),
                            name=name or username, role='user', email=email or None)
                set_session_from_user(username, name or username)
                flash('Account created. Welcome!', 'success')
                return redirect(url_for('search'))
            except Exception as e:
                print(f'Signup error: {e}')
                flash('Could not create account', 'error')
    return render_template('signup.html')


@app.route('/logout')
def logout():
    session.pop('user', None)
    session.pop('name', None)
    session.pop('role', None)
    session.pop('sso_user', None)
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
        session['software_categories'] = request.form.getlist('software_categories') or DEFAULT_SOFTWARE_CATEGORIES
        flash('Settings saved', 'success')
        _init_microsoft_oauth()  # Re-init with new values
        return redirect(url_for('settings'))
    sp = session.get('software_prompt', SOFTWARE_PROMPT)
    ip = session.get('intent_prompt', INTENT_PROMPT)
    tp = session.get('tier_prompt', TIER_PROMPT)
    active = session.get('software_categories', DEFAULT_SOFTWARE_CATEGORIES)
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
        regions=REGIONS,
        software_categories=active,
        category_guidance=CATEGORY_GUIDANCE, active='settings')

# ---------------------------------------------------------------------------
# Routes – Upload
# ---------------------------------------------------------------------------
@app.route('/', methods=['GET'])
def landing():
    if 'user' in session:
        return redirect(url_for('search'))
    stats = dbstore.db_stats() if dbstore.DATABASE_URL else None
    industries = dbstore.distinct_industries() if dbstore.DATABASE_URL else []
    return render_template('landing.html', stats=stats, industries=industries)

@app.route('/enrich', methods=['GET'])
@login_required
def upload():
    return render_template('upload.html', active='enrich')

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
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        flash('Only CSV, XLSX or XLS files accepted', 'error')
        return redirect(url_for('upload'))

    # Save uploaded file
    task_id = str(uuid.uuid4())
    in_path = UPLOAD_DIR / f'{task_id}_input{ext}'
    f.save(in_path)

    # Read columns and preview rows
    try:
        header, rows = read_tabular(str(in_path))
    except Exception as e:
        flash(f'Could not read file: {e}', 'error')
        return redirect(url_for('upload'))
    if not header:
        flash('File is empty', 'error')
        return redirect(url_for('upload'))
    preview_rows = rows[:5]
    # Store preview for mapping page
    csv_previews[task_id] = {
        'header': header,
        'rows': preview_rows,
        'filepath': str(in_path),
        'filename': f.filename,
        'file_format': ext.lstrip('.').upper(),
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
            'software_categories': session.get('software_categories',
                                               DEFAULT_SOFTWARE_CATEGORIES),
        }

        threading.Thread(
            target=process_csv,
            args=(task_id, preview['filepath'], ollama_cfg, mapping, preview.get('filename'),
                  preview.get('file_format'), preview.get('header')),
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


@app.route('/download/template/<task_id>')
@login_required
def download_template(task_id):
    out_path = template_store.get(task_id)
    if not out_path or not os.path.exists(out_path):
        flash('Template export not ready or expired', 'error')
        return redirect(url_for('upload'))
    return send_file(
        out_path,
        as_attachment=True,
        download_name='data_request.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
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
        if not get_user(email=email):
            create_user(email, password_hash=None, name=name, role='user',
                        email=email, sso=True)
        set_session_from_user(email, name, sso=True)
        flash(f'Signed in as {name}', 'success')
        return redirect(url_for('search'))
    except Exception as e:
        flash(f'SSO login failed: {e}', 'error')
        return redirect(url_for('login'))

@app.route('/api/ollama/models')
@login_required
def ollama_models():
    """List available models from the configured Ollama endpoint."""
    try:
        cfg = _ollama_config()
    except Exception:
        cfg = {'url': OLLAMA_BASE_URL.rstrip('/'), 'model': OLLAMA_MODEL, 'api_key': OLLAMA_API_KEY}
    headers = {'Content-Type': 'application/json'}
    if cfg.get('api_key'):
        headers['Authorization'] = f"Bearer {cfg['api_key']}"
    try:
        resp = requests.get(f"{cfg['url']}/api/tags", headers=headers, timeout=15)
        if resp.status_code != 200:
            return jsonify({'error': f'HTTP {resp.status_code}', 'models': []})
        names = [m.get('name') for m in resp.json().get('models', [])]
        names = [n for n in names if n]
    except Exception as e:
        return jsonify({'error': str(e), 'models': []})
    return jsonify({'models': names})


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------
# ICP category guidance used to build the software prompt. Keys are the model
# category keys; each drives a row in the prompt's "{categories}" placeholder.
CATEGORY_GUIDANCE = {
    'EPM / CPM': 'EPM / CPM tooling (e.g. Anaplan, Pigment, Tagetik, Board, OneStream, IBM Planning Analytics/TM1)',
    'ERP': 'ERP platform (e.g. SAP S/4HANA, Oracle Fusion Cloud ERP, Microsoft Dynamics 365, Workday)',
    'Data Tools': 'Data warehousing and pipeline tools (e.g. Snowflake, Databricks, Microsoft Fabric, Informatica)',
    'Business Intelligence (BI)': 'Business intelligence software (e.g. Power BI, Tableau, Qlik Sense, IBM Cognos)',
    'CRM': 'CRM platform (e.g. Salesforce, HubSpot, Microsoft Dynamics, Zoho)',
    'Cloud': 'Cloud platform (e.g. Microsoft Azure, Amazon Web Services, Google Cloud)',
    'Other': 'Any other notable enterprise software in use',
}

DEFAULT_SOFTWARE_CATEGORIES = list(MODEL_SOFTWARE_CATEGORIES)

# The active ICP categories are injected into the "{categories}" placeholder.
SOFTWARE_PROMPT = """You are an Enterprise Technology Intelligence Agent.

Your task is to identify the enterprise software used by the following company, for the ICP technology categories that matter to our business.

Company: {company_name}
Company Domain: {company_domain}

Categories to research:
{categories}

Research using reliable public sources (careers pages, job descriptions, official documentation, BuiltWith, and similar) in order of priority.

Rules:
- Only include technologies explicitly evidenced from public sources. Do not guess or infer.
- Use official product names (e.g. SAP S/4HANA, Salesforce, Oracle Fusion Cloud ERP, Microsoft Dynamics 365, Workday, Power BI, Snowflake, Azure).
- Remove duplicate technologies.
- If a category has no evidence, return an empty array for it.
- Return ONLY valid JSON in the following shape. Do not include explanations, reasoning, or markdown.

{"EPM / CPM": [], "ERP": [], "Data Tools": [], "Business Intelligence (BI)": [], "CRM": [], "Cloud": [], "Other": []}"""


def build_categories_block(active_keys):
    """Render the "{categories}" placeholder rows for the enabled ICP categories."""
    keys = [k for k in (active_keys or DEFAULT_SOFTWARE_CATEGORIES)
            if k in CATEGORY_GUIDANCE]
    if not keys:
        keys = DEFAULT_SOFTWARE_CATEGORIES
    return '\n'.join(f'- {k}: {CATEGORY_GUIDANCE[k]}' for k in keys)


def render_software_prompt(template, company_name, company_domain, active_keys):
    """Render a prompt template with its placeholders filled.

    Templates may use any of: {company_name}, {company_domain}, {categories}.
    Missing placeholders are filled from defaults so edits stay interactive.
    """
    placeholders = {
        'company_name': company_name,
        'company_domain': company_domain,
        'categories': build_categories_block(active_keys),
    }
    result = template
    for key, val in placeholders.items():
        result = result.replace('{' + key + '}', val)
    return result

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
def _software_complete(sw, keys=None):
    """True when every *active* ICP category is already populated (DB hit)."""
    keys = [k for k in (keys or DEFAULT_SOFTWARE_CATEGORIES) if k]
    return bool(sw) and all(sw.get(k) for k in keys)


def _has_values(d):
    return bool(d) and any(v for v in d.values() if v)


def _parse_intent(s):
    """Parse intent_data (a JSON string column) into a dict."""
    if isinstance(s, dict):
        return s
    if not s:
        return {}
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def process_csv(task_id, input_path, ollama_cfg=None, column_map=None,
                filename=None, file_format=None, upload_header=None):
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
    active_cats = [k for k in (ollama_cfg.get('software_categories')
                               or DEFAULT_SOFTWARE_CATEGORIES)
                   if k in CATEGORY_GUIDANCE] or DEFAULT_SOFTWARE_CATEGORIES

    try:
        # Read input (CSV, XLSX or XLS)
        header, rows = read_tabular(input_path)

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

        # Register the upload so raw rows can be persisted later
        try:
            create_upload(task_id, filename, file_format, upload_header or header, len(rows), column_map)
        except Exception as e:
            print(f'DB upload warning ({task_id}): {e}')

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

        # DB-first resolution: pull any known enrichment from Postgres.
        normalized_by_company = {c: normalize_company_name(c) for c in unique_companies}
        db_hits = {}
        try:
            db_hits = lookup_companies(normalized_names=set(normalized_by_company.values()))
        except Exception as e:
            print(f'DB lookup warning ({task_id}): {e}')

        domain_by_company = {}
        db_software_by_company = {}
        db_intent_by_company = {}
        for c in unique_companies:
            hit = db_hits.get(normalized_by_company[c])
            db_software_by_company[c] = row_to_software_dict(hit) if hit else {}
            db_intent_by_company[c] = _parse_intent(hit.get('intent_data')) if hit else {}
            domain_by_company[c] = ((hit.get('domain') if hit else '') or get_company_domain(c))

        # Phase 1 & 2: Software + Intent per company (DB values win, model fills gaps)
        company_software = {}
        company_intent = {}
        source_map = {}

        for idx, company in enumerate(unique_companies):
            status['current_company'] = idx + 1
            status['company_name'] = company
            status['progress_pct'] = int((idx / len(unique_companies)) * 60)
            processing_status[task_id] = status

            domain = domain_by_company[company]
            base_sw = dict(db_software_by_company.get(company) or {})
            base_intent = db_intent_by_company.get(company) or {}

            model_used = False
            swit = [_software_complete(base_sw, active_cats), _has_values(base_intent)]

            # Prompt 1: Software (only for missing categories / new companies)
            sw_final = dict(base_sw)
            if not swit[0]:
                status['software_phase'] = f'processing ({company})'
                processing_status[task_id] = status
                sw_prompt = render_software_prompt(
                    software_prompt_tpl, company, domain, active_cats)
                sw_raw = _call_ollama(sw_prompt, cfg=ollama_cfg)
                sw_data = _extract_json(sw_raw) or {}
                for k, v in sw_data.items():
                    if not sw_final.get(k):
                        sw_final[k] = v if isinstance(v, list) else ([v] if v else [])
                model_used = True
            company_software[company] = sw_final
            status['software_phase'] = 'done'

            # Prompt 2: Intent (only when not already available)
            intent_final = base_intent
            if not swit[1]:
                status['intent_phase'] = f'processing ({company})'
                processing_status[task_id] = status
                in_prompt = intent_prompt_tpl.format(company_name=company, company_domain=domain)
                in_raw = _call_ollama(in_prompt, cfg=ollama_cfg)
                in_data = _extract_json(in_raw) or {}
                intent_final = in_data.get('Intent', in_data) if in_data else {}
                model_used = True
            company_intent[company] = intent_final
            status['intent_phase'] = 'done'

            src = 'model'
            if base_sw or base_intent and not model_used:
                src = 'db'
            elif (base_sw or (base_intent and _has_values(base_intent))) and model_used:
                src = 'mixed'
            source_map[company] = src
            status['enrichment_source'] = src

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
        new_header = list(header) + ['Software', 'Intent', 'Tier (Classified)', 'To be Removed', 'Validation Issues', 'Enrichment Source']
        output_rows = []
        db_records = []
        company_tiers = {}
        company_titles = {}

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
            source = source_map.get(company, 'model')
            new_row.append(source)

            # Clean phone columns
            mobile_idx = col_map.get('mobile')
            if mobile_idx is not None and normalize_phone(row[mobile_idx]) is None:
                new_row[mobile_idx] = v_result['mobile_cleaned']
            mobile_o_idx = col_map.get('mobile_other')
            if mobile_o_idx is not None and normalize_phone(row[mobile_o_idx]) is None:
                new_row[mobile_o_idx] = v_result['mobile_other_cleaned']

            output_rows.append(new_row)
            company_tiers.setdefault(company, []).append(tier_value)
            if title_val:
                company_titles.setdefault(company, [])
                if title_val not in company_titles[company]:
                    company_titles[company].append(title_val)

            # Capture the raw row + computed output for persistence
            db_records.append({
                'row_index': idx,
                'company_normalized': normalized_by_company.get(company, ''),
                'data': dict(zip(header, [str(x) for x in row])) if len(header) else {},
                'output': {
                    'software': sw,
                    'intent': it,
                    'tier': tier_value,
                    'to_be_removed': v_result['to_be_removed'],
                    'validation_issues': v_result['issues'],
                    'enrichment_source': source,
                },
            })

        # Write output
        out_path = PROCESSED_DIR / f'{task_id}_output.csv'
        with open(out_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(new_header)
            writer.writerows(output_rows)

        # Data-request template export (one row per company)
        def _rep_tier(tiers):
            best = None
            for t in tiers:
                m = re.match(r'(\d+)', str(t))
                n = int(m.group(1)) if m else None
                if n and (best is None or n < best):
                    best = n
            return f'Tier {best}' if best else ''

        template_rows = []
        for c in unique_companies:
            hit = db_hits.get(normalized_by_company[c]) or {}
            account = {
                'account_name': c,
                'website_url': hit.get('website_url'),
                'linkedin_url': hit.get('linkedin_url'),
                'industry': hit.get('industry'),
                'location': hit.get('location'),
                'account_type': hit.get('account_type'),
                'partner': hit.get('partner'),
                'intent_data': json.dumps(company_intent.get(c) or {}) if company_intent.get(c) else None,
                'raw': hit.get('raw'),
                **software_dict_to_row(company_software.get(c) or {}),
            }
            enrichment = {
                'tier': _rep_tier(company_tiers.get(c, [])),
                'source': source_map.get(c, 'model'),
            }
            template_rows.append(build_template_row(account, enrichment))

        template_path = PROCESSED_DIR / f'{task_id}_data_request.xlsx'
        try:
            write_template_xlsx(template_rows, str(template_path))
            template_store[task_id] = str(template_path)
        except Exception as e:
            print(f'Template export warning ({task_id}): {e}')

        # Write computed enrichment back to the DB (companies cache)
        writeback = []
        for c in unique_companies:
            sw = company_software.get(c) or {}
            it = company_intent.get(c) or {}
            if not sw and not it and not company_titles.get(c):
                continue
            writeback.append({
                'normalized_name': normalized_by_company[c],
                'account_name': c,
                'domain': domain_by_company.get(c),
                **software_dict_to_row(sw),
                'intent_data': json.dumps(it) if it else None,
                'contact_titles': '; '.join(company_titles.get(c) or []) or None,
                'enrichment_source': source_map.get(c, 'model'),
            })
        try:
            upsert_companies(writeback)
            save_records(task_id, db_records)
        except Exception as e:
            print(f'DB persist warning for {task_id}: {e}')

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
@admin_required
def manage_users():
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'add':
            u = request.form.get('username', '').strip()
            p = request.form.get('password', '')
            n = request.form.get('name', '').strip()
            email = request.form.get('email', '').strip()
            if u and p:
                create_user(u, password_hash=generate_password_hash(p),
                            name=n or u, role='user', email=email or None)
                flash(f'User {u} added', 'success')
        elif action == 'delete':
            u = request.form.get('username', '').strip()
            if u and u != session.get('user', ''):
                delete_user(u)
                flash(f'User {u} deleted', 'success')
        elif action == 'role':
            u = request.form.get('username', '').strip()
            role = request.form.get('role', 'user')
            if u and u != session.get('user'):
                set_user_role(u, role)
                flash(f'{u} role updated to {role}', 'success')
        elif action == 'change_password':
            u = session.get('user', '')
            old = request.form.get('old_password', '')
            new = request.form.get('new_password', '')
            me = get_user(username=u)
            if me and me['password_hash'] and check_password_hash(me['password_hash'], old) and new:
                update_password(u, generate_password_hash(new))
                flash('Password changed', 'success')
            else:
                flash('Old password incorrect', 'error')
    users = list_users()
    return render_template('users.html', users=users, current_user=session.get('user', ''), active='users')


# ---------------------------------------------------------------------------
# Data intelligence portal (self-serve search over the master data)
# ---------------------------------------------------------------------------
@app.route('/search', methods=['GET'])
@login_required
def search():
    page = request.args.get('page', 1, type=int)
    q = request.args.get('q', '').strip()
    industry = request.args.get('industry', '').strip()
    category = request.args.get('category', '').strip()
    location = request.args.get('location', '').strip()
    partner = request.args.get('partner', '').strip()
    title = request.args.get('title', '').strip()

    results, total = [], 0
    if any([q, industry, category, location, partner, title]):
        rows, total = search_companies(q=q or None, industry=industry or None,
                                       category=category or None,
                                       location=location or None,
                                       partner=partner or None,
                                       title=title or None,
                                       page=page)
        results = rows

    if request.args.get('export'):
        rows, _ = search_companies(q=q or None, industry=industry or None,
                                   category=category or None,
                                   location=location or None,
                                   partner=partner or None,
                                   title=title or None, page=1, per_page=10000)
        return _export_search(rows)

    return render_template('search.html', results=results, total=total, page=page,
                           per_page=50, categories=CATEGORY_COL, columns=CAT_COLUMN_LABEL,
                           q=q, industry=industry, category=category,
                           location=location, partner=partner, title=title,
                           industries=dbstore.distinct_industries(),
                           common_titles=dbstore.COMMON_JOB_TITLES,
                           title_groups=dbstore.JOB_TITLE_GROUPS,
                           stats=dbstore.db_stats(), active='search')


def _export_search(rows):
    """Stream the current search results as a data-request template workbook."""
    from io import BytesIO

    template_rows = []
    for hit in rows:
        account = {
            'account_name': hit.get('account_name'),
            'website_url': hit.get('website_url'),
            'linkedin_url': hit.get('linkedin_url'),
            'industry': hit.get('industry'),
            'location': hit.get('location'),
            'account_type': hit.get('account_type'),
            'partner': hit.get('partner'),
            'intent_data': hit.get('intent_data') or '',
            'raw': hit.get('raw'),
        }
        for col in CAT_COLUMN_LABEL:
            account[col] = hit.get(col)
        enrichment = {
            'tier': '',
            'source': hit.get('enrichment_source') or 'db',
        }
        template_rows.append(build_template_row(account, enrichment))

    buf = BytesIO()
    write_template_xlsx(template_rows, buf)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True, download_name='data_request.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/company/<normalized_name>')
@login_required
def company(normalized_name):
    hit = company_detail(normalized_name)
    if not hit:
        flash('Company not found', 'error')
        return redirect(url_for('search'))
    return render_template('company.html', c=hit)


@app.route('/admin', methods=['GET', 'POST'])
@admin_required
def admin():
    result = None
    if request.method == 'POST':
        workbook = request.files.get('workbook')
        if workbook and workbook.filename:
            wb_path = UPLOAD_DIR / f'ingest_{uuid.uuid4()}.xlsx'
            workbook.save(wb_path)
            try:
                from openpyxl import load_workbook
                from ingest_master import build_companies, col_index
                from db import upsert_companies

                wb = load_workbook(str(wb_path), read_only=True, data_only=True)
                sheet = next(iter(wb.sheetnames), None)
                ws = wb[sheet]
                it = ws.iter_rows(values_only=True)
                header = [str(h).strip() for h in (next(it, None) or [])]
                idx = col_index(header)
                compt = build_companies(list(it), idx)
                wb.close()
                written = upsert_companies(compt)
                result = f'Ingested {written} companies'
            except Exception as e:
                result = f'Error: {e}'
    return render_template('admin.html', result=result, active='admin')


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
