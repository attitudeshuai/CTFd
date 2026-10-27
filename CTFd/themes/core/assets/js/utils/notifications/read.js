import Alpine from "alpinejs";
import CTFd from "../../index";

export function fetchUnreadCount() {
  return CTFd.fetch(`/api/v1/notifications`, { method: "HEAD" }).then(response => {
    const count = parseInt(response.headers.get("result-count"), 10);
    return Number.isNaN(count) ? 0 : count;
  });
}

function broadcastCount(count) {
  CTFd.events.controller.broadcast("counter", { count: count });
}

// Force every tab (including this one) back to the server-authoritative count
export function syncUnreadCount() {
  return fetchUnreadCount().then(count => {
    broadcastCount(count);
    Alpine.store("unread_count", count);
    return count;
  });
}

export default () => {
  CTFd._functions.events.eventCount = count => {
    Alpine.store("unread_count", count);
  };

  // Dismissing a toast/alert marks the single notification read on the
  // server; the returned count is the authoritative one.
  CTFd._functions.events.eventRead = eventId => {
    CTFd.fetch(`/api/v1/notifications/${eventId}/read`, {
      method: "POST",
    })
      .then(response => response.json())
      .then(response => {
        if (response.success) {
          const count = response.data.unread_count;
          broadcastCount(count);
          Alpine.store("unread_count", count);
        } else {
          syncUnreadCount();
        }
      })
      .catch(syncUnreadCount);
  };

  document.addEventListener("alpine:init", () => {
    // Start at zero; events.init replaces this with the server count
    CTFd._functions.events.eventCount(0);
  });
};
