import "./main";
import "../compat/json";
import $ from "jquery";
import CTFd from "../compat/CTFd";
import { ezAlert } from "../compat/ezq";
import Vue from "vue";
import Notification from "../components/notifications/Notification.vue";

const notificationCard = Vue.extend(Notification);

function submit(event) {
  event.preventDefault();
  const $form = $(this);
  const params = $form.serializeJSON();

  // Disable button after click
  $form.find("button[type=submit]").attr("disabled", true);

  CTFd.api.post_notification_list({}, params).then((response) => {
    $form.find(":input[name=title]").val("");
    $form.find(":input[name=content]").val("");

    // Admin should also see the notification sent out
    setTimeout(function () {
      $form.find("button[type=submit]").attr("disabled", false);
    }, 1000);
    if (!response.success) {
      ezAlert({
        title: "Error",
        body: "Could not send notification. Please try again.",
        button: "OK",
      });
    }

    let vueContainer = document.createElement("div");
    $("#notifications-list").prepend(vueContainer);
    new notificationCard({
      propsData: {
        id: response.data.id,
        title: response.data.title,
        content: response.data.content,
        html: response.data.html,
        date: response.data.date,
        readCount: 0,
      },
    }).$mount(vueContainer);
  });
}

const RETENTION_CONFIG_KEY = "notification_retention_days";

function saveRetention(event) {
  event.preventDefault();
  const $form = $(this);
  const value = $form.find(":input[name=notification_retention_days]").val();
  const $button = $form.find("#retention-form-save");
  $button.attr("disabled", true);

  const done = () => {
    setTimeout(function () {
      $button.attr("disabled", false);
    }, 1000);
  };

  if (value === "") {
    CTFd.api
      .delete_config({ configKey: RETENTION_CONFIG_KEY })
      .then((response) => {
        // A 404 (config key was never set) resolves to a body without
        // success; either way retention is now unset which is the goal.
        ezAlert({
          title: "Saved",
          body: "Notifications will be kept forever.",
          button: "OK",
        });
      })
      .catch(() => {
        ezAlert({
          title: "Error",
          body: "Could not update the retention period. Please try again.",
          button: "OK",
        });
      })
      .finally(done);
  } else {
    const params = {};
    params[RETENTION_CONFIG_KEY] = parseInt(value, 10);
    CTFd.api
      .patch_config_list({}, params)
      .then((response) => {
        if (response.success) {
          ezAlert({
            title: "Saved",
            body: "The notification retention period has been updated.",
            button: "OK",
          });
        } else {
          ezAlert({
            title: "Error",
            body: "Could not save the retention period. Please try again.",
            button: "OK",
          });
        }
      })
      .finally(done);
  }
}

function deleteNotification(event) {
  event.preventDefault();
  const $elem = $(this);
  const id = $elem.data("notif-id");

  if (confirm("Are you sure you want to delete this notification?")) {
    CTFd.api.delete_notification({ notificationId: id }).then((response) => {
      if (response.success) {
        $elem.parent().remove();
      }
    });
  }
}

$(() => {
  $("#notifications_form").submit(submit);
  $(".delete-notification").click(deleteNotification);
  $("#notification-retention-form").submit(saveRetention);
});
