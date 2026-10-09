/*
 * Every picture on a page opens full size, over the page.
 *
 * Click or press Enter on a picture: it opens with its caption, the others on
 * the page as thumbnails, and arrows to step through them. Esc, the close
 * button, a click outside the picture, or a swipe down closes it; a swipe
 * left or right steps. Focus goes to the close button and comes back to the
 * picture that opened it. Small marks (the logo, icons, badges) are left
 * alone. No library: a page loads nothing it did not before.
 *
 * Author: Kwadwo Daddy Nyame Owusu - Boakye
 */
(function () {
  "use strict";

  var MIN_WIDTH = 160; // narrower than this is a mark, not a picture
  var overlay, stage, caption, counter, thumbs, closeButton, prev, next;
  var gallery = [];
  var at = 0;
  var opener = null;

  function pictures() {
    return Array.prototype.filter.call(
      document.querySelectorAll(".md-content .md-typeset img"),
      function (img) {
        if (img.closest("a") || img.classList.contains("twemoji") || img.closest(".vx-no-zoom")) return false;
        var shown = img.getBoundingClientRect().width || img.width;
        return shown >= MIN_WIDTH || img.naturalWidth >= MIN_WIDTH * 2;
      }
    );
  }

  function captionOf(img) {
    var tour = img.closest(".vx-tour");
    if (tour) {
      var heading = tour.querySelector("h3");
      var title = "";
      if (heading) {
        var copy = heading.cloneNode(true);
        Array.prototype.forEach.call(copy.querySelectorAll(".vx-new, .headerlink"), function (n) { n.remove(); });
        title = copy.textContent.trim();
      }
      // The words under the heading, not the paragraph that holds the picture.
      var said = heading ? heading.nextElementSibling : null;
      var text = said && said.tagName === "P" && !said.querySelector("img") ? said.textContent.trim() : img.alt;
      return { title: title, text: text };
    }
    var next = img.parentElement && img.parentElement.nextElementSibling;
    if (next && next.classList && next.classList.contains("vx-caption")) {
      return { title: "", text: next.textContent.trim() };
    }
    return { title: "", text: img.alt || "" };
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  }

  function build() {
    overlay = el("div", "vx-lb");
    overlay.hidden = true;
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Picture, full size");

    var top = el("div", "vx-lb__top");
    counter = el("span", "vx-lb__count");
    var hint = el("span", "vx-lb__hint", "Esc to close, arrows to step");
    closeButton = el("button", "vx-lb__btn vx-lb__close", "✕");
    closeButton.setAttribute("aria-label", "Close");
    top.append(counter, hint, closeButton);

    var row = el("div", "vx-lb__row");
    prev = el("button", "vx-lb__btn", "‹");
    prev.setAttribute("aria-label", "Previous picture");
    next = el("button", "vx-lb__btn", "›");
    next.setAttribute("aria-label", "Next picture");
    stage = el("div", "vx-lb__stage");
    row.append(prev, stage, next);

    var foot = el("div", "vx-lb__foot");
    caption = el("p", "vx-lb__caption");
    thumbs = el("div", "vx-lb__thumbs");
    foot.append(caption, thumbs);

    overlay.append(top, row, foot);
    document.body.appendChild(overlay);

    closeButton.addEventListener("click", close);
    prev.addEventListener("click", function () { show(at - 1); });
    next.addEventListener("click", function () { show(at + 1); });
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay || e.target === stage || e.target === row) close();
    });

    var x0 = null, y0 = null;
    overlay.addEventListener("touchstart", function (e) {
      x0 = e.touches[0].clientX;
      y0 = e.touches[0].clientY;
    }, { passive: true });
    overlay.addEventListener("touchend", function (e) {
      if (x0 === null) return;
      var dx = e.changedTouches[0].clientX - x0;
      var dy = e.changedTouches[0].clientY - y0;
      x0 = null;
      if (dy > 80 && Math.abs(dy) > Math.abs(dx)) close();
      else if (dx < -50) show(at + 1);
      else if (dx > 50) show(at - 1);
    });
  }

  function show(i) {
    at = (i + gallery.length) % gallery.length;
    var source = gallery[at];
    var big = el("img");
    big.src = source.currentSrc || source.src;
    big.alt = source.alt;
    stage.replaceChildren(big);
    var said = captionOf(source);
    caption.replaceChildren();
    if (said.title) caption.appendChild(el("b", "", said.title));
    if (said.text) caption.appendChild(document.createTextNode(said.text));
    counter.textContent = gallery.length > 1 ? at + 1 + " / " + gallery.length : "";
    Array.prototype.forEach.call(thumbs.children, function (t, k) {
      t.classList.toggle("vx-lb__thumb--on", k === at);
      if (k === at) t.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
  }

  function open(img) {
    if (!overlay) build();
    opener = img;
    gallery = pictures();
    var many = gallery.length > 1;
    prev.hidden = next.hidden = !many;
    thumbs.hidden = !many;
    thumbs.replaceChildren();
    gallery.forEach(function (picture, k) {
      var t = el("button", "vx-lb__thumb");
      t.setAttribute("aria-label", captionOf(picture).title || picture.alt || "Picture " + (k + 1));
      var small = el("img");
      small.src = picture.currentSrc || picture.src;
      small.alt = "";
      t.appendChild(small);
      t.addEventListener("click", function () { show(k); });
      thumbs.appendChild(t);
    });
    overlay.hidden = false;
    document.documentElement.classList.add("vx-lb-open");
    show(Math.max(0, gallery.indexOf(img)));
    closeButton.focus();
  }

  function close() {
    overlay.hidden = true;
    document.documentElement.classList.remove("vx-lb-open");
    stage.replaceChildren();
    if (opener) opener.focus();
  }

  document.addEventListener("keydown", function (e) {
    if (overlay && !overlay.hidden) {
      if (e.key === "Escape") close();
      else if (e.key === "ArrowRight") show(at + 1);
      else if (e.key === "ArrowLeft") show(at - 1);
      else if (e.key === "Tab") {
        // Keep focus inside the overlay while it is open.
        var stops = overlay.querySelectorAll("button:not([hidden])");
        var first = stops[0], last = stops[stops.length - 1];
        if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
        else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
      }
      return;
    }
    if ((e.key === "Enter" || e.key === " ") && e.target.classList && e.target.classList.contains("vx-zoomable")) {
      e.preventDefault();
      open(e.target);
    }
  });

  function mark() {
    pictures().forEach(function (img) {
      if (img.classList.contains("vx-zoomable")) return;
      img.classList.add("vx-zoomable");
      img.tabIndex = 0;
      img.setAttribute("role", "button");
      img.setAttribute("aria-label", "Open full size: " + (img.alt || "picture"));
      img.addEventListener("click", function () { open(img); });
    });
  }

  if (window.document$ && window.document$.subscribe) {
    window.document$.subscribe(mark); // Material's instant navigation, if it is ever turned on
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mark);
  } else {
    mark();
  }
  window.addEventListener("load", mark); // pictures whose width was unknown until they loaded
})();
