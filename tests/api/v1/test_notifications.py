#!/usr/bin/env python
# -*- coding: utf-8 -*-
import datetime

from CTFd.models import NotificationRead, Notifications
from CTFd.utils import set_config
from tests.helpers import (
    create_ctfd,
    destroy_ctfd,
    gen_challenge,
    gen_notification,
    login_as_user,
    register_user,
)


def test_api_notifications_get():
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        gen_notification(app.db)
        with login_as_user(app) as client:
            # test_api_notifications_get
            """Can the users get /api/v1/notifications"""
            r = client.get("/api/v1/notifications", json="")
            assert r.status_code == 200
            assert len(r.get_json()["data"]) == 1
            assert r.get_json()["data"][0]["read"] is False

            # test_api_get_notification_detail
            r = client.get("/api/v1/notifications/1", json="")
            assert r.status_code == 200
            resp = r.get_json()
            assert resp["data"]["title"] == "title"
            assert resp["data"]["content"] == "content"
            assert resp["data"]["read"] is False

            # test_api_notifications_post_non_admin
            """Can the users post /api/v1/notifications if not admin"""
            r = client.post("/api/v1/notifications", json="")
            assert r.status_code == 403
    destroy_ctfd(app)


def test_api_notifications_post_admin():
    """Can the users post /api/v1/notifications if admin"""
    app = create_ctfd()
    with app.app_context():
        gen_challenge(app.db)
        with login_as_user(app, name="admin") as client:
            r = client.post(
                "/api/v1/notifications", json={"title": "title", "content": "content"}
            )
            assert r.status_code == 200
    destroy_ctfd(app)


def test_api_delete_notifications_by_admin():
    """Test that an admin can delete notifications"""
    app = create_ctfd()
    with app.app_context():
        gen_challenge(app.db)
        gen_notification(app.db)
        assert Notifications.query.count() == 1
        with login_as_user(app, name="admin") as client:
            r = client.delete("/api/v1/notifications/1", json="")
            assert r.status_code == 200
            assert r.get_json()["success"] is True
        assert Notifications.query.count() == 0
    destroy_ctfd(app)


def test_api_delete_notifications_by_user():
    """Test that a non-admin cannot delete notifications"""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        gen_challenge(app.db)
        gen_notification(app.db)
        assert Notifications.query.count() == 1
        with login_as_user(app) as client:
            r = client.delete("/api/v1/notifications/1", json="")
            assert r.status_code == 403
        assert Notifications.query.count() == 1
    destroy_ctfd(app)


def _register_two_users(app):
    register_user(app)
    register_user(app, name="user2", email="user2@examplectf.com", password="password")


def _list_and_count(client):
    r = client.get("/api/v1/notifications")
    items = r.get_json()["data"]
    h = client.head("/api/v1/notifications")
    count = int(h.headers["Result-Count"])
    return items, count


def test_api_notifications_read_flag_is_per_user():
    """The read flag and unread count are per user"""
    app = create_ctfd()
    with app.app_context():
        _register_two_users(app)
        gen_notification(app.db)
        gen_notification(app.db)

        with login_as_user(app) as user1, login_as_user(app, "user2") as user2:
            r = user1.post("/api/v1/notifications/1/read", json={})
            assert r.status_code == 200

            items1, count1 = _list_and_count(user1)
            reads1 = {item["id"]: item["read"] for item in items1}
            assert reads1 == {1: True, 2: False}
            assert count1 == 1

            items2, count2 = _list_and_count(user2)
            assert all(item["read"] is False for item in items2)
            assert count2 == 2
    destroy_ctfd(app)


def test_api_notifications_head_matches_list_three_states():
    """HEAD unread count matches the list caliber in all/read/partial states"""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        for _ in range(3):
            gen_notification(app.db)

        with login_as_user(app) as client:
            # All unread
            items, count = _list_and_count(client)
            assert count == 3
            assert count == len([i for i in items if i["read"] is False])

            # Partially read
            client.post("/api/v1/notifications/1/read", json={})
            items, count = _list_and_count(client)
            assert count == 2
            assert count == len([i for i in items if i["read"] is False])

            # All read
            client.post("/api/v1/notifications/read", json={})
            items, count = _list_and_count(client)
            assert count == 0
            assert count == len([i for i in items if i["read"] is False])
            assert all(i["read"] is True for i in items)
    destroy_ctfd(app)


def test_api_notification_mark_one_is_idempotent():
    """Repeatedly marking one notification creates a single association"""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        gen_notification(app.db)

        with login_as_user(app) as client:
            r1 = client.post("/api/v1/notifications/1/read", json={})
            r2 = client.post("/api/v1/notifications/1/read", json={})
            assert r1.get_json()["data"]["unread_count"] == 0
            assert r2.get_json()["data"]["unread_count"] == 0

            missing = client.post("/api/v1/notifications/999/read", json={})
            assert missing.status_code == 404

        assert NotificationRead.query.count() == 1
    destroy_ctfd(app)


def test_api_notification_mark_batch_and_skip_unknown_ids():
    """Batch marking only affects existing ids; unknown ids are ignored"""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        gen_notification(app.db)
        gen_notification(app.db)
        gen_notification(app.db)

        with login_as_user(app) as client:
            r = client.post("/api/v1/notifications/read", json={"ids": [1, 3, 999]})
            assert r.status_code == 200
            assert r.get_json()["data"]["unread_count"] == 1
            items, count = _list_and_count(client)
            assert count == 1
            assert {i["id"] for i in items if i["read"]} == {1, 3}
        assert NotificationRead.query.count() == 2
    destroy_ctfd(app)


def test_api_notification_mark_invalid_ids_payload():
    """A non-list ids payload is rejected with 400"""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        gen_notification(app.db)
        with login_as_user(app) as client:
            r = client.post("/api/v1/notifications/read", json={"ids": "1"})
            assert r.status_code == 400
            r = client.post("/api/v1/notifications/read", json={"ids": ["x"]})
            assert r.status_code == 400
    destroy_ctfd(app)


def test_api_mark_notifications_anonymous_forbidden():
    """Anonymous users cannot mark notifications read"""
    app = create_ctfd()
    with app.app_context():
        gen_notification(app.db)
        with app.test_client() as client:
            assert client.post("/api/v1/notifications/1/read").status_code == 403
            assert client.post("/api/v1/notifications/read").status_code == 403
            # Anonymous listing still works and everything reads as unread
            items = client.get("/api/v1/notifications").get_json()["data"]
            assert all(item["read"] is False for item in items)
            h = client.head("/api/v1/notifications")
            assert int(h.headers["Result-Count"]) == 1
    destroy_ctfd(app)


def test_api_notification_read_isolation_between_users():
    """One user reading everything does not affect another user"""
    app = create_ctfd()
    with app.app_context():
        _register_two_users(app)
        gen_notification(app.db)
        gen_notification(app.db)

        with login_as_user(app) as user1, login_as_user(app, "user2") as user2:
            user1.post("/api/v1/notifications/read", json={})
            _, count1 = _list_and_count(user1)
            _, count2 = _list_and_count(user2)
            assert count1 == 0
            assert count2 == 2
        assert NotificationRead.query.filter_by(user_id=2).count() == 2
        assert NotificationRead.query.filter_by(user_id=3).count() == 0
    destroy_ctfd(app)


def test_api_notification_delete_cascades_reads():
    """Deleting a notification removes its read associations"""
    app = create_ctfd()
    with app.app_context():
        _register_two_users(app)
        gen_notification(app.db)
        with login_as_user(app) as user1, login_as_user(app, "user2") as user2:
            user1.post("/api/v1/notifications/1/read", json={})
            user2.post("/api/v1/notifications/1/read", json={})
        assert NotificationRead.query.count() == 2

        with login_as_user(app, name="admin") as admin:
            r = admin.delete("/api/v1/notifications/1", json="")
            assert r.status_code == 200

        assert Notifications.query.count() == 0
        assert NotificationRead.query.count() == 0
    destroy_ctfd(app)


def test_api_notification_publish_carries_per_user_unread_count():
    """SSE fan-out attaches each subscriber's server-computed unread count"""
    app = create_ctfd()
    with app.app_context():
        _register_two_users(app)
        gen_notification(app.db)
        gen_notification(app.db)
        app.db.session.add(NotificationRead(notification_id=1, user_id=2))
        app.db.session.commit()

        manager = app.events_manager
        gen_user2 = manager.subscribe(user_id=2)
        gen_admin = manager.subscribe(user_id=1)
        next(gen_user2)
        next(gen_admin)

        with login_as_user(app, name="admin") as admin:
            r = admin.post(
                "/api/v1/notifications",
                json={"title": "fresh", "content": "body", "type": "toast"},
            )
            assert r.status_code == 200

        messages = {}
        for client in list(manager.clients.values()):
            message = client["queue"]["ctf"].get_nowait()
            messages[client["user_id"]] = message

        assert messages[2]["type"] == "notification"
        assert messages[1]["type"] == "notification"
        # User 2 read notification 1, so only 2 and 3 are unread
        assert messages[2]["data"]["unread_count"] == 2
        # Admin has read nothing
        assert messages[1]["data"]["unread_count"] == 3
        # Existing payload fields are preserved
        for message in messages.values():
            for key in ("id", "title", "content", "date", "html", "type"):
                assert key in message["data"]

        gen_user2.close()
        gen_admin.close()
    destroy_ctfd(app)


def test_notification_retention_disabled_keeps_old_notifications():
    """Without a configured retention period nothing is ever purged"""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        old = Notifications(
            title="old",
            content="ancient",
            date=datetime.datetime.utcnow() - datetime.timedelta(days=400),
        )
        app.db.session.add(old)
        app.db.session.commit()

        with login_as_user(app) as client:
            items, _ = _list_and_count(client)
            assert len(items) == 1
            assert items[0]["title"] == "old"
        assert Notifications.query.count() == 1
    destroy_ctfd(app)


def test_notification_retention_purges_on_list_and_count():
    """Expired notifications and their reads vanish from list and count"""
    app = create_ctfd()
    with app.app_context():
        _register_two_users(app)
        recent = Notifications(title="recent", content="new")
        old = Notifications(
            title="old",
            content="ancient",
            date=datetime.datetime.utcnow() - datetime.timedelta(days=10),
        )
        app.db.session.add_all([recent, old])
        app.db.session.commit()
        app.db.session.add_all(
            [
                NotificationRead(notification_id=recent.id, user_id=2),
                NotificationRead(notification_id=old.id, user_id=2),
                NotificationRead(notification_id=old.id, user_id=3),
            ]
        )
        app.db.session.commit()

        set_config("notification_retention_days", 7)

        with login_as_user(app) as client, login_as_user(app, "user2") as client2:
            # The GET lazily purges the expired notification
            items, count = _list_and_count(client)
            titles = [item["title"] for item in items]
            assert "old" not in titles
            assert "recent" in titles
            # User 2 already read the surviving notification
            assert count == 0
            # User 3 has not read anything and sees the recent one as unread
            _, count2 = _list_and_count(client2)
            assert count2 == 1

        assert Notifications.query.count() == 1
        # The old notification's read associations were purged; recent's kept
        surviving_id = Notifications.query.filter_by(title="recent").first().id
        read_rows = NotificationRead.query.all()
        assert len(read_rows) == 1
        assert read_rows[0].notification_id == surviving_id
    destroy_ctfd(app)


def test_notification_retention_purges_on_publish():
    """Publishing a notification also triggers expiry cleanup"""
    app = create_ctfd()
    with app.app_context():
        old = Notifications(
            title="old",
            content="ancient",
            date=datetime.datetime.utcnow() - datetime.timedelta(days=30),
        )
        app.db.session.add(old)
        app.db.session.commit()
        set_config("notification_retention_days", 7)

        with login_as_user(app, name="admin") as admin:
            r = admin.post(
                "/api/v1/notifications", json={"title": "new", "content": "x"}
            )
            assert r.status_code == 200

        titles = {n.title for n in Notifications.query.all()}
        assert titles == {"new"}
    destroy_ctfd(app)


def test_api_delete_user_cascades_notification_reads():
    """Deleting a user removes all of their read associations"""
    app = create_ctfd()
    with app.app_context():
        _register_two_users(app)
        gen_notification(app.db)
        with login_as_user(app) as user1, login_as_user(app, "user2") as user2:
            user1.post("/api/v1/notifications/1/read", json={})
            user2.post("/api/v1/notifications/1/read", json={})
        assert NotificationRead.query.count() == 2

        with login_as_user(app, name="admin") as admin:
            r = admin.delete("/api/v1/users/2", json="")
            assert r.status_code == 200

        # The broadcast notification survives but user 2's read row is gone
        assert Notifications.query.count() == 1
        assert NotificationRead.query.count() == 1
        assert NotificationRead.query.filter_by(user_id=2).count() == 0
    destroy_ctfd(app)
