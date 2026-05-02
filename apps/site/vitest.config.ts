import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['__tests__/**/*.spec.ts', '__tests__/**/*.test.ts'],
    testTimeout: 180_000,
    hookTimeout: 180_000,
    environment: 'node',
  },
});
