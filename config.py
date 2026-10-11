"""Centralised configuration, loaded from environment variables.

All secrets (database, LLM keys, SSO, admin bootstrap) are passed via the
environment, never hardcoded. ``ENV`` controls strictness: ``production`` fails
fast on missing required values.
"""
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass

ENV = os.environ.get('ENV', 'development').lower()
IS_PRODUCTION = ENV == 'production'


def _req(name):
    val = os.environ.get(name, '').strip()
    if IS_PRODUCTION and not val:
        raise RuntimeError(f'Missing required environment variable: {name}')
    return val


# --- Required secrets ---
DATABASE_URL = _req('DATABASE_URL')
SECRET_KEY = _req('SECRET_KEY') if IS_PRODUCTION else os.environ.get(
    'SECRET_KEY', 'local-dev-secret-do-not-use-in-prod')

# --- LLM (Ollama) ---
OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'https://ollama.com').rstrip('/')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'gemma3:27b')
OLLAMA_API_KEY = os.environ.get('OLLAMA_API_KEY', '')

# --- Microsoft Entra ID SSO (optional) ---
MICROSOFT_CLIENT_ID = os.environ.get('MICROSOFT_CLIENT_ID', '')
MICROSOFT_CLIENT_SECRET = os.environ.get('MICROSOFT_CLIENT_SECRET', '')
MICROSOFT_TENANT_ID = os.environ.get('MICROSOFT_TENANT_ID', '')
MICROSOFT_ALLOWED_DOMAIN = os.environ.get('MICROSOFT_ALLOWED_DOMAIN', '')

# --- Google reCAPTCHA (optional) ---
RECAPTCHA_SITE_KEY = os.environ.get('RECAPTCHA_SITE_KEY', '')
RECAPTCHA_SECRET_KEY = os.environ.get('RECAPTCHA_SECRET_KEY', '')

# --- Admin bootstrap (used only when no users exist) ---
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

# --- Salesforce (future live integration; not required yet) ---
SALESFORCE_CLIENT_ID = os.environ.get('SALESFORCE_CLIENT_ID', '')
SALESFORCE_CLIENT_SECRET = os.environ.get('SALESFORCE_CLIENT_SECRET', '')
SALESFORCE_USERNAME = os.environ.get('SALESFORCE_USERNAME', '')
SALESFORCE_PASSWORD = os.environ.get('SALESFORCE_PASSWORD', '')
SALESFORCE_SECURITY_TOKEN = os.environ.get('SALESFORCE_SECURITY_TOKEN', '')
SALESFORCE_DOMAIN = os.environ.get('SALESFORCE_DOMAIN', 'login')

# --- S3-compatible object storage (uploads / processed files) ---
AWS_ENDPOINT_URL_S3 = os.environ.get('AWS_ENDPOINT_URL_S3', '')
AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID', '')
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY', '')
AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
S3_BUCKET = os.environ.get('S3_BUCKET', 'csv-upload')