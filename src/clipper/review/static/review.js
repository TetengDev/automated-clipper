// Keyboard shortcuts on the clip page: a approve, r reject, j/k next/previous; and live
// counters on the per-platform title and caption fields.
// External file because the CSP forbids inline scripts.
(function () {
  "use strict";

  function typing(target) {
    var tag = target && target.tagName;
    // A checkbox (a confirm box) takes no text: shortcuts still apply.
    if (tag === "INPUT" && target.type === "checkbox") return false;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (target && target.isContentEditable);
  }

  function usable(id) {
    var el = document.getElementById(id);
    return el && !el.disabled ? el : null;
  }

  // Returns true when the key triggered an action.
  function handle(key, target) {
    var reason = document.getElementById("reason-tag");
    // Approving needs every confirm box ticked: the faces check on sensitive clips (CLP-87),
    // the campaign's reviewer checks (CLP-114).
    var unticked = ["confirm-sensitive", "confirm-reviewer-checks"]
      .map(function (id) { return document.getElementById(id); })
      .filter(function (box) { return box && !box.checked; })[0];
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
        if (unticked) {
          unticked.focus();
        } else {
          approve.click();
        }
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

  // Counters (CLP-117). A field says how its platform counts (`data-count`): code points on
  // Instagram, YouTube and Facebook, where `maxlength` (UTF-16 units) would stop an emoji
  // caption at half the limit, or UTF-16 units on TikTok. The server's check still decides.
  // The counter can over-count but never under-count: the server measures the text after
  // cleaning it (control and invisible characters such as ZWSP, and generic or malformed
  // inline tags, are stripped first), so a text the counter accepts is never refused for
  // length, while one it shows a little over may still be saved.
  function measure(field) {
    var value = field.value;
    return field.getAttribute("data-count") === "code-points" ? Array.from(value).length : value.length;
  }

  function limitOf(field) {
    return parseInt(field.getAttribute("data-limit"), 10);
  }

  function number(n) {
    return n.toLocaleString("en-US");
  }

  function countText(field) {
    var limit = limitOf(field);
    var used = measure(field);
    var unit = field.getAttribute("data-unit");
    if (used > limit) {
      var over = used - limit;
      // "1 character over", "2 characters over".
      var units = over === 1 ? unit.replace(/s$/, "") : unit;
      return number(over) + " " + units + " over the limit of " + number(limit);
    }
    return number(used) + " of " + number(limit) + " " + unit;
  }

  // "under", "near" (the last 10% of the limit) or "over".
  function zone(field) {
    var used = measure(field);
    var limit = limitOf(field);
    if (used > limit) return "over";
    return used >= limit * 0.9 ? "near" : "under";
  }

  // One polite live region for every field. It speaks only when a field's zone changes and
  // stays changed through a typing pause (into the last 10%, past the limit, or back), so a
  // screen reader is not read the count while the reviewer types well within the limit.
  var spoken = {};
  var timers = {};
  function announce(field) {
    var status = document.getElementById("variant-count-status");
    if (!status) return;
    var id = field.getAttribute("data-counter");
    window.clearTimeout(timers[id]);
    timers[id] = window.setTimeout(function () {
      var now = zone(field);
      if (spoken[id] === now) return;
      spoken[id] = now;
      status.textContent = field.getAttribute("data-label") + ": " + countText(field);
    }, 1000);
  }

  var counted = document.querySelectorAll("[data-counter]");
  for (var i = 0; i < counted.length; i++) {
    spoken[counted[i].getAttribute("data-counter")] = zone(counted[i]);
  }

  document.addEventListener("input", function (event) {
    var field = event.target;
    if (!field || !field.hasAttribute || !field.hasAttribute("data-counter")) return;
    var out = document.getElementById(field.getAttribute("data-counter"));
    if (!out) return;
    out.textContent = countText(field);
    out.classList.toggle("over", measure(field) > limitOf(field));
    announce(field);
  });
})();
