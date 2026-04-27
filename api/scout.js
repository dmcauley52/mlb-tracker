export const config = { runtime: "edge" };

export default async function handler(req) {
  // CORS preflight
  if (req.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
      },
    });
  }

  if (req.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }

  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    return new Response(
      JSON.stringify({ error: "ANTHROPIC_API_KEY env var not set" }),
      { status: 500, headers: { "Content-Type": "application/json" } }
    );
  }

  let prompt;
  try {
    const body = await req.json();
    prompt = body.prompt;
    if (!prompt) throw new Error("Missing prompt");
  } catch (e) {
    return new Response(
      JSON.stringify({ error: "Bad request: " + e.message }),
      { status: 400, headers: { "Content-Type": "application/json" } }
    );
  }

  const upstream = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: "claude-sonnet-4-5",
      max_tokens: 1000,
      messages: [{ role: "user", content: prompt }],
    }),
  });

  if (!upstream.ok) {
    const err = await upstream.text();
    return new Response(
      JSON.stringify({ error: `Anthropic error ${upstream.status}: ${err}` }),
      { status: upstream.status, headers: { "Content-Type": "application/json" } }
    );
  }

  const data = await upstream.json();
  const text = data.content?.[0]?.text ?? "No response from model.";

  return new Response(JSON.stringify({ text }), {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
    },
  });
}
