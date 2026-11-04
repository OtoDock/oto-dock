"use strict";
// Own-browser mode's profile: the display name mapped to its folder through
// "Local State", the one profile with the extension when the setting is
// empty, the names listed when nothing matches, and the spawn hook's
// argument rewrite. Run with: node tests/own_profile.test.js
const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { browserProfiles, pickProfile, profileList } = require("../own_profile.js");
const { withProfile, CONNECT_PAGE } = require("../profile_hook.js");
const { extensionInProfile, EXTENSION_ID } = require("../index.js");

function userData(cache, folders, withExtension) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "own-profile-"));
  if (cache) fs.writeFileSync(path.join(dir, "Local State"), JSON.stringify({ profile: { info_cache: cache } }));
  for (const f of folders) {
    fs.mkdirSync(path.join(dir, f, "Extensions"), { recursive: true });
    if (withExtension.includes(f)) fs.mkdirSync(path.join(dir, f, "Extensions", EXTENSION_ID));
  }
  return dir;
}

// The operator's desktop: two profiles he named, the extension in both.
const two = userData(
  { Default: { name: "Dimitris" }, "Profile 1": { name: "Work", is_using_default_name: false } },
  ["Default", "Profile 1", "Guest Profile"], ["Default", "Profile 1"]);
const profiles = browserProfiles(two, extensionInProfile);
{
  assert.deepStrictEqual(profiles.map((p) => [p.folder, p.shown, p.extension]),
    [["Default", "Dimitris", true], ["Profile 1", "Work", true]]);
  // The name maps to the folder, case and spaces aside; the folder name works too.
  assert.deepStrictEqual(pickProfile(" work ", profiles, "Chrome").folder, "Profile 1");
  assert.deepStrictEqual(pickProfile("Profile 1", profiles, "Chrome").folder, "Profile 1");
  // Empty with the extension in both: the browser's own choice, both named
  // for the connect timeout.
  const empty = pickProfile("", profiles, "Chrome");
  assert.strictEqual(empty.folder, "");
  assert.deepStrictEqual(empty.several.map((p) => p.folder), ["Default", "Profile 1"]);
  // A name nothing has: the profiles listed, the setting named.
  const none = pickProfile("Home", profiles, "Chrome");
  assert.strictEqual(none.error,
    'No Chrome profile is named "Home". This browser has: Dimitris (Default), Work (Profile 1). ' +
    "Set Browser profile in the machine's settings to one of them.");
  assert.strictEqual(profileList(profiles), "Dimitris (Default), Work (Profile 1)");
}

// One profile with the extension: picked when the setting is empty.
{
  const dir = userData({ Default: { name: "Person 1" }, "Profile 2": { name: "Ext" } },
    ["Default", "Profile 2"], ["Profile 2"]);
  const pick = pickProfile("", browserProfiles(dir, extensionInProfile), "Edge");
  assert.strictEqual(pick.folder, "Profile 2");
  assert.deepStrictEqual(pick.several, []);
}

// A Google-signed profile on its default name shows the account's name, and
// every name Chrome shows matches.
{
  const dir = userData({ Default: { name: "Person 1", is_using_default_name: true, gaia_given_name: "Ana",
    gaia_name: "Ana Lopez", user_name: "ana@example.test" } }, ["Default"], []);
  const ps = browserProfiles(dir, extensionInProfile);
  assert.strictEqual(ps[0].shown, "Ana");
  for (const name of ["Ana", "ana lopez", "ana@example.test", "Person 1"])
    assert.strictEqual(pickProfile(name, ps, "Chrome").folder, "Default");
}

// Two profiles with one name: the one with the extension, else a refusal.
{
  const dir = userData({ Default: { name: "Me" }, "Profile 1": { name: "Me" } },
    ["Default", "Profile 1"], ["Profile 1"]);
  assert.strictEqual(pickProfile("me", browserProfiles(dir, extensionInProfile), "Chrome").folder, "Profile 1");
  const neither = userData({ Default: { name: "Me" }, "Profile 1": { name: "Me" } }, ["Default", "Profile 1"], []);
  assert.match(pickProfile("me", browserProfiles(neither, extensionInProfile), "Chrome").error,
    /^More than one Chrome profile is named "me"\./);
}

// No Local State (a browser that never ran): a folder name still works.
{
  const ps = browserProfiles(path.join(os.tmpdir(), "own-profile-missing-dir"), extensionInProfile);
  assert.deepStrictEqual(ps, []);
  assert.strictEqual(pickProfile("Profile 3", ps, "Chrome").folder, "Profile 3");
  assert.match(pickProfile("Work", ps, "Chrome").error, /^No Chrome profile is named "Work"\. Set Browser/);
}

// The hook: only the connect page's spawn, before the page, once.
{
  const page = `${CONNECT_PAGE}?mcpRelayUrl=ws%3A%2F%2F127.0.0.1%3A1`;
  assert.deepStrictEqual(withProfile([page], "Profile 1"), ["--profile-directory=Profile 1", page]);
  assert.deepStrictEqual(withProfile(["--no-sandbox", page], "Default"),
    ["--no-sandbox", "--profile-directory=Default", page]);
  assert.deepStrictEqual(withProfile(["--profile-directory=Profile 2", page], "Profile 1"),
    ["--profile-directory=Profile 2", page]);
  assert.deepStrictEqual(withProfile(["https://example.com"], "Profile 1"), ["https://example.com"]);
  assert.deepStrictEqual(withProfile([page], ""), [page]);
  assert.strictEqual(withProfile(undefined, "Profile 1"), undefined);
}

// The browser's name in a sentence.
{
  const { browserLabel } = require("../own_profile.js");
  assert.strictEqual(browserLabel("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"), "Chrome");
  assert.strictEqual(browserLabel("/usr/bin/microsoft-edge"), "Edge");
  assert.strictEqual(browserLabel("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"), "Brave");
}

console.log("own_profile tests passed");
