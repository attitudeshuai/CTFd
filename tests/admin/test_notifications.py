#!/usr/bin/env python
# -*- coding: utf-8 -*-
from CTFd.models import NotificationRead, Notifications
from CTFd.utils import set_config
from tests.helpers import (
    create_ctfd,
    destroy_ctfd,
    gen_notification,
    login_as_user,
    register_user,
)


def test_admin_notifications_page_shows_read_counts():
    """The admin notifications page displays per-notification reader counts"""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        register_user(
            app, name="user2", email="user2@examplectf.com", password="password"
        )
        gen_notification(app.db)
        gen_notification(app.db)

        with login_as_user(app) as user1, login_as_user(app, "user2") as user2:
            user1.post("/api/v1/notifications/1/read", json={})
            user2.post("/api/v1/notifications/1/read", json={})

        with login_as_user(app, name="admin") as admin:
            page = admin.get("/admin/notifications").get_data(as_text=True)
            assert "2 users have read this notification" in page
            assert "0 users have read this notification" in page
            assert 'name="notification_retention_days"' in page
    destroy_ctfd(app)


def test_admin_notifications_page_shows_retention_value():
    """The configured retention period is pre-filled on the admin page"""
    app = create_ctfd()
    with app.app_context():
        set_config("notification_retention_days", 30)
        with login_as_user(app, name="admin") as admin:
            page = admin.get("/admin/notifications").get_data(as_text=True)
            assert 'value="30"' in page
    destroy_ctfd(app)


def test_admin_notifications_non_admin_forbidden():
    """Regular users cannot access the admin notifications page"""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        with login_as_user(app) as client:
            r = client.get("/admin/notifications")
            # Browser (non-JSON) requests are redirected to the login page
            assert r.status_code == 302
            r = client.get("/admin/notifications", json="")
            assert r.status_code == 403
    destroy_ctfd(app)


def test_admin_reset_notifications_clears_reads():
    """Resetting notifications also clears all read associations"""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        gen_notification(app.db)

        user = login_as_user(app)
        user.post("/api/v1/notifications/1/read", json={})
        assert Notifications.query.count() == 1
        assert NotificationRead.query.count() == 1

        admin = login_as_user(app, name="admin")
        with admin.session_transaction() as sess:
            data = {"nonce": sess.get("nonce"), "notifications": "on"}
            r = admin.post("/admin/reset", data=data)
            assert r.status_code == 302

        assert Notifications.query.count() == 0
        assert NotificationRead.query.count() == 0
    destroy_ctfd(app)
