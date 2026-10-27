from flask import render_template

from CTFd.admin import admin
from CTFd.models import Notifications
from CTFd.utils.decorators import admins_only
from CTFd.utils.notifications import (
    get_notification_read_counts,
    get_retention_days,
    purge_expired_notifications,
)


@admin.route("/admin/notifications")
@admins_only
def notifications():
    purge_expired_notifications()
    notifs = Notifications.query.order_by(Notifications.id.desc()).all()
    read_counts = get_notification_read_counts(
        notification_ids=[notif.id for notif in notifs]
    )
    retention_days = get_retention_days()
    return render_template(
        "admin/notifications.html",
        notifications=notifs,
        read_counts=read_counts,
        retention_days=retention_days if retention_days is not None else "",
    )
