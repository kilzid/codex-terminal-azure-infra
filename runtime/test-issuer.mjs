// Local-only OIDC integration fixture. Generates disposable signing keys; no Entra credentials.
import http from 'node:http';
import { generateKeyPairSync, createSign, createHash, randomUUID } from 'node:crypto';
const issuer = 'http://127.0.0.1:19000';
const keys = generateKeyPairSync('rsa', { modulusLength: 2048 });
const other = generateKeyPairSync('rsa', { modulusLength: 2048 });
const jwk = { ...keys.publicKey.export({ format: 'jwk' }), kid: 'test-key', alg: 'RS256', use: 'sig' };
let scenario = 'owner';
const codes = new Map();
const json = (res, value, status = 200) => { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(value)); };
const sign = (claims, key) => {
  const head = Buffer.from(JSON.stringify({ alg: 'RS256', typ: 'JWT', kid: 'test-key' })).toString('base64url');
  const body = Buffer.from(JSON.stringify(claims)).toString('base64url');
  const data = `${head}.${body}`;
  return `${data}.${createSign('RSA-SHA256').update(data).sign(key).toString('base64url')}`;
};
http.createServer(async (req, res) => {
  const url = new URL(req.url, issuer);
  if (url.pathname === '/.well-known/openid-configuration') return json(res, {
    issuer, authorization_endpoint: issuer + '/authorize', token_endpoint: issuer + '/token',
    jwks_uri: issuer + '/keys', response_types_supported: ['code'], subject_types_supported: ['public'],
    id_token_signing_alg_values_supported: ['RS256'], code_challenge_methods_supported: ['S256'],
  });
  if (url.pathname === '/keys') return json(res, { keys: [jwk] });
  if (url.pathname === '/case') { scenario = url.searchParams.get('name'); return json(res, { scenario }); }
  if (url.pathname === '/upstream') { res.end('OWNER_UPSTREAM_REACHED'); return; }
  if (url.pathname === '/authorize') {
    const code = randomUUID();
    codes.set(code, { scenario, nonce: url.searchParams.get('nonce'), challenge: url.searchParams.get('code_challenge') });
    const target = new URL(url.searchParams.get('redirect_uri'));
    if (target.origin !== 'http://127.0.0.1:14181') return json(res, { error: 'invalid_redirect' }, 400);
    target.searchParams.set('code', code); target.searchParams.set('state', url.searchParams.get('state'));
    res.writeHead(302, { Location: target.href }); res.end(); return;
  }
  if (url.pathname === '/token') {
    let raw = ''; for await (const part of req) raw += part;
    const form = new URLSearchParams(raw), item = codes.get(form.get('code'));
    if (!item) return json(res, { error: 'invalid_grant' }, 400);
    codes.delete(form.get('code'));
    if (createHash('sha256').update(form.get('code_verifier') ?? '').digest('base64url') !== item.challenge)
      return json(res, { error: 'invalid_pkce' }, 400);
    const now = Math.floor(Date.now() / 1000);
    const claims = {
      iss: item.scenario === 'wrong-issuer' ? 'http://different-issuer.invalid' : issuer,
      sub: 'test-user', aud: item.scenario === 'wrong-audience' ? 'different-client' : 'test-client',
      oid: item.scenario === 'other-user' ? randomUUID() : process.env.OWNER_OID,
      nonce: item.scenario === 'wrong-nonce' ? 'invalid-nonce' : item.nonce, iat: now, exp: now + 600,
    };
    return json(res, { token_type: 'Bearer', access_token: 'test-access', expires_in: 600,
      id_token: sign(claims, item.scenario === 'bad-signature' ? other.privateKey : keys.privateKey) });
  }
  res.writeHead(404); res.end();
}).listen(19000, '127.0.0.1');
