"""
Captain tenure chain.

This module is the single place that answers "who is the captain of this
team?" and "is the current user allowed to act as captain?". The captain
pointer (``Teams.captain_id``) is the materialized head of the chain; the
chain resolves it into one of three states:

- ``active``: the pointer targets an existing, non-banned team member
- ``banned``: the pointer targets a user account that has been banned
- ``orphaned``: the pointer is empty (or targets a deleted account)

All permission checks, badges, menus, admin filters and write actions
(succession / takeover) must go through this module instead of comparing
``team.captain_id`` directly.
"""

from flask import session
from sqlalchemy import and_, or_

from CTFd.models import Notifications, Teams, Users, db

#: Resolvable captain states, in the order accepted by query filters.
CAPTAIN_ACTIVE = "active"
CAPTAIN_BANNED = "banned"
CAPTAIN_ORPHANED = "orphaned"
CAPTAIN_STATES = (CAPTAIN_ORPHANED, CAPTAIN_BANNED, CAPTAIN_ACTIVE)


def get_captain(team):
    """
    Return the effective captain Users object for a team, or None when the
    team is orphaned or its captain is banned.
    """
    if team is None:
        return None

    captain = getattr(team, "captain", None)
    if captain is None:
        captain_id = getattr(team, "captain_id", None)
        if captain_id is None:
            return None
        captain = Users.query.filter_by(id=captain_id).first()

    if captain is None or captain.banned:
        return None
    return captain


def get_captain_id(team):
    """Return the effective captain's user id, or None."""
    captain = get_captain(team)
    if captain:
        return captain.id
    return None


def get_captain_state(team):
    """
    Resolve the captain state of a team: ``active``, ``banned`` or
    ``orphaned``. The state is derived solely from the chain, never from a
    cached equality comparison.
    """
    if team is None:
        return CAPTAIN_ORPHANED

    captain_id = getattr(team, "captain_id", None)
    if captain_id is None:
        return CAPTAIN_ORPHANED

    captain = getattr(team, "captain", None)
    if captain is None:
        captain = Users.query.filter_by(id=captain_id).first()

    if captain is None:
        # The pointer targets a deleted account: the team has no captain.
        return CAPTAIN_ORPHANED
    if captain.banned:
        return CAPTAIN_BANNED
    return CAPTAIN_ACTIVE


def is_current_user_captain(team):
    """
    Return True when the currently logged in user is the team's effective
    (active, non-banned) captain. Replaces every ``team.captain_id ==
    session["id"]`` permission check in the codebase.
    """
    if not session.get("id", False):
        return False
    return get_captain_id(team) == session["id"]


def get_captain_filters(state):
    """
    Build Teams query filters matching the chain's state resolution so that
    admin listings and the teams API filter with exactly the same semantics
    that :func:`get_captain_state` applies per row.
    """
    banned_captain_select = db.session.query(Users.id).filter(
        Users.banned == True  # noqa: E712
    )

    if state == CAPTAIN_ORPHANED:
        return [Teams.captain_id.is_(None)]
    if state == CAPTAIN_BANNED:
        return [Teams.captain_id.in_(banned_captain_select)]
    if state == CAPTAIN_ACTIVE:
        return [
            and_(
                Teams.captain_id.isnot(None),
                Teams.captain_id.notin_(banned_captain_select),
            )
        ]
    raise ValueError("Unknown captain state: {!r}".format(state))


def notification_visibility_filters(team_id=None, user_id=None):
    """
    Filters restricting team/user scoped notifications to the audience that
    is allowed to see them:

    - global notifications (no team/user scope) are visible to everyone
    - team notifications are visible only to members of that team
    - user notifications are visible only to that user
    """
    conditions = [
        # Global notifications are visible to everyone.
        and_(Notifications.team_id.is_(None), Notifications.user_id.is_(None))
    ]

    if team_id is not None:
        # Team notifications are visible only to the team's members.
        conditions.append(
            and_(
                Notifications.team_id == team_id,
                Notifications.user_id.is_(None),
            )
        )
    if user_id is not None:
        # User targeted notifications are visible only to that user.
        conditions.append(Notifications.user_id == user_id)

    return [or_(*conditions)]


def locked_team(team_id):
    """
    Re-read a team row with a row lock for the duration of the current
    transaction so that concurrent takeovers/transfers cannot interleave the
    validation and the write. Engines without ``SELECT ... FOR UPDATE``
    (SQLite) degrade this to a plain read.
    """
    return Teams.query.filter_by(id=team_id).with_for_update().first()


def notify_captain_change(team, captain=None, orphaned=False):
    """
    Record a team scoped notification describing a captain chain write.
    Succession/takeover notifications contain the new captain's user name.
    The row is added to the current session but not committed; callers own
    the commit.
    """
    if orphaned:
        title = "Team without captain"
        content = (
            "Team {team} no longer has a captain. "
            "Please contact an administrator to appoint one.".format(team=team.name)
        )
    else:
        title = "New team captain"
        content = "{name} is now the captain of {team}.".format(
            name=captain.name, team=team.name
        )

    notification = Notifications(
        title=title,
        content=content,
        team_id=team.id,
    )
    db.session.add(notification)
    return notification


def transfer_captain(team, captain):
    """
    Chain write action for both self transfer and admin takeover: validate
    that the new captain is an active member, move the pointer and record a
    team notification. Does not commit.
    """
    if captain is None or captain.team_id != team.id or captain.banned:
        raise ValueError("Captain must be an active member of the team")

    team.captain_id = captain.id
    notify_captain_change(team, captain=captain)
    return team


def succeed_captain_after_deletion(team):
    """
    Chain write action used by the user deletion flow. The deleted user has
    already been removed by the caller, so successor selection runs over the
    remaining members:

    - an eligible successor exists -> hand the team over deterministically
    - otherwise -> the team becomes orphaned (``captain_id = NULL``)

    Either outcome is recorded with a team notification. Does not commit.
    """
    successor = team.get_captain_successor()
    if successor:
        team.captain_id = successor.id
        notify_captain_change(team, captain=successor)
    else:
        team.captain_id = None
        notify_captain_change(team, orphaned=True)
    return team
