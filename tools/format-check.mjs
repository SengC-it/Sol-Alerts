import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

const root = process.cwd();
const ignored = new Set(["node_modules", "dist", ".git", ".pytest_cache", "__pycache__", ".cache", "reports"]);
const extensions = new Set([".ts", ".mjs", ".json", ".sql", ".py", ".md", ".yml", ".yaml", ".toml", ".example", ".txt"]);
const failures = [];

async function walk(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (ignored.has(entry.name) || entry.name.endsWith(".egg-info")) continue;
    const path = join(directory, entry.name);
    if (entry.isDirectory()) await walk(path);
    else if (extensions.has(path.slice(path.lastIndexOf(".")))) {
      const content = await readFile(path, "utf8");
      if (/[ \t]+\r?\n/.test(content)) failures.push(`${path}: trailing whitespace`);
      if (content.length > 0 && !content.endsWith("\n")) failures.push(`${path}: missing final newline`);
    }
  }
}

await walk(root);
if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log("format-check: ok");
