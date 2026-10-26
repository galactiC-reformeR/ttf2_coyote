/**
 * ttf2_coyote 宣发站 — 本地静态服务
 *
 * 用法：node serve.mjs [端口] [绑定地址]
 * 默认 8123 端口、只绑 127.0.0.1（同一台机器访问）。
 * 想在手机上看时：node serve.mjs 8123 0.0.0.0，然后用本机内网 IP 打开。
 */
import { createServer } from "node:http";
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { extname, join, normalize, sep } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL(".", import.meta.url));
const PORT = Number(process.argv[2] || 8123);
const HOST = process.argv[3] || "127.0.0.1";

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".gif": "image/gif",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
  ".md": "text/plain; charset=utf-8",
};

/** 把请求路径解析成站点内的绝对文件路径，越界一律拒绝。 */
function resolvePath(urlPath) {
  const decoded = decodeURIComponent(urlPath.split("?")[0].split("#")[0]);
  const relative = normalize(decoded).replace(/^([/\\])+/, "");
  const absolute = join(ROOT, relative === "" ? "index.html" : relative);
  if (absolute !== ROOT.replace(/[/\\]$/, "") && !absolute.startsWith(ROOT)) return null;
  return absolute;
}

const server = createServer(async (req, res) => {
  const target = resolvePath(req.url || "/");
  if (!target) {
    res.writeHead(403, { "content-type": "text/plain; charset=utf-8" });
    res.end("403 越界路径");
    return;
  }

  let filePath = target;
  try {
    const info = await stat(filePath);
    if (info.isDirectory()) filePath = join(filePath, "index.html");
    const final = await stat(filePath);
    if (!final.isFile()) throw new Error("not a file");

    res.writeHead(200, {
      "content-type": MIME[extname(filePath).toLowerCase()] || "application/octet-stream",
      "content-length": final.size,
      "cache-control": "no-cache",
    });
    createReadStream(filePath).pipe(res);
  } catch {
    res.writeHead(404, { "content-type": "text/html; charset=utf-8" });
    res.end(
      '<!DOCTYPE html><html lang="zh-CN"><meta charset="utf-8">' +
        '<body style="background:#05080d;color:#e8eef7;font:16px/1.7 \'Segoe UI\',sans-serif;padding:60px">' +
        "<h1 style=\"color:#ff7a1a\">404</h1><p>这个路径不存在。</p>" +
        `<p><a href="/" style="color:#4dd2ff">回到首页</a></p></body></html>`,
    );
  }
});

server.listen(PORT, HOST, () => {
  const shown = HOST === "0.0.0.0" || HOST === "::" ? "127.0.0.1" : HOST;
  process.stdout.write(`宣发站已启动：http://${shown}:${PORT}/  （根目录 ${ROOT}）\n`);
});
