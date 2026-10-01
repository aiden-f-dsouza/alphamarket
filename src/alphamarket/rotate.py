"""Replace a venue key on an existing account (wave2, w2-account-security).

STUB router; included by main.py. Contract (plan D17): TOTP-gated
(mfa.require_connect_ready) + rate limit ("venues_user") + ownership; prove
the new key read-only exactly as connect does; credentials.store; status
ACTIVE; enqueue SYNC (urgent) and a one-off RECONCILE; audit
CREDENTIAL_ROTATE + SCOPE_CHECK; security alert. Never re-anchors.
"""

from fastapi import APIRouter

router = APIRouter(tags=["rotate"])
