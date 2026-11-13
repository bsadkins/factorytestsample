import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

const created: string[] = [];

/**
 * Creates a directory under the OS temporary directory and records it for removal by
 * removeOwnedTempDirs(), which the Jest setup file runs after every test file.
 */
export function ownedTempDir(prefix: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  created.push(dir);
  return dir;
}

/** The directories created by ownedTempDir() in this test file that have not been removed yet. */
export function ownedTempDirs(): readonly string[] {
  return [...created];
}

/** Removes exactly the directories recorded by ownedTempDir(); nothing else in the temporary directory is touched. */
export function removeOwnedTempDirs(): void {
  for (const dir of created.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}
