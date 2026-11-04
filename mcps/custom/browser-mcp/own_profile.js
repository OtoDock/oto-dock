"use strict";
// Own-browser mode's profile: the machine setting "Browser profile"
// (OTO_BROWSER_PROFILE, set by the proxy from the machine row) names the
// Chrome/Edge/Brave profile that has the Playwright Extension, as the browser
// shows it. Chrome opens a link in its last-used profile, which may lack the
// extension, so the connect page is opened with that profile's FOLDER
// (--profile-directory, profile_hook.js). The display names live in the
// user data folder's "Local State" (profile.info_cache: folder → names).

const fs = require("fs");
const path = require("path");

function isProfileFolder(name) {
  return name === "Default" || /^Profile \d+$/.test(name);
}

function folderOrder(a, b) {
  const n = (f) => (f === "Default" ? -1 : Number(f.slice("Profile ".length)));
  return n(a) - n(b);
}

function readInfoCache(userDataDir) {
  try {
    const state = JSON.parse(fs.readFileSync(path.join(userDataDir, "Local State"), "utf8"));
    const cache = state && state.profile && state.profile.info_cache;
    return cache && typeof cache === "object" ? cache : null;
  } catch (_) {
    return null;
  }
}

// Every profile of the browser: its folder, the name the browser shows,
// every name a person may type for it, and whether the extension is in it
// (`hasExtension(profileDir)`). A Google-signed profile left on its default
// name shows the account's name instead (is_using_default_name).
function browserProfiles(userDataDir, hasExtension) {
  const cache = readInfoCache(userDataDir) || {};
  const folders = new Set(Object.keys(cache).filter(isProfileFolder));
  try {
    for (const entry of fs.readdirSync(userDataDir))
      if (isProfileFolder(entry)) folders.add(entry);
  } catch (_) {
    /* no user data folder: no profiles */
  }
  return [...folders].sort(folderOrder).map((folder) => {
    const info = cache[folder] && typeof cache[folder] === "object" ? cache[folder] : {};
    const text = (v) => (typeof v === "string" && v.trim() ? v.trim() : "");
    const gaia = text(info.gaia_given_name) || text(info.gaia_name);
    const shown = (info.is_using_default_name && gaia) || text(info.name) || folder;
    const names = [info.name, info.gaia_given_name, info.gaia_name, info.user_name, info.shortcut_name]
      .map(text).filter(Boolean);
    return { folder, shown, names, extension: Boolean(hasExtension(path.join(userDataDir, folder))) };
  });
}

// The browser's name as a person reads it, from its executable.
function browserLabel(exePath) {
  const exe = String(exePath || "").toLowerCase();
  return exe.includes("edge") ? "Edge" : exe.includes("brave") ? "Brave"
    : exe.includes("vivaldi") ? "Vivaldi" : exe.includes("chromium") ? "Chromium" : "Chrome";
}

function profileList(profiles) {
  return profiles.map((p) => (p.shown === p.folder ? p.folder : `${p.shown} (${p.folder})`)).join(", ");
}

// The folder the connect page opens in: `{folder}` ("" = the browser's own
// choice), `{error}` for a setting that names no profile, and `several`
// (the profiles holding the extension) when the setting is empty and more
// than one has it, which the connect timeout names.
function pickProfile(setting, profiles, browserName) {
  const want = String(setting || "").trim();
  if (!want) {
    const withExtension = profiles.filter((p) => p.extension);
    if (withExtension.length === 1) return { folder: withExtension[0].folder, several: [] };
    return { folder: "", several: withExtension.length > 1 ? withExtension : [] };
  }
  const low = want.toLowerCase();
  const hits = profiles.filter(
    (p) => p.folder.toLowerCase() === low || p.names.some((n) => n.toLowerCase() === low)
  );
  const usable = hits.length > 1 ? hits.filter((p) => p.extension) : hits;
  if (usable.length === 1) return { folder: usable[0].folder, several: [] };
  if (!profiles.length && isProfileFolder(want)) return { folder: want, several: [] };
  const has = profiles.length ? ` This browser has: ${profileList(profiles)}.` : "";
  const what = hits.length > 1 ? `More than one ${browserName} profile is named` : `No ${browserName} profile is named`;
  return {
    error: `${what} "${want}".${has} Set Browser profile in the machine's settings to one of them.`,
    several: [],
  };
}

module.exports = { browserProfiles, pickProfile, profileList, isProfileFolder, browserLabel };
