#!/usr/bin/env node
// Read only Qoder's own login store. Credentials never go to stdout.
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync, renameSync, unlinkSync, mkdirSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);

const regions = {
  global: { directory: 'com.qoder.app.stable', service: 'Qoder App Safe Storage' },
  cn: { directory: 'com.qodercn.app.stable', service: 'Qoder CN App Safe Storage' },
};
let temporary;
try {
  const options = new Map();
  for (let index = 0; index < args.length; index += 2) {
    const flag = args[index], value = args[index + 1];
    if (!['--region', '--output'].includes(flag) || options.has(flag) ||
        !value || value.startsWith('--')) throw new Error('INVALID_ARGUMENTS');
    options.set(flag, value);
  }
  const region = options.get('--region');
  if (!['global', 'cn'].includes(region)) throw new Error('INVALID_ARGUMENTS');
  if (process.platform !== 'darwin' || !regions[region]) throw new Error('INVALID_PLATFORM_OR_REGION');
  const output = options.get('--output') || path.join(path.dirname(fileURLToPath(import.meta.url)), `credentials-${region}.json`);
  if (!output) throw new Error('MISSING_OUTPUT');
  const source = path.join(homedir(), 'Library/Application Support', regions[region].directory, 'auth.v1.dat');
  const encrypted = readFileSync(source);
  if (encrypted.subarray(0, 3).toString() !== 'v10') throw new Error('UNSUPPORTED_FORMAT');
  const password = execFileSync('/usr/bin/security',
    ['find-generic-password', '-s', regions[region].service, '-w'],
    { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 180000 }).trimEnd();
  const key = crypto.pbkdf2Sync(password, 'saltysalt', 1003, 16, 'sha1');
  const decipher = crypto.createDecipheriv('aes-128-cbc', key, Buffer.alloc(16, 32));
  const auth = JSON.parse(Buffer.concat([decipher.update(encrypted.subarray(3)), decipher.final()]).toString('utf8'));
  if (auth.schemaVersion !== 1 || typeof auth.token !== 'string' || !auth.token ||
      !auth.user || typeof auth.user.id !== 'string') throw new Error('INVALID_AUTH');
  if (!Number.isFinite(Date.parse(auth.expiresAt)) || Date.parse(auth.expiresAt) <= Date.now()) {
    throw new Error('TOKEN_EXPIRED');
  }
  mkdirSync(path.dirname(output), { recursive: true });
  temporary = `${output}.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`;
  writeFileSync(temporary, JSON.stringify({ token: auth.token, expiresAt: auth.expiresAt,
    account_id: auth.user.id, source: 'macos-qoder' }, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
  renameSync(temporary, output);
  temporary = undefined;
  console.log(JSON.stringify({ result: 'saved', region, expiresAt: auth.expiresAt }));
} catch (error) {
  // Never echo an exception which might include decrypted data or keychain output.
  const known = new Set(['INVALID_PLATFORM_OR_REGION', 'MISSING_OUTPUT', 'UNSUPPORTED_FORMAT',
    'INVALID_AUTH', 'TOKEN_EXPIRED', 'INVALID_ARGUMENTS']);
  const code = known.has(error.message) ? error.message :
    error.code === 'ENOENT' ? 'AUTH_FILE_NOT_FOUND' : 'KEYCHAIN_OR_DECRYPTION_FAILED';
  console.log(JSON.stringify({ result: 'error', code }));
  process.exitCode = 2;
} finally {
  if (temporary) { try { unlinkSync(temporary); } catch {} }
}
