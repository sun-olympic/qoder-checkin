// Adapter for Qoder CLI 1.1.64's embedded credential codec. No secrets to stdout.
import fs from 'node:fs';
import path from 'node:path';
const [binary, config, output] = process.argv.slice(2);
let stage = 'extract';
try {
  const bytes = fs.readFileSync(binary);
  const source = bytes.toString('latin1');
  const marker = source.indexOf('function _t(e,t)');
  if (marker < 0) throw new Error();
  const start = source.lastIndexOf('\0', marker) + 1;
  const end = source.indexOf('\0', marker);
  const glue = bytes.subarray(start, end).toString('utf8');
  const codec = await import('data:text/javascript;base64,' + Buffer.from(glue).toString('base64'));
  stage = 'wasm';
  const encodedStart = source.indexOf('AGFzbQE', end);
  const encoded = /^[A-Za-z0-9+/=]+/.exec(source.slice(encodedStart));
  if (!encoded) throw new Error();
  codec.initSync({module: Buffer.from(encoded[0], 'base64')});
  const encrypted = fs.readFileSync(path.join(config, '.auth/user'), 'utf8').trim();
  const machine = fs.readFileSync(path.join(config, '.auth/machine_id'), 'utf8').trim();
  stage = 'decrypt';
  const decoded = codec.credential_storage_decrypt(encrypted, machine.slice(0,16));
  stage = 'parse';
  const user = JSON.parse(decoded);
  if (!output) { console.log(JSON.stringify({keys: Object.keys(user)})); }
  else {
    const token = user.security_oauth_token || user.access_token;
    if (typeof token !== 'string' || !token || typeof user.uid !== 'string') throw new Error();
    const expires = Number(user.expire_time);
    if (!Number.isFinite(expires) || expires <= 0) throw new Error();
    const expiresAt = new Date(expires > 1e11 ? expires : expires * 1000).toISOString();
    const nickname = typeof user.name === 'string' ? user.name.trim() : '';
    fs.writeFileSync(output, JSON.stringify({token, account_id:user.uid, nickname, source:'qoder-cli', expiresAt}) + '\n', {mode:0o600, flag:'wx'});
  }
} catch {
  console.error('无法读取官方 CLI 凭证（' + stage + '）；未输出任何密钥。');
  process.exitCode = 1;
}
