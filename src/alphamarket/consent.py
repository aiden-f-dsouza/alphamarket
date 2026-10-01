"""Venue consent screens (wave2, workstream w2-p8-connect; invariant 10).

STUB. Contract:

consent_text(venue) -> ConsentScreen(venue, version, body, sha256)
    The exact text shown before a connect: what is read, that access is
    read-only, what becomes public, how to revoke. Built from the venue
    profile and docs/venues/<venue>.md; versioned; sha256 of the UTF-8 body.

require_consent(db, user, venue, text_sha256, request) -> ConsentScreen
    Recompute the current text; a missing or different hash raises
    HTTPException(422) (the page re-renders the screen). Inserts the
    consent_texts row if absent.

record_consent(db, user, screen, account, request) -> VenueConsent
    Append the venue_consents row (ip, user agent) in the same transaction
    that creates the account, and audit VENUE_CONSENT.
"""
