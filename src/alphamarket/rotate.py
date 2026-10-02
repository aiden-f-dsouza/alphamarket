"""Replace a venue key on an existing account (wave2, w2-account-security).

STUB router; included by main.py. Contract (plan D17): TOTP-gated
(mfa.require_connect_ready) + rate limit ("venues_user") + ownership; prove
the new key read-only exactly as connect does; credentials.store; status
ACTIVE; enqueue SYNC (urgent) and a one-off RECONCILE; audit
CREDENTIAL_ROTATE + SCOPE_CHECK; security alert. Never re-anchors.

Duplicate the verification steps of venues.connect_account here for now;
import only public names from venues (ensure_venue_open, oauth_expires_at)
and scopecheck (prove, apply_evidence, evidence_json). Never refactor
venues.py (w2-p8-connect edits it); a shared _verify_new_credential() is
extracted at integration.
"""

from fastapi import APIRouter

router = APIRouter(tags=["rotate"])
