#!/usr/bin/env node
/**
 * `which database does this copy of the dashboard actually talk to?`
 *
 * Two copies of this app now exist on one machine — the live one reading Mumbai and
 * the staging one reading the Mac Mini's own Postgres — and they differ only by an
 * env file. This prints the identity of the database behind whichever env file you
 * hand it, so "I thought I was looking at staging" is a five-second answer instead of
 * a debugging session.
 *
 *   npm run which-db                              # this directory's .env.local
 *   npm run which-db -- --env-file ../Scanner/app/.env.local
 *
 * The connection string is never printed. The password is never read. The host is
 * shown with the user masked, plus `inet_server_addr()` from the server itself, which
 * is the only field that cannot lie about which cluster answered.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const argv = process.argv.slice(2);
const flag = (name) => {
  const i = argv.indexOf(`--${name}`);
  return i === -1 ? null : argv[i + 1];
};

const envFile = resolve(flag("env-file") ?? ".env.local");
let loaded = 0;
try {
  for (const line of readFileSync(envFile, "utf8").split("\n")) {
    const m = /^([A-Z0-9_]+)=(.*)$/.exec(line.trim());
    if (!m) continue;
    process.env[m[1]] = m[2];
    loaded += 1;
  }
} catch (error) {
  console.error(`cannot read ${envFile}: ${error.message}`);
  process.exit(2);
}

const url = process.env.DATABASE_URL ?? "";
if (url === "") {
  console.error(`${envFile} sets no DATABASE_URL — this copy runs on replay data, not a database.`);
  process.exit(1);
}

// Masked for display: scheme + host + port + database, never the credential.
const display = url
  .replace(/^(postgres(?:ql)?):\/\/[^@]*@/, "$1://•••@")
  .replace(/:[^:/@]+@/, ":•••@");

const { query, healthcheck, closeDb } = await import("../lib/db.ts");

console.log(`env file       ${envFile}  (${loaded} variables loaded)`);
console.log(`DATABASE_URL   ${display}`);
console.log(`DATABASE_SSL   ${process.env.DATABASE_SSL ?? "(unset -> require)"}`);

const health = await healthcheck();
console.log(`healthcheck    ${health.ok ? "ok" : "FAILED"}  ${health.detail}`);
if (!health.ok) {
  const { lastConnectionError } = await import("../lib/db.ts");
  console.log(`               ${lastConnectionError() ?? "no further detail"}`);
  await closeDb();
  process.exit(1);
}

const [server] = await query(
  `select current_database() as db,
          coalesce(inet_server_addr()::text, '(not reported)') as server_addr,
          current_setting('server_version') as version,
          now()::text as db_now`,
);
console.log(`server         ${server.db} @ ${server.server_addr}   PostgreSQL ${server.version}`);
console.log(`server clock   ${server.db_now}`);

// Not every table keeps a `created_at`; the heartbeat row is stamped `updated_at`.
// Ask the catalog which time column exists rather than guessing and printing an error.
const timeColumn = new Map(
  (
    await query(
      `select table_name, column_name from information_schema.columns
        where table_schema = 'public' and column_name in ('created_at', 'updated_at')`,
    )
  ).map((row) => [row.table_name, row.column_name]),
);

for (const table of ["instruments", "candles_1m", "signals", "worker_heartbeats"]) {
  const stamp = timeColumn.get(table);
  try {
    const [row] = await query(
      `select count(*)::bigint as n${stamp ? `, max(${stamp})::text as newest` : ""} from ${table}`,
    );
    console.log(`  ${table.padEnd(18)} ${String(row.n).padStart(8)} rows${stamp ? `   newest ${row.newest ?? "-"}` : ""}`);
  } catch (error) {
    console.log(`  ${table.padEnd(18)} unreadable: ${error.message.split("\n")[0]}`);
  }
}

const beats = await query(
  `select worker_id, status, updated_at::text from worker_heartbeats order by updated_at desc limit 4`,
).catch(() => []);
for (const b of beats) console.log(`  heartbeat        ${b.worker_id}  ${b.status}  ${b.updated_at}`);

await closeDb();
