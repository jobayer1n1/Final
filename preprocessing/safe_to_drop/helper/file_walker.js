'use strict';

const fs = require('fs');
const path = require('path');

const SKIP_DIRS = new Set(['node_modules', '_metadata', '.git', '__MACOSX']);
const JS_EXT = new Set(['.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx']);
const MAX_FILE_SIZE = 5 * 1024 * 1024;

function walkJsFiles(extDir) {
  const out = [];
  const stack = [extDir];
  while (stack.length) {
    const dir = stack.pop();
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); }
    catch { continue; }
    for (const e of entries) {
      const full = path.join(dir, e.name);
      if (e.isDirectory()) {
        if (SKIP_DIRS.has(e.name)) continue;
        stack.push(full);
      } else if (e.isFile()) {
        const ext = path.extname(e.name).toLowerCase();
        if (!JS_EXT.has(ext)) continue;
        let skipped = null;
        try {
          const st = fs.statSync(full);
          if (st.size > MAX_FILE_SIZE) skipped = 'too_large';
        } catch { /* ignore */ }
        out.push({ path: full, skipped });
      }
    }
  }
  return out;
}

module.exports = { walkJsFiles };