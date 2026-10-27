#!/usr/bin/env python
# -*- coding: utf-8 -*-

import datetime

from CTFd.models import Tokens, Users
from CTFd.schemas.tokens import TokenSchema
from CTFd.utils.security.auth import generate_user_token
from tests.helpers import create_ctfd, destroy_ctfd, gen_user, login_as_user


def test_api_tag_list_post():
    """Can a user create a token"""
    app = create_ctfd()
    with app.app_context():
        user = gen_user(app.db, name="user")
        user_id = user.id
        with login_as_user(app) as client:
            r = client.post("/api/v1/tokens", json={})
            assert r.status_code == 200
            resp = r.get_json()
            value = resp["data"]["value"]
            token = Tokens.query.filter_by(value=value).first()
            assert token.user_id == user_id
            assert token.expiration > datetime.datetime.utcnow()

            data = {"expiration": "9999-12-30"}
            r = client.post("/api/v1/tokens", json=data)
            assert r.status_code == 200
            resp = r.get_json()
            value = resp["data"]["value"]
            token = Tokens.query.filter_by(value=value).first()
            assert token.user_id == user_id
            assert token.expiration.year == 9999
    destroy_ctfd(app)


def test_api_tag_list_get():
    """Can a user get /api/v1/tokens"""
    app = create_ctfd()
    with app.app_context():
        user = gen_user(app.db, name="user")
        generate_user_token(user)

        user2 = gen_user(app.db, name="user2", email="user2@examplectf.com")
        generate_user_token(user2)
        generate_user_token(user2)
        with login_as_user(app) as client:
            r = client.get("/api/v1/tokens", json="")
            assert r.status_code == 200
            resp = r.get_json()
            assert len(resp["data"]) == 1

        with login_as_user(app, name="user2") as client:
            r = client.get("/api/v1/tokens", json="")
            assert r.status_code == 200
            resp = r.get_json()
            assert len(resp["data"]) == 2
    destroy_ctfd(app)


def test_api_tag_detail_get():
    """Can a user get /api/v1/tokens/<token_id>"""
    app = create_ctfd()
    with app.app_context():
        user = gen_user(app.db, name="user")
        generate_user_token(user)

        with login_as_user(app) as client:
            r = client.get("/api/v1/tokens/1", json="")
            assert r.status_code == 200
            resp = r.get_json()
            assert sorted(resp["data"].keys()) == sorted(TokenSchema().views["user"])

        with login_as_user(app, "admin") as client:
            r = client.get("/api/v1/tokens/1", json="")
            assert r.status_code == 200
            resp = r.get_json()
            assert sorted(resp["data"].keys()) == sorted(TokenSchema().views["admin"])

        gen_user(app.db, name="user2", email="user2@examplectf.com")
        with login_as_user(app, "user2") as client:
            r = client.get("/api/v1/tokens/1", json="")
            assert r.status_code == 404
    destroy_ctfd(app)


def test_api_token_delete():
    """Can tokens be deleted by owners, and admins"""
    app = create_ctfd()
    with app.app_context():
        # Can be deleted by the user
        user = gen_user(app.db)
        user_id = user.id
        username = user.name
        token = generate_user_token(user)
        token_id = token.id
        with login_as_user(app, username) as client:
            r = client.delete("/api/v1/tokens/" + str(token_id), json="")
            assert r.status_code == 200
            assert Tokens.query.count() == 0

        # Can be deleted by admins
        user = Users.query.filter_by(id=user_id).first()
        token = generate_user_token(user)
        token_id = token.id
        with login_as_user(app, "admin") as client:
            r = client.delete("/api/v1/tokens/" + str(token_id), json="")
            assert r.status_code == 200
            assert Tokens.query.count() == 0

        # First user
        first_user = Users.query.filter_by(id=user_id).first()
        token = generate_user_token(first_user)
        token_id = token.id
        # Second user
        second_user = gen_user(app.db, name="user2", email="user2@examplectf.com")
        username2 = second_user.name
        with login_as_user(app, username2) as client:
            r = client.delete("/api/v1/tokens/" + str(token_id), json="")
            assert r.status_code == 404
            assert Tokens.query.count() == 1
    destroy_ctfd(app)


def test_api_token_scope_defaults_to_full():
    """Tokens created without specifying a scope get full scope"""
    app = create_ctfd()
    with app.app_context():
        gen_user(app.db, name="user")
        with login_as_user(app) as client:
            r = client.post("/api/v1/tokens", json={})
            assert r.status_code == 200
            resp = r.get_json()
            assert resp["data"]["scope"] == "full"
            value = resp["data"]["value"]

        token = Tokens.query.filter_by(value=value).first()
        assert token.scope == "full"

        with login_as_user(app) as client:
            r = client.get("/api/v1/tokens", json="")
            assert r.status_code == 200
            assert r.get_json()["data"][0]["scope"] == "full"

            r = client.get("/api/v1/tokens/" + str(token.id), json="")
            assert r.status_code == 200
            assert r.get_json()["data"]["scope"] == "full"
    destroy_ctfd(app)


def test_api_token_scope_read_only():
    """A read-only scope can be selected at creation and is visible later"""
    app = create_ctfd()
    with app.app_context():
        gen_user(app.db, name="user")
        with login_as_user(app) as client:
            r = client.post("/api/v1/tokens", json={"scope": "read_only"})
            assert r.status_code == 200
            resp = r.get_json()
            assert resp["data"]["scope"] == "read_only"
            token_id = resp["data"]["id"]
            value = resp["data"]["value"]

        token = Tokens.query.filter_by(value=value).first()
        assert token.scope == "read_only"

        with login_as_user(app) as client:
            r = client.get("/api/v1/tokens", json="")
            assert r.status_code == 200
            scopes = [t["scope"] for t in r.get_json()["data"]]
            assert "read_only" in scopes

            r = client.get("/api/v1/tokens/" + str(token_id), json="")
            assert r.status_code == 200
            assert r.get_json()["data"]["scope"] == "read_only"
    destroy_ctfd(app)


def test_api_token_scope_invalid():
    """An unknown scope is rejected without creating a token"""
    app = create_ctfd()
    with app.app_context():
        gen_user(app.db, name="user")
        with login_as_user(app) as client:
            r = client.post("/api/v1/tokens", json={"scope": "superpowers"})
            assert r.status_code == 400
            resp = r.get_json()
            assert resp["success"] is False
            assert "scope" in resp["errors"]
            assert Tokens.query.count() == 0
    destroy_ctfd(app)


def test_api_token_scope_is_immutable():
    """A token scope cannot be changed after creation; recreate instead"""
    app = create_ctfd()
    with app.app_context():
        user = gen_user(app.db, name="user")
        token = generate_user_token(user, scope="read_only")
        token_id = token.id

        # The token detail endpoint exposes no PATCH/PUT handler at all
        detail_rule = next(
            rule
            for rule in app.url_map.iter_rules()
            if rule.rule == "/api/v1/tokens/<token_id>"
        )
        assert "PATCH" not in detail_rule.methods
        assert "PUT" not in detail_rule.methods

        with login_as_user(app) as client:
            r = client.patch("/api/v1/tokens/" + str(token_id), json={"scope": "full"})
            assert r.status_code in (404, 405)
        assert Tokens.query.filter_by(id=token_id).first().scope == "read_only"
    destroy_ctfd(app)
