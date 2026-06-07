import { existsSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const root = process.cwd();
const packageRoot = join(root, 'dist', 'win-unpacked');
const requiredPaths = [
  'AI CanvasPro.exe',
  'resources/app.asar',
  'resources/app-web/server.py',
  'resources/app-web/index.html',
  'resources/app-web/main.js',
  'resources/app-web/native/screenshot-helper/bin/screenshot-helper.exe',
  'resources/runtime/python/python.exe',
  'resources/runtime/python/Lib/site-packages/cv2',
  'resources/runtime/python/Lib/site-packages/numpy',
  'resources/runtime/python/Lib/site-packages/PIL',
  'resources/runtime/python/Lib/site-packages/requests',
  'resources/runtime/python/Lib/site-packages/scenedetect',
];
const excludedPaths = [
  'resources/runtime/python/Lib/test',
  'resources/runtime/python/Lib/ensurepip',
  'resources/runtime/python/Lib/idlelib',
  'resources/runtime/python/Lib/venv',
  'resources/runtime/python/Lib/tkinter',
  'resources/runtime/python/Lib/site-packages/pip',
  'resources/runtime/python/Scripts/pip.exe',
  'resources/app-web/api/requester.test.js',
];

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

const failures = [];
for (const rel of requiredPaths) {
  if (!existsSync(join(packageRoot, rel))) failures.push(`missing required: ${rel}`);
}
for (const rel of excludedPaths) {
  if (existsSync(join(packageRoot, rel))) failures.push(`unexpected packaged path: ${rel}`);
}

if (failures.length) {
  console.error(failures.join('\n'));
  process.exitCode = 1;
} else {
  console.log('Package verification passed.');
  console.log(`Package root: ${packageRoot}`);
}
