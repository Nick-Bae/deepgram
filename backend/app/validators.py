"""
Shared regex patterns for identifier fields.

All patterns are applied via Pydantic Field(pattern=...) in request models
and fastapi Path(pattern=...) / Query(pattern=...) for path/query parameters.
"""

# Org IDs: Firestore auto-IDs (20 alphanum chars) or custom slugs.
# Allow uppercase, lowercase, digits, hyphens, underscores.
ORG_ID = r"^[a-zA-Z0-9_-]{2,120}$"

# Service keys: short human-readable slugs chosen by admins.
SERVICE_KEY = r"^[a-zA-Z0-9_-]{2,80}$"

# Church slugs: URL-safe lowercase slugs (e.g. "my-church").
CHURCH_SLUG = r"^[a-z0-9-]{2,80}$"

# Sermon IDs: allow dots in addition to alphanum/hyphen/underscore.
SERMON_ID = r"^[a-zA-Z0-9_.-]{1,120}$"

# Plan keys: lowercase identifiers (e.g. "starter", "pro_monthly").
PLAN_KEY = r"^[a-z0-9_-]{3,32}$"

# BCP-47 language codes: e.g. "ko", "en", "zh-TW".
LANG_CODE = r"^[a-z]{2,3}(-[A-Za-z0-9]{1,8})*$"

# Membership roles.
ROLE = r"^(owner|admin|host|listener|viewer)$"
