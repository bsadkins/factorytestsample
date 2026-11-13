/**
 * Replaces 64-character asset hashes in a synthesised template so snapshots record topology,
 * not the content of Lambda source or templates. Asset identity is asserted separately in
 * test/packaging/python-assets.test.ts.
 */
export function normaliseAssetHashes<T>(template: T): T {
  return JSON.parse(JSON.stringify(template).replace(/[0-9a-f]{64}/g, 'ASSET_HASH')) as T;
}
