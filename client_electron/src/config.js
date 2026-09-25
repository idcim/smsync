"use strict";

const fs = require("fs");
const path = require("path");

const DEFAULT_DOMAINS = ["smsync.h6.fan", "smsync.qisop.com"];

function loadEnvFile() {
  const candidates = [
    path.join(__dirname, "..", ".env"),
    path.join(process.cwd(), ".env"),
  ];
  for (const file of candidates) {
    try {
      for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
        const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
        if (m && !(m[1] in process.env)) process.env[m[1]] = m[2];
      }
      return;
    } catch {
      /* try next candidate */
    }
  }
}

loadEnvFile();

const username = process.env.SMSYNC_USERNAME || "";
const password = process.env.SMSYNC_PASSWORD || "";
const raw = (process.env.SMSYNC_DOMAINS || "")
  .split(",")
  .map((d) => d.trim())
  .filter(Boolean);

// Each entry may be a bare domain (-> https/wss) or a full URL with scheme
// (http(s):// or ws(s)://, useful for local testing like http://127.0.0.1:8000).
function toServer(d) {
  const u = d.replace(/\/+$/, "");
  if (/^https?:\/\//.test(u)) return { http: u, ws: u.replace(/^http/, "ws") };
  if (/^wss?:\/\//.test(u)) return { http: u.replace(/^ws/, "http"), ws: u };
  return { http: `https://${u}`, ws: `wss://${u}` };
}

const servers = (raw.length ? raw : DEFAULT_DOMAINS).map(toServer);

module.exports = { username, password, servers, toServer };
