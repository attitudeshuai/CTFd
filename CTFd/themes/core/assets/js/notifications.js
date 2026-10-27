import Alpine from "alpinejs";
import CTFd from "./index";
import { syncUnreadCount } from "./utils/notifications/read";

window.CTFd = CTFd;
window.Alpine = Alpine;

const unreadSection = document.getElementById("unread-section");
const readSection = document.getElementById("read-section");
const unreadContainer = document.getElementById("notifications-unread");
const readContainer = document.getElementById("notifications-read");

function applyCount(count) {
  CTFd.events.controller.broadcast("counter", { count: count });
  Alpine.store("unread_count", count);
}

function moveCardToRead(card) {
  const button = card.querySelector(".mark-notification-read");
  if (button) {
    button.remove();
  }
  if (readContainer) {
    readContainer.prepend(card);
  }
  updateSections();
}

function updateSections() {
  if (unreadContainer && unreadContainer.children.length === 0) {
    unreadSection.classList.add("d-none");
  }
  if (readContainer && readContainer.children.length > 0) {
    readSection.classList.remove("d-none");
  }
}

document.addEventListener("click", event => {
  const markAllButton = event.target.closest("#mark-all-notifications");
  if (markAllButton) {
    markAllButton.setAttribute("disabled", true);
    CTFd.fetch(`/api/v1/notifications/read`, { method: "POST" })
      .then(response => response.json())
      .then(response => {
        if (response.success) {
          if (unreadContainer) {
            const cards = Array.from(
              unreadContainer.querySelectorAll(".notification-card"),
            );
            cards.forEach(card => moveCardToRead(card));
          }
          applyCount(response.data.unread_count);
        }
      })
      .catch(syncUnreadCount);
    return;
  }

  const markOneButton = event.target.closest(".mark-notification-read");
  if (markOneButton) {
    const notificationId = markOneButton.getAttribute("data-notif-id");
    markOneButton.setAttribute("disabled", true);
    CTFd.fetch(`/api/v1/notifications/${notificationId}/read`, {
      method: "POST",
    })
      .then(response => response.json())
      .then(response => {
        if (response.success) {
          moveCardToRead(markOneButton.closest(".notification-card"));
          applyCount(response.data.unread_count);
        }
      })
      .catch(syncUnreadCount);
  }
});

// Ensure the navbar badge matches the server when entering the page
syncUnreadCount();

Alpine.start();
