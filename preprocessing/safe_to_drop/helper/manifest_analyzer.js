'use strict';

const fs = require('fs');
const { DATA_PERMISSIONS, EXFIL_PERMISSIONS } = require('./constants');

function isExternalHostPattern(pattern) {
  if (typeof pattern !== 'string') return false;
  const p = pattern.trim();
  if (!p) return false;
  if (p === '<all_urls>') return true;
  if (/^https?:\/\//i.test(p)) return true;
  if (/^\*:\/\//.test(p)) return true;
  return false;
}

function analyze(manifestPath) {
  const result = {
    readable: false,
    errors: [],
    manifest_version: null,
    data_signals: [],
    exfil_signals: [],
    external_hosts: [],
    has_content_scripts: false,
  };

  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf-8'));
    result.readable = true;
  } catch (err) {
    result.errors.push(`manifest_unreadable: ${err.message}`);
    return result;
  }

  result.manifest_version = manifest.manifest_version || null;

  for (const key of ['permissions', 'optional_permissions']) {
    if (!Array.isArray(manifest[key])) continue;
    const tag = key.startsWith('optional') ? '_opt' : '';
    for (const p of manifest[key]) {
      if (typeof p !== 'string') continue;
      if (DATA_PERMISSIONS.has(p)) result.data_signals.push(`perm${tag}:${p}`);
      if (EXFIL_PERMISSIONS.has(p)) result.exfil_signals.push(`perm${tag}:${p}`);
    }
  }

  for (const key of ['host_permissions', 'optional_host_permissions']) {
    if (!Array.isArray(manifest[key])) continue;
    const tag = key.startsWith('optional') ? '_opt' : '';
    for (const h of manifest[key]) {
      if (isExternalHostPattern(h)) {
        result.exfil_signals.push(`host${tag}:${h}`);
        result.external_hosts.push(h);
      }
    }
  }

  if (Array.isArray(manifest.content_scripts) && manifest.content_scripts.length > 0) {
    result.has_content_scripts = true;
    result.data_signals.push('content_scripts');
    for (const cs of manifest.content_scripts) {
      for (const m of (cs.matches || [])) {
        if (isExternalHostPattern(m)) {
          result.exfil_signals.push(`cs_match:${m}`);
          result.external_hosts.push(m);
        }
      }
    }
  }

  if (manifest.externally_connectable && Array.isArray(manifest.externally_connectable.matches)) {
    for (const m of manifest.externally_connectable.matches) {
      if (isExternalHostPattern(m)) {
        result.exfil_signals.push(`externally_connectable:${m}`);
        result.external_hosts.push(m);
      }
    }
  }

  result.external_hosts = Array.from(new Set(result.external_hosts)).sort();
  return result;
}

module.exports = { analyze, isExternalHostPattern };