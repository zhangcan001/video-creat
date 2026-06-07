import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { env, platform, versions } from 'node:process';

const root = process.cwd();
const appData = env.APPDATA || '';
const logDir = appData ? join(appData, 'AI CanvasPro', 'logs') : '';

function mb(bytes) {
  return `${Math.round((bytes / 1024 / 1024) * 100) / 100} MB`;
}

function dirSize(path) {
  if (!existsSync(path)) return 0;
  let total = 0;
  const stack = [path];
  while (stack.length) {
    const current = stack.pop();
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const fullPath = join(current, entry.name);
      if (entry.isDirectory()) {
        stack.push(fullPath);
      } else if (entry.isFile()) {
        total += statSync(fullPath).size;
      }
    }
  }
  return total;
}

function readTail(path, lines = 30) {
  if (!existsSync(path)) return '(missing)';
  return readFileSync(path, 'utf8').split(/\r?\n/).slice(-lines).join('\n');
}

const paths = [
  'dist',
  'dist/win-unpacked',
  'desktop-runtime',
  'node_modules',
  'venv',
];

console.log('AI CanvasPro diagnostics');
console.log(`cwd: ${root}`);
console.log(`platform: ${platform}`);
console.log(`node: ${versions.node}`);
console.log(`electron app: ${existsSync(join(root, 'dist', 'win-unpacked', 'AI CanvasPro.exe')) ? 'built' : 'missing'}`);
console.log('');
console.log('Directory sizes');
for (const path of paths) {
  console.log(`${path}: ${mb(dirSize(join(root, path)))}`);
}
console.log('');
console.log('Desktop log tail');
console.log(readTail(join(logDir, 'desktop.log.jsonl')));
console.log('');
console.log('Server log tail');
console.log(readTail(join(logDir, 'server.log')));
