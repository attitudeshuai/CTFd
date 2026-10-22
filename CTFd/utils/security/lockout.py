import time

from flask_babel import ngettext

from CTFd.cache import cache
from CTFd.utils import get_config

#: Config keys for account level login lockout
LOGIN_LOCKOUT_ENABLED = "login_lockout_enabled"
LOGIN_LOCKOUT_THRESHOLD = "login_lockout_threshold"
LOGIN_LOCKOUT_DURATION = "login_lockout_duration"

#: Conservative defaults; the feature itself is disabled unless turned on
DEFAULT_LOGIN_LOCKOUT_THRESHOLD = 5
DEFAULT_LOGIN_LOCKOUT_DURATION = 900


def _failure_count_key(user_id):
    return f"login_failure_count_user_{user_id}"


def _locked_until_key(user_id):
    return f"login_locked_until_user_{user_id}"


def _last_failure_key(user_id):
    return f"login_failure_time_user_{user_id}"


def get_lockout_settings():
    """
    Return ``(enabled, threshold, duration)`` for account level login lockout.

    The feature is considered disabled unless the switch is on and both the
    failure threshold and lock duration are positive integers.
    """
    enabled = bool(get_config(LOGIN_LOCKOUT_ENABLED, default=False))
    threshold = get_config(
        LOGIN_LOCKOUT_THRESHOLD, default=DEFAULT_LOGIN_LOCKOUT_THRESHOLD
    )
    duration = get_config(
        LOGIN_LOCKOUT_DURATION, default=DEFAULT_LOGIN_LOCKOUT_DURATION
    )
    try:
        threshold = int(threshold)
        duration = int(duration)
    except (TypeError, ValueError):
        return False, 0, 0
    return enabled and threshold > 0 and duration > 0, threshold, duration


def is_lockout_enabled():
    return get_lockout_settings()[0]


def clear_login_failures(user_id):
    """Reset the consecutive failure bookkeeping for an account."""
    cache.delete(_failure_count_key(user_id))
    cache.delete(_last_failure_key(user_id))
    cache.delete(_locked_until_key(user_id))


def get_lockout_state(user_id):
    """
    Return the current lockout state for an account:

    ``locked``, ``remaining`` seconds, ``locked_until`` timestamp, consecutive
    ``failures`` and ``last_failure`` timestamp.

    Lock keys carry a TTL so locks release automatically. A timestamp that has
    lapsed while the key still lingers is treated as expired and cleaned up.
    """
    now = int(time.time())
    locked_until = cache.get(_locked_until_key(user_id))
    failures = cache.get(_failure_count_key(user_id))
    last_failure = cache.get(_last_failure_key(user_id))

    failures = int(failures or 0)

    if locked_until is not None and int(locked_until) <= now:
        # The lock expired but stale keys remain. Start from a clean slate.
        clear_login_failures(user_id)
        locked_until = None
        failures = 0
        last_failure = None

    locked = locked_until is not None
    remaining = max(0, int(locked_until) - now) if locked else 0

    return {
        "locked": locked,
        "remaining": remaining,
        "locked_until": int(locked_until) if locked else None,
        "failures": failures,
        "last_failure": int(last_failure) if last_failure else None,
    }


def record_login_failure(user_id):
    """
    Record a consecutive failed login for an account and lock it once the
    configured threshold is reached. Returns the resulting lockout state.

    Does nothing when automatic lockout is disabled.
    """
    enabled, threshold, duration = get_lockout_settings()
    if enabled is False:
        return get_lockout_state(user_id)

    now = int(time.time())
    failures = cache.atomic_inc(_failure_count_key(user_id), timeout=duration)
    cache.set(_last_failure_key(user_id), now, timeout=duration)
    if failures >= threshold:
        locked_until = now + duration
        cache.set(_locked_until_key(user_id), locked_until, timeout=duration)
    return get_lockout_state(user_id)


def lock_user(user_id, duration=None):
    """
    Manually lock an account on behalf of an administrator. Works even when
    automatic lockout is switched off. Returns the resulting lockout state.
    """
    if duration is None:
        _enabled, _threshold, configured_duration = get_lockout_settings()
        duration = configured_duration or DEFAULT_LOGIN_LOCKOUT_DURATION

    duration = int(duration)
    if duration <= 0:
        return get_lockout_state(user_id)

    now = int(time.time())
    locked_until = now + duration
    cache.set(_locked_until_key(user_id), locked_until, timeout=duration)

    # Keep the counters consistent with the locked state
    _enabled, threshold, _duration = get_lockout_settings()
    count = threshold if threshold > 0 else DEFAULT_LOGIN_LOCKOUT_THRESHOLD
    cache.set(_failure_count_key(user_id), count, timeout=duration)
    cache.set(_last_failure_key(user_id), now, timeout=duration)
    return get_lockout_state(user_id)


def unlock_user(user_id):
    """Manually release an account lock on behalf of an administrator."""
    clear_login_failures(user_id)
    return get_lockout_state(user_id)


def format_lockout_duration(seconds):
    """Render a remaining lock duration as a human readable string."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    parts = []
    if hours:
        parts.append(ngettext("%(num)d hour", "%(num)d hours", hours) % {"num": hours})
    if minutes:
        parts.append(
            ngettext("%(num)d minute", "%(num)d minutes", minutes) % {"num": minutes}
        )
    if secs or not parts:
        parts.append(
            ngettext("%(num)d second", "%(num)d seconds", secs) % {"num": secs}
        )
    return " ".join(parts)


def locked_out_message(remaining):
    from flask_babel import gettext

    return gettext(
        "Your account has been locked due to too many failed login attempts. "
        "Please try again in %(time)s or contact an administrator.",
        time=format_lockout_duration(remaining),
    )
