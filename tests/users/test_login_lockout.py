#!/usr/bin/env python
# -*- coding: utf-8 -*-

import threading

from freezegun import freeze_time

from CTFd.cache import cache
from CTFd.utils import set_config
from CTFd.utils.security.lockout import (
    get_lockout_state,
    lock_user,
    unlock_user,
)
from tests.helpers import create_ctfd, destroy_ctfd, login_as_user, register_user


def login(app, name="user", password="wrongpassword", email=None):
    """Perform one login POST and return (client, response)."""
    identifier = email or name
    with app.test_client() as client:
        client.get("/login")
        with client.session_transaction() as sess:
            nonce = sess["nonce"]
        resp = client.post(
            "/login",
            data={"name": identifier, "password": password, "nonce": nonce},
            follow_redirects=False,
        )
        return client, resp


def enable_lockout(app, threshold=3, duration=300):
    with app.app_context():
        set_config("login_lockout_enabled", True)
        set_config("login_lockout_threshold", threshold)
        set_config("login_lockout_duration", duration)


def test_lockout_disabled_by_default():
    """Without configuration, login behaves exactly as before."""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        for _ in range(6):
            _client, resp = login(app)
            assert resp.status_code == 200
            assert b"locked" not in resp.data

        # A correct password still logs in
        client, resp = login(app, password="password")
        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert sess.get("id") == 2
    destroy_ctfd(app)


def test_threshold_zero_disables_lockout():
    """A zero threshold is equivalent to switching the feature off."""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        enable_lockout(app, threshold=0)
        for _ in range(6):
            _client, resp = login(app)
            assert b"locked" not in resp.data
        client, resp = login(app, password="password")
        assert resp.status_code == 302
    destroy_ctfd(app)


def test_account_locks_after_threshold():
    """Consecutive failures up to the threshold lock the account."""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        enable_lockout(app, threshold=3, duration=300)

        _c, first = login(app)
        _c, second = login(app)
        _c, third = login(app)

        assert b"Your username or password is incorrect" in first.data
        assert b"Your username or password is incorrect" in second.data
        assert b"locked" in third.data
        assert b"Please try again in" in third.data

        state = get_lockout_state(2)
        assert state["locked"] is True
        assert state["failures"] == 3
        assert 0 < state["remaining"] <= 300
    destroy_ctfd(app)


def test_correct_password_blocked_while_locked():
    """Even the right password is rejected during an active lock."""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        enable_lockout(app, threshold=2, duration=300)

        login(app)
        login(app)

        client, resp = login(app, password="password")
        assert resp.status_code == 200
        assert b"locked" in resp.data
        with client.session_transaction() as sess:
            assert sess.get("id") is None
    destroy_ctfd(app)


def test_lock_expires_automatically():
    """The lock releases itself once the duration elapses."""
    app = create_ctfd()
    with freeze_time("2025-01-01 00:00:00") as frozen:
        with app.app_context():
            register_user(app)
            enable_lockout(app, threshold=2, duration=60)

            login(app)
            _c, locked_resp = login(app)
            assert b"locked" in locked_resp.data

            # Still locked a second before expiry
            frozen.tick(59)
            with app.app_context():
                assert get_lockout_state(2)["locked"] is True
            _c, blocked = login(app, password="password")
            assert b"locked" in blocked.data

            frozen.tick(2)
            with app.app_context():
                assert get_lockout_state(2)["locked"] is False
            client, resp = login(app, password="password")
            assert resp.status_code == 302
            with client.session_transaction() as sess:
                assert sess.get("id") == 2
    destroy_ctfd(app)


def test_successful_login_resets_failures():
    """Logging in clears the consecutive failure count."""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        enable_lockout(app, threshold=3, duration=300)

        login(app)
        login(app)

        # Success resets the streak
        client, resp = login(app, password="password")
        assert resp.status_code == 302
        assert get_lockout_state(2)["failures"] == 0

        # Two failures after the reset must not lock a threshold-3 account
        login(app)
        _c, resp = login(app)
        assert b"locked" not in resp.data
        assert get_lockout_state(2)["locked"] is False
    destroy_ctfd(app)


def test_lockout_isolated_per_account():
    """A lock on one account never affects another account."""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        register_user(
            app, name="user2", email="user2@examplectf.com", password="password"
        )
        enable_lockout(app, threshold=2, duration=300)

        login(app, name="user", email="user@examplectf.com")
        _c, resp = login(app, name="user", email="user@examplectf.com")
        assert b"locked" in resp.data

        # The second account shares the same source IP but is unaffected
        client, resp = login(
            app,
            name="user2",
            email="user2@examplectf.com",
            password="password",
        )
        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert sess.get("id") == 3
        assert get_lockout_state(3)["locked"] is False
    destroy_ctfd(app)


def test_nonexistent_accounts_are_not_counted():
    """Failures against unknown accounts don't create lockout state."""
    app = create_ctfd()
    with app.app_context():
        enable_lockout(app)
        for _ in range(5):
            _c, resp = login(app, name="nobody", email="nobody@example.com")
            assert b"Your username or password is incorrect" in resp.data
            assert b"locked" not in resp.data
    destroy_ctfd(app)


def test_atomic_inc_is_concurrency_safe():
    """Parallel increments must never lose a count, on any cache backend."""
    app = create_ctfd()
    with app.app_context():
        key = "test_atomic_inc_key"
        attempts = 20
        barrier = threading.Barrier(attempts)

        def worker():
            with app.app_context():
                barrier.wait()
                cache.atomic_inc(key, timeout=300)

        threads = [threading.Thread(target=worker) for _ in range(attempts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert int(cache.get(key)) == attempts
    destroy_ctfd(app)


def test_admin_lockout_api():
    """Admins can inspect, manually lock and unlock an account."""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        admin = login_as_user(app, name="admin", password="password")

        resp = admin.get("/api/v1/users/2/lockout")
        assert resp.status_code == 200
        assert resp.get_json()["data"]["locked"] is False

        resp = admin.post("/api/v1/users/2/lockout", json={"duration": 120})
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["locked"] is True
        assert 0 < data["remaining"] <= 120

        # The manual lock blocks login even with the correct password
        _c, login_resp = login(app, password="password")
        assert b"locked" in login_resp.data

        # Invalid durations are rejected
        resp = admin.post("/api/v1/users/2/lockout", json={"duration": -1})
        assert resp.status_code == 400

        resp = admin.delete("/api/v1/users/2/lockout", json="")
        assert resp.status_code == 200
        assert resp.get_json()["data"]["locked"] is False

        # Unlock is immediate
        client, login_resp = login(app, password="password")
        assert login_resp.status_code == 302
        with client.session_transaction() as sess:
            assert sess.get("id") == 2
    destroy_ctfd(app)


def test_manual_lock_works_when_lockout_disabled():
    """An administrator lock applies even when automatic lockout is off."""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        # Lockout stays disabled by default
        admin = login_as_user(app, name="admin", password="password")
        admin.post("/api/v1/users/2/lockout", json={})

        with app.app_context():
            assert get_lockout_state(2)["locked"] is True
        _c, resp = login(app, password="password")
        assert b"locked" in resp.data

        with app.app_context():
            unlock_user(2)
        client, resp = login(app, password="password")
        assert resp.status_code == 302
    destroy_ctfd(app)


def test_non_admin_cannot_access_lockout_api():
    """Only administrators may manage account locks."""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        user = login_as_user(app)

        # Browser style requests are redirected to the login page
        assert user.get("/api/v1/users/2/lockout").status_code == 302
        # API style requests get a 403
        assert user.post("/api/v1/users/2/lockout", json={}).status_code == 403
        assert user.delete("/api/v1/users/2/lockout", json="").status_code == 403
    destroy_ctfd(app)


def test_admin_pages_show_lockout_state():
    """The user detail and list pages expose the lock status."""
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        enable_lockout(app)
        admin = login_as_user(app, name="admin", password="password")

        with app.app_context():
            lock_user(2, duration=300)

        resp = admin.get("/admin/users/2")
        assert resp.status_code == 200
        assert b"locked" in resp.data
        assert b"Unlock User" in resp.data

        resp = admin.get("/admin/users")
        assert resp.status_code == 200
        assert b"locked" in resp.data
    destroy_ctfd(app)
