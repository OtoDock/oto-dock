import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { viteStaticCopy } from 'vite-plugin-static-copy'
import path from 'path'

// animejs v4 ships only an ESM module tree (no script-tag build), so the
// script-tag artifact the UI kit serves is bundled here: IIFE, global `anime`,
// minified. Rolldown is vite 8's own bundler (pinned to vite's range).
function bundleAnimeIife(): Plugin {
  return {
    name: 'otodock:bundle-anime-iife',
    apply: 'build',
    async closeBundle() {
      const { rolldown } = await import('rolldown')
      const bundle = await rolldown({ input: 'animejs' })
      await bundle.write({
        format: 'iife',
        name: 'anime',
        file: path.resolve(__dirname, 'dist/ui-kit/anime.min.js'),
        minify: true,
      })
      await bundle.close()
    },
  }
}

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    // UI kit for display_ui artifacts: the proxy serves dist/ui-kit/* at
    // /ui-kit/* as the only subresource origin sandboxed artifact iframes can
    // load from (self-hosted — OSS installs may be offline). The woff2s are
    // needed because the iframe never sees the dashboard's bundled webfonts.
    viteStaticCopy({
      targets: [
        {
          src: 'node_modules/echarts/dist/echarts.min.js',
          dest: 'ui-kit',
          rename: { stripBase: true },
        },
        {
          src: 'node_modules/@tailwindcss/browser/dist/index.global.js',
          dest: 'ui-kit',
          rename: { stripBase: true, name: 'tailwind.js' },
        },
        { src: 'ui-kit/*', dest: 'ui-kit', rename: { stripBase: true } },
        {
          src: 'node_modules/@fontsource/comfortaa/files/comfortaa-{latin,greek}-{400,500,600,700}-normal.woff2',
          dest: 'ui-kit/fonts',
          rename: { stripBase: true },
        },
        {
          src: 'node_modules/@fontsource/jetbrains-mono/files/jetbrains-mono-{latin,greek}-{400,700}-normal.woff2',
          dest: 'ui-kit/fonts',
          rename: { stripBase: true },
        },
      ],
    }),
    bundleAnimeIife(),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  base: '/',
  build: {
    outDir: 'dist',
  },
  server: {
    port: 5173,
    proxy: {
      '/v1': {
        target: 'http://localhost:8400',
        changeOrigin: true,
      },
      '/auth': {
        target: 'http://localhost:8400',
        changeOrigin: true,
      },
      '/ws': {
        target: 'http://localhost:8400',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
