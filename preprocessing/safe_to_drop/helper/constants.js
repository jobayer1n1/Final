'use strict';

// Permissions that indicate the extension can READ user data.
const DATA_PERMISSIONS = new Set([
  'tabs', 'activeTab', 'scripting', 'webRequest',
  'declarativeNetRequest', 'declarativeNetRequestFeedback',
  'cookies', 'storage', 'unlimitedStorage',
  'history', 'bookmarks', 'downloads', 'downloads.open',
  'clipboardRead', 'clipboardWrite',
  'management', 'debugger', 'nativeMessaging', 'userScripts',
  'offscreen', 'webNavigation', 'topSites', 'sessions',
  'browsingData', 'pageCapture', 'tabCapture', 'desktopCapture',
  'identity', 'geolocation', 'contextMenus', 'privacy',
  'search', 'fontSettings', 'accessibilityFeatures.read',
]);

// Permissions that indicate the extension can SEND data out.
const EXFIL_PERMISSIONS = new Set([
  'nativeMessaging', 'debugger', 'proxy', 'webRequestBlocking',
  'declarativeNetRequestWithHostAccess',
]);

// chrome.<path> / browser.<path> read APIs (path excludes the root namespace).
const CHROME_DATA_APIS = new Set([
  // --- cookies ---
  'cookies.get', 'cookies.getAll',
  'cookies.onChanged.addListener',      // ← ADDED: passive cookie harvest
  'cookies.onChanged.removeListener',   // ← ADDED (cheap, symmetric)

  // --- storage ---
  'storage.local.get', 'storage.sync.get', 'storage.session.get', 'storage.managed.get',
  'storage.onChanged.addListener',      // ← ADDED: passive storage harvest

  // --- history ---
  'history.search', 'history.getVisits',
  'history.onVisited.addListener',      // ← ADDED: passive history harvest

  // --- bookmarks ---
  'bookmarks.getTree', 'bookmarks.search', 'bookmarks.get',
  'bookmarks.onCreated.addListener',    // ← ADDED: passive bookmark harvest

  // --- downloads ---
  'downloads.search',

  // --- tabs / scripting ---
  'tabs.query', 'tabs.get', 'tabs.captureVisibleTab', 'tabs.executeScript',
  'scripting.executeScript', 'scripting.getRegisteredContentScripts',

  // --- webRequest (listener-based reads) ---
  'webRequest.onBeforeRequest', 'webRequest.onBeforeSendHeaders',
  'webRequest.onHeadersReceived', 'webRequest.onCompleted',
  'webNavigation.onCommitted', 'webNavigation.onCompleted',

  // --- metadata about the browser/extension ---
  'management.getAll', 'management.get',
  'topSites.get', 'sessions.getRecentlyClosed',
  'identity.getAuthToken', 'identity.getProfileUserInfo', 'identity.getAccounts',

  // --- capture ---
  'tabCapture.capture', 'tabCapture.getMediaStreamId',
  'desktopCapture.chooseDesktopMedia',
  'pageCapture.saveAsMHTML',

  // --- misc ---
  'browsingData.remove',
  'offscreen.createDocument',
]);

const CHROME_EXFIL_APIS = new Set([
  // --- cookies ---
  'cookies.set', 'cookies.remove',

  // --- tabs / downloads ---
  'tabs.update', 'tabs.create', 'tabs.sendMessage',
  'downloads.download',

  // --- network rule injection ---
  'declarativeNetRequest.updateDynamicRules', 'declarativeNetRequest.updateSessionRules',

  // --- out-of-process egress ---
  'runtime.sendNativeMessage', 'runtime.sendMessage', 'runtime.connectNative',
  'management.launchApp',
  'identity.launchWebAuthFlow',
  'scripting.registerContentScripts',
]);

// Web-platform sink identifiers (call or new).
const NETWORK_CALL_SINKS = new Set([
  'fetch',
  'navigator.sendBeacon',
  'XMLHttpRequest', 'WebSocket', 'EventSource',
  'navigator.share',
]);

// Member-name assignments that indicate exfil.
const NETWORK_MEMBER_SINKS = new Set(['src', 'href', 'action', 'formAction']);

// Web data sources (matched as a prefix of the resolved member path).
const WEB_DATA_ROOTS = [
  'document.cookie',
  'document.forms',
  'document.referrer',
  'localStorage', 'sessionStorage',
  'window.localStorage', 'window.sessionStorage',
  'window.name',
  'navigator.clipboard',
  'navigator.userAgent', 'navigator.userAgentData',
  'indexedDB',
];

const WEB_DATA_CALLS = new Set([
  'navigator.clipboard.read', 'navigator.clipboard.readText',
  'document.getSelection', 'window.getSelection', 'getSelection',
  'document.querySelector', 'document.querySelectorAll', 'document.getElementById',
  'indexedDB.open',
]);

const DATA_LISTENER_EVENTS = new Set([
  'input', 'change', 'submit', 'keydown', 'keypress', 'keyup',
  'paste', 'copy', 'cut', 'beforeinput',
]);

module.exports = {
  DATA_PERMISSIONS,
  EXFIL_PERMISSIONS,
  CHROME_DATA_APIS,
  CHROME_EXFIL_APIS,
  NETWORK_CALL_SINKS,
  NETWORK_MEMBER_SINKS,
  WEB_DATA_ROOTS,
  WEB_DATA_CALLS,
  DATA_LISTENER_EVENTS,
};