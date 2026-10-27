import json
from collections import defaultdict
from queue import Queue

from flask import current_app
from gevent import Timeout, spawn
from tenacity import retry, wait_exponential

from CTFd.cache import cache
from CTFd.utils import string_types


class ServerSentEvent(object):
    def __init__(self, data, type=None, id=None):
        self.data = data
        self.type = type
        self.id = id

    def __str__(self):
        if isinstance(self.data, string_types):
            data = self.data
        else:
            data = json.dumps(self.data)
        lines = ["data:{value}".format(value=line) for line in data.splitlines()]
        if self.type:
            lines.insert(0, "event:{value}".format(value=self.type))
        if self.id:
            lines.append("id:{value}".format(value=self.id))
        return "\n".join(lines) + "\n\n"

    def to_dict(self):
        d = {"data": self.data}
        if self.type:
            d["type"] = self.type
        if self.id:
            d["id"] = self.id
        return d


def enrich_event_message(message, user_id, count_cache, app=None):
    """
    Return a copy of a fan-out event with a server-computed per-user
    unread_count added to a notification event's data. Non-notification
    events and anonymous subscriptions are returned untouched. The count is
    computed once per distinct user per fan-out.
    """
    if user_id is None or message.get("type") != "notification":
        return message

    data = message.get("data")
    if not isinstance(data, dict):
        return message

    if user_id not in count_cache:
        # Imported lazily to avoid import cycles; a fresh app context is
        # provided by the Redis listener greenlet which has no request
        # context, otherwise reuse the current (request) context.
        context = app.app_context() if app is not None else current_app.app_context()
        with context:
            from CTFd.utils.notifications import get_unread_count

            count_cache[user_id] = get_unread_count(user_id)

    enriched_data = dict(data)
    enriched_data["unread_count"] = count_cache[user_id]
    enriched_message = dict(message)
    enriched_message["data"] = enriched_data
    return enriched_message


class EventManager(object):
    def __init__(self):
        # client id -> {"queue": per-channel queues, "user_id": subscriber}
        self.clients = {}

    def publish(self, data, type=None, id=None, channel="ctf"):
        event = ServerSentEvent(data, type=type, id=id)
        message = event.to_dict()
        count_cache = {}
        for client in list(self.clients.values()):
            outgoing = enrich_event_message(message, client["user_id"], count_cache)
            client["queue"][channel].put(outgoing)
        return len(self.clients)

    def listen(self):
        pass

    def subscribe(self, channel="ctf", user_id=None):
        q = defaultdict(Queue)
        self.clients[id(q)] = {"queue": q, "user_id": user_id}
        try:
            # Immediately yield a ping event to force Response headers to be set
            # or else some reverse proxies will incorrectly buffer SSE
            yield ServerSentEvent(data="ping", type="ping")
            while True:
                with Timeout(5, False):
                    message = self.clients[id(q)]["queue"][channel].get()
                    yield ServerSentEvent(**message)
                yield ServerSentEvent(data="ping", type="ping")
        finally:
            del self.clients[id(q)]
            del q


class RedisEventManager(EventManager):
    def __init__(self):
        super(EventManager, self).__init__()
        self.client = cache.cache._write_client
        self.clients = {}
        # Captured while listen() is started within an application context so
        # that the listener greenlet can perform database queries later.
        self.app = current_app._get_current_object()

    def publish(self, data, type=None, id=None, channel="ctf"):
        event = ServerSentEvent(data, type=type, id=id)
        message = json.dumps(event.to_dict())
        return self.client.publish(message=message, channel=channel)

    def listen(self, channel="ctf"):
        app = self.app

        @retry(wait=wait_exponential(min=1, max=30))
        def _listen():
            while True:
                pubsub = self.client.pubsub()
                pubsub.subscribe(channel)
                try:
                    while True:
                        message = pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=5
                        )
                        if message:
                            if message["type"] == "message":
                                event = json.loads(message["data"])
                                count_cache = {}
                                for client in list(self.clients.values()):
                                    outgoing = enrich_event_message(
                                        event,
                                        client["user_id"],
                                        count_cache,
                                        app=app,
                                    )
                                    client["queue"][channel].put(outgoing)
                finally:
                    pubsub.close()

        spawn(_listen)
