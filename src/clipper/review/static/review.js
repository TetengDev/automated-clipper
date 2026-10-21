// Keyboard shortcuts on the clip page: a approve, r reject, j/k next/previous.
// External file because the CSP forbids inline scripts.
(function () {
  "use strict";

  function typing(target) {
    var tag = target && target.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (target && target.isContentEditable);
  }

  function usable(id) {
    var el = document.getElementById(id);
    return el && !el.disabled ? el : null;
  }

  // Returns true when the key triggered an action.
  function handle(key, target) {
    var reason = document.getElementById("reason-tag");
    if (key === "r" && reason && target === reason) {
      // `r` pressed again in the reason picker submits once a reason is chosen.
      var submit = usable("reject-btn");
      if (!submit || !reason.value) return false;
      submit.click();
      return true;
    }
    if (typing(target)) return false;
    switch (key) {
      case "a": {
        var approve = usable("approve-btn");
        if (!approve) return false;
        approve.click();
        return true;
      }
      case "r": {
        var reject = usable("reject-btn");
        if (!reject || !reason) return false;
        if (reason.value) {
          reject.click();
        } else {
          reason.focus();
        }
        return true;
      }
      case "j":
      case "k": {
        var link = document.getElementById(key === "j" ? "next-clip" : "prev-clip");
        if (!link) return false;
        window.location.assign(link.href);
        return true;
      }
      default:
        return false;
    }
  }

  document.addEventListener("keydown", function (event) {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    if (handle(event.key, event.target)) event.preventDefault();
  });
})();
