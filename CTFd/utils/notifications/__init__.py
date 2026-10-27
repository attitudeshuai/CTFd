import datetime

from sqlalchemy.exc import IntegrityError

from CTFd.models import NotificationRead, Notifications, db
from CTFd.utils import get_config
from CTFd.utils.helpers.models import build_model_filters

RETENTION_CONFIG_KEY = "notification_retention_days"


def get_retention_days():
    """
    Return the configured notification retention period as a positive int
    number of days. Returns None when retention is disabled (unset, empty or
    a non-positive value) in which case notifications are never purged.
    """
    value = get_config(RETENTION_CONFIG_KEY)
    if value is None:
        return None
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return days


def purge_expired_notifications():
    """
    Lazily delete notifications older than the configured retention period
    along with their read associations. When no retention period is
    configured this function performs no writes and returns 0.
    """
    days = get_retention_days()
    if days is None:
        return 0

    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    expired_ids = [
        row[0]
        for row in db.session.query(Notifications.id)
        .filter(Notifications.date < cutoff)
        .all()
    ]
    if not expired_ids:
        return 0

    NotificationRead.query.filter(
        NotificationRead.notification_id.in_(expired_ids)
    ).delete(synchronize_session=False)
    Notifications.query.filter(Notifications.id.in_(expired_ids)).delete(
        synchronize_session=False
    )
    db.session.commit()
    return len(expired_ids)


def build_notification_filters(query_args):
    """
    Consume the list-style filter arguments (q, field, since_id) out of the
    provided args dict and return SQLAlchemy filter expressions. This is the
    single filtering entry point shared by the notification list and unread
    count endpoints so that both calibers can never drift.
    """
    q = query_args.pop("q", None)
    field = str(query_args.pop("field", None))
    filters = build_model_filters(model=Notifications, query=q, field=field)

    since_id = query_args.pop("since_id", None)
    if since_id:
        filters.append(Notifications.id > since_id)

    return filters


def notification_query(query_args=None):
    if query_args is None:
        query_args = {}
    filters = build_notification_filters(query_args)
    return Notifications.query.filter_by(**query_args).filter(*filters)


def get_read_notification_ids(user_id):
    if user_id is None:
        return set()
    rows = (
        NotificationRead.query.filter_by(user_id=user_id)
        .with_entities(NotificationRead.notification_id)
        .all()
    )
    return {row[0] for row in rows}


def get_unread_count(user_id, query_args=None):
    """
    Unread notification count for the given user. The caliber matches the
    notification list exactly: notifications matching the filters without a
    NotificationRead association for this user. Anonymous users (user_id is
    None) see every matching notification as unread.
    """
    query = notification_query(query_args)
    if user_id is None:
        return query.count()

    read_exists = (
        NotificationRead.query.filter(
            NotificationRead.notification_id == Notifications.id,
            NotificationRead.user_id == user_id,
        )
        .with_entities(NotificationRead.id)
        .exists()
    )
    return query.filter(~read_exists).count()


def _insert_read_rows(user_id, notification_ids, retries_left=1):
    existing = {
        row[0]
        for row in NotificationRead.query.filter_by(user_id=user_id)
        .filter(NotificationRead.notification_id.in_(notification_ids))
        .with_entities(NotificationRead.notification_id)
        .all()
    }
    missing = [nid for nid in notification_ids if nid not in existing]
    if not missing:
        return

    db.session.add_all(
        [
            NotificationRead(notification_id=notification_id, user_id=user_id)
            for notification_id in missing
        ]
    )
    try:
        db.session.commit()
    except IntegrityError:
        # Concurrent requests (e.g. multiple tabs) may race on the unique
        # constraint. Re-check and insert the remaining missing rows once.
        db.session.rollback()
        if retries_left <= 0:
            raise
        _insert_read_rows(user_id, notification_ids, retries_left=retries_left - 1)


def mark_notifications_read(user_id, notification_ids=None):
    """
    Create the per-user read associations for the given notification ids,
    idempotently. Unknown or already purged ids are silently skipped. An
    empty/None ids list marks every existing notification as read for this
    user. Returns the authoritative unread count after marking.
    """
    if notification_ids:
        valid_ids = [
            row[0]
            for row in Notifications.query.filter(
                Notifications.id.in_(notification_ids)
            )
            .with_entities(Notifications.id)
            .all()
        ]
    else:
        valid_ids = [
            row[0] for row in Notifications.query.with_entities(Notifications.id).all()
        ]

    if valid_ids:
        _insert_read_rows(user_id, valid_ids)

    return get_unread_count(user_id)


def get_notification_read_counts(notification_ids=None):
    """
    Aggregated read counts per notification for the admin notifications
    page: {notification_id: distinct reader count}.
    """
    query = db.session.query(
        NotificationRead.notification_id,
        db.func.count(NotificationRead.id),
    ).group_by(NotificationRead.notification_id)
    if notification_ids is not None:
        query = query.filter(NotificationRead.notification_id.in_(notification_ids))
    return {row[0]: row[1] for row in query.all()}
