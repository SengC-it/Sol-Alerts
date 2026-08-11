import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

const root = process.cwd();
const ignored = new Set(["node_modules", "dist", ".git", ".pytest_cache", "__pycache__", ".cache"]);
const extensions = new Set([".ts", ".mjs", ".json", ".sql", ".py", ".yml", ".yaml", ".toml", ".md", ".example"]);
const failures = [];

async function walk(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (ignored.has(entry.name)) continue;
    const path = join(directory, entry.name);
    if (entry.isDirectory()) await walk(path);
    else if (extensions.has(path.slice(path.lastIndexOf(".")))) {
      const content = await readFile(path, "utf8");
      if (/NEXT_PUBLIC_[A-Z0-9_]*(SERVICE_ROLE|SECRET|PASSWORD|PRIVATE)/.test(content)) failures.push(`${path}: server secret must not be NEXT_PUBLIC_`);
      if (/\bapiKey\s*[:=]/.test(content) && !path.endsWith(".example")) failures.push(`${path}: account/API key boundary violation`);
      if (/binance\.com\/api\/v3\/order|\/fapi\/v1\/account|\/fapi\/v2\/account/.test(content)) failures.push(`${path}: Binance account/trading endpoint is forbidden`);
    }
  }
}

await walk(root);
if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("lint: ok");
