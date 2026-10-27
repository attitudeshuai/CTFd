import { Howl } from "howler";
import CTFd from "@ctfdio/ctfd-js";

// Legacy localStorage bookkeeping kept around only to perform the one-time
// client-side migration. After migration these keys are never used for
// counting again.
const LEGACY_READ_KEY = "CTFd:read_notifications";
const LEGACY_UNREAD_KEY = "CTFd:unread_notifications";
const MIGRATION_KEY = "CTFd:notifications_read_migrated";

function fetchUnreadCount() {
  return CTFd.fetch(`/api/v1/notifications`, { method: "HEAD" }).then(response => {
    const count = parseInt(response.headers.get("result-count"), 10);
    return Number.isNaN(count) ? 0 : count;
  });
}

function publishCount(count) {
  // Sync the authoritative server count between tabs
  CTFd.events.controller.broadcast("counter", { count: count });
  CTFd._functions.events.eventCount(count);
}

function migrateLegacyReadState() {
  let alreadyMigrated = false;
  try {
    alreadyMigrated = localStorage.getItem(MIGRATION_KEY) !== null;
  } catch (error) {
    return Promise.resolve();
  }

  if (alreadyMigrated) {
    try {
      localStorage.removeItem(LEGACY_READ_KEY);
      localStorage.removeItem(LEGACY_UNREAD_KEY);
    } catch (error) {
      // Ignore storage failures and fall back to server state
    }
    return Promise.resolve();
  }

  let ids = [];
  try {
    ids = JSON.parse(localStorage.getItem(LEGACY_READ_KEY)) || [];
  } catch (error) {
    ids = [];
  }
  ids = ids.filter(id => Number.isInteger(id));

  const finish = () => {
    try {
      localStorage.setItem(MIGRATION_KEY, "1");
      localStorage.removeItem(LEGACY_READ_KEY);
      localStorage.removeItem(LEGACY_UNREAD_KEY);
    } catch (error) {
      // Ignore storage failures and fall back to server state
    }
  };

  if (ids.length === 0) {
    finish();
    return Promise.resolve();
  }

  // Fold the legacy local read set into the server-side read associations
  // once. The endpoint is idempotent so concurrent tabs stay safe.
  return CTFd.fetch(`/api/v1/notifications/read`, {
    method: "POST",
    body: JSON.stringify({ ids: ids }),
  })
    .then(() => finish())
    .catch(() => {});
}

// Replace the vendored ctfd-js event wiring with server-authoritative
// behavior without modifying the dependency itself. The WindowController
// (cross-tab broadcasts), render hooks and sound handling are reused.
export default function patchNotificationEvents() {
  const events = CTFd.events;

  events.init = (root, eventSounds) => {
    events.source = new EventSource(root + "/events");
    for (let i = 0; i < eventSounds.length; i++) {
      eventSounds[i] = `${root}${eventSounds[i]}`;
    }
    events.howl = new Howl({
      src: eventSounds,
    });

    // Fold legacy local state into the server first, then pull the
    // authoritative unread count so that the number is continuous across
    // the upgrade.
    migrateLegacyReadState().then(() => {
      fetchUnreadCount().then(publishCount);
    });
  };

  events.connect = () => {
    events.source.addEventListener(
      "notification",
      function (event) {
        const data = JSON.parse(event.data);

        // Sync render between tabs
        events.controller.broadcast("notification", data);

        // The count is computed server-side per receiving user
        if (typeof data.unread_count === "number") {
          publishCount(data.unread_count);
        } else {
          fetchUnreadCount().then(publishCount);
        }

        // Render in the master tab
        events.render(data);

        // Only play sounds in the master tab
        if (data.sound) {
          events.howl.play();
        }
      },
      false,
    );
  };
}
