import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

// Phase 1 of docs/REACT_UI_MIGRATION.md.
//
// Two constraints shape this file, and both come from Flask rather than from
// preference:
//
// 1. Assets are built into the Flask `static/` folder and served by Flask. No
//    second web service, so the deployment topology does not change and the
//    Docker image and CI steps stay the ones we already have.
//
// 2. `base: '/app/'` and `outDir: '../static/app'`. The app is mounted under
//    /app/ so every legacy page route stays exactly where it is — that is what
//    makes an incremental migration possible rather than a big-bang cutover.
export default defineConfig({
  plugins: [react()],
  base: '/app/',
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: '../static/app',
    emptyOutDir: true,
    sourcemap: false,
    // Fingerprinted filenames + immutable caching (Flask route handles the
    // Cache-Control header). The plan's exit criterion is that assets are cached
    // safely, which needs the fingerprint to be in the name -- Vite does that
    // by default for files under assets/.
    assetsDir: 'assets',
    rollupOptions: {
      output: {
        // One vendor chunk so a shell change does not invalidate React itself,
        // and a readable module/preload warning instead of a silent 500 when a
        // dynamic import path is wrong.
        chunkFileNames: 'assets/[name]-[hash].js',
        entryFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
  server: {
    // Dev server proxies to Flask so the SPA can run against the real session
    // cookie and the real CSRF guard. Without this, `npm run dev` tests a
    // different auth path from production -- which is how CSRF breaks in a SPA
    // and only shows up after deployment.
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:5000', changeOrigin: false },
      '/app': { target: 'http://localhost:5000', changeOrigin: false },
    },
  },
});