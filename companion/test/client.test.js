'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizeServer, apiRequest } = require('../src/lib/client');

test('only HTTPS origins or literal localhost development origins accepted', () => {
  assert.equal(normalizeServer(' https://empire-mmr.duckdns.org/ '), 'https://empire-mmr.duckdns.org');
  assert.equal(normalizeServer('http://127.0.0.1:5000'), 'http://127.0.0.1:5000');
  for (const value of ['http://46.224.167.9', 'https://good.example/path', 'https://token@good.example', 'file:///tmp', 'http://localhost.evil.example', 'https://good.example?token=x']) assert.throws(() => normalizeServer(value));
});
test('bearer token stays on exact origin and redirects are rejected', async () => {
  let options;
  await apiRequest('https://ladder.example', 'device-secret', '/api/v1/players', null, async (url, init) => {
    assert.equal(url, 'https://ladder.example/api/v1/players'); options = init;
    return new Response(JSON.stringify({ players: [] }), { status: 200 });
  });
  assert.equal(options.redirect, 'error'); assert.equal(options.headers.Authorization, 'Bearer device-secret');
  await assert.rejects(apiRequest('https://ladder.example', 'token', 'https://evil.example', null, () => { throw new Error('not reached'); }), /Unsupported/);
});
test('JSON API errors and unavailable API report actionable messages', async () => {
  await assert.rejects(apiRequest('https://ladder.example', 'token', '/api/v1/players', null, async () => new Response(JSON.stringify({ error: 'Token revoked' }), { status: 401 })), /Token revoked/);
  await assert.rejects(apiRequest('https://ladder.example', 'token', '/api/v1/players', null, async () => new Response('<html>404</html>', { status: 404 })), /companion API/);
});
