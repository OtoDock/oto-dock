import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'path'

// Test config is kept separate from vite.config.ts so the production build
// never pulls in vitest. Mirrors the build's react plugin + `@` alias.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/tests/setup.ts'],
    css: false,
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    // Node 26 defines a global localStorage that is undefined without
    // --localstorage-file and hides jsdom's; turning Node's off gives the
    // workers jsdom's storage again (Node 22 and 24 accept the flag too).
    execArgv: ['--no-experimental-webstorage'],
  },
})
