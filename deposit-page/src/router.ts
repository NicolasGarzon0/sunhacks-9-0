export type Route = { name: "home" } | { name: "bet"; token: string } | { name: "notfound" };

export function matchRoute(pathname: string): Route {
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === "/") return { name: "home" };
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
