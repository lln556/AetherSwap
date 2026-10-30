export const BUFF_ORIGIN = "https://buff.163.com/";
export const AUTHORIZED_ORIGINS_KEY = "authorizedOrigins";

export function canonicalOriginFromUrl(value) {
  try {
    const url = new URL(String(value || ""));
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (!url.hostname || url.username || url.password) return null;
    return url.origin;
  } catch {
    return null;
  }
}

// Chrome match patterns cannot distinguish ports. The exact port is therefore
// stored and checked separately for every privileged operation.
export function permissionPatternForOrigin(origin) {
  const canonical = canonicalOriginFromUrl(origin);
  if (!canonical) throw new Error("无效的来源");
  const url = new URL(canonical);
  return `${url.protocol}//${url.hostname}/*`;
}

export function isValidRequestId(value) {
  return typeof value === "string" && /^[A-Za-z0-9._:-]{1,128}$/.test(value);
}

export function safeText(value, fallback = "") {
  return typeof value === "string" ? value.slice(0, 1000) : fallback;
}

export function sanitizeSession(session) {
  if (!session || typeof session !== "object" || Array.isArray(session)) return null;
  const safe = {};
  for (const key of ["status", "message", "replace_block_reason"]) {
    if (typeof session[key] === "string") safe[key] = session[key].slice(0, 1000);
  }
  if (session.checked_at === null || Number.isFinite(session.checked_at)) {
    safe.checked_at = session.checked_at;
  }
  if (Number.isFinite(session.retry_after)) safe.retry_after = session.retry_after;
  if (typeof session.can_replace === "boolean") safe.can_replace = session.can_replace;
  return Object.keys(safe).length ? safe : null;
}

export function sanitizedResult({ok, error, code, session} = {}) {
  const result = {ok: ok === true};
  const safeError = safeText(error);
  const safeCode = safeText(code);
  if (safeError) result.error = safeError;
  if (safeCode) result.code = safeCode;
  const safeSession = sanitizeSession(session);
  if (safeSession && Object.keys(safeSession).length) result.session = safeSession;
  return result;
}
