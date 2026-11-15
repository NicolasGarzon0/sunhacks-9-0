export type Route = { name: "bet"; token: string } | { name: "notfound" };

export function matchRoute(pathname: string): Route {
  const path = pathname.replace(/\/+$/, "") || "/";
  const m = /^\/b\/([^/]+)$/.exec(path);
  if (m) {
    try {
      return { name: "bet", token: decodeURIComponent(m[1]) };
    } catch {
      return { name: "notfound" };
    }
  }
  return { name: "notfound" };
}
