// Production server: static Vite build + the same authenticated transit proxy as dev.
import { createReadStream, existsSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { resolve, extname, sep } from 'node:path';
import { createDeLijnMiddleware, loadRoutes } from './delijn.mjs';

const root = fileURLToPath(new URL('../dist/', import.meta.url));
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml' };
const transit = createDeLijnMiddleware({ key: process.env.DELIJN_API_KEY,
  allowedOrigin: process.env.DELIJN_ALLOWED_ORIGIN, routes: loadRoutes(process.env.DELIJN_ROUTES_FILE) });
const server = createServer((req, res) => {
  transit(req, res, () => {
    if (!['GET', 'HEAD'].includes(req.method)) { res.writeHead(405, { Allow: 'GET, HEAD' }).end(); return; }
    let pathname;
    try { pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname); }
    catch { res.writeHead(400).end(); return; }
    const path = resolve(root, `.${pathname === '/' ? '/index.html' : pathname}`);
    if (!path.startsWith(root.endsWith(sep) ? root : root + sep) || !existsSync(path) || !statSync(path).isFile()) { res.writeHead(404).end(); return; }
    res.setHeader('Content-Type', types[extname(path)] || 'application/octet-stream');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    if (req.method === 'HEAD') { res.end(); return; }
    const stream = createReadStream(path);
    stream.on('error', () => { if (!res.headersSent) res.statusCode = 500; res.end(); });
    stream.pipe(res);
  }).catch(() => { if (!res.headersSent) res.writeHead(500); res.end(); });
});
server.listen(Number(process.env.PORT || 3000), process.env.HOST || '127.0.0.1', () => {
  console.log(`Ghent viewer: http://${process.env.HOST || '127.0.0.1'}:${server.address().port}`);
});
