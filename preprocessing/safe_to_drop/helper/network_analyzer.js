'use strict';

const net = require('net');
const { traverse, resolveChain, unwrapTS, canonicalName, lineOf } = require('./ast_utils');
const { NETWORK_CALL_SINKS, NETWORK_MEMBER_SINKS } = require('./constants');

// Additional sinks we match by name when the receiver is unresolved.
const FALLBACK_CALL_METHODS = new Set(['sendBeacon']);
const SENSITIVE_SET_ATTRIBUTES = new Set(['src', 'href', 'action', 'formaction']);

function ipv4Private(host) {
  const parts = host.split('.').map(Number);
  if (parts.length !== 4 || parts.some((n) => !Number.isInteger(n) || n < 0 || n > 255)) return false;
  const [a, b] = parts;
  return a === 127 || a === 10 || (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) || (a === 169 && b === 254) ||
    (a >= 224 && a <= 239) || a === 0 || a >= 240;
}

function ipv6Private(host) {
  const h = host.toLowerCase();
  if (h.startsWith('::ffff:')) {
    const v4 = h.slice(7);
    return net.isIPv4(v4) ? ipv4Private(v4) : true;
  }
  return h === '::' || h === '::1' || h.startsWith('fc') || h.startsWith('fd') ||
    /^fe[89ab]/.test(h) || h.startsWith('ff');
}

function isPublicHttpUrl(str) {
  if (typeof str !== 'string' || !/^https?:\/\//i.test(str)) return false;
  try {
    const u = new URL(str);
    let host = u.hostname.toLowerCase().replace(/\.+$/, '').replace(/^\[|\]$/g, '');
    if (!host || host === 'localhost' || host === 'local') return false;
    if (net.isIPv4(host)) return !ipv4Private(host);
    if (net.isIPv6(host)) return !ipv6Private(host);
    return host.includes('.');
  } catch { return false; }
}

function isPublicIpLiteral(str) {
  if (typeof str !== 'string') return false;
  if (net.isIPv4(str)) return !ipv4Private(str);
  if (net.isIPv6(str)) return !ipv6Private(str);
  return false;
}

function propertyNameOf(node) {
  if (!node) return null;
  if (node.computed) {
    const p = unwrapTS(node.property);
    if (p.type === 'StringLiteral') return p.value;
    if (p.type === 'TemplateLiteral' && p.expressions.length === 0) {
      return p.quasis.map((q) => q.value.cooked || '').join('');
    }
    return null;
  }
  const p = unwrapTS(node.property);
  return p.type === 'Identifier' ? p.name : null;
}

function handleCallLike(path, kind, sinks) {
  const callee = unwrapTS(path.node.callee);
  if (!callee) return;

  // 1. Fully resolved sink (fetch, navigator.sendBeacon, new WebSocket, ...)
  if (
    callee.type === 'Identifier' ||
    callee.type === 'MemberExpression' ||
    callee.type === 'OptionalMemberExpression'
  ) {
    const resolved = resolveChain(callee, path.scope);
    const canonical = canonicalName(resolved);
    if (canonical && NETWORK_CALL_SINKS.has(canonical)) {
      sinks.push({ kind, name: canonical, line: lineOf(path.node), via: 'resolved' });
      return;
    }
  }

  // 2. Unresolved receiver, known distinctive method name
  if (callee.type === 'MemberExpression' || callee.type === 'OptionalMemberExpression') {
    const method = propertyNameOf(callee);
    if (!method) return;
    if (FALLBACK_CALL_METHODS.has(method)) {
      sinks.push({ kind, name: `<unresolved>.${method}`, line: lineOf(path.node), via: 'fallback' });
      return;
    }
    if (method === 'setAttribute') {
      const a0 = path.node.arguments[0];
      const attr = a0 && a0.type === 'StringLiteral' ? a0.value.toLowerCase() : null;
      if (attr && SENSITIVE_SET_ATTRIBUTES.has(attr)) {
        sinks.push({ kind: 'setAttribute', name: `setAttribute(${attr})`, line: lineOf(path.node), via: 'fallback' });
      } else if (!attr) {
        sinks.push({ kind: 'setAttribute', name: 'setAttribute(dynamic)', line: lineOf(path.node), via: 'fallback' });
      }
    }
  }
}

function analyze(ast) {
  const sinks = [];
  const staticUrls = new Set();
  const staticIps = new Set();

  traverse(ast, {
    CallExpression(p) { handleCallLike(p, 'call', sinks); },
    OptionalCallExpression(p) { handleCallLike(p, 'call', sinks); },
    NewExpression(p) { handleCallLike(p, 'new', sinks); },

    AssignmentExpression(p) {
      const left = unwrapTS(p.node.left);
      if (!left) return;
      if (left.type !== 'MemberExpression' && left.type !== 'OptionalMemberExpression') return;
      const prop = propertyNameOf(left);
      if (!prop || !NETWORK_MEMBER_SINKS.has(prop)) return;

      const objResolved = resolveChain(left.object, p.scope);
      const canonical = canonicalName(objResolved) || '<unresolved>';
      sinks.push({
        kind: 'assign',
        name: `${canonical}.${prop}`,
        line: lineOf(p.node),
        via: objResolved && objResolved.resolved ? 'resolved' : 'unresolved',
      });
    },

    StringLiteral(p) {
      const v = p.node.value;
      if (typeof v !== 'string') return;
      if (isPublicHttpUrl(v)) staticUrls.add(v);
      else if (isPublicIpLiteral(v)) staticIps.add(v);
    },

    TemplateLiteral(p) {
      if (p.node.expressions.length !== 0) return;
      const v = p.node.quasis.map((q) => q.value.cooked || '').join('');
      if (isPublicHttpUrl(v)) staticUrls.add(v);
    },
  });

  return {
    sinks,
    static_urls: Array.from(staticUrls).sort(),
    static_ips: Array.from(staticIps).sort(),
  };
}

module.exports = { analyze, isPublicHttpUrl };