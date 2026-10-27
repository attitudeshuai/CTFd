import glob
import os
import re
import time

from freezegun import freeze_time

from CTFd.models import UserRecoveryCodes, Users
from CTFd.utils import set_config
from CTFd.utils.security.twofactor import (
    LOGIN_ATTEMPT_LIMIT,
    RECOVERY_CODE_COUNT,
    TOTP_STEP,
    build_otpauth_uri,
    consume_recovery_code,
    count_remaining_recovery_codes,
    format_recovery_code,
    generate_recovery_codes,
    generate_totp,
    generate_totp_secret,
    hash_recovery_code,
    issue_recovery_codes,
    normalize_recovery_code,
    verify_totp,
)
from tests.helpers import (
    create_ctfd,
    destroy_ctfd,
    gen_user,
    login_as_user,
    register_user,
)

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


# ---- TOTP primitives -------------------------------------------------------


def test_totp_rfc6238_vectors():
    """TOTP generation matches the RFC 6238 algorithm reference values"""
    for timestamp, expected_8 in RFC_VECTORS:
        assert generate_totp(RFC_SECRET, for_time=timestamp, digits=8) == expected_8
        assert generate_totp(RFC_SECRET, for_time=timestamp) == expected_8[-6:]


def test_totp_verification_window():
    """verify_totp accepts +/-1 time step and rejects +/-2"""
    secret = generate_totp_secret()
    with freeze_time("2026-01-01 12:00:00"):
        current = generate_totp(secret)
        assert verify_totp(secret, current)

        token = generate_totp(secret, for_time=1767268800 + TOTP_STEP)
        assert verify_totp(secret, token, for_time=1767268800)

        token = generate_totp(secret, for_time=1767268800 + 2 * TOTP_STEP)
        assert verify_totp(secret, token, for_time=1767268800) is False

        assert verify_totp(secret, "000000") is False
        assert verify_totp(secret, "not-a-code") is False
        assert verify_totp(secret, "") is False


def test_totp_verification_normalizes_whitespace():
    secret = generate_totp_secret()
    with freeze_time("2026-01-01 12:00:00"):
        token = generate_totp(secret)
        assert verify_totp(secret, " {} ".format(token))


def test_otpauth_uri():
    uri = build_otpauth_uri("JBSWY3DPEHPK3PXP", "alice", "My CTF")
    assert uri.startswith("otpauth://totp/My%20CTF:alice?")
    assert "secret=JBSWY3DPEHPK3PXP" in uri
    assert "issuer=My%20CTF" in uri


# ---- Recovery codes --------------------------------------------------------


def test_recovery_code_format():
    codes = generate_recovery_codes()
    assert len(codes) == RECOVERY_CODE_COUNT
    assert len(set(codes)) == RECOVERY_CODE_COUNT
    for code in codes:
        groups = code.split("-")
        assert len(groups) == 2
        assert all(len(group) == 4 for group in groups)
        normalized = normalize_recovery_code(code)
        assert format_recovery_code(normalized) == code
        assert hash_recovery_code(code) != code
        assert hash_recovery_code(code) == hash_recovery_code(normalized.lower())


def test_recovery_code_issuance_and_consumption():
    app = create_ctfd()
    with app.app_context():
        user = gen_user(app.db, name="2fauser", email="2fauser@examplectf.com")
        plaintext_codes = issue_recovery_codes(user)
        assert len(plaintext_codes) == RECOVERY_CODE_COUNT
        assert count_remaining_recovery_codes(user) == RECOVERY_CODE_COUNT
        for code in plaintext_codes:
            stored = UserRecoveryCodes.query.filter_by(user_id=user.id).all()
            assert code not in [row.code_hash for row in stored]

        first_code = plaintext_codes[0]
        consumed = consume_recovery_code(user, first_code)
        assert consumed is not None
        assert consumed.used is True
        assert count_remaining_recovery_codes(user) == RECOVERY_CODE_COUNT - 1
        assert consume_recovery_code(user, first_code) is None
        assert count_remaining_recovery_codes(user) == RECOVERY_CODE_COUNT - 1

        second_code = plaintext_codes[1].lower().replace("-", "")
        assert consume_recovery_code(user, second_code) is not None

        assert consume_recovery_code(user, "ZZZZ-ZZZZ") is None
        assert consume_recovery_code(user, "") is None

        new_codes = issue_recovery_codes(user)
        assert len(new_codes) == RECOVERY_CODE_COUNT
        assert consume_recovery_code(user, plaintext_codes[2]) is None
        assert consume_recovery_code(user, new_codes[0]) is not None
    destroy_ctfd(app)


def test_totp_secret_column_exists():
    app = create_ctfd()
    with app.app_context():
        assert hasattr(Users, "totp_secret")
        user = gen_user(app.db, name="secretuser", email="secretuser@examplectf.com")
        assert user.totp_secret is None
        user.totp_secret = generate_totp_secret()
        app.db.session.commit()
        fetched = Users.query.filter_by(name="secretuser").first()
        assert fetched.totp_secret is not None
    destroy_ctfd(app)


def test_migration_chain_has_single_new_head():
    migrations_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "migrations", "versions")
    )
    revisions = {}
    for path in glob.glob(os.path.join(migrations_dir, "*.py")):
        text = open(path, encoding="utf-8").read()
        revision = re.search(r'revision = "([^"]+)"', text)
        down_revision = re.search(r'down_revision = "([^"]+)"', text)
        if revision and down_revision:
            revisions[revision.group(1)] = down_revision.group(1)
        elif revision:
            revisions[revision.group(1)] = None
    heads = [rev for rev in revisions if rev not in revisions.values()]
    assert "d2f4a8c1b9e3" in heads
    assert len(heads) == 1
    assert revisions["d2f4a8c1b9e3"] == "a3e1c5d7b9f2"


# ---- Helpers ---------------------------------------------------------------


def _enroll_user(app, name="user2fa", secret=None, issue_codes=True):
    with app.app_context():
        user = gen_user(
            app.db,
            name,
            "{}@examplectf.com".format(name),
            "password",
        )
        if secret is None:
            secret = generate_totp_secret()
        user.totp_secret = secret
        app.db.session.commit()
        codes = issue_recovery_codes(user) if issue_codes else []
        user_id = user.id
    return user_id, secret, codes


def _begin_login(client, name="user2fa", password="password"):
    client.get("/login")
    with client.session_transaction() as sess:
        nonce = sess.get("nonce")
    response = client.post(
        "/login",
        data={"name": name, "password": password, "nonce": nonce},
    )
    return response, nonce


def _post_two_factor(client, code, nonce):
    return client.post(
        "/login/two_factor",
        data={"code": code, "nonce": nonce},
    )


def _settings_client(app):
    register_user(app)
    return login_as_user(app)


def _get_nonce(client):
    with client.session_transaction() as sess:
        return sess.get("nonce")


# ---- Login flow ------------------------------------------------------------


def test_login_without_two_factor_is_unchanged():
    app = create_ctfd()
    with app.app_context():
        register_user(app)
    client = login_as_user(app)
    with client.session_transaction() as sess:
        assert sess.get("id")
        assert sess.get("hash")
        assert "two_factor_user_id" not in sess
    destroy_ctfd(app)


def test_login_requires_second_step_when_enabled():
    app = create_ctfd()
    _enroll_user(app)
    with app.app_context():
        client = app.test_client()
        response, nonce = _begin_login(client)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/login/two_factor")
        with client.session_transaction() as sess:
            assert "id" not in sess
            assert "hash" not in sess
            assert sess["two_factor_user_id"]

        assert client.get("/login/two_factor").status_code == 200

        client.get("/logout")
        response = client.get("/login/two_factor")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/login")
    destroy_ctfd(app)


def test_login_with_current_totp_succeeds():
    app = create_ctfd()
    user_id, secret, _ = _enroll_user(app)
    with app.app_context():
        client = app.test_client()
        _, nonce = _begin_login(client)
        response = _post_two_factor(client, generate_totp(secret), nonce)
        assert response.status_code == 302
        with client.session_transaction() as sess:
            assert sess["id"] == user_id
            assert "two_factor_user_id" not in sess
    destroy_ctfd(app)


def test_login_totp_time_window():
    frozen = "2026-03-01 09:00:00"
    app = create_ctfd()
    user_id, secret, _ = _enroll_user(app)
    with app.app_context():
        with freeze_time(frozen):
            now = time.time()
            client = app.test_client()
            _, nonce = _begin_login(client)
            previous = generate_totp(secret, for_time=now - TOTP_STEP)
            response = _post_two_factor(client, previous, nonce)
            assert response.status_code == 302
            with client.session_transaction() as sess:
                assert sess["id"] == user_id

    app2 = create_ctfd()
    user_id2, secret2, _ = _enroll_user(app2)
    with app2.app_context():
        with freeze_time(frozen):
            now = time.time()
            client = app2.test_client()
            _, nonce = _begin_login(client)
            far_old = generate_totp(secret2, for_time=now - 2 * TOTP_STEP)
            response = _post_two_factor(client, far_old, nonce)
            assert response.status_code == 200
            with client.session_transaction() as sess:
                assert "id" not in sess

            response = _post_two_factor(client, "000000", nonce)
            assert response.status_code == 200
            with client.session_transaction() as sess:
                assert "id" not in sess
    destroy_ctfd(app)
    destroy_ctfd(app2)


def test_totp_code_cannot_be_replayed():
    app = create_ctfd()
    user_id, secret, _ = _enroll_user(app)
    with app.app_context():
        client = app.test_client()
        with freeze_time("2026-03-01 09:00:00"):
            _, nonce = _begin_login(client)
            code = generate_totp(secret)
            first = _post_two_factor(client, code, nonce)
            assert first.status_code == 302

        client.get("/logout")

        with freeze_time("2026-03-01 09:00:15"):
            _, nonce = _begin_login(client)
            second = _post_two_factor(client, code, nonce)
            assert second.status_code == 200
            with client.session_transaction() as sess:
                assert "id" not in sess
    destroy_ctfd(app)


def test_login_with_recovery_code_consumes_it():
    app = create_ctfd()
    user_id, secret, codes = _enroll_user(app)
    with app.app_context():
        client = app.test_client()
        _, nonce = _begin_login(client)
        recovery_code = codes[0]
        response = _post_two_factor(client, recovery_code, nonce)
        assert response.status_code == 302
        with client.session_transaction() as sess:
            assert sess["id"] == user_id
        user = Users.query.filter_by(id=user_id).first()
        assert count_remaining_recovery_codes(user) == RECOVERY_CODE_COUNT - 1

        client.get("/logout")
        _, nonce = _begin_login(client)
        response = _post_two_factor(client, recovery_code, nonce)
        assert response.status_code == 200
        with client.session_transaction() as sess:
            assert "id" not in sess
    destroy_ctfd(app)


def test_failed_attempts_return_to_password_step_without_locking():
    app = create_ctfd()
    user_id, secret, codes = _enroll_user(app)
    with app.app_context():
        client = app.test_client()
        _, nonce = _begin_login(client)
        for _ in range(LOGIN_ATTEMPT_LIMIT - 1):
            response = _post_two_factor(client, "000000", nonce)
            assert response.status_code == 200
            with client.session_transaction() as sess:
                assert "id" not in sess
                assert sess.get("two_factor_user_id") is not None

        response = _post_two_factor(client, "000000", nonce)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/login")
        with client.session_transaction() as sess:
            assert "two_factor_user_id" not in sess

        # Account is not locked: a fresh password login with the correct code works
        _, nonce = _begin_login(client)
        response = _post_two_factor(client, generate_totp(secret), nonce)
        assert response.status_code == 302
        with client.session_transaction() as sess:
            assert sess["id"] == user_id

        # Recovery codes still work as well
        client.get("/logout")
        _, nonce = _begin_login(client)
        response = _post_two_factor(client, codes[0], nonce)
        assert response.status_code == 302
    destroy_ctfd(app)


def test_wrong_password_does_not_create_pending_state():
    app = create_ctfd()
    _enroll_user(app)
    with app.app_context():
        client = app.test_client()
        client.get("/login")
        with client.session_transaction() as sess:
            nonce = sess.get("nonce")
        response = client.post(
            "/login",
            data={"name": "user2fa", "password": "wrongpassword", "nonce": nonce},
        )
        assert response.status_code == 200
        with client.session_transaction() as sess:
            assert "two_factor_user_id" not in sess
    destroy_ctfd(app)


# ---- Settings / enrollment -------------------------------------------------


def test_settings_shows_enable_tab_for_password_account():
    app = create_ctfd()
    with app.app_context():
        client = _settings_client(app)
        response = client.get("/settings")
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        assert "/settings/two_factor/setup" in text
        assert "Two Factor Authentication" in text
        assert "API access tokens are not affected" in text
    destroy_ctfd(app)


def test_settings_hides_twofactor_tab_for_oauth_only_account():
    app = create_ctfd()
    with app.app_context():
        user = Users(
            name="oauthuser",
            email="oauthuser@examplectf.com",
            oauth_id=999,
            verified=True,
        )
        app.db.session.add(user)
        app.db.session.commit()
        user_id = user.id

        client = app.test_client()
        with client.session_transaction() as sess:
            sess["id"] = user_id
            sess["nonce"] = "nonce"
            sess["hash"] = ""

        text = client.get("/settings").get_data(as_text=True)
        assert "Two Factor Authentication" not in text

        response = client.get("/settings/two_factor/setup")
        assert response.status_code == 403
    destroy_ctfd(app)


def test_vendored_qrcode_javascript_is_served_with_license():
    app = create_ctfd()
    with app.app_context():
        client = _settings_client(app)
        response = client.get(
            "/themes/core/static/assets/vendor/qrcode-generator.js"
        )
        assert response.status_code == 200
        assert "MIT license" in response.get_data(as_text=True)
    destroy_ctfd(app)


def test_setup_page_shows_secret_uri_and_qr():
    app = create_ctfd()
    with app.app_context():
        client = _settings_client(app)
        nonce = _get_nonce(client)
        response = client.post(
            "/settings/two_factor/setup", data={"nonce": nonce}
        )
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        with client.session_transaction() as sess:
            secret = sess["totp_setup_secret"]
        assert secret in text
        assert "otpauth://totp/" in text
        assert "issuer=" in text
        assert "qrcode-generator.js" in text

        user = Users.query.filter_by(name="user").first()
        assert user.totp_secret is None
        assert UserRecoveryCodes.query.filter_by(user_id=user.id).count() == 0
    destroy_ctfd(app)


def test_abandoning_and_restarting_setup_leaves_no_state():
    app = create_ctfd()
    with app.app_context():
        client = _settings_client(app)
        nonce = _get_nonce(client)
        client.post("/settings/two_factor/setup", data={"nonce": nonce})
        with client.session_transaction() as sess:
            first_secret = sess["totp_setup_secret"]

        nonce = _get_nonce(client)
        client.post("/settings/two_factor/setup", data={"nonce": nonce})
        with client.session_transaction() as sess:
            second_secret = sess["totp_setup_secret"]
        assert first_secret != second_secret

        user = Users.query.filter_by(name="user").first()
        assert user.totp_secret is None
        assert UserRecoveryCodes.query.filter_by(user_id=user.id).count() == 0
    destroy_ctfd(app)


def test_confirm_with_wrong_code_does_not_enable():
    app = create_ctfd()
    with app.app_context():
        client = _settings_client(app)
        nonce = _get_nonce(client)
        client.post("/settings/two_factor/setup", data={"nonce": nonce})

        nonce = _get_nonce(client)
        response = client.post(
            "/settings/two_factor/confirm",
            data={"code": "000000", "nonce": nonce},
        )
        assert response.status_code == 400

        user = Users.query.filter_by(name="user").first()
        assert user.totp_secret is None
        assert UserRecoveryCodes.query.filter_by(user_id=user.id).count() == 0
        with client.session_transaction() as sess:
            assert sess.get("totp_setup_secret") is not None
    destroy_ctfd(app)


def test_confirm_with_valid_code_enables_and_issues_codes():
    app = create_ctfd()
    with app.app_context():
        client = _settings_client(app)
        nonce = _get_nonce(client)
        client.post("/settings/two_factor/setup", data={"nonce": nonce})
        with client.session_transaction() as sess:
            secret = sess["totp_setup_secret"]

        nonce = _get_nonce(client)
        response = client.post(
            "/settings/two_factor/confirm",
            data={"code": generate_totp(secret), "nonce": nonce},
        )
        assert response.status_code == 200
        text = response.get_data(as_text=True)

        user = Users.query.filter_by(name="user").first()
        assert user.totp_secret == secret
        rows = UserRecoveryCodes.query.filter_by(user_id=user.id).all()
        assert len(rows) == RECOVERY_CODE_COUNT
        assert all(row.used is False for row in rows)
        codes = set(re.findall(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b", text))
        assert len(codes) == RECOVERY_CODE_COUNT
        assert all(row.code_hash not in text for row in rows)

        with client.session_transaction() as sess:
            assert "totp_setup_secret" not in sess

        settings_page = client.get("/settings").get_data(as_text=True)
        assert "Two factor authentication is enabled" in settings_page
        assert "10 recovery codes remaining" in settings_page
    destroy_ctfd(app)


def test_setup_is_redirected_when_already_enabled():
    app = create_ctfd()
    with app.app_context():
        client = _settings_client(app)
        nonce = _get_nonce(client)
        client.post("/settings/two_factor/setup", data={"nonce": nonce})
        with client.session_transaction() as sess:
            secret = sess["totp_setup_secret"]
        nonce = _get_nonce(client)
        client.post(
            "/settings/two_factor/confirm",
            data={"code": generate_totp(secret), "nonce": nonce},
        )

        response = client.get("/settings/two_factor/setup")
        assert response.status_code == 302
    destroy_ctfd(app)


def test_disable_requires_password_and_code():
    app = create_ctfd()
    with app.app_context():
        client = _settings_client(app)
        nonce = _get_nonce(client)
        client.post("/settings/two_factor/setup", data={"nonce": nonce})
        with client.session_transaction() as sess:
            secret = sess["totp_setup_secret"]
        nonce = _get_nonce(client)
        client.post(
            "/settings/two_factor/confirm",
            data={"code": generate_totp(secret), "nonce": nonce},
        )

        nonce = _get_nonce(client)
        response = client.post(
            "/settings/two_factor/disable",
            data={
                "password": "wrongpassword",
                "code": generate_totp(secret),
                "nonce": nonce,
            },
            follow_redirects=True,
        )
        assert "Your current password is incorrect" in response.get_data(as_text=True)
        user = Users.query.filter_by(name="user").first()
        assert user.totp_secret is not None

        nonce = _get_nonce(client)
        response = client.post(
            "/settings/two_factor/disable",
            data={"password": "password", "code": "000000", "nonce": nonce},
            follow_redirects=True,
        )
        assert "Invalid two factor authentication code" in response.get_data(as_text=True)
        user = Users.query.filter_by(name="user").first()
        assert user.totp_secret is not None

        nonce = _get_nonce(client)
        response = client.post(
            "/settings/two_factor/disable",
            data={
                "password": "password",
                "code": generate_totp(secret),
                "nonce": nonce,
            },
            follow_redirects=True,
        )
        assert "has been disabled" in response.get_data(as_text=True)
        user = Users.query.filter_by(name="user").first()
        assert user.totp_secret is None
        assert UserRecoveryCodes.query.filter_by(user_id=user.id).count() == 0

        # Login is single factor again
        client.get("/logout")
        client.get("/login")
        with client.session_transaction() as sess:
            nonce = sess.get("nonce")
        response = client.post(
            "/login",
            data={"name": "user", "password": "password", "nonce": nonce},
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/challenges")
    destroy_ctfd(app)


def test_recovery_codes_regeneration_replaces_codes():
    app = create_ctfd()
    with app.app_context():
        client = _settings_client(app)
        nonce = _get_nonce(client)
        client.post("/settings/two_factor/setup", data={"nonce": nonce})
        with client.session_transaction() as sess:
            secret = sess["totp_setup_secret"]
        nonce = _get_nonce(client)
        codes_page = client.post(
            "/settings/two_factor/confirm",
            data={"code": generate_totp(secret), "nonce": nonce},
        ).get_data(as_text=True)
        old_codes = set(re.findall(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b", codes_page))

        nonce = _get_nonce(client)
        response = client.post(
            "/settings/two_factor/recovery/regenerate",
            data={"code": "000000", "nonce": nonce},
            follow_redirects=True,
        )
        assert "Invalid two factor authentication code" in response.get_data(as_text=True)
        user = Users.query.filter_by(name="user").first()
        assert UserRecoveryCodes.query.filter_by(user_id=user.id).count() == (
            RECOVERY_CODE_COUNT
        )

        nonce = _get_nonce(client)
        new_page = client.post(
            "/settings/two_factor/recovery/regenerate",
            data={"code": generate_totp(secret), "nonce": nonce},
        ).get_data(as_text=True)
        new_codes = set(re.findall(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b", new_page))
        assert len(new_codes) == RECOVERY_CODE_COUNT
        assert new_codes.isdisjoint(old_codes)

        # Old recovery codes no longer work at login
        client.get("/logout")
        client.get("/login")
        with client.session_transaction() as sess:
            nonce = sess.get("nonce")
        client.post(
            "/login",
            data={"name": "user", "password": "password", "nonce": nonce},
        )
        response = _post_two_factor(client, list(old_codes)[0], nonce)
        assert response.status_code == 200
        with client.session_transaction() as sess:
            assert "id" not in sess
    destroy_ctfd(app)


def test_low_recovery_codes_warning_on_settings():
    app = create_ctfd()
    with app.app_context():
        client = _settings_client(app)
        nonce = _get_nonce(client)
        client.post("/settings/two_factor/setup", data={"nonce": nonce})
        with client.session_transaction() as sess:
            secret = sess["totp_setup_secret"]
        nonce = _get_nonce(client)
        client.post(
            "/settings/two_factor/confirm",
            data={"code": generate_totp(secret), "nonce": nonce},
        )

        user = Users.query.filter_by(name="user").first()
        rows = UserRecoveryCodes.query.filter_by(user_id=user.id).all()
        for row in rows[:-2]:
            row.used = True
        app.db.session.commit()

        text = client.get("/settings").get_data(as_text=True)
        assert "running low on recovery codes" in text
        assert "2 recovery codes remaining" in text
    destroy_ctfd(app)


def test_low_recovery_codes_flash_after_login():
    app = create_ctfd()
    user_id, secret, _ = _enroll_user(app, name="user", issue_codes=False)
    with app.app_context():
        user = Users.query.filter_by(id=user_id).first()
        plaintext_codes = issue_recovery_codes(user)
        rows = UserRecoveryCodes.query.filter_by(user_id=user_id).all()
        code_hashes = {hash_recovery_code(c) for c in plaintext_codes}
        assert len(code_hashes) == RECOVERY_CODE_COUNT
        last_hash = hash_recovery_code(plaintext_codes[-1])
        for row in rows:
            if row.code_hash != last_hash:
                row.used = True
        app.db.session.commit()

        client = app.test_client()
        client.get("/login")
        with client.session_transaction() as sess:
            nonce = sess.get("nonce")
        client.post(
            "/login",
            data={"name": "user", "password": "password", "nonce": nonce},
        )
        response = _post_two_factor(
            client, plaintext_codes[-1], nonce
        )
        assert response.status_code == 302

        settings_page = client.get("/settings").get_data(as_text=True)
        assert "0 recovery codes remaining" in settings_page
        assert "generate new recovery codes" in settings_page
    destroy_ctfd(app)


# ---- Enforcement policy ----------------------------------------------------


def test_admin_can_configure_enforcement_policy():
    app = create_ctfd()
    with app.app_context():
        client = login_as_user(app, "admin", "password")
        response = client.patch(
            "/api/v1/configs", json={"two_factor_required": "admins"}
        )
        assert response.status_code == 200
        assert response.get_json()["success"] is True

        config_page = client.get("/admin/config").get_data(as_text=True)
        assert 'name="two_factor_required"' in config_page
        assert 'selected value="admins"' in config_page
        assert "API access tokens remain unaffected" in config_page
    destroy_ctfd(app)


def test_admins_policy_gates_admins_but_not_regular_users():
    app = create_ctfd()
    with app.app_context():
        set_config("two_factor_required", "admins")
        admin_client = login_as_user(app, "admin", "password")

        response = admin_client.get("/challenges")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/settings/two_factor/setup")
        assert admin_client.get("/settings").status_code == 200
        assert admin_client.get("/settings/two_factor/setup").status_code == 200

        response = admin_client.get(
            "/api/v1/users/me", headers={"Accept": "application/json"}
        )
        assert response.status_code == 403

        # Once enrolled the admin is no longer gated
        from CTFd.cache import clear_user_session

        admin = Users.query.filter_by(name="admin").first()
        admin.totp_secret = generate_totp_secret()
        app.db.session.commit()
        clear_user_session(user_id=admin.id)
        assert admin_client.get("/challenges").status_code == 200

        register_user(app)
        user_client = login_as_user(app)
        assert user_client.get("/challenges").status_code == 200
    destroy_ctfd(app)


def test_all_policy_gates_regular_users_but_not_oauth_only():
    app = create_ctfd()
    with app.app_context():
        set_config("two_factor_required", "all")
        register_user(app)
        client = login_as_user(app)
        response = client.get("/challenges")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/settings/two_factor/setup")

        oauth_user = Users(
            name="oauthuser2",
            email="oauthuser2@examplectf.com",
            oauth_id=31337,
            verified=True,
        )
        app.db.session.add(oauth_user)
        app.db.session.commit()
        user_id = oauth_user.id

        oauth_client = app.test_client()
        with oauth_client.session_transaction() as sess:
            sess["id"] = user_id
            sess["nonce"] = "nonce"
            sess["hash"] = ""
            sess["two_factor_enforced"] = True
        assert oauth_client.get("/challenges").status_code == 200
    destroy_ctfd(app)


def test_policy_change_does_not_affect_existing_sessions():
    app = create_ctfd()
    with app.app_context():
        register_user(app)
        client = login_as_user(app)
        with client.session_transaction() as sess:
            assert sess.get("two_factor_enforced") is False

        assert client.get("/challenges").status_code == 200

        set_config("two_factor_required", "all")
        # Existing session keeps working
        assert client.get("/challenges").status_code == 200

        # A new login is gated
        client.get("/logout")
        client.get("/login")
        with client.session_transaction() as sess:
            nonce = sess.get("nonce")
        client.post(
            "/login",
            data={"name": "user", "password": "password", "nonce": nonce},
        )
        with client.session_transaction() as sess:
            assert sess.get("two_factor_enforced") is True
        response = client.get("/challenges")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/settings/two_factor/setup")

        # Relaxing the policy releases the gate immediately
        set_config("two_factor_required", "off")
        assert client.get("/challenges").status_code == 200
    destroy_ctfd(app)


def test_forced_user_can_complete_enrollment_through_gate():
    app = create_ctfd()
    with app.app_context():
        set_config("two_factor_required", "all")
        register_user(app)
        client = login_as_user(app)

        response = client.get("/notifications")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/settings/two_factor/setup")

        response = client.get("/settings/two_factor/setup")
        assert response.status_code == 200
        with client.session_transaction() as sess:
            nonce = sess.get("nonce")

        client.post("/settings/two_factor/setup", data={"nonce": nonce})
        with client.session_transaction() as sess:
            nonce = sess.get("nonce")
            secret = sess["totp_setup_secret"]

        response = client.post(
            "/settings/two_factor/confirm",
            data={"code": generate_totp(secret), "nonce": nonce},
        )
        assert response.status_code == 200

        assert client.get("/challenges").status_code == 200

        set_config("two_factor_required", "off")
    destroy_ctfd(app)
