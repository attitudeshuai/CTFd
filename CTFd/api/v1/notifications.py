from typing import List

from flask import current_app, make_response, request, session
from flask_restx import Namespace, Resource

from CTFd.api.v1.helpers.request import validate_args
from CTFd.api.v1.helpers.schemas import sqlalchemy_to_pydantic
from CTFd.api.v1.schemas import APIDetailedSuccessResponse, APIListSuccessResponse
from CTFd.constants import RawEnum
from CTFd.models import NotificationRead, Notifications, db
from CTFd.schemas.notifications import NotificationSchema
from CTFd.utils.decorators import admins_only, authed_only
from CTFd.utils.notifications import (
    get_read_notification_ids,
    get_unread_count,
    mark_notifications_read,
    notification_query,
    purge_expired_notifications,
)
from CTFd.utils.user import authed

notifications_namespace = Namespace(
    "notifications", description="Endpoint to retrieve Notifications"
)

NotificationModel = sqlalchemy_to_pydantic(Notifications)
TransientNotificationModel = sqlalchemy_to_pydantic(Notifications, exclude=["id"])


class NotificationDetailedSuccessResponse(APIDetailedSuccessResponse):
    data: NotificationModel


class NotificationListSuccessResponse(APIListSuccessResponse):
    data: List[NotificationModel]


notifications_namespace.schema_model(
    "NotificationDetailedSuccessResponse", NotificationDetailedSuccessResponse.apidoc()
)

notifications_namespace.schema_model(
    "NotificationListSuccessResponse", NotificationListSuccessResponse.apidoc()
)


def _current_user_id():
    if authed():
        return session.get("id")
    return None


def _annotate_read(items, user_id):
    read_ids = get_read_notification_ids(user_id)
    for item in items:
        item["read"] = item["id"] in read_ids
    return items


@notifications_namespace.route("")
class NotificantionList(Resource):
    @notifications_namespace.doc(
        description="Endpoint to get notification objects in bulk",
        responses={
            200: ("Success", "NotificationListSuccessResponse"),
            400: (
                "An error occured processing the provided or stored data",
                "APISimpleErrorResponse",
            ),
        },
    )
    @validate_args(
        {
            "title": (str, None),
            "content": (str, None),
            "user_id": (int, None),
            "team_id": (int, None),
            "q": (str, None),
            "field": (
                RawEnum("NotificationFields", {"title": "title", "content": "content"}),
                None,
            ),
            "since_id": (int, None),
        },
        location="query",
    )
    def get(self, query_args):
        purge_expired_notifications()

        notifications = notification_query(query_args).all()
        schema = NotificationSchema(many=True)
        result = schema.dump(notifications)
        if result.errors:
            return {"success": False, "errors": result.errors}, 400
        data = _annotate_read(result.data, _current_user_id())
        return {"success": True, "data": data}

    @notifications_namespace.doc(
        description="Endpoint to get statistics for notification objects in bulk",
        responses={200: ("Success", "APISimpleSuccessResponse")},
    )
    @validate_args(
        {
            "title": (str, None),
            "content": (str, None),
            "user_id": (int, None),
            "team_id": (int, None),
            "q": (str, None),
            "field": (
                RawEnum("NotificationFields", {"title": "title", "content": "content"}),
                None,
            ),
            "since_id": (int, None),
        },
        location="query",
    )
    def head(self, query_args):
        purge_expired_notifications()

        # Result-Count is the unread count for the current user, produced with
        # exactly the same filtering as the list endpoint
        notification_count = get_unread_count(_current_user_id(), query_args)
        response = make_response()
        response.headers["Result-Count"] = notification_count
        return response

    @admins_only
    @notifications_namespace.doc(
        description="Endpoint to create a notification object",
        responses={
            200: ("Success", "NotificationDetailedSuccessResponse"),
            400: (
                "An error occured processing the provided or stored data",
                "APISimpleErrorResponse",
            ),
        },
    )
    def post(self):
        # Remove expired notifications before publishing so that the
        # per-recipient unread count computed during fan-out stays consistent
        purge_expired_notifications()

        req = request.get_json()

        schema = NotificationSchema()
        result = schema.load(req)

        if result.errors:
            return {"success": False, "errors": result.errors}, 400

        db.session.add(result.data)
        db.session.commit()

        response = schema.dump(result.data)

        # Grab additional settings
        notif_type = req.get("type", "alert")
        notif_sound = req.get("sound", True)
        response.data["type"] = notif_type
        response.data["sound"] = notif_sound

        current_app.events_manager.publish(data=response.data, type="notification")

        return {"success": True, "data": response.data}


@notifications_namespace.route("/read")
class NotificationReadAll(Resource):
    @authed_only
    @notifications_namespace.doc(
        description=(
            "Endpoint to mark notifications as read for the current user. "
            "Without a request body every notification is marked read; pass "
            "an optional list of notification ids to mark in bulk."
        ),
        responses={200: ("Success", "APISimpleSuccessResponse")},
    )
    def post(self):
        req = request.get_json(silent=True) or {}
        notification_ids = req.get("ids")

        if notification_ids is not None:
            if not isinstance(notification_ids, list):
                return (
                    {
                        "success": False,
                        "errors": {"ids": "ids must be a list of notification ids"},
                    },
                    400,
                )
            try:
                notification_ids = [
                    int(notification_id) for notification_id in notification_ids
                ]
            except (TypeError, ValueError):
                return (
                    {
                        "success": False,
                        "errors": {"ids": "ids must be a list of notification ids"},
                    },
                    400,
                )

        unread_count = mark_notifications_read(
            user_id=session["id"], notification_ids=notification_ids
        )
        db.session.close()
        return {"success": True, "data": {"unread_count": unread_count}}


@notifications_namespace.route("/<notification_id>")
@notifications_namespace.param("notification_id", "A Notification ID")
class Notification(Resource):
    @notifications_namespace.doc(
        description="Endpoint to get a specific notification object",
        responses={
            200: ("Success", "NotificationDetailedSuccessResponse"),
            400: (
                "An error occured processing the provided or stored data",
                "APISimpleErrorResponse",
            ),
        },
    )
    def get(self, notification_id):
        notif = Notifications.query.filter_by(id=notification_id).first_or_404()
        schema = NotificationSchema()
        response = schema.dump(notif)
        if response.errors:
            return {"success": False, "errors": response.errors}, 400

        response.data["read"] = notif.id in get_read_notification_ids(
            _current_user_id()
        )
        return {"success": True, "data": response.data}

    @admins_only
    @notifications_namespace.doc(
        description="Endpoint to delete a notification object",
        responses={200: ("Success", "APISimpleSuccessResponse")},
    )
    def delete(self, notification_id):
        notif = Notifications.query.filter_by(id=notification_id).first_or_404()
        NotificationRead.query.filter_by(notification_id=notif.id).delete()
        db.session.delete(notif)
        db.session.commit()
        db.session.close()

        return {"success": True}


@notifications_namespace.route("/<notification_id>/read")
@notifications_namespace.param("notification_id", "A Notification ID")
class NotificationReadOne(Resource):
    @authed_only
    @notifications_namespace.doc(
        description=(
            "Endpoint to mark a single notification as read for the "
            "current user. Idempotent: repeated calls create no duplicate "
            "state."
        ),
        responses={200: ("Success", "APISimpleSuccessResponse")},
    )
    def post(self, notification_id):
        notif = Notifications.query.filter_by(id=notification_id).first_or_404()
        target_id = notif.id
        unread_count = mark_notifications_read(
            user_id=session["id"], notification_ids=[target_id]
        )
        db.session.close()
        return {
            "success": True,
            "data": {"id": target_id, "read": True, "unread_count": unread_count},
        }
