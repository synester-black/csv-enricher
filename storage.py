"""S3-compatible object storage (Neon storage) for uploaded and processed files."""
import os

import boto3
from botocore.config import Config

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

ENDPOINT = os.environ.get('AWS_ENDPOINT_URL_S3', '').strip()
ACCESS_KEY = os.environ.get('AWS_ACCESS_KEY_ID', '').strip()
SECRET_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY', '').strip()
REGION = os.environ.get('AWS_REGION', 'us-east-1').strip()
BUCKET = os.environ.get('S3_BUCKET', 'csv-upload').strip()

_client = None


def enabled():
    return bool(ENDPOINT and ACCESS_KEY and SECRET_KEY and BUCKET)


def client():
    global _client
    if _client is None:
        _client = boto3.client(
            's3',
            endpoint_url=ENDPOINT,
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
            region_name=REGION,
            config=Config(signature_version='s3v4',
                          s3={'addressing_style': 'path'}),
        )
    return _client


def upload_file(local_path, key, content_type='application/octet-stream'):
    client().upload_file(
        str(local_path), BUCKET, key,
        ExtraArgs={'ContentType': content_type},
    )
    return key


def download_file(key, local_path):
    client().download_file(BUCKET, key, str(local_path))
    return local_path


def put_bytes(data, key, content_type='application/octet-stream'):
    client().put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type)
    return key


def presigned_url(key, expires=3600):
    if not key:
        return None
    return client().generate_presigned_url(
        'get_object', Params={'Bucket': BUCKET, 'Key': key}, ExpiresIn=expires)


def delete_object(key):
    if key:
        client().delete_object(Bucket=BUCKET, Key=key)
