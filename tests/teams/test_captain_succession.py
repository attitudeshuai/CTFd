#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Tests for the captain tenure chain: deterministic successor selection,
succession on captain deletion, orphaned/banned states, admin takeover,
team scoped notifications and admin/API filters sharing one resolution
path.
"""

from CTFd.models import Notifications, Teams, Users
from tests.helpers import create_ctfd, destroy_ctfd, gen_team, gen_user, login_as_user


def _members(team_id):
    return Users.query.filter_by(team_id=team_id).order_by(Users.id.asc()).all()


def test_successor_selection_is_deterministic():
    """The successor is the earliest active member and never changes"""
    app = create_ctfd(user_mode="teams")
    with app.app_context():
        team = gen_team(app.db)
        members = _members(team.id)

        first = team.get_captain_successor()
        second = team.get_captain_successor()
        assert first is not None
        assert first.id == second.id

        next_member = team.get_captain_successor(exclude_user_id=first.id)
        assert next_member.id == members[1].id

        # Banned members are skipped in favor of the next eligible member
        next_member.banned = True
        app.db.session.commit()

        successor = team.get_captain_successor(exclude_user_id=first.id)
        assert successor.id == members[2].id

        # Nobody is eligible when every member is banned
        for member in members:
            member.banned = True
        app.db.session.commit()
        assert team.get_captain_successor(exclude_user_id=first.id) is None
    destroy_ctfd(app)


def test_deleting_captain_promotes_earliest_member():
    """Deleting a captain account immediately hands the team to a member"""
    app = create_ctfd(user_mode="teams")
    with app.app_context():
        team = gen_team(app.db)
        team_id = team.id
        captain_id = team.captain_id
        members = _members(team_id)
        expected = next(m for m in members if m.id != captain_id)

        with login_as_user(app, "admin") as admin:
            r = admin.delete(f"/api/v1/users/{captain_id}", json="")
            assert r.status_code == 200

        team = Teams.query.filter_by(id=team_id).first()
        assert team.captain_id == expected.id
        assert Users.query.filter_by(id=team.captain_id).first() is not None

        notif = Notifications.query.filter_by(team_id=team_id).first()
        assert notif is not None
        assert expected.name in notif.content
    destroy_ctfd(app)


def test_deleting_lone_captain_orphans_team():
    """A team whose captain is deleted with no successors becomes orphaned"""
    app = create_ctfd(user_mode="teams")
    with app.app_context():
        team = Teams(name="solo", email="solo@examplectf.com", password="pwd")
        app.db.session.add(team)
        app.db.session.commit()

        captain = gen_user(
            app.db,
            name="solo_captain",
            email="solo_captain@examplectf.com",
            team_id=team.id,
        )
        team.captain_id = captain.id
        app.db.session.commit()

        team_id = team.id
        captain_id = captain.id

        with login_as_user(app, "admin") as admin:
            r = admin.delete(f"/api/v1/users/{captain_id}", json="")
            assert r.status_code == 200

        team = Teams.query.filter_by(id=team_id).first()
        assert team.captain_id is None
        assert team.members == []

        notif = Notifications.query.filter_by(team_id=team_id).first()
        assert notif is not None
    destroy_ctfd(app)


def test_deleting_captain_with_only_banned_members_orphans_team():
    """Banned members cannot take over so the team becomes captainless"""
    app = create_ctfd(user_mode="teams")
    with app.app_context():
        team = gen_team(app.db)
        team_id = team.id
        captain_id = team.captain_id

        for member in _members(team_id):
            if member.id != captain_id:
                member.banned = True
        app.db.session.commit()

        with login_as_user(app, "admin") as admin:
            r = admin.delete(f"/api/v1/users/{captain_id}", json="")
            assert r.status_code == 200

        team = Teams.query.filter_by(id=team_id).first()
        assert team.captain_id is None
    destroy_ctfd(app)


def test_orphaned_teams_are_filterable_in_api():
    """Orphaned teams can be filtered via the teams API"""
    app = create_ctfd(user_mode="teams")
    with app.app_context():
        team = gen_team(app.db)
        team_id = team.id

        with login_as_user(app, "admin") as admin:
            r = admin.get("/api/v1/teams?view=admin&captain=orphaned")
            assert r.status_code == 200
            assert r.get_json()["data"] == []

            member_ids = [member.id for member in _members(team_id)]
            for member_id in member_ids:
                admin.delete(f"/api/v1/users/{member_id}", json="")

            r = admin.get("/api/v1/teams?view=admin&captain=orphaned")
            assert r.status_code == 200
            data = r.get_json()["data"]
            assert len(data) == 1
            assert data[0]["id"] == team_id
            assert data[0]["captain_id"] is None
    destroy_ctfd(app)


def test_admin_teams_listing_filters_orphans():
    """The admin teams page can list orphaned teams and shows a marker"""
    app = create_ctfd(user_mode="teams")
    with app.app_context():
        team = gen_team(app.db)
        team_name = team.name
        team.captain_id = None
        app.db.session.commit()

        with login_as_user(app, "admin") as admin:
            r = admin.get("/admin/teams?captain=orphaned")
            assert r.status_code == 200
            text = r.get_data(as_text=True)
            assert team_name in text
            assert "no captain" in text
    destroy_ctfd(app)


def test_banning_captain_keeps_role_and_admin_can_take_over():
    """A banned captain keeps the title but an admin can transfer the role"""
    app = create_ctfd(user_mode="teams")
    with app.app_context():
        team = gen_team(app.db)
        team_id = team.id
        captain_id = team.captain_id
        new_captain = next(m for m in _members(team_id) if m.id != captain_id)
        new_captain_name = new_captain.name

        with login_as_user(app, "admin") as admin:
            r = admin.patch(f"/api/v1/users/{captain_id}", json={"banned": True})
            assert r.status_code == 200

        team = Teams.query.filter_by(id=team_id).first()
        assert team.captain_id == captain_id

        # Members see that the captain is unavailable
        with login_as_user(app, new_captain_name) as member:
            page = member.get("/team")
            assert page.status_code == 200
            assert "unavailable" in page.get_data(as_text=True)

        # The existing admin appointment flow performs the takeover
        with login_as_user(app, "admin") as admin:
            r = admin.patch(
                f"/api/v1/teams/{team_id}",
                json={"captain_id": new_captain.id},
            )
            assert r.status_code == 200

        team = Teams.query.filter_by(id=team_id).first()
        assert team.captain_id == new_captain.id
        assert Notifications.query.filter_by(team_id=team_id).count() == 1

        # The new captain can act immediately without logging in again
        with login_as_user(app, new_captain_name) as captain:
            r = captain.post("/api/v1/teams/me/members", json="")
            assert r.status_code == 200
            assert "code" in r.get_json()["data"]
    destroy_ctfd(app)


def test_orphaned_team_page_marks_state_and_hides_actions():
    """Members of an orphaned team see the captainless state on /team"""
    app = create_ctfd(user_mode="teams")
    with app.app_context():
        team = gen_team(app.db)
        member = _members(team.id)[1]
        team.captain_id = None
        app.db.session.commit()

        with login_as_user(app, member.name) as client:
            r = client.get("/team")
            assert r.status_code == 200
            text = r.get_data(as_text=True)
            assert "has no captain" in text
            assert 'x-data="CaptainMenu"' not in text
    destroy_ctfd(app)


def test_admin_can_appoint_captain_for_orphaned_team():
    """The existing Choose Captain flow recovers an orphaned team"""
    app = create_ctfd(user_mode="teams")
    with app.app_context():
        team = gen_team(app.db)
        team_id = team.id
        member_id = _members(team_id)[0].id
        team.captain_id = None
        app.db.session.commit()

        with login_as_user(app, "admin") as admin:
            page = admin.get(f"/admin/teams/{team_id}")
            assert page.status_code == 200

            r = admin.patch(f"/api/v1/teams/{team_id}", json={"captain_id": member_id})
            assert r.status_code == 200

        team = Teams.query.filter_by(id=team_id).first()
        assert team.captain_id == member_id
    destroy_ctfd(app)


def test_team_notifications_are_only_visible_to_members():
    """Team scoped captain notifications must not leak to other users"""
    app = create_ctfd(user_mode="teams")
    with app.app_context():
        team1 = gen_team(app.db, name="team1", email="team1@examplectf.com")
        team2 = gen_team(app.db, name="team2", email="team2@examplectf.com")
        team1_id = team1.id

        app.db.session.add(
            Notifications(
                title="New team captain",
                content="succession happened",
                team_id=team1.id,
            )
        )
        app.db.session.add(Notifications(title="global", content="everyone"))
        app.db.session.commit()

        member1 = _members(team1.id)[1]
        member2 = _members(team2.id)[1]

        with login_as_user(app, member1.name) as client:
            r = client.get("/api/v1/notifications")
            titles = [n["title"] for n in r.get_json()["data"]]
            assert "New team captain" in titles
            assert "global" in titles

        with login_as_user(app, member2.name) as client:
            r = client.get("/api/v1/notifications")
            titles = [n["title"] for n in r.get_json()["data"]]
            assert "New team captain" not in titles
            assert "global" in titles

            targeted = Notifications.query.filter_by(team_id=team1_id).first().id
            r = client.get(f"/api/v1/notifications/{targeted}")
            assert r.status_code == 404

        with login_as_user(app, "admin") as admin:
            r = admin.get("/api/v1/notifications")
            titles = [n["title"] for n in r.get_json()["data"]]
            assert "New team captain" in titles
            assert "global" in titles
    destroy_ctfd(app)


def test_normal_captain_flow_is_unchanged():
    """An active captain can still self transfer and the result is recorded"""
    app = create_ctfd(user_mode="teams")
    with app.app_context():
        team = gen_team(app.db)
        team_id = team.id
        captain_name = team.captain.name
        new_captain = next(m for m in _members(team_id) if m.id != team.captain_id)

        with login_as_user(app, captain_name) as captain:
            r = captain.patch("/api/v1/teams/me", json={"captain_id": new_captain.id})
            assert r.status_code == 200

        team = Teams.query.filter_by(id=team_id).first()
        assert team.captain_id == new_captain.id
        assert Notifications.query.filter_by(team_id=team_id).count() == 1
    destroy_ctfd(app)
