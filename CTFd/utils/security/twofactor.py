import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from urllib.parse import quote

from CTFd.cache import cache
from CTFd.models import UserRecoveryCodes, db

#: Length of a TOTP time step in seconds
TOTP_STEP = 30
#: Default number of digits in a TOTP code
TOTP_DIGITS = 6
#: Number of time steps accepted before/after the current one
TOTP_WINDOW = 1

#: Number of recovery codes issued when two factor authentication is enabled
RECOVERY_CODE_COUNT = 10
#: Number of unused recovery codes remaining before the user is warned
RECOVERY_CODE_LOW_COUNT = 2

#: Number of failed second factor attempts before the pending login is reset
LOGIN_ATTEMPT_LIMIT = 5
#: How long the failed attempt counter lives in the cache
LOGIN_ATTEMPT_TTL = 300

#: How long consumed TOTP counters are remembered for replay protection
_TOTP_REPLAY_TTL = TOTP_STEP * 3

_RECOVERY_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_RECOVERY_GROUP_SIZE = 4
_RECOVERY_GROUPS = 2


def generate_totp_secret(length=20):
    """
    Generate a new base32 encoded TOTP shared secret.
    The default of 20 random bytes produces a 32 character base32 string.
    """
    return base64.b32encode(os.urandom(length)).decode("utf-8").rstrip("=")


def _decode_totp_secret(secret):
    """
    Decode a base32 TOTP secret, tolerating missing padding and lowercase.
    Raises ValueError on malformed input.
    """
    if isinstance(secret, str) is False:
        raise ValueError("TOTP secret must be a string")
    padded = secret.strip().upper().replace(" ", "") + "=" * (-len(secret) % 8)
    return base64.b32decode(padded, casefold=True)


def _hotp(key, counter, digits=TOTP_DIGITS):
    """
    Compute an HOTP code (RFC 4226) for the given counter using HMAC-SHA1.
    """
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    token = binary % (10 ** digits)
    return str(token).zfill(digits)


def generate_totp(secret, for_time=None, digits=TOTP_DIGITS):
    """
    Generate the TOTP code (RFC 6238) for a secret at the given time.

    :param secret: base32 encoded shared secret
    :param for_time: epoch seconds to generate the code for; defaults to now
    :param digits: number of digits in the resulting code
    """
    if for_time is None:
        for_time = time.time()
    key = _decode_totp_secret(secret)
    counter = int(for_time) // TOTP_STEP
    return _hotp(key, counter, digits=digits)


def verify_totp(secret, token, for_time=None, window=TOTP_WINDOW):
    """
    Verify a TOTP code while allowing +/- ``window`` time steps of drift.
    Invalid secrets or tokens safely return False instead of raising.
    """
    try:
        if token is None:
            return False
        token = str(token).strip()
        if token.isdigit() is False or len(token) != TOTP_DIGITS:
            return False
        key = _decode_totp_secret(secret)
    except (ValueError, TypeError):
        return False

    if for_time is None:
        for_time = time.time()
    current_counter = int(for_time) // TOTP_STEP

    for counter in range(current_counter - window, current_counter + window + 1):
        if hmac.compare_digest(_hotp(key, counter), token):
            return True
    return False


def build_otpauth_uri(secret, account, issuer):
    """
    Build an otpauth:// URI suitable for QR provisioning in authenticator apps.
    """
    issuer_label = quote(str(issuer), safe="")
    account_label = quote(str(account), safe="")
    return (
        f"otpauth://totp/{issuer_label}:{account_label}"
        f"?secret={secret}&issuer={issuer_label}"
    )


def normalize_recovery_code(code):
    """
    Normalize a user supplied recovery code: uppercase, no separators or spaces.
    """
    return (
        str(code)
        .strip()
        .upper()
        .replace("-", "")
        .replace(" ", "")
    )


def format_recovery_code(code):
    """
    Format a normalized recovery code as XXXX-XXXX.
    """
    normalized = normalize_recovery_code(code)
    groups = [
        normalized[i : i + _RECOVERY_GROUP_SIZE]
        for i in range(0, len(normalized), _RECOVERY_GROUP_SIZE)
    ]
    return "-".join(groups[:_RECOVERY_GROUPS])


def hash_recovery_code(code):
    """
    Hash a recovery code for at rest storage. Recovery codes are high entropy
    so a plain SHA-256 of the normalized value provides sufficient protection.
    """
    normalized = normalize_recovery_code(code)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def generate_recovery_codes(count=RECOVERY_CODE_COUNT):
    """
    Generate a set of unique formatted one-time recovery codes.
    """
    codes = set()
    target_length = _RECOVERY_GROUP_SIZE * _RECOVERY_GROUPS
    while len(codes) < count:
        raw = "".join(
            secrets.choice(_RECOVERY_ALPHABET) for _ in range(target_length)
        )
        codes.add(format_recovery_code(raw))
    return list(codes)


def issue_recovery_codes(user, count=RECOVERY_CODE_COUNT):
    """
    Replace all existing recovery codes for a user with a freshly generated set.
    Returns the plaintext codes which are only available at issuance time.
    """
    # Delete ORM instances (instead of a bulk delete) so that the session
    # identity map does not collide with newly issued rows that reuse primary
    # keys after a replacement.
    existing_codes = UserRecoveryCodes.query.filter_by(user_id=user.id).all()
    for recovery_code in existing_codes:
        db.session.delete(recovery_code)
    db.session.flush()

    plaintext_codes = generate_recovery_codes(count=count)
    for code in plaintext_codes:
        db.session.add(
            UserRecoveryCodes(user_id=user.id, code_hash=hash_recovery_code(code))
        )
    db.session.commit()
    return plaintext_codes


def consume_recovery_code(user, code):
    """
    Consume a recovery code for a user. Returns the now-used code row on success
    and None when the code is missing, malformed, unknown or already consumed.
    """
    normalized = normalize_recovery_code(code)
    expected_length = _RECOVERY_GROUP_SIZE * _RECOVERY_GROUPS
    if len(normalized) != expected_length:
        return None

    recovery_code = UserRecoveryCodes.query.filter_by(
        user_id=user.id, code_hash=hash_recovery_code(normalized), used=False
    ).first()
    if recovery_code is None:
        return None

    recovery_code.used = True
    db.session.commit()
    return recovery_code


def count_remaining_recovery_codes(user):
    """
    Count the recovery codes that a user has not yet consumed.
    """
    return UserRecoveryCodes.query.filter_by(user_id=user.id, used=False).count()


TWO_FACTOR_POLICIES = ("off", "admins", "all")


def get_two_factor_requirement():
    """
    Return the currently configured two factor enforcement policy:
    "off", "admins" or "all". Defaults to "off".
    """
    from CTFd.utils import get_config

    policy = get_config("two_factor_required")
    if policy in ("admins", "all"):
        return policy
    return "off"


def two_factor_required_for_user(user):
    """
    Whether the given user must enroll in two factor authentication under the
    currently configured policy. OAuth-only accounts (no password) are exempt.
    """
    if user is None or user.password is None:
        return False
    policy = get_two_factor_requirement()
    if policy == "all":
        return True
    if policy == "admins":
        return getattr(user, "type", None) == "admin"
    return False


def _used_totp_counters_key(user_id):
    return f"totp_used_counters_user_{user_id}"


def verify_and_consume_totp(user, code, for_time=None, window=TOTP_WINDOW):
    """
    Verify a TOTP code during login while preventing code replay within the
    validity window. Successfully consumed time counters are remembered in the
    cache so the same code cannot complete a second login until it expires.
    """
    try:
        code = str(code).strip()
        if code.isdigit() is False or len(code) != TOTP_DIGITS:
            return False
        key = _decode_totp_secret(user.totp_secret)
    except (ValueError, TypeError):
        return False

    if for_time is None:
        for_time = time.time()
    current_counter = int(for_time) // TOTP_STEP

    cache_key = _used_totp_counters_key(user.id)
    used_counters = set(cache.get(cache_key) or [])
    # Drop counters that can no longer be valid in the current acceptance window
    used_counters = {c for c in used_counters if c >= current_counter - window}

    for counter in range(current_counter - window, current_counter + window + 1):
        if counter in used_counters:
            continue
        if hmac.compare_digest(_hotp(key, counter), code):
            used_counters.add(counter)
            cache.set(cache_key, list(used_counters), timeout=_TOTP_REPLAY_TTL)
            return True
    return False


def register_failed_second_factor(attempt_key):
    """
    Record a failed second factor attempt against a pending login key.
    Returns the number of failed attempts seen within the tracking window.
    """
    cache_key = f"two_factor_attempts_{attempt_key}"
    attempts = cache.get(cache_key)
    if attempts is None:
        cache.set(cache_key, 1, timeout=LOGIN_ATTEMPT_TTL)
        return 1
    attempts = int(attempts) + 1
    cache.set(cache_key, attempts, timeout=LOGIN_ATTEMPT_TTL)
    return attempts


def clear_failed_second_factor(attempt_key):
    """
    Clear the failed attempt counter for a pending login key.
    """
    cache.delete(f"two_factor_attempts_{attempt_key}")
