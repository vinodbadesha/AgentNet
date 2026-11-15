import hashlib
import hmac
import json
import time
import httpx

def sign(secret, body, timestamp):
    message = timestamp.encode() + b"." + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return "sha256=" + digest

def verify(secret, body, timestamp, signature):
    expected_signature = sign(secret, body, timestamp)
    return hmac.compare_digest(expected_signature, signature)

def is_fresh(timestamp, max_age_seconds = 300):
    return abs(time.time() - int(timestamp)) <= max_age_seconds

def deliver_webhook(store, notification_id, url, secret, payload, max_attempts=3, base_delay=1, client=None):
    body = json.dumps(payload).encode()
    http_client = client or httpx.Client(timeout=5)
    outcome = "failed"

    for attempt in range(max_attempts):
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-AgentNet-Timestamp": timestamp,
            "X-AgentNet-Signature": sign(secret, body, timestamp)
        }
        try:
            response = http_client.post(url, content=body, headers=headers)
            if response.status_code < 300:
                outcome = "delivered"
                break
        except httpx.HTTPError:
            pass
        
        if attempt < max_attempts-1:
            time.sleep(base_delay*2**attempt)

    store.q("update notifications set delivery=? where id=?",
            (f"webhook_{outcome}", notification_id))
    return outcome