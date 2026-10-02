"""Policy and runtime configuration.

Every limit lives here. The model never supplies a limit, a window, a currency
list or a recipient — those come from administrator-controlled configuration so
that a prompt-injected or hallucinating model cannot widen its own authority.
"""

from __future__ import annotations

import os
from typing import Dict, Tuple

POLICY_VERSION = "policy-1"

# mock = deterministic local platform (offline tests, judges can run with no
# credentials); sandbox = the real PayPal sandbox REST API.
MODE = os.getenv("PAYPILOT_MODE", "mock")

SUPPORTED_CURRENCIES: Tuple[str, ...] = ("USD",)

# The only actor that may operate this instance (single-operator app).
LOCAL_ACTOR_ID = "local-operator"

# Actions the executor is allowed to dispatch at all.
ALLOWED_ACTIONS: Tuple[str, ...] = ("create_order", "capture", "refund", "payout")

# Actions that move money out and therefore require an operator approval that
# binds the exact arguments.
APPROVAL_REQUIRED_ACTIONS: Tuple[str, ...] = ("refund", "payout")

# Minor units (cents).
PER_TRANSACTION_LIMITS_MINOR: Dict[str, int] = {
    "create_order": 50_000,
    "capture": 50_000,
    "refund": 50_000,
    "payout": 10_000,
}

# Rolling 24h ceilings, tracked per action so an incoming capture can never
# increase outgoing authorization.
WINDOW_24H_LIMITS_MINOR: Dict[str, int] = {
    "refund": 50_000,
    "payout": 20_000,
}
WINDOW_24H_SECONDS = 24 * 3600

# Rolling velocity ceiling, counted in logical operations (retries excluded).
VELOCITY_LIMIT_COUNT = 8
VELOCITY_WINDOW_SECONDS = 60

# Amount parsing guards.
MAX_AMOUNT_PRECISION = 2
MAX_AMOUNT_MINOR = 100_000_000  # $1,000,000.00 hard ceiling, overflow guard
MAX_REASON_CHARS = 200

# Operator approval lifetime.
APPROVAL_TTL_SECONDS = 900

# Bounded autonomy: a run may spend at most this much of the model / platform.
MAX_MODEL_CALLS = 3
MAX_STEPS = 8
MAX_PLATFORM_REQUESTS = 20
RUN_DEADLINE_SECONDS = 300

DB_PATH = os.getenv("PAYPILOT_DB", "paypilot-{}.sqlite3".format(MODE))
RECIPIENTS_PATH = os.getenv("PAYPILOT_RECIPIENTS", "recipients.json")

# Dispatch policy: bounded attempts, then a human decides.
MAX_DISPATCH_ATTEMPTS = 3

# How long a platform is assumed to remember a PayPal-Request-Id. The real
# retention window is NOT documented for every endpoint (architecture §3 marks
# payouts as "unknown - must verify"), so we treat this as a conservative local
# policy: inside the window an unknown outcome may be retried with the same key,
# outside it we fail closed and ask a human.
REQUEST_ID_RETENTION_SECONDS = 6 * 3600
