import { removeOwnedTempDirs } from './temp-dirs';

// afterAll runs whether or not the file's tests pass, so a failing suite does not leak its
// synthesised cloud assemblies (tens of MB each) or fixtures.
afterAll(removeOwnedTempDirs);
