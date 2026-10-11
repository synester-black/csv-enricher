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

# Master workbook columns -> canonical enrichment category used by the app.
MASTER_SOFTWARE_COLUMNS = {
    'EPM': 'EPM / CPM',
    'ERP': 'ERP',
    'CRM': 'CRM',
    'Data Tools': 'Data Tools',
    'Business Intelligence Software': 'Business Intelligence (BI)',
    'Cloud': 'Cloud',
    'Other': 'Other',
}

# Categories produced by the LLM software prompt.
MODEL_SOFTWARE_CATEGORIES = [
    'ERP', 'CRM', 'Accounting & Finance', 'HRIS / HCM', 'Payroll',
    'Procurement', 'Manufacturing', 'Business Intelligence (BI)', 'Analytics',
]


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
    software          JSONB NOT NULL DEFAULT '{}'::jsonb,
    intent            JSONB NOT NULL DEFAULT '{}'::jsonb,
    enrichment_source TEXT,
    raw               JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies (domain);
CREATE INDEX IF NOT EXISTS idx_companies_software_gin ON companies USING gin (software);

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
     industry, location, partner, account_type, employees, software, intent,
     enrichment_source, raw)
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
    software          = companies.software || EXCLUDED.software,
    intent            = CASE
                            WHEN EXCLUDED.intent = '{}'::jsonb THEN companies.intent
                            ELSE EXCLUDED.intent
                        END,
    enrichment_source = COALESCE(EXCLUDED.enrichment_source, companies.enrichment_source),
    raw               = COALESCE(EXCLUDED.raw, companies.raw),
    updated_at        = now()
"""

_UPSERT_TEMPLATE = (
    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s::jsonb)"
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
        json.dumps(c.get('software') or {}),
        json.dumps(c.get('intent') or {}),
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
CATEGORY_COL = {
    'EPM / CPM': 'EPM / CPM',
    'ERP': 'ERP',
    'Data Tools': 'Data Tools',
    'Business Intelligence (BI)': 'Business Intelligence (BI)',
    'CRM': 'CRM',
    'Cloud': 'Cloud',
}


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
    if category and category in CATEGORY_COL:
        clauses.append("software->>%s IS NOT NULL AND jsonb_array_length(software->%s) > 0")
        params += [category, category]
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
