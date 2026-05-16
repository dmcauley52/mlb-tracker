export const config = { runtime: "edge" };

const MAX_PROMPT_CHARS = 12000;

function corsHeaders(req) {
  const origin = req.headers.get("origin") || "";
  const allowed = isAllowedOrigin(origin, req.headers.get("host") || "");
  return {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": allowed && origin ? origin : "null",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Vary": "Origin",
  };
}

function isAllowedOrigin(origin, host) {
  if (!origin) return true;
  const configured = (process.env.ALLOWED_ORIGINS || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const defaults = [
    host ? `https://${host}` : "",
    host ? `http://${host}` : "",
    process.env.VERCEL_URL ? `https://${process.env.VERCEL_URL}` : "",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
  ].filter(Boolean);
  return [...configured, ...defaults].includes(origin);
}

function json(req, body, status) {
  return new Response(JSON.stringify(body), {
    status,
    headers: corsHeaders(req),
  });
}

export default async function handler(req) {
  if (!isAllowedOrigin(req.headers.get("origin") || "", req.headers.get("host") || "")) {
    return json(req, { error: "Origin not allowed" }, 403);
  }

  // CORS preflight
  if (req.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: corsHeaders(req),
    });
  }

  if (req.method !== "POST") {
    return json(req, { error: "Method not allowed" }, 405);
  }

  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    return json(req, { error: "ANTHROPIC_API_KEY env var not set" }, 500);
  }

  let prompt;
  try {
    const len = Number(req.headers.get("content-length") || 0);
    if (len > MAX_PROMPT_CHARS * 2) throw new Error("Request too large");
    const body = await req.json();
    prompt = body.prompt;
    if (typeof prompt !== "string" || !prompt.trim()) throw new Error("Missing prompt");
    if (prompt.length > MAX_PROMPT_CHARS) throw new Error("Prompt too large");
  } catch (e) {
    return json(req, { error: "Bad request: " + e.message }, 400);
  }

  const upstream = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: "claude-haiku-4-5-20251001",
      max_tokens: 1000,
      messages: [{ role: "user", content: prompt }],
    }),
  });

  if (!upstream.ok) {
    const err = await upstream.text();
    return json(req, { error: `Anthropic error ${upstream.status}: ${err}` }, upstream.status);
  }

  const data = await upstream.json();
  const text = data.content?.[0]?.text ?? "No response from model.";

  return json(req, { text }, 200);
}
