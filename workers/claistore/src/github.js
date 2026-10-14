// ponytail: direct port of claistore.py, no caching — add KV cache if GitHub rate limits hit
const SKILLS_DIR = "skills";
const INDEX_FILE = "index.json";

function githubBase(env) {
  return `https://api.github.com/repos/${env.GITHUB_REPO}/contents/${SKILLS_DIR}`;
}

function ghHeaders(env) {
  return {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "claistore-worker",
  };
}

async function ghRequest(env, method, path, data) {
  const url = `${githubBase(env)}/${path}`;
  const opts = { method, headers: ghHeaders(env) };
  if (data) opts.body = JSON.stringify(data);

  const resp = await fetch(url, opts);
  if (resp.status >= 400) {
    const text = await resp.text();
    throw new Error(`GitHub API ${resp.status}: ${text}`);
  }
  return resp.json();
}

async function getFileSha(env, path) {
  try {
    const result = await ghRequest(env, "GET", path);
    return result.sha;
  } catch (e) {
    if (e.message.includes("404")) return null;
    throw e;
  }
}

async function writeFile(env, path, content, message) {
  const sha = await getFileSha(env, path);
  const data = {
    message,
    content: btoa(unescape(encodeURIComponent(content))),
    branch: env.GITHUB_BRANCH || "main",
  };
  if (sha) data.sha = sha;
  return ghRequest(env, "PUT", path, data);
}

async function readFile(env, path) {
  try {
    const result = await ghRequest(env, "GET", path);
    return decodeURIComponent(escape(atob(result.content || "")));
  } catch (e) {
    if (e.message.includes("404")) return null;
    throw e;
  }
}

export async function fetchIndex(env) {
  const content = await readFile(env, INDEX_FILE);
  if (!content) return [];
  try {
    return JSON.parse(content);
  } catch {
    return [];
  }
}

async function writeIndex(env, items) {
  await writeFile(env, INDEX_FILE, JSON.stringify(items, null, 2), "Update Claistore index");
}

export async function publishSkill(env, skillId, skillData, content) {
  const safeId = skillId.replace(/[^a-zA-Z0-9_-]/g, "_");
  await writeFile(env, `${safeId}.md`, content, `Add skill: ${skillData.name || skillId}`);
  await writeFile(env, `${safeId}.json`, JSON.stringify(skillData, null, 2), `Add skill metadata: ${skillData.name || skillId}`);

  const index = await fetchIndex(env);
  index.push(skillData);
  await writeIndex(env, index);
  return { ok: true, skill_id: skillId };
}

export async function readSkillFile(env, skillId) {
  const safeId = skillId.replace(/[^a-zA-Z0-9_-]/g, "_");
  try {
    const content = await readFile(env, `${safeId}.md`);
    const metaRaw = await readFile(env, `${safeId}.json`);
    const metadata = metaRaw ? JSON.parse(metaRaw) : null;
    return { content, metadata };
  } catch (e) {
    if (e.message.includes("404")) return { content: null, metadata: null };
    throw e;
  }
}

export async function testConnection(env) {
  if (!env.GITHUB_REPO || !env.GITHUB_TOKEN) {
    return { ok: false, error: "Not configured (missing repo or token)" };
  }
  const url = `${githubBase(env)}/${INDEX_FILE}`;
  try {
    const resp = await fetch(url, { headers: ghHeaders(env) });
    if (resp.status === 401 || resp.status === 403) {
      return { ok: false, error: `Authentication failed (HTTP ${resp.status})` };
    }
    if (resp.status === 404) {
      return { ok: false, error: "Repo or path not found (HTTP 404)" };
    }
    if (resp.status >= 400) {
      return { ok: false, error: `GitHub API error ${resp.status}` };
    }
    return { ok: true, repo: env.GITHUB_REPO, branch: env.GITHUB_BRANCH || "main" };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}
