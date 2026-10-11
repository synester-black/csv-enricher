"""Postgres persistence layer for the CSV Enricher.

A single ``DATABASE_URL`` (for example a Neon pooled connection string) is used
for everything: the master company cache, uploaded files and their rows.
"""
import os
import re
import json
import unicodedata
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

try:  # optional, handy for local development
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass

DATABASE_URL = os.environ.get('DATABASE_URL', '')

# ---------------------------------------------------------------------------
# Company name normalization
# ---------------------------------------------------------------------------
# Mirrors the rules documented for the master database: case, punctuation,
# legal suffixes and trailing S-number / C-number / IC-number are stripped.
_LEGAL_SUFFIXES = {
    'limited', 'ltd', 'plc', 'llp', 'llc', 'inc', 'incorporated',
    'corp', 'corporation', 'co', 'company', 'gmbh', 'ag', 'sa', 'sas',
    'srl', 'bv', 'nv', 'ab', 'oy', 'aps', 'pte', 'pvt', 'private',
}

# Master-workbook / template columns -> (db column, template label, model key).
# The master data template works with these 7 technology columns plus intent.
CATEGORIES = [
    ('epm_tool',                           'EPM Tool',                           'EPM',  'EPM / CPM'),
    ('erp_tool',                           'ERP Tool',                           'ERP',  'ERP'),
    ('data_tools',                         'Data Tools',                         'Data Tools', 'Data Tools'),
    ('business_intelligence_software',     'Business Intelligence Software',     'Business Intelligence Software', 'Business Intelligence (BI)'),
    ('crm_platform',                       'CRM Platform',                       'CRM',  'CRM'),
    ('cloud',                              'Cloud',                              'Cloud', 'Cloud'),
    ('other',                              'Other',                              'Other', 'Other'),
]

# db column -> human template label (for the search UI)
CAT_COLUMN_LABEL = {t[0]: t[1] for t in CATEGORIES}
# master workbook header -> canonical model key
MASTER_SOFTWARE_COLUMNS = {t[2]: t[3] for t in CATEGORIES}
# model key (e.g. 'EPM / CPM') -> db column
MODELKEY_COL = {t[3]: t[0] for t in CATEGORIES}
MODEL_SOFTWARE_CATEGORIES = [t[3] for t in CATEGORIES]


def row_to_software_dict(row):
    """Convert a companies row (with category columns) into {model_key: [values]}."""
    return {
        key: [v.strip() for v in (row.get(col) or '').split(';') if v.strip()]
        for col, _label, _master, key in CATEGORIES
    }


def software_dict_to_row(sw):
    """Convert {model_key: [values] or str} into {db_column: 'a; b'}."""
    out = {}
    for col, _label, _master, key in CATEGORIES:
        vals = sw.get(key)
        if isinstance(vals, (list, tuple)):
            vals = [str(v).strip() for v in vals if str(v).strip()]
            out[col] = '; '.join(vals) if vals else None
        elif vals:
            out[col] = str(vals).strip() or None
        else:
            out[col] = None
    return out


def normalize_company_name(name):
    """Return a stable lookup key for a company name."""
    if not name:
        return ''
    s = unicodedata.normalize('NFKD', str(name))
    s = s.encode('ascii', 'ignore').decode('ascii').lower()
    s = s.replace('&', ' and ')
    # trailing S-number / C-number / IC-number
    s = re.sub(r'[\s\-_.,/]*\b(?:s|c|ic)\d{1,6}\b\s*$', '', s)
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    tokens = s.split()
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return ' '.join(tokens)


NOISE = {'none', 'none mentioned', 'not mentioned', 'n/a', 'na', '-', 'null',
         'not listed', 'not applicable', 'not specified', 'unspecified'}


def _is_noise(v):
    return v.strip().lower() in NOISE


def split_multi(value):
    """Split a semicolon/comma/newline separated master cell into values."""
    if value is None:
        return []
    parts = re.split(r'[;\n]|,\s', str(value))
    out = []
    for p in parts:
        p = re.sub(r'\s+', ' ', p).strip()
        if p and not _is_noise(p):
            out.append(p)
    return out


def extract_domain(url):
    if not url or _is_noise(str(url)):
        return ''
    m = re.search(r'https?://([^/\s]+)', str(url).strip())
    host = m.group(1) if m else str(url).strip()
    host = host.lower()
    if host.startswith('www.'):
        host = host[4:]
    return host.split('/')[0]


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------
@contextmanager
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL is not set')
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    id                BIGSERIAL PRIMARY KEY,
    normalized_name   TEXT UNIQUE NOT NULL,
    account_name      TEXT,
    account_id        TEXT,
    domain            TEXT,
    website_url       TEXT,
    linkedin_url      TEXT,
    industry          TEXT,
    location          TEXT,
    partner           TEXT,
    account_type      TEXT,
    employees         TEXT,
    epm_tool                          TEXT,
    erp_tool                          TEXT,
    data_tools                        TEXT,
    business_intelligence_software    TEXT,
    crm_platform                      TEXT,
    cloud                             TEXT,
    other                             TEXT,
    intent_data                       TEXT,
    enrichment_source TEXT,
    raw               JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies (domain);

CREATE TABLE IF NOT EXISTS uploads (
    id              UUID PRIMARY KEY,
    filename        TEXT,
    file_format     TEXT,
    original_header JSONB,
    column_map      JSONB,
    row_count       INTEGER,
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS records (
    id                 BIGSERIAL PRIMARY KEY,
    upload_id          UUID REFERENCES uploads(id) ON DELETE CASCADE,
    row_index          INTEGER,
    company_normalized TEXT,
    data               JSONB,
    output             JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_records_company ON records (company_normalized);
CREATE INDEX IF NOT EXISTS idx_records_upload ON records (upload_id);

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT UNIQUE,
    email         TEXT UNIQUE,
    name          TEXT,
    password_hash TEXT,
    role          TEXT NOT NULL DEFAULT 'user',
    sso           BOOLEAN NOT NULL DEFAULT false,
    org_id        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE companies ADD COLUMN IF NOT EXISTS org_id TEXT;
ALTER TABLE records  ADD COLUMN IF NOT EXISTS org_id TEXT;

-- Migrate legacy JSONB store to the master-template columns.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS epm_tool TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS erp_tool TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS data_tools TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS business_intelligence_software TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS crm_platform TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS cloud TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS other TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS intent_data TEXT;
ALTER TABLE companies DROP COLUMN IF EXISTS software;
ALTER TABLE companies DROP COLUMN IF EXISTS intent;
"""


def init_schema():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)


# ---------------------------------------------------------------------------
# Upserts / lookups
# ---------------------------------------------------------------------------
_UPSERT_COMPANY = """
INSERT INTO companies
    (normalized_name, account_name, account_id, domain, website_url, linkedin_url,
     industry, location, partner, account_type, employees,
     epm_tool, erp_tool, data_tools, business_intelligence_software,
     crm_platform, cloud, other, intent_data, enrichment_source, raw)
VALUES %s
ON CONFLICT (normalized_name) DO UPDATE SET
    account_name      = COALESCE(EXCLUDED.account_name, companies.account_name),
    account_id        = COALESCE(EXCLUDED.account_id, companies.account_id),
    domain            = COALESCE(EXCLUDED.domain, companies.domain),
    website_url       = COALESCE(EXCLUDED.website_url, companies.website_url),
    linkedin_url      = COALESCE(EXCLUDED.linkedin_url, companies.linkedin_url),
    industry          = COALESCE(EXCLUDED.industry, companies.industry),
    location          = COALESCE(EXCLUDED.location, companies.location),
    partner           = COALESCE(EXCLUDED.partner, companies.partner),
    account_type      = COALESCE(EXCLUDED.account_type, companies.account_type),
    employees         = COALESCE(EXCLUDED.employees, companies.employees),
    epm_tool                          = COALESCE(EXCLUDED.epm_tool, companies.epm_tool),
    erp_tool                          = COALESCE(EXCLUDED.erp_tool, companies.erp_tool),
    data_tools                        = COALESCE(EXCLUDED.data_tools, companies.data_tools),
    business_intelligence_software    = COALESCE(EXCLUDED.business_intelligence_software, companies.business_intelligence_software),
    crm_platform                      = COALESCE(EXCLUDED.crm_platform, companies.crm_platform),
    cloud                             = COALESCE(EXCLUDED.cloud, companies.cloud),
    other                             = COALESCE(EXCLUDED.other, companies.other),
    intent_data                       = COALESCE(EXCLUDED.intent_data, companies.intent_data),
    enrichment_source = COALESCE(EXCLUDED.enrichment_source, companies.enrichment_source),
    raw               = COALESCE(EXCLUDED.raw, companies.raw),
    updated_at        = now()
"""

_UPSERT_TEMPLATE = (
    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
    "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)"
)


def _company_row(c):
    return (
        c['normalized_name'],
        c.get('account_name'),
        c.get('account_id'),
        c.get('domain'),
        c.get('website_url'),
        c.get('linkedin_url'),
        c.get('industry'),
        c.get('location'),
        c.get('partner'),
        c.get('account_type'),
        c.get('employees'),
        c.get('epm_tool'),
        c.get('erp_tool'),
        c.get('data_tools'),
        c.get('business_intelligence_software'),
        c.get('crm_platform'),
        c.get('cloud'),
        c.get('other'),
        c.get('intent_data'),
        c.get('enrichment_source'),
        json.dumps(c.get('raw')) if c.get('raw') is not None else None,
    )


def upsert_companies(companies):
    """Insert/update a list of company dicts. Returns number written."""
    rows = [
        _company_row(c) for c in companies
        if c.get('normalized_name')
    ]
    if not rows:
        return 0
    with get_conn() as conn, conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur, _UPSERT_COMPANY, rows, template=_UPSERT_TEMPLATE, page_size=500
        )
    return len(rows)


def lookup_companies(normalized_names=None, domains=None):
    """Return {normalized_name: row_dict} for names and/or domains."""
    normalized_names = list({n for n in (normalized_names or []) if n})
    domains = list({d for d in (domains or []) if d})
    if not normalized_names and not domains:
        return {}
    clauses, params = [], []
    if normalized_names:
        clauses.append('normalized_name = ANY(%s)')
        params.append(normalized_names)
    if domains:
        clauses.append('domain = ANY(%s)')
        params.append(domains)
    sql = 'SELECT * FROM companies WHERE ' + ' OR '.join(clauses)
    out = {}
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        for r in cur.fetchall():
            out[r['normalized_name']] = dict(r)
    return out


# ---------------------------------------------------------------------------
# Uploads / records
# ---------------------------------------------------------------------------
def create_upload(upload_id, filename, file_format, header, row_count, column_map=None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO uploads (id, filename, file_format, original_header, column_map, row_count)
               VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s)
               ON CONFLICT (id) DO NOTHING""",
            (
                str(upload_id), filename, file_format,
                json.dumps(header or []),
                json.dumps(column_map or {}),
                row_count or 0,
            ),
        )


def save_records(upload_id, records):
    """Persist rows. ``records`` is a list of dicts with row_index/data/output."""
    rows = [
        (
            str(upload_id),
            r.get('row_index'),
            r.get('company_normalized'),
            json.dumps(r.get('data') or {}),
            json.dumps(r.get('output') or {}),
        )
        for r in records
    ]
    if not rows:
        return 0
    with get_conn() as conn, conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO records (upload_id, row_index, company_normalized, data, output)
               VALUES %s""",
            rows,
            template='(%s,%s,%s,%s::jsonb,%s::jsonb)',
            page_size=500,
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Users / authentication
# ---------------------------------------------------------------------------
def get_user(username=None, email=None):
    if not username and not email:
        return None
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if username:
            cur.execute('SELECT * FROM users WHERE username = %s', (username,))
        else:
            cur.execute('SELECT * FROM users WHERE email = %s', (email,))
        r = cur.fetchone()
        return dict(r) if r else None


def create_user(username, password_hash=None, name='', role='user',
                email=None, sso=False):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO users (username, email, name, password_hash, role, sso)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (username) DO NOTHING""",
            (username, email, name, password_hash, role, sso),
        )
        return cur.rowcount


def list_users():
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute('SELECT id, username, email, name, role, sso, created_at '
                    'FROM users ORDER BY created_at')
        return [dict(r) for r in cur.fetchall()]


def delete_user(username):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('DELETE FROM users WHERE username = %s', (username,))
        return cur.rowcount


def update_password(username, password_hash):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('UPDATE users SET password_hash = %s, updated_at = now() '
                    'WHERE username = %s', (password_hash, username))
        return cur.rowcount


def set_user_role(username, role):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('UPDATE users SET role = %s, updated_at = now() '
                    'WHERE username = %s', (role, username))
        return cur.rowcount


def count_users():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM users')
        return cur.fetchone()[0]


def bootstrap_admin():
    """Create the default admin user when no users exist yet."""
    try:
        if count_users() == 0:
            import config
            from werkzeug.security import generate_password_hash
            create_user(
                config.ADMIN_USERNAME,
                password_hash=generate_password_hash(config.ADMIN_PASSWORD),
                name='Admin', role='admin', email=config.ADMIN_EMAIL,
            )
            return True
    except Exception as e:
        print(f'Admin bootstrap warning: {e}')
    return False


# ---------------------------------------------------------------------------
# Portal search
# ---------------------------------------------------------------------------
CATEGORY_COL = {t[3]: t[1] for t in CATEGORIES}


def _category_column(category):
    """Map a category label/model key to its db column, or return None."""
    if category in MODELKEY_COL:
        return MODELKEY_COL[category]
    for col, label, _master, _key in CATEGORIES:
        if label.lower() == category.lower():
            return col
    return None


def search_companies(q='', industry='', category='', location='', partner='',
                     page=1, per_page=50, org_id=None):
    """Search companies by text / filters. Returns (rows, total)."""
    clauses, params = [], []
    if q:
        clauses.append('normalized_name ILIKE %s OR account_name ILIKE %s')
        like = f'%{q}%'
        params += [like, like]
    if industry:
        clauses.append('industry ILIKE %s')
        params.append(f'%{industry}%')
    if location:
        clauses.append('location ILIKE %s')
        params.append(f'%{location}%')
    if partner:
        clauses.append('partner ILIKE %s')
        params.append(f'%{partner}%')
    col = _category_column(category) if category else None
    if col:
        clauses.append(f'({col} IS NOT NULL AND {col} <> %s)')
        params.append('')
    if org_id:
        clauses.append('org_id = %s')
        params.append(org_id)

    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    limit = per_page
    offset = (page - 1) * per_page

    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f'SELECT count(*) FROM companies {where}', params)
        total = cur.fetchone()['count']
        cur.execute(
            f'SELECT * FROM companies {where} ORDER BY account_name LIMIT %s OFFSET %s',
            params + [limit, offset],
        )
        rows = [dict(r) for r in cur.fetchall()]
    return rows, total


def company_detail(normalized_name):
    hits = lookup_companies(normalized_names=[normalized_name])
    return hits.get(normalized_name)


def db_stats():
    """Header stats for the dashboard/search pages."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*),"
                " count(DISTINCT industry),"
                " count(*) FILTER (WHERE epm_tool IS NOT NULL OR erp_tool IS NOT NULL"
                "   OR data_tools IS NOT NULL OR business_intelligence_software IS NOT NULL"
                "   OR crm_platform IS NOT NULL OR cloud IS NOT NULL OR other IS NOT NULL)"
                " FROM companies")
            total, industries, with_tech = cur.fetchone()
            cur.execute("SELECT count(*) FROM users")
            users = cur.fetchone()[0]
    except Exception:
        return {'companies': 0, 'industries': 0, 'tech_companies': 0, 'users': 0}
    return {'companies': total, 'industries': industries or 0,
            'tech_companies': with_tech or 0, 'users': users}


def distinct_industries():
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT industry FROM companies "
                        "WHERE industry IS NOT NULL AND industry <> '' ORDER BY industry")
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []
