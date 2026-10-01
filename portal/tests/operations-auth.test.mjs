import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
import test from "node:test";

// Run the actual route with only its external dependencies replaced. No build
// or deployed credentials are needed to exercise the authorization boundary.
const source = await readFile(new URL("../app/api/operations/route.ts", import.meta.url), "utf8");
const javascript = stripTypeScriptTypes(source.replace(/^import .*;\n/gm, ""))
  .replace(/export async function/g, "async function");
const factory = new (Object.getPrototypeOf(async function () {}).constructor)(
  "env", "ensureDb", "operations", "desc", "getChatGPTUser", "fetch",
  `${javascript}\nreturn { GET, POST };`,
);

async function route(user, bindings = {}) {
  const effects = { reads: 0, writes: [], dispatches: [] };
  const db = {
    select() { effects.reads++; return this; },
    from() { return this; },
    orderBy() { return this; },
    async limit() { return [{ actor: "private-admin" }]; },
    insert() { return this; },
    async values(value) { effects.writes.push(value); },
  };
  const api = await factory(
    { OPS_ADMIN_EMAILS: "admin@example.test", ...bindings },
    async () => db, { createdAt: "createdAt" }, (value) => value,
    async () => user,
    async (...args) => { effects.dispatches.push(args); return new Response(null, { status: 204 }); },
  );
  return { ...api, effects };
}

const admin = { email: "ADMIN@example.test", userId: "admin" };
const payload = { kind: "audit", execute: false };
function request(body = payload, headers = {}) {
  return new Request("https://portal.example.test/api/operations", {
    method: "POST", headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
}

for (const [name, user, status] of [
  ["anonymous", null, 401],
  ["non-admin", { email: "employee@example.test", userId: "employee" }, 403],
]) {
  test(`${name} cannot read records or save plans or dispatch workflows`, async () => {
    const api = await route(user);
    assert.equal((await api.GET()).status, status);
    assert.equal((await api.POST(request())).status, status);
    assert.equal((await api.POST(request({ ...payload, execute: true }))).status, status);
    assert.deepEqual(api.effects, { reads: 0, writes: [], dispatches: [] });
  });
}

test("admin can read and plan without dispatching", async () => {
  const api = await route(admin);
  assert.equal((await api.GET()).status, 200);
  const response = await api.POST(request());
  assert.equal(response.status, 200);
  assert.equal((await response.json()).status, "PLANNED");
  assert.equal(api.effects.writes[0].actor, admin.email);
  assert.equal(api.effects.dispatches.length, 0);
});

test("empty admin allowlist fails closed", async () => {
  const api = await route(admin, { OPS_ADMIN_EMAILS: "" });
  assert.equal((await api.POST(request())).status, 403);
  assert.equal(api.effects.writes.length, 0);
});

test("only explicit boolean execution dispatches a workflow", async () => {
  const api = await route(admin, { GITHUB_TOKEN: "unit-test-token" });
  assert.equal((await api.POST(request({ ...payload, execute: "false" }))).status, 400);
  assert.equal(api.effects.dispatches.length, 0);
  assert.equal((await api.POST(request({ ...payload, execute: true }))).status, 200);
  assert.equal(api.effects.dispatches.length, 1);
  assert.equal(api.effects.writes[0].status, "DISPATCHED");
});

test("cross-origin and non-JSON writes cannot reach the database", async () => {
  const api = await route(admin);
  assert.equal((await api.POST(request(payload, { Origin: "https://attacker.example" }))).status, 403);
  assert.equal((await api.POST(request(payload, { "Content-Type": "text/plain" }))).status, 415);
  assert.equal(api.effects.writes.length, 0);
});
