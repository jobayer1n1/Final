'use strict';

const { traverse, resolveChain, unwrapTS, canonicalName, lineOf } = require('./ast_utils');
const { WEB_DATA_ROOTS, WEB_DATA_CALLS, DATA_LISTENER_EVENTS } = require('./constants');

function matchesRoot(canonical, roots) {
  for (const r of roots) {
    if (canonical === r || canonical.startsWith(r + '.')) return r;
  }
  return null;
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

function analyze(ast) {
  const sources = [];
  const seen = new Set();
  const push = (rec) => {
    const key = `${rec.kind}|${rec.path}|${rec.line}`;
    if (seen.has(key)) return;
    seen.add(key);
    sources.push(rec);
  };

  const visitMember = (p) => {
    const node = p.node;
    if (node.type !== 'MemberExpression' && node.type !== 'OptionalMemberExpression') return;
    const resolved = resolveChain(node, p.scope);
    const canonical = canonicalName(resolved);
    if (!canonical) return;
    const root = matchesRoot(canonical, WEB_DATA_ROOTS);
    if (!root) return;
    push({ kind: 'member', path: canonical, matched_root: root, line: lineOf(node) });
  };

  const visitCall = (p) => {
    const callee = unwrapTS(p.node.callee);
    if (!callee) return;

    // addEventListener('input'|'change'|...)
    if (callee.type === 'MemberExpression' || callee.type === 'OptionalMemberExpression') {
      const method = propertyNameOf(callee);
      if (method === 'addEventListener') {
        const arg0 = p.node.arguments[0];
        if (arg0 && arg0.type === 'StringLiteral' && DATA_LISTENER_EVENTS.has(arg0.value)) {
          const objResolved = resolveChain(callee.object, p.scope);
          const objPath = canonicalName(objResolved) || '<unresolved>';
          push({
            kind: 'listener',
            path: `${objPath}.addEventListener(${arg0.value})`,
            matched_root: 'addEventListener',
            line: lineOf(p.node),
          });
        }
      }
    }

    // navigator.clipboard.read / document.querySelector / getSelection / ...
    if (
      callee.type === 'Identifier' ||
      callee.type === 'MemberExpression' ||
      callee.type === 'OptionalMemberExpression'
    ) {
      const resolved = resolveChain(callee, p.scope);
      const canonical = canonicalName(resolved);
      if (canonical && WEB_DATA_CALLS.has(canonical)) {
        push({ kind: 'call', path: canonical, matched_root: canonical, line: lineOf(p.node) });
      }
    }
  };

  traverse(ast, {
    MemberExpression: visitMember,
    OptionalMemberExpression: visitMember,
    CallExpression: visitCall,
    OptionalCallExpression: visitCall,
    // Match bare `localStorage` / `sessionStorage` / `indexedDB` identifiers,
    // but skip when they are the object of a MemberExpression (that's covered above).
    Identifier(p) {
      const name = p.node.name;
      if (!WEB_DATA_ROOTS.includes(name)) return;
      const parent = p.parent;
      if (parent && (parent.type === 'MemberExpression' || parent.type === 'OptionalMemberExpression') && parent.object === p.node) return;
      push({ kind: 'identifier', path: name, matched_root: name, line: lineOf(p.node) });
    },
  });

  return { sources };
}

module.exports = { analyze };