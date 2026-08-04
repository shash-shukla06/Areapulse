const IssuesStore = (() => {
  const PREFIX = 'areapulse_cache_v1';
  const ISSUES_TTL_MS = 30000;
  const MY_ISSUES_TTL_MS = 30000;

  function readKey(key, ttlMs) {
    try {
      const raw = sessionStorage.getItem(key);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (Date.now() - parsed.ts > ttlMs) return null;
      return parsed.data;
    } catch (e) {
      return null;
    }
  }

  function writeKey(key, data) {
    try {
      sessionStorage.setItem(key, JSON.stringify({ ts: Date.now(), data }));
    } catch (e) {
      // sessionStorage full/unavailable (private browsing etc.) — skip caching,
      // callers still get correct data, they just always hit the network
    }
  }

  async function getIssues({ tag = null, status = null, force = false } = {}) {
    const key = `${PREFIX}:issues:${tag || ''}:${status || ''}`;
    if (!force) {
      const cached = readKey(key, ISSUES_TTL_MS);
      if (cached) return cached;
    }
    const params = new URLSearchParams();
    if (tag) params.set('tag', tag);
    if (status) params.set('status', status);
    const qs = params.toString();
    const res = await fetch('/issues' + (qs ? `?${qs}` : ''));
    if (!res.ok) throw new Error(`GET /issues failed: ${res.status}`);
    const data = await res.json();
    writeKey(key, data);
    return data;
  }

  async function getMyIssues({ user, force = false } = {}) {
    if (!user) return [];
    const key = `${PREFIX}:myissues:${user}`;
    if (!force) {
      const cached = readKey(key, MY_ISSUES_TTL_MS);
      if (cached) return cached;
    }
    const res = await fetch(`/my-issues-data?user=${encodeURIComponent(user)}`);
    if (!res.ok) throw new Error(`GET /my-issues-data failed: ${res.status}`);
    const data = await res.json();
    writeKey(key, data);
    return data;
  }

  function invalidate() {
    try {
      Object.keys(sessionStorage)
        .filter((k) => k.startsWith(PREFIX))
        .forEach((k) => sessionStorage.removeItem(k));
    } catch (e) {
      // ignore
    }
  }

  return { getIssues, getMyIssues, invalidate };
})();