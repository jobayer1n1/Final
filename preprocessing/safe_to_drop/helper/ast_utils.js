'use strict';

const fs = require('fs');
const parser = require('@babel/parser');
const traverse = require('@babel/traverse').default;

// Identifiers that act as "roots" we resolve without a local binding.
const GLOBAL_ROOTS = new Set([
  'window', 'document', 'globalThis', 'self', 'top', 'parent', 'frames',
  'navigator', 'location', 'history', 'screen', 'performance',
  'localStorage', 'sessionStorage', 'indexedDB', 'crypto', 'caches',
  'chrome', 'browser',
  'fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource',
  'Image', 'Audio', 'Worker', 'SharedWorker', 'BroadcastChannel',
  'eval', 'Function', 'atob', 'btoa',
  'String', 'Number', 'Object', 'Array', 'JSON', 'Reflect',
  'setTimeout', 'setInterval', 'clearTimeout', 'clearInterval',
  'console',
]);

const MAX_DEPTH = 6;

function parseFile(filePath) {
  let code;
  try {
    code = fs.readFileSync(filePath, 'utf-8');
  } catch (err) {
    return { code: null, ast: null, error: `read_error: ${err.message}` };
  }
  try {
    const ast = parser.parse(code, {
      sourceType: 'unambiguous',
      plugins: [
        'typescript', 'jsx', 'decorators-legacy', 'classProperties',
        'optionalChaining', 'nullishCoalescingOperator', 'dynamicImport',
        'topLevelAwait', 'classPrivateProperties', 'classPrivateMethods',
        'importMeta', 'objectRestSpread',
      ],
      errorRecovery: true,
    });
    return { code, ast, error: null };
  } catch (err) {
    return { code, ast: null, error: `parse_error: ${err.message}` };
  }
}

/** Strip TS-only wrappers that don't change runtime semantics. */
function unwrapTS(node) {
  let n = node;
  while (
    n && (
      n.type === 'TSNonNullExpression' ||
      n.type === 'TSAsExpression' ||
      n.type === 'TSTypeAssertion' ||
      n.type === 'TSInstantiationExpression' ||
      n.type === 'ChainExpression' ||
      n.type === 'ParenthesizedExpression'
    )
  ) {
    n = n.expression;
  }
  return n;
}

/** Returns { id, init } for a VariableDeclarator binding, or null. */
function getDeclaratorInfo(binding) {
  if (!binding || !binding.path) return null;
  const node = binding.path.node;
  if (!node) return null;
  if (node.type === 'VariableDeclarator') {
    return { id: node.id, init: node.init };
  }
  if (node.type === 'Identifier' && binding.path.parentPath) {
    const parent = binding.path.parentPath.node;
    if (parent && parent.type === 'VariableDeclarator') {
      return { id: parent.id, init: parent.init };
    }
  }
  return null;
}

/** Build a map of identifierName -> resolvedPathInfo for a destructuring pattern. */
function extractBindingsFromPattern(pattern, base, depth = 0) {
  if (depth > MAX_DEPTH || !pattern) return {};
  pattern = unwrapTS(pattern);
  if (!pattern) return {};

  if (pattern.type === 'Identifier') {
    return { [pattern.name]: base };
  }

  if (pattern.type === 'AssignmentPattern') {
    return extractBindingsFromPattern(pattern.left, base, depth + 1);
  }

  if (pattern.type === 'ObjectPattern') {
    const out = {};
    for (const prop of pattern.properties) {
      if (prop.type !== 'ObjectProperty' && prop.type !== 'Property') continue;
      let key = null;
      if (prop.computed) {
        if (prop.key.type === 'StringLiteral') key = prop.key.value;
        else if (prop.key.type === 'TemplateLiteral' && prop.key.expressions.length === 0) {
          key = prop.key.quasis.map((q) => q.value.cooked || '').join('');
        } else continue;
      } else if (prop.key.type === 'Identifier') {
        key = prop.key.name;
      } else if (prop.key.type === 'StringLiteral' || prop.key.type === 'NumericLiteral') {
        key = String(prop.key.value);
      }
      if (key == null) continue;

      const newBase = {
        resolved: true,
        root: base.root,
        path: base.path.concat(key),
        fullPath: base.fullPath + '.' + key,
      };
      Object.assign(out, extractBindingsFromPattern(prop.value, newBase, depth + 1));
    }
    return out;
  }

  if (pattern.type === 'ArrayPattern') {
    const out = {};
    for (let i = 0; i < pattern.elements.length; i++) {
      const el = pattern.elements[i];
      if (!el) continue;
      const newBase = {
        resolved: true,
        root: base.root,
        path: base.path.concat(String(i)),
        fullPath: base.fullPath + '.' + i,
      };
      Object.assign(out, extractBindingsFromPattern(el, newBase, depth + 1));
    }
    return out;
  }

  return {};
}

/**
 * Resolve a chain like `chrome.cookies.set` (possibly through local bindings,
 * destructuring, `window.chrome.cookies.set`, TS wrappers, optional chains).
 * Returns { resolved, root, path, fullPath } or { resolved: false, reason }.
 */
function resolveChain(node, scope, depth = 0) {
  if (depth > MAX_DEPTH) return { resolved: false, reason: 'max_depth' };
  if (!node) return { resolved: false, reason: 'no_node' };

  node = unwrapTS(node);
  if (!node) return { resolved: false, reason: 'no_node' };

  if (node.type === 'Identifier') {
    const binding = scope ? scope.getBinding(node.name) : null;
    if (binding) {
      const info = getDeclaratorInfo(binding);
      if (info && info.init) {
        const bindingScope = binding.scope || scope;
        if (info.id.type !== 'Identifier') {
          const base = resolveChain(info.init, bindingScope, depth + 1);
          if (!base.resolved) return base;
          const mappings = extractBindingsFromPattern(info.id, base, depth + 1);
          if (mappings[node.name]) return mappings[node.name];
          return { resolved: false, reason: 'destructure_mapping_missing' };
        }
        return resolveChain(info.init, bindingScope, depth + 1);
      }
      return { resolved: false, reason: 'unresolvable_binding' };
    }

    if (GLOBAL_ROOTS.has(node.name)) {
      return {
        resolved: true,
        root: node.name,
        path: [],
        fullPath: node.name,
        via: 'global',
      };
    }
    return { resolved: false, reason: 'unknown_identifier' };
  }

  if (node.type === 'ThisExpression') {
    return { resolved: true, root: 'this', path: [], fullPath: 'this', via: 'this' };
  }

  if (node.type === 'MemberExpression' || node.type === 'OptionalMemberExpression') {
    const objResolved = resolveChain(node.object, scope, depth);
    if (!objResolved.resolved) return objResolved;

    let prop = null;
    if (node.computed) {
      const p = unwrapTS(node.property);
      if (p.type === 'StringLiteral') prop = p.value;
      else if (p.type === 'TemplateLiteral' && p.expressions.length === 0) {
        prop = p.quasis.map((q) => q.value.cooked || '').join('');
      } else {
        return { resolved: false, reason: 'dynamic_property' };
      }
    } else {
      const p = unwrapTS(node.property);
      if (p.type === 'Identifier') prop = p.name;
      else return { resolved: false, reason: 'unsupported_property' };
    }

    return {
      resolved: true,
      root: objResolved.root,
      path: objResolved.path.concat(prop),
      fullPath: objResolved.fullPath + '.' + prop,
      via: 'member',
    };
  }

  return { resolved: false, reason: 'unsupported_expression' };
}

function lineOf(node) {
  return node && node.loc ? node.loc.start.line : 0;
}

/**
 * Canonicalise a resolved chain by stripping common roots so callers can
 * match against a stable name:
 *   window.fetch         -> fetch
 *   globalThis.fetch     -> fetch
 *   window.navigator.sendBeacon -> navigator.sendBeacon
 *   chrome.cookies.set   -> chrome.cookies.set
 */
function canonicalName(resolved) {
  if (!resolved || !resolved.resolved) return null;
  const full = resolved.path.length
    ? `${resolved.root}.${resolved.path.join('.')}`
    : resolved.root;
  return full.replace(/^(window|globalThis|self|top|parent|frames|this)\./, '');
}

module.exports = {
  parseFile,
  resolveChain,
  unwrapTS,
  canonicalName,
  lineOf,
  traverse,
  GLOBAL_ROOTS,
};