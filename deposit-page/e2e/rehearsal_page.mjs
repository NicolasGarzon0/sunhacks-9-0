// DEV ONLY. Opens a bet link in real Chrome and reports which view the page shows. Used by backend/scripts/rehearsal.py.
//   node e2e/rehearsal_page.mjs <link> <locked|settled|refunded|expired> <screenshot name> [timeout seconds]
// Prints ONE line of JSON {ok, expect, text}. Screenshots go to ../screenshots (gitignored). The link is a bet link, not a secret
// of the user's, but it is never printed in full.
import { chromium } from "playwright-core";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const [link, expect, name, seconds = "30"] = process.argv.slice(2);
const here = dirname(fileURLToPath(import.meta.url));
const SHOTS = resolve(here, "../screenshots");
mkdirSync(SHOTS, { recursive: true });

const MARKERS = {
  locked: (p) => p.getByTestId("locked-note"),
  settled: (p) => p.getByTestId("winner"),
  refunded: (p) => p.getByTestId("refund-reason"),
  expired: (p) => p.getByText("This bet is over"),
};

if (!link || !MARKERS[expect] || !name) {
  console.error("usage: rehearsal_page.mjs <link> <locked|settled|refunded|expired> <name> [seconds]");
  process.exit(2);
}

const browser = await chromium.launch({ channel: "chrome", headless: true });
let out;
try {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  const page = await ctx.newPage();
  await page.goto(link);
  const marker = MARKERS[expect](page);
  try {
    await marker.waitFor({ timeout: Number(seconds) * 1000 });
    const text = ((await marker.first().textContent()) ?? "").trim();
    const body = ((await page.locator("body").textContent()) ?? "").replace(/\s+/g, " ").trim();
    out = { ok: true, expect, text, body: body.slice(0, 400) };
  } catch {
    const body = ((await page.locator("body").textContent()) ?? "").replace(/\s+/g, " ").trim();
    out = { ok: false, expect, text: null, body: body.slice(0, 400) };
  }
  await page.screenshot({ path: `${SHOTS}/rehearsal-${name}.png`, fullPage: true });
} finally {
  await browser.close();
}
console.log(JSON.stringify(out));
process.exit(out.ok ? 0 : 1);
