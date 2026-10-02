'use strict';

const { traverse, resolveChain, unwrapTS, canonicalName, lineOf } = require('./ast_utils');

const EVAL_NAMES = new Set(['eval']);
const FUNCTION_NAMES = new Set(['Function']);
const TIMER_NAMES = new Set(['setTimeout', 'setInterval']);
const ATOB_NAMES = new Set(['atob']);
const FROM_CHAR_CODE_NAMES = new Set(['String.fromCharCode']);

function nameOfCallee(callee, scope) {
  const resolved = resolveChain(callee, scope);
  return canonicalName(resolved);
}

function analyze(ast) {
  const signals = [];
  let longBase64Count = 0;

  const visitCallLike = (p) => {
    const callee = unwrapTS(p.node.callee);
    if (!callee) return;
    if (
      callee.type !== 'Identifier' &&
      callee.type !== 'MemberExpression' &&
      callee.type !== 'OptionalMemberExpression'
    ) return;

    const name = nameOfCallee(callee, p.scope);
    if (!name) return;

    if (EVAL_NAMES.has(name)) {
      const a0 = p.node.arguments[0];
      signals.push({
        type: a0 && a0.type === 'StringLiteral' ? 'eval_string' : 'eval_dynamic',
        line: lineOf(p.node),
      });
    } else if (FUNCTION_NAMES.has(name)) {
      signals.push({ type: 'function_constructor', line: lineOf(p.node) });
    } else if (TIMER_NAMES.has(name)) {
      const a0 = p.node.arguments[0];
      if (a0 && a0.type === 'StringLiteral') {
        signals.push({ type: 'timer_with_string', line: lineOf(p.node) });
      }
    } else if (ATOB_NAMES.has(name)) {
      signals.push({ type: 'atob', line: lineOf(p.node) });
    } else if (FROM_CHAR_CODE_NAMES.has(name) && p.node.arguments.length >= 8) {
      signals.push({ type: 'fromcharcode_chain', line: lineOf(p.node), arg_count: p.node.arguments.length });
    }
  };

  traverse(ast, {
    CallExpression: visitCallLike,
    OptionalCallExpression: visitCallLike,
    NewExpression(p) {
      const callee = unwrapTS(p.node.callee);
      if (!callee) return;
      if (
        callee.type !== 'Identifier' &&
        callee.type !== 'MemberExpression' &&
        callee.type !== 'OptionalMemberExpression'
      ) return;
      const name = nameOfCallee(callee, p.scope);
      if (name && FUNCTION_NAMES.has(name)) {
        signals.push({ type: 'function_constructor', line: lineOf(p.node) });
      }
    },
    StringLiteral(p) {
      const v = p.node.value;
      if (typeof v === 'string' && v.length >= 200 && /^[A-Za-z0-9+/=]+$/.test(v)) {
        longBase64Count++;
      }
    },
  });

  if (longBase64Count >= 3) {
    signals.push({ type: 'long_base64_strings', count: longBase64Count });
  }

  return { signals };
}

module.exports = { analyze };