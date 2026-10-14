import { fetchIndex, publishSkill, readSkillFile, testConnection } from "./github.js";
import storeHtml from "./store.html";

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
  });
}

function checkAuth(env, req) {
  const token = env.WORKER_AUTH_TOKEN;
  if (!token) return true; // no token configured = open
  const auth = req.headers.get("Authorization") || "";
  return auth === `Bearer ${token}`;
}

async function handleList(env) {
  try {
    const items = await fetchIndex(env);
    return json({ ok: true, items });
  } catch (e) {
    return json({ ok: false, error: e.message }, 500);
  }
}

async function handleUpload(env, req) {
  if (!checkAuth(env, req)) return json({ ok: false, error: "Unauthorized" }, 401);

  try {
    const body = await req.json();
    const { name, kind, author, description, attachment } = body;
    if (!name || !attachment) {
      return json({ ok: false, error: "Missing name or attachment" }, 400);
    }

    // Extract content from data URL
    const dataUrl = attachment.data_url || "";
    const commaIdx = dataUrl.indexOf(",");
    const raw = commaIdx >= 0 ? dataUrl.slice(commaIdx + 1) : dataUrl;
    const isBase64 = dataUrl.startsWith("data:") && dataUrl.includes(";base64");
    const content = isBase64 ? decodeURIComponent(escape(atob(raw))) : raw;

    const skillId = name.toLowerCase().replace(/[^a-z0-9_-]/g, "-").replace(/-+/g, "-").slice(0, 80);
    const skillData = {
      id: skillId,
      name,
      kind: kind || "tool",
      author: author || "anonymous",
      description: description || "",
      file_name: attachment.name,
      size_bytes: content.length,
      uploaded_at: new Date().toISOString(),
      downloads: 0,
    };

    const result = await publishSkill(env, skillId, skillData, content);
    return json(result);
  } catch (e) {
    return json({ ok: false, error: e.message }, 500);
  }
}

async function handleDownload(env, id) {
  try {
    const { content, metadata } = await readSkillFile(env, id);
    if (!content) return json({ ok: false, error: "Not found" }, 404);

    const fileName = (metadata?.file_name || `${id}.md`).replace(/[\r\n"\\]/g, "").slice(0, 255);
    return new Response(content, {
      headers: {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": `attachment; filename="${fileName}"`,
        "Access-Control-Allow-Origin": "*",
      },
    });
  } catch (e) {
    return json({ ok: false, error: e.message }, 500);
  }
}

async function handleTest(env, req) {
  if (!checkAuth(env, req)) return json({ ok: false, error: "Unauthorized" }, 401);
  const result = await testConnection(env);
  return json(result);
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const path = url.pathname;

    // CORS preflight
    if (req.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
      });
    }

    // Health check
    if (path === "/health") {
      return json({ ok: true });
    }

    // Serve store page
    if (path === "/" || path === "/index.html") {
      return new Response(storeHtml, {
        headers: { "Content-Type": "text/html; charset=utf-8" },
      });
    }

    // API routes
    if (path === "/api/items" && req.method === "GET") {
      return handleList(env);
    }
    if (path === "/api/items" && req.method === "POST") {
      return handleUpload(env, req);
    }
    if (path.startsWith("/api/items/") && path.endsWith("/download")) {
      const id = path.split("/")[3];
      return handleDownload(env, id);
    }
    if (path === "/api/test" && req.method === "POST") {
      return handleTest(env, req);
    }

    return json({ ok: false, error: "Not found" }, 404);
  },
};
