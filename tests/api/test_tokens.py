import datetime
import os
from io import BytesIO

from CTFd.exceptions import UserNotFoundException, UserTokenExpiredException
from CTFd.models import Files, Notifications, Teams, Tokens, Users
from CTFd.utils.security.auth import (
    generate_user_token,
    lookup_user_token,
    token_scope_allows_request,
)
from tests.helpers import create_ctfd, destroy_ctfd, gen_team, gen_token, gen_user


def test_generate_user_token():
    app = create_ctfd()
    with app.app_context():
        user = gen_user(app.db)
        token = generate_user_token(user, expiration=None)
        assert token.user_id == user.id
        assert token.expiration > datetime.datetime.utcnow()
        assert Tokens.query.count() == 1
    destroy_ctfd(app)


def test_lookup_user_token():
    app = create_ctfd()
    with app.app_context():
        user = gen_user(app.db)
        # Good Token
        token = gen_token(app.db, user_id=user.id)
        user = lookup_user_token(token.value)
        assert user.id == token.user_id

        # Expired Token
        expiration = datetime.datetime.utcnow() + datetime.timedelta(days=-1)
        token = gen_token(app.db, user_id=user.id, expiration=expiration)
        try:
            lookup_user_token(token.value)
        except UserTokenExpiredException:
            pass
        except Exception as e:
            raise e

        # Nonexistant token
        try:
            lookup_user_token("wat")
        except UserNotFoundException:
            pass
        except Exception as e:
            raise e
    destroy_ctfd(app)


def test_user_token_access():
    app = create_ctfd()
    with app.app_context():
        with app.test_client() as client:
            r = client.get("/api/v1/users/me", json="")
            assert r.status_code == 403

        with app.test_client() as client:
            user = gen_user(app.db, name="user2", email="user2@examplectf.com")
            expiration = datetime.datetime.utcnow() + datetime.timedelta(days=-1)
            token = generate_user_token(user, expiration=expiration)
            headers = {"Authorization": "token " + token.value}
            r = client.get("/api/v1/users/me", headers=headers, json="")
            assert r.status_code == 401

        with app.test_client() as client:
            headers = {"Authorization": "token invalid_token"}
            r = client.get("/api/v1/users/me", headers=headers, json="")
            assert r.status_code == 401

        with app.test_client() as client:
            user = gen_user(app.db, name="user1", email="user1@examplectf.com")
            token = generate_user_token(user, expiration=None)
            headers = {"Authorization": "token " + token.value}
            r = client.get("/api/v1/users/me", headers=headers, json="")
            assert r.status_code == 200
            resp = r.get_json()
            assert resp["data"]["email"] == "user1@examplectf.com"
            assert resp["data"]["name"] == "user1"
    destroy_ctfd(app)


def test_token_api_file_upload():
    """Test that tokens can upload files with multipart/form-data content type"""
    app = create_ctfd()
    with app.app_context():
        admin = Users.query.filter_by(id=1).first()
        token = generate_user_token(admin, expiration=None)
        with app.test_client() as client:
            headers = {"Authorization": "token " + token.value}
            r = client.post(
                "/api/v1/files",
                headers=headers,
                content_type="multipart/form-data",
                data={
                    "file": (BytesIO(b"test file content"), "test.txt"),
                },
            )
            assert r.status_code == 200
            f = Files.query.filter_by(id=1).first()
            filepath = os.path.join(app.config["UPLOAD_FOLDER"] + "/" + f.location)
            with open(filepath) as f:
                assert f.read() == "test file content"
            os.remove(filepath)
    destroy_ctfd(app)


def test_banned_user_token_access():
    """Can a banned user still use an API token"""
    app = create_ctfd()
    with app.app_context():
        user = gen_user(app.db, name="user1", email="user1@examplectf.com")
        token = generate_user_token(user, expiration=None)
        headers = {"Authorization": "token " + token.value}

        with app.test_client() as client:
            r = client.get("/api/v1/users/me", headers=headers, json="")
            assert r.status_code == 200

        Users.query.filter_by(id=user.id).first().banned = True
        app.db.session.commit()

        with app.test_client() as client:
            r = client.get("/api/v1/users/me", headers=headers, json="")
            assert r.status_code == 403
    destroy_ctfd(app)


def test_banned_team_token_access():
    """Can a member of a banned team still use an API token"""
    app = create_ctfd(user_mode="teams")
    with app.app_context():
        team = gen_team(app.db, name="team")
        user = Users.query.filter_by(id=team.captain_id).first()
        token = generate_user_token(user, expiration=None)
        headers = {"Authorization": "token " + token.value}

        with app.test_client() as client:
            r = client.get("/api/v1/users/me", headers=headers, json="")
            assert r.status_code == 200

        Teams.query.filter_by(id=team.id).first().banned = True
        app.db.session.commit()

        with app.test_client() as client:
            r = client.get("/api/v1/users/me", headers=headers, json="")
            assert r.status_code == 403
    destroy_ctfd(app)


def test_default_token_scope_is_full():
    """Tokens created without a scope default to full scope"""
    app = create_ctfd()
    with app.app_context():
        user = gen_user(app.db, name="user1", email="user1@examplectf.com")
        token = generate_user_token(user, expiration=None)
        assert token.scope == "full"

        # Full scope tokens can still call write endpoints
        headers = {"Authorization": "token " + token.value}
        with app.test_client() as client:
            r = client.post("/api/v1/tokens", headers=headers, json={})
            assert r.status_code == 200
            assert Tokens.query.count() == 2
    destroy_ctfd(app)


def test_read_only_token_read_endpoints():
    """Read-only tokens can call the same query endpoints as full tokens"""
    app = create_ctfd()
    with app.app_context():
        user = gen_user(app.db, name="user1", email="user1@examplectf.com")
        token = generate_user_token(user, expiration=None, scope="read_only")
        headers = {"Authorization": "token " + token.value}

        with app.test_client() as client:
            r = client.get("/api/v1/users/me", headers=headers, json="")
            assert r.status_code == 200
            resp = r.get_json()
            assert resp["data"]["email"] == "user1@examplectf.com"
            assert resp["data"]["name"] == "user1"

            r = client.get("/api/v1/tokens", headers=headers, json="")
            assert r.status_code == 200
            resp = r.get_json()
            assert len(resp["data"]) == 1
            assert resp["data"][0]["scope"] == "read_only"

            # Safe HTTP methods other than GET also work
            r = client.head("/api/v1/notifications", headers=headers, json="")
            assert r.status_code in (200, 302)
    destroy_ctfd(app)


def test_read_only_token_write_endpoints_denied():
    """Read-only tokens are denied any write endpoint without side effects"""
    app = create_ctfd()
    with app.app_context():
        user = gen_user(app.db, name="user1", email="user1@examplectf.com")
        token = generate_user_token(user, expiration=None, scope="read_only")
        headers = {"Authorization": "token " + token.value}

        with app.test_client() as client:
            # POST: the rejection happens before a new token can be created
            r = client.post("/api/v1/tokens", headers=headers, json={})
            assert r.status_code == 403
            body = r.get_data(as_text=True)
            assert "read-only" in body
            assert "scope" in body
            assert Tokens.query.count() == 1

            # PATCH: user attributes are not modified
            r = client.patch(
                "/api/v1/users/me", headers=headers, json={"name": "newname"}
            )
            assert r.status_code == 403
            assert Users.query.filter_by(id=user.id).first().name == "user1"

            # DELETE: the token survives the attempted revocation
            r = client.delete(
                "/api/v1/tokens/" + str(token.id), headers=headers, json=""
            )
            assert r.status_code == 403
            assert Tokens.query.filter_by(id=token.id).first() is not None
    destroy_ctfd(app)


def test_admin_read_only_token_is_still_restricted():
    """Read-only scope applies to admin tokens as well"""
    app = create_ctfd()
    with app.app_context():
        admin = Users.query.filter_by(id=1).first()
        token = generate_user_token(admin, expiration=None, scope="read_only")
        headers = {"Authorization": "token " + token.value}

        with app.test_client() as client:
            # Admin reads still work
            r = client.get("/api/v1/notifications", headers=headers, json="")
            assert r.status_code == 200

            # Admin writes are rejected despite the admin user type
            r = client.post(
                "/api/v1/notifications",
                headers=headers,
                json={"title": "title", "content": "content"},
            )
            assert r.status_code == 403
            assert Notifications.query.count() == 0

            # Explicitly classified read paths (CSV/backup exports) work even
            # though they use POST
            r = client.post(
                "/api/v1/exports/raw",
                headers=headers,
                json={"type": "csv", "args": {"table": "scoreboard"}},
            )
            assert r.status_code == 200
            assert "attachment" in r.headers["Content-Disposition"]
            assert ".csv" in r.headers["Content-Disposition"]
            assert "place" in r.get_data(as_text=True)

            r = client.post("/api/v1/exports/raw", headers=headers, json={})
            assert r.status_code == 200
            # ZIP file magic bytes
            assert r.data[:2] == b"PK"
    destroy_ctfd(app)


def test_read_only_token_file_upload_denied():
    """Read-only tokens cannot upload files with multipart/form-data"""
    app = create_ctfd()
    with app.app_context():
        admin = Users.query.filter_by(id=1).first()
        token = generate_user_token(admin, expiration=None, scope="read_only")
        with app.test_client() as client:
            headers = {"Authorization": "token " + token.value}
            r = client.post(
                "/api/v1/files",
                headers=headers,
                content_type="multipart/form-data",
                data={
                    "file": (BytesIO(b"test file content"), "test.txt"),
                },
            )
            assert r.status_code == 403
            assert Files.query.count() == 0
    destroy_ctfd(app)


def test_legacy_token_without_scope_has_full_access():
    """A token whose scope is missing behaves as a full scope token"""
    app = create_ctfd()
    with app.app_context():
        # Full, unknown and missing scopes all allow writes; only an explicit
        # read-only scope triggers the restriction.
        assert token_scope_allows_request("full", "POST") is True
        assert token_scope_allows_request(None, "POST") is True
        assert token_scope_allows_request("future_scope", "DELETE") is True

        assert token_scope_allows_request("read_only", "GET") is True
        assert token_scope_allows_request("read_only", "HEAD") is True
        assert token_scope_allows_request("read_only", "OPTIONS") is True
        assert token_scope_allows_request("read_only", "POST") is False
        assert token_scope_allows_request("read_only", "PUT") is False
        assert token_scope_allows_request("read_only", "PATCH") is False
        assert token_scope_allows_request("read_only", "DELETE") is False
    destroy_ctfd(app)


def test_read_only_token_marker_classifies_endpoint():
    """The read-safe marker explicitly classifies unclassified endpoints"""
    from CTFd.utils.decorators import read_only_token_allowed

    class UnmarkedView:
        pass

    @read_only_token_allowed
    class MarkedView:
        pass

    @read_only_token_allowed
    def marked_view_func():
        pass

    assert token_scope_allows_request("read_only", "POST", UnmarkedView) is False
    assert token_scope_allows_request("read_only", "POST", MarkedView) is True
    assert token_scope_allows_request("read_only", "POST", marked_view_func) is True
