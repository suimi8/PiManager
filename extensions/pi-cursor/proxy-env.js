"use strict";

// Proxy env derived from the desktop app's pi-manager.json; shared by the
// quick-ask path and terminal launches so both honour the configured proxy.
function proxyEnvFromManagerConfig(manager) {
  const env = {};
  if (manager && manager.proxy_enabled && manager.proxy_url) {
    const url = String(manager.proxy_url);
    env.HTTP_PROXY = url;
    env.HTTPS_PROXY = url;
    env.http_proxy = url;
    env.https_proxy = url;
  }
  return env;
}

module.exports = { proxyEnvFromManagerConfig };
