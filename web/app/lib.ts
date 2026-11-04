export const tokenKey = "dr_admin_token";

export function getToken(): string {
  try { return sessionStorage.getItem(tokenKey) ?? ""; } catch { return ""; }
}

/** fetch with the seller's admin token; throws the API's `detail` text on an error status */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", "X-Admin-Token": getToken(), ...init.headers },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `request failed (${res.status})`);
  return body as T;
}

export const gbp = (minor: number | null, cur = "GBP") =>
  minor == null ? "" : new Intl.NumberFormat("en-GB", { style: "currency", currency: cur }).format(minor / 100);
