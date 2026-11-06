import { defineConfig, loadEnv } from 'vite';
import { createDeLijnMiddleware, loadRoutes } from './server/delijn.mjs';

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), ''), ...process.env };
  const middleware = () => createDeLijnMiddleware({ key: env.DELIJN_API_KEY, routes: loadRoutes(env.DELIJN_ROUTES_FILE) });
  return {
  base: './',
  worker: { format: 'es' },
  build: { chunkSizeWarningLimit: 2000 },
  plugins: [{ name: 'delijn-proxy',
    configureServer(server) { server.middlewares.use(middleware()); },
    configurePreviewServer(server) { server.middlewares.use(middleware()); },
  }],
  };
});
