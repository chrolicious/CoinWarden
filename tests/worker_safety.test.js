"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync("worker/index.js", "utf8").replace("export default {", "const worker = {");
const context = vm.createContext({ console, Response, Request, Headers, URL, crypto: require("node:crypto").webcrypto });
vm.runInContext(source, context);

test("conditional R2 conflict is retried instead of reported as saved", async () => {
  let writes = 0;
  const env = { DATA: {
    get: async () => ({ json: async () => ({ values: [] }), etag: "fixture" }),
    put: async () => { writes++; return null; },
  } };
  context.env = env;
  await assert.rejects(
    () => vm.runInContext("withJsonObject(env, 'fixture', { values: [] }, data => { data.values.push(1); return true; })", context),
    /conditional R2 write conflict/,
  );
  assert.equal(writes, 5);
});

test("sequenced events remain distinct within one timestamp", () => {
  const result = vm.runInContext(
    "eventKey({character:'fixture',event_id:1,ts:100,type:'gold_delta'}) !== eventKey({character:'fixture',event_id:2,ts:100,type:'gold_delta'})",
    context,
  );
  assert.equal(result, true);
});
