#!/usr/bin/env node
// First-run setup works before BB has an agent. Never put keys in BB settings.
import { access, chmod, lstat, mkdir, readFile, writeFile } from 'node:fs/promises';
import { homedir, userInfo } from 'node:os';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawn } from 'node:child_process';
import { emitKeypressEvents } from 'node:readline';

export const ADAPTER = '@agentclientprotocol/claude-agent-acp@0.100.0';
const MODEL = 'k3.1-pro[1m]';

function command(executable, args, options = {}) {
  return new Promise((yes, no) => {
    const child = spawn(executable, args, { stdio: 'inherit', ...options });
    child.once('error', () => no(new Error('Не удалось запустить установку. Проверь Node.js 22+ и npm.')));
    child.once('exit', code => code === 0 ? yes() : no(new Error(`Установка завершилась с кодом ${code}. Сохрани текущий шаг и передай помощнику текст ошибки без ключа.`)));
  });
}

export async function privateDirectory(path) {
  await mkdir(path, { recursive: true, mode: 0o700 });
  if ((await lstat(path)).isSymbolicLink()) throw new Error('Папка профиля не должна быть символической ссылкой.');
  if (process.platform === 'win32') {
    // Set the ACL before writing the key. Do not rely on POSIX mode on Windows.
    await command('icacls.exe', [path, '/inheritance:r', '/grant:r', `${userInfo().username}:(OI)(CI)F`], { stdio: 'ignore' });
  } else await chmod(path, 0o700);
}

export function registration(root, executable = process.execPath) {
  const launcher = join(root, 'launch.mjs');
  const skills = relative(homedir(), join(root, 'claude', 'skills'));
  return { id: 'claude-kimi', displayName: 'Kimi (CC)', command: executable,
    args: [launcher], modelCli: { listArgs: [launcher, '--list-models'], primaryModels: [MODEL] },
    nativeSkillRoots: { user: skills.startsWith('..') ? [] : [skills.split(sep).join('/')],
      project: ['.claude/skills', '.agents/skills'].map(path => ({ path, recursive: true, ancestors: true })) } };
}

async function npmCliPath() {
  const bin = dirname(process.execPath);
  for (const path of [join(bin, 'node_modules/npm/bin/npm-cli.js'),
    resolve(bin, '../lib/node_modules/npm/bin/npm-cli.js'), '/usr/share/nodejs/npm/bin/npm-cli.js']) {
    try { await access(path); return path; } catch { /* next supported Node installation */ }
  }
  throw new Error('npm не найден рядом с Node.js. Установи Node.js 22+ официальным установщиком с npm.');
}

export async function provision({ root, apiKey, install = command, npmCli = null }) {
  if (!/^sk-kimi-[A-Za-z0-9_-]+$/.test(apiKey)) throw new Error('Ожидается ключ Kimi Code вида sk-kimi-…; значение не сохранено.');
  root = resolve(root);
  // A separate directory and exclusive writes preserve every existing profile.
  await mkdir(dirname(root), { recursive: true });
  await mkdir(root, { mode: 0o700 });
  await privateDirectory(root);
  const configRoot = join(root, 'claude');
  await privateDirectory(configRoot);
  await writeFile(join(root, 'credentials.json'), JSON.stringify({ apiKey }), { mode: 0o600, flag: 'wx' });
  const env = { ANTHROPIC_BASE_URL: 'https://api.kimi.ai/coding/', ANTHROPIC_MODEL: MODEL,
    ANTHROPIC_CUSTOM_MODEL_OPTION: MODEL, ANTHROPIC_CUSTOM_MODEL_OPTION_NAME: 'Kimi K3.1 Pro (1M)',
    CLAUDE_MODEL_CONFIG: JSON.stringify({ availableModels: [MODEL] }),
    ANTHROPIC_DEFAULT_FABLE_MODEL: MODEL, ANTHROPIC_DEFAULT_OPUS_MODEL: MODEL,
    ANTHROPIC_DEFAULT_SONNET_MODEL: MODEL, ANTHROPIC_DEFAULT_HAIKU_MODEL: MODEL,
    CLAUDE_CODE_SUBAGENT_MODEL: MODEL, CLAUDE_CODE_EFFORT_LEVEL: 'high', MAX_THINKING_TOKENS: '16384',
    CLAUDE_CODE_AUTO_COMPACT_WINDOW: '1048576', CLAUDE_CODE_MAX_CONTEXT_TOKENS: '1048576' };
  await writeFile(join(configRoot, 'settings.json'), JSON.stringify({ env }, null, 2), { mode: 0o600, flag: 'wx' });
  await writeFile(join(configRoot, '.claude.json'), JSON.stringify({ hasCompletedOnboarding: true, penguinModeOrgEnabled: true }), { mode: 0o600, flag: 'wx' });
  const runtime = join(root, 'runtime');
  await install(process.execPath,
    [npmCli ?? await npmCliPath(), 'install', '--prefix', runtime, '--no-audit', '--no-fund', ADAPTER]);
  const adapterDir = join(runtime, 'node_modules', '@agentclientprotocol', 'claude-agent-acp', 'dist');
  const launcher = `import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawn } from 'node:child_process';
const root = dirname(fileURLToPath(import.meta.url));
function run(command, args, env) {
  const child = spawn(command, args, { env, stdio: 'inherit' });
  const interrupt = () => child.kill('SIGINT');
  const terminate = () => child.kill('SIGTERM');
  process.on('SIGINT', interrupt); process.on('SIGTERM', terminate);
  child.once('error', () => { console.error('Компонент Kimi не запустился.'); process.exitCode = 1; });
  child.once('close', (code, signal) => {
    process.off('SIGINT', interrupt); process.off('SIGTERM', terminate);
    process.exitCode = code ?? (signal === 'SIGINT' ? 130 : 143);
  });
}
if (process.argv.includes('--list-models')) {
  console.log('k3.1-pro[1m] - Kimi K3.1 Pro (1M)');
} else {
  const apiKey = JSON.parse(readFileSync(join(root, 'credentials.json'), 'utf8')).apiKey;
  const settings = JSON.parse(readFileSync(join(root, 'claude', 'settings.json'), 'utf8'));
  const env = { ...process.env, ...settings.env, ANTHROPIC_API_KEY: apiKey, CLAUDE_CONFIG_DIR: join(root, 'claude') };
  delete env.ANTHROPIC_AUTH_TOKEN;
  delete env.CLAUDE_CODE_EXECUTABLE;
  delete env.CLAUDE_CODE_OAUTH_TOKEN;
  delete env.CLAUDE_CODE_USE_BEDROCK;
  delete env.CLAUDE_CODE_USE_VERTEX;
  delete env.CLAUDE_CODE_USE_FOUNDRY;
  const adapterDir = ${JSON.stringify(adapterDir)};
  if (process.argv[2] === '--claude') {
    delete process.env.CLAUDE_CODE_EXECUTABLE;
    Object.assign(process.env, env);
    const { claudeCliPath } = await import(pathToFileURL(join(adapterDir, 'acp-agent.js')));
    const cli = await claudeCliPath();
    run(cli, process.argv.slice(3), env);
  } else {
    run(process.execPath, [join(adapterDir, 'index.js'), ...process.argv.slice(2)], env);
  }
}
`;
  await writeFile(join(root, 'launch.mjs'), launcher, { mode: 0o600, flag: 'wx' });
  const agent = registration(root);
  await writeFile(join(root, 'bb-agent.json'), JSON.stringify(agent, null, 2), { mode: 0o600, flag: 'wx' });
  return agent;
}

async function secret() {
  if (!process.stdin.isTTY) throw new Error('Запусти настройщик в обычном локальном терминале: ключ вводится скрыто.');
  process.stdout.write('Ключ Kimi Code (ввод скрыт): ');
  emitKeypressEvents(process.stdin);
  process.stdin.setRawMode(true);
  process.stdin.resume();
  return new Promise((yes, no) => {
    let value = '';
    const finish = () => { process.stdin.off('keypress', key); process.stdin.setRawMode(false); process.stdin.pause(); process.stdout.write('\n'); };
    const key = (text, event = {}) => {
      if (event.ctrl && event.name === 'c') { finish(); no(new Error('Настройка отменена.')); }
      else if (event.name === 'return') { finish(); yes(value.trim()); }
      else if (event.name === 'backspace') value = value.slice(0, -1);
      else if (text && !event.ctrl && !event.meta) value += text;
    };
    process.stdin.on('keypress', key);
  });
}

async function main() {
  if (Number(process.versions.node.split('.')[0]) < 22) throw new Error('Нужен Node.js 22 или новее.');
  if (process.argv.length > 2) throw new Error('Запусти без аргументов. Ключ не передают в командной строке.');
  const root = join(homedir(), '.bb', 'providers', 'kimi-cc');
  let exists = false;
  try { await lstat(root); exists = true; } catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (exists) {
  try {
    // Re-running never overwrites credentials, reinstalls dependencies or migrates an old provider.
    const saved = JSON.parse(await readFile(join(root, 'bb-agent.json'), 'utf8'));
    if (saved.id !== 'claude-kimi') throw new Error('Папка занята другим профилем.');
    console.log('Профиль уже подготовлен. Существующие настройки сохранены.');
    console.log(JSON.stringify([saved], null, 2));
    return;
  } catch (error) { throw new Error(error.code === 'ENOENT' ? 'Сохранилась незавершённая настройка Kimi. Ничего не перезаписано; обратись к помощнику.' : error.message); }
  }
  const agent = await provision({ root, apiKey: await secret() });
  console.log('Профиль подготовлен. В BB: Settings → Plugins → ACP providers → Custom agents.');
  console.log('Если список пуст, вставь массив ниже. Если записи есть, сохрани их и попроси помощника добавить Kimi:');
  console.log(JSON.stringify([agent], null, 2));
  console.log('Затем открой обычный разговор: Kimi (CC) → Kimi K3.1 Pro (1M). Нужен реальный ответ модели.');
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  main().catch(error => { console.error(error.code === 'EEXIST' ? 'Профиль уже существует; ничего не перезаписано. Обратись к помощнику.' : error.message); process.exitCode = 1; });
}
