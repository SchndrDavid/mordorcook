/* Offline support.
 *
 * The kitchen is where the connection is worst, so a recipe you have already
 * opened stays readable when the network drops. Writes are never queued or
 * faked: if the server cannot be reached, saving fails and says so.
 */
"use strict";

const VERSION = "mordorcook-v1";
const SHELL = VERSION + "-shell";
const DATA = VERSION + "-data";
const PHOTOS = VERSION + "-photos";

const SHELL_FILES = ["./", "./index.html", "./manifest.webmanifest",
                     "./icons/icon-192.png", "./icons/icon-512.png"];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(SHELL)
      .then(function (cache) { return cache.addAll(SHELL_FILES); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys
        .filter(function (key) { return key.indexOf(VERSION) !== 0; })
        .map(function (key) { return caches.delete(key); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  const request = event.request;

  // Anything that changes data goes straight to the server, always.
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // The shell answers every in-app address, so a reload while offline still
  // lands in the app rather than on the browser's error page.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(function () {
        return caches.match("./index.html", { ignoreSearch: true });
      })
    );
    return;
  }

  // Photo bytes never change once written, so serve them from the cache.
  if (/\/api\/photos\/[0-9a-f]{32}$/.test(url.pathname)) {
    event.respondWith(
      caches.open(PHOTOS).then(function (cache) {
        return cache.match(request).then(function (hit) {
          if (hit) return hit;
          return fetch(request).then(function (response) {
            if (response.ok) cache.put(request, response.clone());
            return response;
          });
        });
      })
    );
    return;
  }

  // Recipe reads: fresh when possible, last known copy when not.
  if (url.pathname.indexOf("/api/") !== -1) {
    event.respondWith(
      fetch(request).then(function (response) {
        if (response.ok) {
          const copy = response.clone();
          caches.open(DATA).then(function (cache) { cache.put(request, copy); });
        }
        return response;
      }).catch(function () {
        return caches.match(request).then(function (hit) {
          return hit || Response.json(
            { detail: "You are offline and this has not been loaded before." },
            { status: 503 }
          );
        });
      })
    );
    return;
  }

  // Everything else: cache first, it is all static.
  event.respondWith(
    caches.match(request).then(function (hit) { return hit || fetch(request); })
  );
});
