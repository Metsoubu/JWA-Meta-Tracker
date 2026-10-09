// Applies the saved theme before the page paints (dark is the default).
(function () {
  var theme = "dark";
  try {
    var saved = localStorage.getItem("jwa-theme");
    if (saved === "light" || saved === "dark") theme = saved;
  } catch (e) { /* storage unavailable: keep dark */ }
  document.documentElement.setAttribute("data-theme", theme);
})();
