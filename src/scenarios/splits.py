"""Split rules shared by anchors.sql, the intent dataset and the e2e scenarios."""
import hashlib
from datetime import date

SPLIT_DATE = date(2025, 7, 1)   # train/dev anchors before, test anchors on or after (event time, never process_date)


def customer_bucket(seed, customer_id):
    """Same bucket as anchors.sql: first 8 hex digits of sha256('<seed>:<customer_id>') mod 100."""
    return int(hashlib.sha256(f"{seed}:{customer_id}".encode("utf-8")).hexdigest()[:8], 16) % 100


def split_of_bucket(bucket):
    return "train" if bucket < 70 else "dev" if bucket < 85 else "test"


def customer_split(seed, customer_id):
    return split_of_bucket(customer_bucket(seed, customer_id))
