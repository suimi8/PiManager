"use strict";

const VSIX_NAME_RE = /^pi-manager-pi-cursor-([0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]+)?)\.vsix$/i;

function parseVersion(value) {
  const match = String(value || "")
    .trim()
    .replace(/^[vV]/, "")
    .match(/^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?/);
  if (!match) return null;
  return [1, 2, 3, 4].map((index) => Number.parseInt(match[index] || "0", 10));
}

function compareVersions(left, right) {
  const a = parseVersion(left);
  const b = parseVersion(right);
  if (!a || !b) return 0;
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    const delta = (a[index] || 0) - (b[index] || 0);
    if (delta) return delta > 0 ? 1 : -1;
  }
  return 0;
}

function findVsixAsset(release) {
  const assets = Array.isArray(release && release.assets) ? release.assets : [];
  const candidates = [];
  for (const asset of assets) {
    const name = String((asset && asset.name) || "");
    const match = name.match(VSIX_NAME_RE);
    const url = String((asset && asset.browser_download_url) || "");
    if (!match || !url) continue;
    candidates.push({ name, version: match[1], url });
  }
  candidates.sort((left, right) => compareVersions(right.version, left.version));
  return candidates[0] || null;
}

function vsixUpdateInfo(localVersion, release) {
  const asset = findVsixAsset(release);
  const releaseUrl = String((release && release.html_url) || "");
  if (!asset) {
    return {
      ok: true,
      local: localVersion,
      remote: null,
      hasUpdate: false,
      asset: null,
      releaseUrl,
      message: "最新 PiManager Release 中没有找到 VSIX",
    };
  }
  const hasUpdate = compareVersions(asset.version, localVersion) > 0;
  return {
    ok: true,
    local: localVersion,
    remote: asset.version,
    hasUpdate,
    asset,
    releaseUrl,
    message: hasUpdate
      ? `发现 Pi Cursor 扩展 v${asset.version}（当前 v${localVersion}）`
      : `Pi Cursor 扩展已是最新（本地 v${localVersion}，Release v${asset.version}）`,
  };
}

module.exports = {
  compareVersions,
  findVsixAsset,
  parseVersion,
  vsixUpdateInfo,
};
