// The iOS app's channel for the dashboard, injected into the main frame at
// document start: `window.OtoDockNative` with the Android object's shape
// (`postMessage(string)`, `addEventListener('message', fn)`), over WebKit's
// message handler of the same name. A reply the app returns for a posted
// message is delivered to the listeners as `{data: json}`, so the
// dashboard's `{id, r}` protocol holds. The app appends the call with the
// active installation's origin, so the object exists on that origin's
// documents and on the bundled setup page only, as the Android object does;
// a page of any other origin (a sign-in's sibling host) has no object, though
// WebKit's handlers are visible to every page and the app's judge refuses
// what they carry. The app judges every message on its sender; this file
// holds no other rule. On the bundled setup page the same handler also stands behind
// `window.Android`, the synchronous object that page calls, with the
// installs list the app pushes after each load.
(function (activeOrigin) {
  'use strict'
  var handlers = window.webkit && window.webkit.messageHandlers
  var native = handlers && handlers.OtoDockNative
  if (!native) return
  var setupPage = location.protocol === 'capacitor:' && /\/setup\.html$/.test(location.pathname)
  if (!setupPage && !(activeOrigin && location.origin === activeOrigin)) return
  var listeners = []
  function deliver (data) {
    for (var i = 0; i < listeners.length; i++) {
      try { listeners[i]({ data: data }) } catch (e) { /* a listener's own error */ }
    }
  }
  var channel = {
    postMessage: function (message) {
      if (typeof message !== 'string') throw new TypeError('a message is a string')
      var answer = native.postMessage(message)
      if (answer && typeof answer.then === 'function') {
        answer.then(function (reply) { if (typeof reply === 'string') deliver(reply) }, function () {})
      }
    },
    addEventListener: function (type, listener) {
      if (type === 'message' && typeof listener === 'function') listeners.push(listener)
    }
  }
  Object.defineProperty(window, 'OtoDockNative', { value: channel, writable: false, configurable: false })

  if (setupPage) {
    var installs = []
    function post (method, args) {
      channel.postMessage(JSON.stringify({ m: 'setup.' + method, a: args || [] }))
    }
    window.__otodockSetInstalls = function (list) { installs = Array.isArray(list) ? list : [] }
    window.Android = {
      getInstallations: function () { return JSON.stringify(installs) },
      setFavorite: function (id) {
        for (var i = 0; i < installs.length; i++) installs[i].favorite = installs[i].id === id
        post('setFavorite', [id])
      },
      openPrivacyPolicy: function () { post('openPrivacyPolicy') },
      cancelAddInstallation: function () { post('cancelAddInstallation') },
      switchFromSetup: function (id) { post('switchFromSetup', [id]) },
      addInstallation: function (url) { post('addInstallation', [url]) }
    }
  }
})
