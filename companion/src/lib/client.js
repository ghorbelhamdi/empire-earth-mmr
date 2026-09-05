'use strict';

function normalizeServer(value) {
  let url;
  try { url = new URL(String(value).trim()); } catch { throw new Error('Enter a valid server address.'); }
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && local)) throw new Error('Use HTTPS. HTTP is allowed only on localhost for development.');
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/') throw new Error('Use only the server origin, without credentials, a path, or query parameters.');
  return url.origin;
}

async function apiRequest(server, token, endpoint, payload, fetchImpl = fetch) {
  const origin = normalizeServer(server);
  if (!['/api/v1/players', '/api/v1/companion', '/api/v1/matches/preview', '/api/v1/matches'].includes(endpoint)) throw new Error('Unsupported API endpoint.');
  if (typeof token !== 'string' || !token.trim() || /[\r\n]/.test(token)) throw new Error('Enter a companion token from your ladder administrator.');
  let response;
  try {
    response = await fetchImpl(origin + endpoint, {
      method: payload ? 'POST' : 'GET',
      headers: { Authorization: `Bearer ${token}`, Accept: 'application/json', ...(payload ? { 'Content-Type': 'application/json' } : {}) },
      ...(payload ? { body: JSON.stringify(payload) } : {}),
      redirect: 'error', signal: AbortSignal.timeout(20000)
    });
  } catch { throw new Error('Could not reach the ladder. Check the server and connection. Your draft is saved; retry uses the same submission ID.'); }
  const text = await response.text();
  let data;
  try { data = JSON.parse(text); } catch { throw new Error(`Server returned an unexpected response (${response.status}). Check that the companion API is installed.`); }
  if (!response.ok) {
    const error = new Error(String(data.error || `Server error ${response.status}`).slice(0, 500));
    error.status = response.status;
    throw error;
  }
  return data;
}

module.exports = { normalizeServer, apiRequest };
