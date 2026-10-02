#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const { parseFile } = require('./ast_utils');
const { walkJsFiles } = require('./file_walker');
const manifestAnalyzer = require('./manifest_analyzer');
const chromeApi = require('./chrome_api_analyzer');
const network = require('./network_analyzer');
const dataAccess = require('./data_access_analyzer');
const obfuscation = require('./obfuscation_analyzer');
const { isPublicHttpUrl } = require('./network_analyzer');

function analyzeExtension(extDir) {
  const extId = path.basename(extDir);
  const manifest = manifestAnalyzer.analyze(path.join(extDir, 'manifest.json'));
  const files = walkJsFiles(extDir);

  const dataSignals = new Set(manifest.data_signals);
  const exfilSignals = new Set(manifest.exfil_signals);
  const obfuscSignals = new Set();
  const keepReasons = new Set();

  // ← ADDED: cookie target tracking
  const cookiesSetUrls = new Set();
  const cookiesSetDomains = new Set();
  let hasCookiesSetWithDynamicTarget = false;

  let filesScanned = 0;
  let filesParseFailed = 0;
  let filesSkipped = 0;
  const perFile = [];

  for (const f of files) {
    const rel = path.relative(extDir, f.path);
    if (f.skipped) {
      filesSkipped++;
      keepReasons.add(`file_skipped:${f.skipped}:${rel}`);
      continue;
    }
    const { code, ast, error } = parseFile(f.path);
    if (error) {
      filesParseFailed++;
      keepReasons.add(`file_parse_failed:${rel}`);
      continue;
    }
    filesScanned++;

    let chromeRes = { data: [], exfil: [], other: [] };
    let networkRes = { sinks: [], static_urls: [], static_ips: [] };
    let dataRes = { sources: [] };
    let obfuscRes = { signals: [] };

    try { chromeRes = chromeApi.analyze(ast); }
    catch (e) { keepReasons.add(`chrome_analyzer_error:${rel}`); }
    try { networkRes = network.analyze(ast); }
    catch (e) { keepReasons.add(`network_analyzer_error:${rel}`); }
    try { dataRes = dataAccess.analyze(ast); }
    catch (e) { keepReasons.add(`data_access_error:${rel}`); }
    try { obfuscRes = obfuscation.analyze(ast); }
    catch (e) { keepReasons.add(`obfuscation_error:${rel}`); }

    for (const r of chromeRes.data) dataSignals.add(`chrome_api:${r.api}`);
    for (const r of chromeRes.exfil) exfilSignals.add(`chrome_api:${r.api}`);
    for (const s of networkRes.sinks) exfilSignals.add(`network_sink:${s.kind}:${s.name}`);
    for (const u of networkRes.static_urls) exfilSignals.add(`static_url:${u}`);
    for (const ip of networkRes.static_ips) exfilSignals.add(`static_ip:${ip}`);
    for (const s of dataRes.sources) dataSignals.add(`web_data:${s.path}`);
    for (const o of obfuscRes.signals) obfuscSignals.add(`obfuscation:${o.type}`);

    // ← ADDED: harvest cookie target URLs/domains from cookies.set / cookies.remove
    for (const r of [...chromeRes.exfil, ...chromeRes.other]) {
      if (r.api !== 'cookies.set' && r.api !== 'cookies.remove') continue;
      if (r.url_static && isPublicHttpUrl(r.url_static)) cookiesSetUrls.add(r.url_static);
      if (r.domain_static) cookiesSetDomains.add(r.domain_static);
      if (r.has_dynamic_first_arg) hasCookiesSetWithDynamicTarget = true;
    }

    perFile.push({
      file: rel,
      chrome_api: chromeRes,
      network: networkRes,
      data_access: dataRes,
      obfuscation: obfuscRes,
    });
  }

  if (!manifest.readable) keepReasons.add('manifest_unreadable');
  if (filesParseFailed > 0) keepReasons.add(`files_parse_failed:${filesParseFailed}`);
  if (filesSkipped > 0) keepReasons.add(`files_skipped:${filesSkipped}`);

  const safeToDrop =
    manifest.readable &&
    dataSignals.size === 0 &&
    exfilSignals.size === 0 &&
    obfuscSignals.size === 0 &&
    filesParseFailed === 0 &&
    filesSkipped === 0;

  return {
    extension_id: extId,
    manifest,
    summary: {
      safe_to_drop: safeToDrop,
      files_total: files.length,
      files_scanned: filesScanned,
      files_parse_failed: filesParseFailed,
      files_skipped: filesSkipped,
      data_signal_count: dataSignals.size,
      exfil_signal_count: exfilSignals.size,
      obfuscation_signal_count: obfuscSignals.size,
      data_signals: Array.from(dataSignals).sort(),
      exfil_signals: Array.from(exfilSignals).sort(),
      obfuscation_signals: Array.from(obfuscSignals).sort(),
      keep_reasons: Array.from(keepReasons).sort(),

      // ← ADDED: cookie target reporting (informational only — does not affect drop)
      cookies_set_urls: Array.from(cookiesSetUrls).sort(),
      cookies_set_domains: Array.from(cookiesSetDomains).sort(),
      has_cookies_set_with_dynamic_target: hasCookiesSetWithDynamicTarget,
    },
    files: perFile,
  };
}

function main() {
  const args = process.argv.slice(2);
  let extDir = null;
  let compact = false;
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--dir' || args[i] === '-d') extDir = args[++i];
    else if (args[i] === '--compact') compact = true;
  }
  if (!extDir) {
    process.stderr.write('Usage: node analyze_extension.js --dir <extension_dir> [--compact]\n');
    process.exit(1);
  }
  if (!fs.existsSync(extDir)) {
    process.stderr.write(`Extension dir not found: ${extDir}\n`);
    process.exit(1);
  }
  const result = analyzeExtension(extDir);
  if (compact) delete result.files;
  process.stdout.write(JSON.stringify(result));
}

if (require.main === module) main();

module.exports = { analyzeExtension };