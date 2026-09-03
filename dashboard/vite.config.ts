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

// three.js ships ESM-only (upstream dropped the UMD build), so the kit's
// script-tag artifact is bundled the same way as anime: one IIFE, global
// `THREE`, with a curated addon set attached under THREE.* — camera controls
// (Orbit/Map), the EffectComposer post-processing chain (bloom is the kit's
// signature "wow" lever), fat lines, and RoundedBoxGeometry — everything a
// mini-app needs for impressive scenes while staying inside the artifact
// CSP (no loaders/workers/WASM). NEVER add examples/jsm/lines/webgpu/*
// (it drags the whole WebGPU renderer in). The same package pin feeds BOTH
// surfaces: the dashboard map imports `three` as ESM (tree-shaken into its
// lazy chunk) and mini-apps load this global build — the Tailwind dual-path
// precedent. Version bumps hit both at once.
function bundleThreeIife(): Plugin {
  const entry = 'otodock-three-kit-entry'
  return {
    name: 'otodock:bundle-three-iife',
    apply: 'build',
    async closeBundle() {
      const { rolldown } = await import('rolldown')
      const bundle = await rolldown({
        input: entry,
        plugins: [
          {
            name: 'otodock:three-kit-entry',
            resolveId(id: string) {
              if (id === entry) return entry
              return null
            },
            load(id: string) {
              if (id !== entry) return null
              return (
                "export * from 'three';\n" +
                "export { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';\n" +
                "export { MapControls } from 'three/examples/jsm/controls/MapControls.js';\n" +
                "export { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';\n" +
                "export { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';\n" +
                "export { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';\n" +
                "export { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js';\n" +
                "export { ShaderPass } from 'three/examples/jsm/postprocessing/ShaderPass.js';\n" +
                "export { Pass, FullScreenQuad } from 'three/examples/jsm/postprocessing/Pass.js';\n" +
                "export { Line2 } from 'three/examples/jsm/lines/Line2.js';\n" +
                "export { LineGeometry } from 'three/examples/jsm/lines/LineGeometry.js';\n" +
                "export { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js';\n" +
                "export { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js';\n"
              )
            },
          },
        ],
      })
      await bundle.write({
        format: 'iife',
        name: 'THREE',
        file: path.resolve(__dirname, 'dist/ui-kit/three.min.js'),
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
    bundleThreeIife(),
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
      // Wake-word wasm/model bundle — served by the proxy from
      // proxy/assets/kws (outside dist, see config.KWS_ASSETS_DIR).
      '/kws-assets': {
        target: 'http://localhost:8400',
        changeOrigin: true,
      },
    },
  },
})
