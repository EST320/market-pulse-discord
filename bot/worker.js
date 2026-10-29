// Cloudflare Worker: the always-on front door for the bot's slash commands,
// /market and /dip.
//
// Discord sends every slash command to this URL and expects an answer within
// three seconds, which is far less than drawing the chart takes. So the worker
// only does three things:
//   1. checks that the request really comes from Discord (Ed25519 signature),
//   2. answers with a "thinking..." placeholder,
//   3. starts the GitHub Actions workflow for the command, which draws the
//      chart and fills the placeholder in.
//
// Settings (Worker -> Settings -> Variables and Secrets):
//   DISCORD_PUBLIC_KEY   secret  the application's public key
//   GITHUB_TOKEN         secret  fine-grained token with Actions read/write on the repository
//   GITHUB_REPO          text    owner/name; set in wrangler.toml
//   ALLOWED_GUILD_ID     text    optional: only this server may use the command

const PING = 1;
const APPLICATION_COMMAND = 2;
const PONG = 1;
const MESSAGE = 4;
const DEFERRED_MESSAGE = 5;
const EPHEMERAL = 64;

// Command name -> workflow file, and a function turning the command's options
// into workflow inputs (or into an error message for the user).
const COMMANDS = {
  market: { workflow: "market-now.yml", inputs: () => ({}) },
  dip: { workflow: "dip.yml", inputs: dipInputs },
};

const SYMBOL = /^[A-Z0-9][A-Z0-9.\-]{0,9}$/;

function option(interaction, name) {
  return (interaction.data?.options || []).find((o) => o.name === name)?.value;
}

function dipInputs(interaction) {
  const symbol = String(option(interaction, "symbol") || "").trim().toUpperCase().replace("/", "-");
  if (!SYMBOL.test(symbol)) {
    return { error: `“${symbol}” 不是有效的股票代码。` };
  }
  const threshold = option(interaction, "threshold");
  if (threshold !== undefined && !(threshold >= 1 && threshold <= 50)) {
    return { error: "跌幅阈值需要在 1 到 50 之间。" };
  }
  return { symbol, threshold: threshold === undefined ? "" : String(threshold) };
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("market-pulse-discord bot endpoint", { status: 200 });
    }

    const signature = request.headers.get("X-Signature-Ed25519");
    const timestamp = request.headers.get("X-Signature-Timestamp");
    const body = await request.text();
    if (!signature || !timestamp || !(await isFromDiscord(env.DISCORD_PUBLIC_KEY, signature, timestamp + body))) {
      return new Response("invalid request signature", { status: 401 });
    }

    const interaction = JSON.parse(body);

    if (interaction.type === PING) {
      return json({ type: PONG });
    }

    const command = interaction.type === APPLICATION_COMMAND ? COMMANDS[interaction.data?.name] : undefined;
    if (command) {
      if (env.ALLOWED_GUILD_ID && interaction.guild_id !== env.ALLOWED_GUILD_ID) {
        return reply("这个命令只能在指定的服务器里使用。");
      }
      const inputs = command.inputs(interaction);
      if (inputs.error) {
        return reply(inputs.error);
      }
      // Keep working after the response has been sent.
      ctx.waitUntil(startWorkflow(env, interaction, command.workflow, inputs));
      return json({ type: DEFERRED_MESSAGE });
    }

    return reply("未知命令。");
  },
};

function json(payload) {
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
  });
}

// A message only the person who typed the command can see.
function reply(content) {
  return json({ type: MESSAGE, data: { content, flags: EPHEMERAL } });
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) {
    bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  }
  return bytes;
}

async function isFromDiscord(publicKeyHex, signatureHex, message) {
  try {
    const key = await crypto.subtle.importKey("raw", hexToBytes(publicKeyHex), { name: "Ed25519" }, false, ["verify"]);
    return await crypto.subtle.verify("Ed25519", key, hexToBytes(signatureHex), new TextEncoder().encode(message));
  } catch (error) {
    return false;
  }
}

async function startWorkflow(env, interaction, workflow, extraInputs) {
  let problem = null;
  try {
    const response = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${workflow}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "Content-Type": "application/json",
          "User-Agent": "market-pulse-discord-worker",
        },
        body: JSON.stringify({
          ref: "main",
          inputs: {
            ...extraInputs,
            application_id: interaction.application_id,
            interaction_token: interaction.token,
          },
        }),
      },
    );
    if (!response.ok) {
      problem = `GitHub 返回 ${response.status}`;
    }
  } catch (error) {
    problem = "无法连接 GitHub";
  }

  // If the workflow never started, nothing else will fill the placeholder in.
  if (problem) {
    await fetch(
      `https://discord.com/api/v10/webhooks/${interaction.application_id}/${interaction.token}/messages/@original`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: `生成失败：${problem}。请稍后再试。` }),
      },
    );
  }
}
