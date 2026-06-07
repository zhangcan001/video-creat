import { rmSync } from 'node:fs';
import { join } from 'node:path';

const root = process.cwd();
const targets = [
  'dist',
  'dist-obfuscated',
  'electron-build',
  '.electron-runtime',
  'server-run.log',
  'server-run.err.log',
  'electron-run.log',
  'electron-run.err.log',
];

for (const target of targets) {
  rmSync(join(root, target), { force: true, recursive: true });
}

console.log(`Cleaned ${targets.length} build/runtime artifact paths.`);
