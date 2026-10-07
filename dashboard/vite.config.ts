import { defineConfig, type Plugin, type ResolvedConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { viteStaticCopy } from 'vite-plugin-static-copy'
import path from 'path'
import type { ClientRequest, IncomingMessage } from 'http'
import { precompressDir } from './build/precompress'
import { stampBuildId } from './build/buildId'

// The resolved output directory, so a build with `--outDir` writes its kit
// files and compressed siblings THERE and never touches the real dist.
function resolvedOutDir(config: ResolvedConfig): string {
  return path.resolve(config.root, config.build.outDir)
}

// animejs v4 ships only an ESM module tree (no script-tag build), so the
// script-tag artifact the UI kit serves is bundled here: IIFE, global `anime`,
// minified. Rolldown is vite 8's own bundler (pinned to vite's range).
function bundleAnimeIife(): Plugin {
  let outDir = path.resolve(__dirname, 'dist')
  return {
    name: 'otodock:bundle-anime-iife',
    apply: 'build',
    configResolved(config) {
      outDir = resolvedOutDir(config)
    },
    async closeBundle() {
      const { rolldown } = await import('rolldown')
      const bundle = await rolldown({ input: 'animejs' })
      await bundle.write({
        format: 'iife',
        name: 'anime',
        file: path.join(outDir, 'ui-kit/anime.min.js'),
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
// app needs for impressive scenes while staying inside the artifact
// CSP (no loaders/workers/WASM). NEVER add examples/jsm/lines/webgpu/*
// (it drags the whole WebGPU renderer in). The same package pin feeds BOTH
// surfaces: the dashboard map imports `three` as ESM (tree-shaken into its
// lazy chunk) and apps load this global build — the Tailwind dual-path
// precedent. Version bumps hit both at once.
function bundleThreeIife(): Plugin {
  const entry = 'otodock-three-kit-entry'
  let outDir = path.resolve(__dirname, 'dist')
  return {
    name: 'otodock:bundle-three-iife',
    apply: 'build',
    configResolved(config) {
      outDir = resolvedOutDir(config)
    },
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
        file: path.join(outDir, 'ui-kit/three.min.js'),
        minify: true,
      })
      await bundle.close()
    },
  }
}

// The external link's host page (SHARING.md "External links"): the proxy's
// /s/<token> HTML loads dist/ui-kit/share-host.js by a stable, un-hashed
// path (the /ui-kit/ prefix is already login-exempt), so it is built here
// like the other kit scripts, from src/sharehost/main.ts.
function bundleShareHostIife(): Plugin {
  let outDir = path.resolve(__dirname, 'dist')
  return {
    name: 'otodock:bundle-share-host-iife',
    apply: 'build',
    configResolved(config) {
      outDir = resolvedOutDir(config)
    },
    async closeBundle() {
      const { rolldown } = await import('rolldown')
      const bundle = await rolldown({ input: path.resolve(__dirname, 'src/sharehost/main.ts') })
      await bundle.write({
        format: 'iife',
        file: path.join(outDir, 'ui-kit/share-host.js'),
        minify: true,
      })
      await bundle.close()
    },
  }
}

// The widget kit (proxy APPS.md "The widget kit"): standard shapes for app
// pages, bound to the catalog feeds through the runtime, built like the
// share host from src/uikit/widgets.ts to a stable un-hashed kit path.
function bundleWidgetsIife(): Plugin {
  let outDir = path.resolve(__dirname, 'dist')
  return {
    name: 'otodock:bundle-widgets-iife',
    apply: 'build',
    configResolved(config) {
      outDir = resolvedOutDir(config)
    },
    async closeBundle() {
      const { rolldown } = await import('rolldown')
      const bundle = await rolldown({ input: path.resolve(__dirname, 'src/uikit/widgets.ts') })
      await bundle.write({
        format: 'iife',
        file: path.join(outDir, 'ui-kit/otodock-widgets.js'),
        minify: true,
      })
      await bundle.close()
    },
  }
}

// Every text asset under assets/ and ui-kit/ gets .br and .gz siblings; the
// proxy serves the one the client accepts and never compresses at request
// time. Registered LAST: the static-copy targets land in writeBundle and the
// four IIFE writers above run earlier in the same sequential closeBundle
// pass, so the kit files exist by now.
function precompressAssets(): Plugin {
  let outDir = path.resolve(__dirname, 'dist')
  return {
    name: 'otodock:precompress-assets',
    apply: 'build',
    configResolved(config) {
      outDir = resolvedOutDir(config)
    },
    async closeBundle(error?: Error) {
      if (error) return
      await Promise.all([
        precompressDir(path.join(outDir, 'assets')),
        precompressDir(path.join(outDir, 'ui-kit')),
      ])
    },
  }
}

// The dev server's own pages reach the proxy under the proxy's origin.
function ownOriginToTarget(proxyReq: ClientRequest, req: IncomingMessage) {
  const origin = proxyReq.getHeader('origin')
  if (!origin || !req.headers.host) return
  try {
    if (new URL(String(origin)).host === req.headers.host) proxyReq.setHeader('origin', 'http://localhost:8400')
  } catch { /* not a URL: left as it is */ }
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
    bundleShareHostIife(),
    bundleWidgetsIife(),
    // The build id in index.html (a hash of the built page + public/): the
    // proxy reads it back and a stale page reloads once. Before the
    // precompress plugin, which must stay last.
    stampBuildId(),
    precompressAssets(),
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
      // changeOrigin rewrites Host to the target; the proxy also refuses a
      // cookie write whose Origin is another host (its origin check), so a
      // page of this dev server names the target's origin instead (any
      // other origin passes through and is refused, as in production).
      '/v1': {
        target: 'http://localhost:8400',
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyReq', ownOriginToTarget)
        },
      },
      '/auth': {
        target: 'http://localhost:8400',
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyReq', ownOriginToTarget)
        },
      },
      // No changeOrigin: the proxy's dashboard socket accepts a page whose
      // Origin names the Host it was sent to, so the browser's Host must
      // pass through.
      '/ws': {
        target: 'http://localhost:8400',
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
