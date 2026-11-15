// Live check driver: drives the deposit page in real Chrome (playwright-core, system Chrome) against the devnet stack.
// Dev tool only. Needs the stack running (see README "Live check"). Screenshots go to ../screenshots (gitignored).
import { chromium } from "playwright-core";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const run = promisify(execFile);
const here = dirname(fileURLToPath(import.meta.url));
const SHOTS = resolve(here, "../screenshots");
const BACKEND = resolve(here, "../../backend");
const MINUTES = process.env.LIVE_MINUTES || "9";
mkdirSync(SHOTS, { recursive: true });

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function helper(...args) {
  let stdout;
  try {
    ({ stdout } = await run(`${BACKEND}/.venv/bin/python`, [resolve(here, "live_helper.py"), ...args], { cwd: BACKEND, env: process.env, timeout: 180000 }));
  } catch (e) {
    throw new Error(`helper ${args[0]} failed: ${String(e.stderr || e.message).trim().split("\n").slice(-3).join(" | ").slice(0, 300)}`);
  }
  return JSON.parse(stdout.trim().split("\n").filter((l) => l.startsWith("{")).pop());
}
const shot = async (page, name, full = false) => { await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: full }); console.log(`  shot ${name}.png`); };

async function waitFor(fn, what, timeout = 120000, every = 1500) {
  const end = Date.now() + timeout;
  for (;;) {
    try { const v = await fn(); if (v) return v; } catch { /* retry */ }
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await sleep(every);
  }
}

const browser = await chromium.launch({ channel: "chrome", headless: true });
const phone = { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true };

async function deposit(page, team, amount, name, label) {
  await page.getByRole("radiogroup").waitFor({ timeout: 30000 });
  const bal = page.locator(".bal");
  await waitFor(async () => /^\$\d/.test(await bal.textContent()), `${label} wallet balance to load`, 90000, 1000);
  if ((await bal.textContent()) === "$0.00") {
    await page.getByRole("button", { name: "Get test USDC" }).waitFor({ timeout: 20000 });
    await shot(page, `${label}-02-needs-usdc`);
    await page.getByRole("button", { name: "Get test USDC" }).click();
    await waitFor(async () => (await bal.textContent()) === "$25.00", `${label} test USDC`, 120000);
    check(`${label}: got test USDC from the airdrop button`, true);
  }
  await page.getByRole("radio", { name: new RegExp(`^${team}`) }).click();
  await page.getByLabel("Amount (USD)").fill(amount);
  await page.getByLabel(/Display name/).fill(name);
  await waitFor(async () => /about \$[\d.]+ \([\d.]+x\) after the 5% fee/.test(await page.getByTestId("preview").textContent()), `${label} preview`, 15000, 500);
  check(`${label}: payout preview shows in plain words`, true, (await page.getByTestId("preview").textContent()).slice(0, 80));
  await shot(page, `${label}-03-form-ready`);
  await page.getByRole("button", { name: /^Deposit \$/ }).click();
  const outcome = await Promise.race([
    page.getByTestId("success").waitFor({ timeout: 120000 }).then(() => "success"),
    page.locator(".error-box").waitFor({ timeout: 120000 }).then(() => "error"),
  ]);
  if (outcome === "error") {
    await shot(page, `${label}-04-ERROR`);
    throw new Error(`${label} deposit failed: ${await page.locator(".error-box").textContent()}`);
  }
  check(`${label}: deposit confirmed, success screen shown`, true);
  await shot(page, `${label}-04-success`);
}

try {
  console.log(`opening a market (kickoff in ${MINUTES} min, a real text goes to the live test number) ...`);
  const m = process.env.LIVE_ATTACH ? await helper("latest") : await helper("open", MINUTES);
  const token = new URL(m.link).pathname.split("/").pop();
  console.log(`  bet ${m.bet_id.slice(0, 8)} link ${new URL(m.link).origin}/b/${token.slice(0, 6)}… funding closes ${m.funding_closes_at}`);
  check("the link in the text points at the local page", m.link.startsWith(process.env.DEPOSIT_PAGE_URL || "http://127.0.0.1:5173"));

  // --- invalid token, desktop shot ---------------------------------------------------------
  const ctxC = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const bad = await ctxC.newPage();
  await bad.goto(`${process.env.DEPOSIT_PAGE_URL || "http://127.0.0.1:5173"}/b/not-a-real-token`);
  await bad.getByText("This link is not valid").waitFor({ timeout: 20000 });
  check("invalid token: friendly explanation, no technical code", !/BAD_TOKEN|404/.test(await bad.locator("body").textContent()));
  await shot(bad, "00-invalid-token");
  await bad.goto(`${process.env.DEPOSIT_PAGE_URL || "http://127.0.0.1:5173"}/`);
  await bad.getByText("Friendly bets with your group").waitFor();
  await shot(bad, "00-home");
  await bad.close();

  // --- A: first player, phone viewport -------------------------------------------------------
  const ctxA = await browser.newContext(phone);
  const A = await ctxA.newPage();
  A.on("console", (m) => { if (m.type() === "error") console.log("  [A console]", m.text().slice(0, 200)); });
  A.on("pageerror", (e) => console.log("  [A pageerror]", String(e).slice(0, 300)));
  A.on("response", (r) => { if (r.url().includes("/deposit_") || r.status() >= 400) console.log("  [A net]", r.request().method(), r.url().replace(/\/b\/.*|markets\/[^/]+/, "markets/<t>"), r.status()); });
  A.on("requestfailed", (r) => console.log("  [A reqfailed]", r.url().slice(0, 100), r.failure()?.errorText));
  const navs = [];
  A.on("framenavigated", (f) => { if (f === A.mainFrame()) navs.push(f.url()); });
  await A.goto(m.link);
  await A.getByRole("radiogroup").waitFor({ timeout: 30000 });
  await shot(A, "A-01-open-empty", true);
  check("A: open view shows both teams and the countdown", (await A.getByRole("heading", { level: 1 }).textContent()).includes("Arizona Cardinals") && /\d/.test(await A.getByTestId("countdown").textContent()));
  const Bp = (async () => {
    const ctxB = await browser.newContext(phone);
    const B = await ctxB.newPage();
    await B.goto(m.link);
    await deposit(B, "Philadelphia Eagles", "7", "Ben", "B");
    await B.getByRole("button", { name: "View the pool" }).click();
    return B;
  })();
  await deposit(A, "Arizona Cardinals", "5", "Ana", "A");
  await A.evaluate(() => { window.__noReload = true; });
  await A.getByRole("button", { name: "View the pool" }).click();
  check("A: position card shows the deposit", (await A.getByTestId("your-position").textContent()).includes("$5.00 on Arizona Cardinals"));
  check("A: the other team is disabled with a reason", await A.getByRole("radio", { name: /^Philadelphia Eagles/ }).isDisabled());
  await shot(A, "A-05-after-deposit-other-side-disabled", true);

  // --- B: second player, separate browser profile (own wallet), other side ------------------------
  const B = await Bp;

  // A sees B's deposit without reloading (poll)
  await waitFor(async () => (await A.locator(".stats dd").first().textContent()) === "$12.00", "A sees $12.00", 30000);
  check("A: pool updated to $12.00 by polling, no reload", true);
  await shot(A, "A-06-pool-updated-by-polling", true);
  check("A: the page never navigated or reloaded so far", (await A.evaluate(() => window.__noReload === true)) && navs.length === 1);

  // --- C: reconnecting banner (desktop), runs while we wait for funding to close ----------------------
  const C = await ctxC.newPage();
  await C.goto(m.link);
  await C.getByRole("heading", { level: 1 }).waitFor({ timeout: 60000 });
  await shot(C, "C-01-desktop-open", true);
  const cTask = (async () => {
    await ctxC.route(`**/markets/**`, (r) => r.abort());
    await C.getByText(/Reconnecting/).waitFor({ timeout: 150000 });
    await shot(C, "C-02-reconnecting-banner");
    check("C: reconnecting banner appears after repeated failures, last data still shown", (await C.locator(".stats dd").first().textContent()) === "$12.00");
    await ctxC.unroute("**/markets/**");
    await C.getByText(/Reconnecting/).waitFor({ state: "detached", timeout: 120000 });
    check("C: banner goes away once the server answers again", true);
  })();

  // --- funding closes: A flips to locked without reload ----------------------------------
  console.log("  waiting for funding to close ...");
  await A.getByTestId("locked-note").waitFor({ timeout: 8 * 60000 });
  check("A: switched to the locked view by itself (no reload)", (await A.evaluate(() => window.__noReload === true)) && navs.length === 1);
  await shot(A, "A-07-locked", true);
  await B.getByTestId("locked-note").waitFor({ timeout: 60000 });
  check("B: locked view with the position card", (await B.getByTestId("your-position").textContent()).includes("$7.00 on Philadelphia Eagles"));
  await shot(B, "B-07-locked");

  // --- result: home (Eagles) wins ----------------------------------------------------------
  const fin = await helper("finish", m.game_id, "21", "14");
  check("simulated final result set (Eagles win)", fin.ok);
  await A.getByTestId("winner").waitFor({ timeout: 8 * 60000 });
  check("A: switched to settled by itself (no reload)", (await A.evaluate(() => window.__noReload === true)) && navs.length === 1);
  check("A: winner is the Eagles", (await A.getByTestId("winner").textContent()).includes("Philadelphia Eagles won"));
  await B.getByTestId("winner").waitFor({ timeout: 120000 });
  await shot(A, "A-08-settled-loser", true);
  await waitFor(async () => (await B.getByTestId("your-position").textContent()).includes("You won $11.75"), "B payout text", 90000);
  check("B: payout 11.75 = 7 + 5 stake minus the 0.25 fee on the losing side", true);
  await waitFor(async () => (await B.locator("body").textContent()).includes("Paid to your wallet."), "B paid", 240000, 4000);
  check("B: shows the payout as paid", true);
  const settledText = await B.locator("body").textContent();
  check("settled view: fee $0.25 and a final multiplier", settledText.includes("$0.25") && /\d\.\d+x/.test(settledText));
  await shot(B, "B-08-settled-winner-paid", true);
  check("A: your position says Seattle did not win", (await A.getByTestId("your-position").textContent()).includes("did not win"));

  await cTask;

  // --- link expiry: A's page finds out while open ------------------------------------------
  console.log("  waiting for the link to expire (payouts done + 1 minute grace) ...");
  await A.getByText("This bet is over").waitFor({ timeout: 8 * 60000 });
  check("expired link (410): page explains it without codes", !/LINK_EXPIRED|410/.test(await A.locator("body").textContent()));
  await shot(A, "A-09-expired");
  const st = await helper("status", m.bet_id);
  console.log(`  final bet status: ${st.status}, tasks: ${JSON.stringify(st.tasks)}`);
} catch (e) {
  check("run finished without an exception", false, String(e).slice(0, 300));
} finally {
  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
}
