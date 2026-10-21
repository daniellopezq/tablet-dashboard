// fetch wrapper: JSON in/out, timeouts, and errors that carry the HTTP status.

export class ApiError extends Error {
  constructor(message, status = 0, body = null) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export async function request(method, url, { body, timeout = 10000 } = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  const opts = { method, signal: ctl.signal, headers: { Accept: 'application/json' }, cache: 'no-store' };
  if (body !== undefined) {
    if (body instanceof Blob) {
      opts.body = body;
      opts.headers['Content-Type'] = body.type || 'application/octet-stream';
    } else {
      opts.body = JSON.stringify(body);
      opts.headers['Content-Type'] = 'application/json';
    }
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch (err) {
    throw new ApiError(err.name === 'AbortError' ? 'timed out' : 'network error');
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (!res.ok) {
    throw new ApiError(data?.error?.message ?? data?.error ?? `HTTP ${res.status}`, res.status, data);
  }
  return data;
}

export const getJSON = (url, opts) => request('GET', url, opts);
export const postJSON = (url, body, opts) => request('POST', url, { ...opts, body });
