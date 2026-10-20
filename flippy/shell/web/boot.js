// Start the app window: load the settings model, apply the window mode, show the route.
(async () => {
  "use strict";
  const F = window.F;
  F.call("mark", { name: "script" }).catch(() => {});
  await F.load();
  document.getElementById("version").textContent = F.state.version ? `Flippy ${F.state.version}` : "Flippy";
  const route = F.state.route || F.here();
  if (location.hash.slice(1) !== route) history.replaceState(null, "", "#" + route);
  F.render(route);
  if (route !== "setup") F.checkSetup();  // the sidebar's dot until setup is done (Setup checks by itself)
  if (F.state.demo) F.runPointerDemo(F.state.demo);  // flippy-ask demo-pointer, asked before the page loaded
  requestAnimationFrame(() => F.call("mark", { name: "painted" }).catch(() => {}));
})();
