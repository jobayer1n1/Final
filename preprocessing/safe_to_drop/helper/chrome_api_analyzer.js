'use strict';

const { traverse, resolveChain, unwrapTS, lineOf } = require('./ast_utils');
const { CHROME_DATA_APIS, CHROME_EXFIL_APIS } = require('./constants');

const CHROME_ROOTS = new Set(['chrome', 'browser']);

const COOKIE_TARGET_APIS = new Set(['cookies.set', 'cookies.remove']);

/** Pull the chrome-api path from a resolved chain, or null if not chrome. */
function chromeApiPath(resolved) {
  if (!resolved || !resolved.resolved) return null;
  const { root, path } = resolved;
  if (CHROME_ROOTS.has(root)) {
    if (path.length < 2) return null;
    return path.join('.');
  }
  // window.chrome.* / globalThis.chrome.* / self.chrome.*
  if (path.length >= 3 && CHROME_ROOTS.has(path[0])) {
    return path.slice(1).join('.');
  }
  return null;
}

function matchesPrefix(apiPath, set) {
  if (!apiPath) return false;
  if (set.has(apiPath)) return true;
  for (const p of set) if (apiPath.startsWith(p + '.')) return true;
  return false;
}

// ← ADDED
/**
 * Extract statically-resolvable `url` and `domain` properties from the first
 * argument of `chrome.cookies.set(...)` / `chrome.cookies.remove(...)`.
 * Only StringLiteral and no-interpolation TemplateLiteral values are recorded.
 * Dynamic values are intentionally ignored here (the API call itself is
 * already an exfil signal that blocks drop).
 */
function extractCookieTargets(node) {
  const out = { url: null, domain: null, has_dynamic_first_arg: false };
  const a0 = node.arguments && node.arguments[0] ? unwrapTS(node.arguments[0]) : null;
  if (!a0) return out;
  if (a0.type !== 'ObjectExpression') {
    out.has_dynamic_first_arg = true;
    return out;
  }
  for (const prop of a0.properties) {
    if (prop.type === 'SpreadElement') { out.has_dynamic_first_arg = true; continue; }
    if (prop.type !== 'ObjectProperty') continue;

    let key = null;
    if (prop.computed) {
      const k = unwrapTS(prop.key);
      if (k.type === 'StringLiteral') key = k.value;
      else if (k.type === 'TemplateLiteral' && k.expressions.length === 0) {
        key = k.quasis.map((q) => q.value.cooked || '').join('');
      }
    } else {
      const k = unwrapTS(prop.key);
      if (k.type === 'Identifier') key = k.name;
      else if (k.type === 'StringLiteral') key = k.value;
    }
    if (key !== 'url' && key !== 'domain') continue;

    const v = unwrapTS(prop.value);
    if (v && v.type === 'StringLiteral') out[key] = v.value;
    else if (v && v.type === 'TemplateLiteral' && v.expressions.length === 0) {
      out[key] = v.quasis.map((q) => q.value.cooked || '').join('');
    } else {
      out.has_dynamic_first_arg = true;
    }
  }
  return out;
}

function visitCalldown(path, out) {
  const node = path.node;
  const callee = unwrapTS(node.callee);
  if (!callee) return;

  if (
    callee.type !== 'Identifier' &&
    callee.type !== 'MemberExpression' &&
    callee.type !== 'OptionalMemberExpression'
  ) {
    return;
  }

  const resolved = resolveChain(callee, path.scope);
  const apiPath = chromeApiPath(resolved);
  if (!apiPath) return;

  const record = { api: apiPath, line: lineOf(node) };

  // ← ADDED: attach cookie target URL/domain when resolvable
  if (COOKIE_TARGET_APIS.has(apiPath)) {
    const targets = extractCookieTargets(node);
    if (targets.url) record.url_static = targets.url;
    if (targets.domain) record.domain_static = targets.domain;
    if (targets.has_dynamic_first_arg) record.has_dynamic_first_arg = true;
  }

  if (matchesPrefix(apiPath, CHROME_DATA_APIS)) out.data.push(record);
  else if (matchesPrefix(apiPath, CHROME_EXFIL_APIS)) out.exfil.push(record);
  else out.other.push(record);
}

function analyze(ast) {
  const data = [];
  const exfil = [];
  const other = [];
  const out = { data, exfil, other };

  traverse(ast, {
    CallExpression(p) { visitCalldown(p, out); },
    OptionalCallExpression(p) { visitCalldown(p, out); },
    NewExpression(p) {
      const callee = unwrapTS(p.node.callee);
      if (!callee) return;
      if (
        callee.type !== 'Identifier' &&
        callee.type !== 'MemberExpression' &&
        callee.type !== 'OptionalMemberExpression'
      ) return;
      const resolved = resolveChain(callee, p.scope);
      const apiPath = chromeApiPath(resolved);
      if (!apiPath) return;
      const record = { api: apiPath, line: lineOf(p.node) };
      if (COOKIE_TARGET_APIS.has(apiPath)) {
        const targets = extractCookieTargets(p.node);
        if (targets.url) record.url_static = targets.url;
        if (targets.domain) record.domain_static = targets.domain;
        if (targets.has_dynamic_first_arg) record.has_dynamic_first_arg = true;
      }
      if (matchesPrefix(apiPath, CHROME_DATA_APIS)) out.data.push(record);
      else if (matchesPrefix(apiPath, CHROME_EXFIL_APIS)) out.exfil.push(record);
      else out.other.push(record);
    },
  });

  return { data, exfil, other };
}

module.exports = { analyze };