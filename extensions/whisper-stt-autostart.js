import { spawn } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const DEFAULT_CONFIG = join(homedir(), ".pi", "agent", "stt.json");
const PROJECT_DIR = dirname(dirname(fileURLToPath(import.meta.url)));
const SERVICE_SOURCE_DIR = join(PROJECT_DIR, "service");
const RUNTIME_DIR = join(homedir(), ".pi", "agent", "services", "whisper-stt");
const START_SCRIPT = join(SERVICE_SOURCE_DIR, "start.sh");
const LOG_FILE = join(RUNTIME_DIR, "server.log");
const HEALTH_URL = "http://127.0.0.1:10301/health";
const EXPECTED_TRANSCRIPTION_URL = "http://127.0.0.1:10301/v1/audio/transcriptions";
const READY_TIMEOUT_MS = 30_000;

const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const text = (value) => typeof value === "string" ? value.trim() : "";
const expandPath = (value) => {
  const expanded = value === "~" ? homedir() : value.startsWith("~/") ? join(homedir(), value.slice(2)) : value;
  return isAbsolute(expanded) ? expanded : resolve(expanded);
};
const delay = (ms) => new Promise((resolvePromise) => setTimeout(resolvePromise, ms));

async function configuredForThisServer() {
  const configuredPath = text(process.env.PI_STT_CONFIG);
  const configPath = configuredPath ? expandPath(configuredPath) : DEFAULT_CONFIG;
  if (!existsSync(configPath)) return false;

  const config = object(JSON.parse(await readFile(configPath, "utf8")));
  const statePath = `${configPath}.profile.json`;
  let persistedProfile = "";
  try {
    persistedProfile = text(object(JSON.parse(await readFile(statePath, "utf8"))).profile);
  } catch {
    // A missing profile sidecar means the configured/default profile is active.
  }

  const profileName = text(process.env.PI_STT_PROFILE) || persistedProfile || text(config.profile) || "default";
  const profile = object(object(config.profiles)[profileName]);
  const modeName = text(process.env.PI_STT_MODE) || text(config.mode) || "default";
  const mode = object(object(config.modes)[modeName]);
  let provider = { ...object(config.provider) };

  for (const override of [object(profile.provider), object(mode.provider)]) {
    if (Object.keys(override).length === 0) continue;
    if (text(provider.type) && text(override.type) && text(provider.type) !== text(override.type)) {
      provider = { ...override };
    } else {
      provider = { ...provider, ...override };
    }
  }

  const endpoint = text(provider.endpoint);
  const type = text(provider.type).toLowerCase();
  if (!endpoint || !["local", "openai-compatible"].includes(type)) return false;
  try {
    const url = new URL(endpoint);
    return url.protocol === "http:" && url.hostname === "127.0.0.1" && url.port === "10301" &&
      url.pathname === new URL(EXPECTED_TRANSCRIPTION_URL).pathname;
  } catch {
    return false;
  }
}

async function serverIsHealthy() {
  try {
    const response = await fetch(HEALTH_URL, { signal: AbortSignal.timeout(1000) });
    if (!response.ok) return false;
    const body = await response.json();
    return body.status === "ok" && body.model === "large-v3";
  } catch {
    return false;
  }
}

function launchServer() {
  if (!existsSync(START_SCRIPT)) throw new Error(`Whisper launcher not found: ${START_SCRIPT}`);
  mkdirSync(RUNTIME_DIR, { recursive: true, mode: 0o700 });
  const logFd = openSync(LOG_FILE, "a", 0o600);
  try {
    const child = spawn(START_SCRIPT, [], {
      cwd: dirname(START_SCRIPT),
      detached: true,
      stdio: ["ignore", logFd, logFd],
      env: { ...process.env, PI_WHISPER_PORT: "10301", PI_WHISPER_MODEL: "large-v3" },
    });
    child.spawnError = undefined;
    child.once("error", (error) => { child.spawnError = error; });
    child.unref();
    return child;
  } finally {
    closeSync(logFd);
  }
}

async function stopChild(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  child.kill("SIGTERM");
  await Promise.race([
    new Promise((resolvePromise) => child.once("exit", resolvePromise)),
    delay(2000),
  ]);
  if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
}

export default function whisperSttAutostart(pi) {
  let ownedChild;

  pi.on("session_start", async (_event, ctx) => {
    if (ctx.mode !== "tui") return;

    let enabled;
    try {
      enabled = await configuredForThisServer();
    } catch (error) {
      if (ctx.hasUI) ctx.ui.notify(`Whisper STT autostart: não foi possível ler stt.json (${error.message}).`, "warning");
      return;
    }
    if (!enabled) return;

    ctx.ui.setStatus("whisper-stt-autostart", "Whisper STT iniciando…");
    if (await serverIsHealthy()) {
      ctx.ui.setStatus("whisper-stt-autostart", "Whisper STT pronto · large-v3");
      return;
    }

    try {
      ownedChild = launchServer();
      const deadline = Date.now() + READY_TIMEOUT_MS;
      while (Date.now() < deadline) {
        if (ownedChild.spawnError) throw new Error(`${ownedChild.spawnError.message}; consulte ${LOG_FILE}`);
        if (ownedChild.exitCode !== null || ownedChild.signalCode !== null) {
          if (await serverIsHealthy()) {
            ownedChild = undefined;
            ctx.ui.setStatus("whisper-stt-autostart", "Whisper STT pronto · large-v3");
            return;
          }
          throw new Error(`processo encerrou; consulte ${LOG_FILE}`);
        }
        if (await serverIsHealthy()) {
          // The listener is shared across Pi sessions; no single session owns its lifetime.
          ownedChild = undefined;
          ctx.ui.setStatus("whisper-stt-autostart", "Whisper STT pronto · large-v3");
          return;
        }
        await delay(300);
      }
      throw new Error(`servidor não ficou pronto em ${READY_TIMEOUT_MS / 1000}s; consulte ${LOG_FILE}`);
    } catch (error) {
      if (ownedChild) await stopChild(ownedChild).catch(() => {});
      ownedChild = undefined;
      ctx.ui.setStatus("whisper-stt-autostart", undefined);
      if (ctx.hasUI) ctx.ui.notify(`Whisper STT autostart falhou: ${error.message}`, "warning");
    }
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    if (ctx.mode === "tui") ctx.ui.setStatus("whisper-stt-autostart", undefined);
    // Keep the shared listener alive. It unloads large-v3 after the global idle timeout.
  });
}
