// Koine: display and convenience JS only, moved out of inline <script> tags
// so the CSP can drop 'unsafe-inline' from script-src. Every value read here
// (data-series, stat text) is the same platform-computed data the server
// already rendered; nothing here fetches.

// Menus are <details> elements and work without this; it only closes an
// open one on an outside click or Escape.
document.addEventListener("click", function (e) {
  document.querySelectorAll("details.menu[open]").forEach(function (d) {
    if (!d.contains(e.target)) d.removeAttribute("open");
  });
});

document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") {
    document.querySelectorAll("details.menu[open]").forEach(function (d) {
      d.removeAttribute("open");
    });
  }
});

// Equity curves draw themselves in once, and any chart with a data-series
// attribute gets a hover crosshair.
requestAnimationFrame(function () {
  requestAnimationFrame(function () {
    document.querySelectorAll(".draw").forEach(function (el) { el.classList.add("done"); });
  });
});

document.querySelectorAll("[data-series]").forEach(function (svg) {
  var series = JSON.parse(svg.getAttribute("data-series"));
  var wrap = svg.parentNode;
  var tip = document.createElement("div");
  tip.className = "chart-tip";
  wrap.appendChild(tip);
  var ns = "http://www.w3.org/2000/svg";
  var vb = svg.viewBox.baseVal;
  var line = document.createElementNS(ns, "line");
  var dot = document.createElementNS(ns, "circle");
  line.setAttribute("y1", 0);
  line.setAttribute("y2", vb.height);
  line.setAttribute("stroke", "var(--text2)");
  line.setAttribute("vector-effect", "non-scaling-stroke");
  line.setAttribute("opacity", 0);
  dot.setAttribute("r", 4);
  dot.setAttribute("fill", "var(--text)");
  dot.setAttribute("opacity", 0);
  svg.appendChild(line);
  svg.appendChild(dot);
  var lo = +svg.dataset.lo;
  var hi = +svg.dataset.hi;
  var span = hi - lo || 1;
  svg.addEventListener("mousemove", function (e) {
    var r = svg.getBoundingClientRect();
    var n = series.length;
    var i = Math.max(0, Math.min(n - 1, Math.round(((e.clientX - r.left) / r.width) * (n - 1))));
    var x = (vb.width * i) / Math.max(n - 1, 1);
    var y = vb.height * (1 - (series[i][1] - lo) / span);
    line.setAttribute("x1", x);
    line.setAttribute("x2", x);
    line.setAttribute("opacity", 1);
    dot.setAttribute("cx", x);
    dot.setAttribute("cy", y);
    dot.setAttribute("opacity", 1);
    tip.textContent =
      series[i][0] +
      " · $" +
      series[i][1].toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    tip.style.left = (x / vb.width) * r.width + "px";
    tip.style.top = (y / vb.height) * r.height - 8 + "px";
    tip.classList.add("on");
  });
  svg.addEventListener("mouseleave", function () {
    line.setAttribute("opacity", 0);
    dot.setAttribute("opacity", 0);
    tip.classList.remove("on");
  });
});

// Invite page: copy the invite link. The element is absent on every other
// page, so this is a no-op there.
var copyBtn = document.getElementById("copy-link");
if (copyBtn) {
  copyBtn.addEventListener("click", function () {
    var btn = this;
    var input = document.getElementById("invite-link");
    input.select();
    navigator.clipboard.writeText(input.value).then(function () {
      var label = btn.lastChild;
      label.textContent = btn.dataset.copied;
      setTimeout(function () {
        label.textContent = "Copy";
      }, 1500);
    });
  });
}

// Confirm before a destructive form submit (dashboard "Disconnect"), in
// place of an inline onsubmit="return confirm(...)" handler.
document.querySelectorAll("form[data-confirm]").forEach(function (form) {
  form.addEventListener("submit", function (e) {
    if (!window.confirm(form.dataset.confirm)) e.preventDefault();
  });
});

// Auto-submit a filter control (marketplace "Show incubating" checkbox), in
// place of an inline onchange="this.form.submit()" handler.
document.querySelectorAll("[data-autosubmit]").forEach(function (el) {
  el.addEventListener("change", function () {
    this.form.submit();
  });
});
