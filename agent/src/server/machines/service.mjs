// SPDX-License-Identifier: AGPL-3.0-or-later
// Rootfs and lifecycle adapted from the recorded BuzzardOS podman donor.
import fs from 'node:fs/promises';
import { createWriteStream, watch } from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import net from 'node:net';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { privateDir, readJson, writeJson, json, jsonBody, socketRequest } from '../common.mjs';
import { claimInstance, processStart } from '../instance.mjs';
import { defaults, validate, prepareRuntime, prepareStorage, machineRoot, packageRoot, daemonSocket, locations,
  load, containerName, machineLabel, definition, displayCommand, displayAutostart, helper, userNamespace, mount } from './config.mjs';
import { podman, podmanLaunch, installed } from './process.mjs';
import { startMedia, stopMedia, mediaDevices, mediaStatus, integrationArguments } from './media.mjs';
import { duringUpdate, mutations, runtimeVersion, updateMessage, updateError } from './update.mjs';
import { binary } from '../access/paths.mjs';
import { installPendingAutostart, stoppedAutostart } from './autostart.mjs';
import { shutdownOwnedMachines, stopOwnedGateway as stopGateway } from './shutdown.mjs';

process.umask(0o077);
await privateDir(machineRoot);
await privateDir(path.dirname(daemonSocket));
const owner = await claimInstance('machines');
if (!owner) process.exit(0);
await fs.rm(daemonSocket, { force: true });
const active = new Map();
const runtime = await runtimeVersion(true), started = await processStart(process.pid);
let updatePending = false, retiring = false, retirementStarted = false, mutationRequests = 0, upgradeCheck;
const activationWatchers = new Map(), activationWaiters = new Set();
let server, browserOwner, browserWatcher, closing = false, shutdownPromise, shutdownReply = false, exiting = false;
let browserAdoption = Promise.resolve();
function armGuard() {
  if (Number(process.env.TASKARIAN_GUARD_PID) !== process.ppid) throw Error('The container runtime guard is unavailable');
  process.kill(process.ppid, 'SIGTERM');
}
function shutdown() {
  if (shutdownPromise) return shutdownPromise;
  closing = true;
  for (const watcher of activationWatchers.values()) watcher.close();
  activationWatchers.clear();
  for (const wake of activationWaiters) wake();
  for (const job of active.values()) job.cancel.abort();
  shutdownPromise = (async () => {
    // Let cancelled commands unwind before inspecting the durable private store.
    // The guard bounds cleanup even when a child or the JS event loop is stuck.
    await Promise.race([Promise.allSettled([...active.values()].map(job => job.completion)), sleep(2000)]);
    return shutdownOwnedMachines({ stopMedia });
  })();
  return shutdownPromise;
}
function exitOwner(code = 0) {
  if (exiting) return;
  exiting = true;
  browserWatcher?.kill('SIGTERM'); browserWatcher = null;
  server?.close(); server?.closeAllConnections();
  owner.close(); process.exit(code);
}
function terminate() {
  try { armGuard(); } catch (error) { console.error('Container runtime guard:', error.message); }
  void shutdown().then(() => exitOwner(), error => { console.error('Container shutdown:', error.message); exitOwner(1); });
}
function adoptBrowser(value) {
  const result = browserAdoption.then(async () => {
    if (closing) throw Error('The container service is stopping');
    if (!Number.isInteger(value?.pid) || value.pid < 2 || !/^[1-9][0-9]*$/.test(value.started || '')) throw Error('Invalid browser process identity');
    if (browserOwner) {
      if (browserOwner.pid !== value.pid || browserOwner.started !== value.started) throw Error('Containers belong to another browser process');
      return;
    }
    if (await processStart(value.pid) !== value.started || (await fs.stat(`/proc/${value.pid}`)).uid !== process.getuid()) throw Error('The browser process is no longer running');
    const child = spawn(binary('runtime-guard'), ['wait-owner', String(value.pid), value.started, 'guard-stop'], { stdio: ['ignore', 'pipe', 'inherit'] });
    browserWatcher = child;
    child.once('exit', () => { if (browserWatcher === child && !retirementStarted) terminate(); });
    child.once('error', () => { if (browserWatcher === child && !retirementStarted) terminate(); });
    try {
      await new Promise((resolve, reject) => {
        let output = '';
        const finish = error => {
          clearTimeout(timer); child.stdout.off('data', data); child.off('exit', ended); child.off('error', finish);
          error ? reject(error) : resolve();
        };
        const timer = setTimeout(() => finish(Error('Could not track the browser process')), 5000);
        const data = bytes => { output += bytes; if (output === 'ready\n') finish(); else if (output.length > 64 || output.includes('\n')) finish(Error('Invalid browser lifetime tracker response')); };
        const ended = () => finish(Error('The browser lifetime tracker could not start'));
        child.stdout.on('data', data); child.once('exit', ended); child.once('error', finish);
      });
      if (closing || await processStart(value.pid) !== value.started) throw Error('The browser process is no longer running');
      browserOwner = { pid: value.pid, started: value.started };
    } catch (error) { child.kill('SIGTERM'); throw error; }
  });
  browserAdoption = result.catch(() => {}); return result;
}
process.on('SIGTERM', () => { if (shutdownReply) void shutdown().catch(() => {}); else terminate(); });
process.on('SIGINT', () => process.emit('SIGTERM'));
function watchActivation(id) {
  if (activationWatchers.has(id)) return;
  try {
    const watcher = watch(locations(id).exchange, (_event, name) => {
      if (String(name) === 'open.json') for (const wake of activationWaiters) wake();
    });
    watcher.on('error', () => { watcher.close(); activationWatchers.delete(id); });
    activationWatchers.set(id, watcher);
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
}
async function pendingActivations() {
  const result = [];
  for (const r of await records()) {
    const value = await readJson(path.join(r.files.exchange,'open.json'), null).catch(() => null);
    if (value?.schema === 1 && value.action === 'open' && /^[a-f0-9-]{36}$/.test(value.id) && r.state.activation !== value.id) result.push({machineId:r.config.id,id:value.id});
  }
  return result;
}
let catalogueQueue = Promise.resolve();
const catalogue = fn => { const result = catalogueQueue.then(() => { if (closing) throw Error('The container service is stopping'); return fn(); }); catalogueQueue = result.catch(() => {}); return result; };
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function records() {
  const directories = await fs.readdir(machineRoot, { withFileTypes: true });
  const result = [];
  for (const dir of directories) if (dir.isDirectory() && /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(dir.name)) {
    try { result.push(await load(dir.name)); }
    catch (error) { result.push({ files: locations(dir.name), config: { id: dir.name, name: dir.name }, state: { state: 'error', lastError: error.message } }); }
  }
  return result;
}
function brief(record) {
  return { id: record.config.id, name: record.config.name, autostart: Boolean(record.config.autostart), ...record.state,
    displayAutostart: displayAutostart(record.config),
    pendingChanges: !!record.state.definition && record.state.definition !== definition(record.config),
    configRevision: definition(record.config), operation: active.get(record.config.id)?.id || record.state.operation || null };
}
async function distinct(name, except) {
  const key = name.trim().normalize('NFKC').toLocaleLowerCase();
  if ((await records()).some(r => r.config.id !== except && r.config.name.normalize('NFKC').toLocaleLowerCase() === key)) throw Error('A machine already has that name');
}
async function saveState(id, fields) {
  const file = locations(id).state;
  const state = { ...await readJson(file, {}), ...fields, updatedAt: new Date().toISOString() };
  await writeJson(file, state);
  return state;
}
async function inspect(id, { signal } = {}) {
  const result = await podman(['container', 'inspect', containerName(id)], { allowFailure: true, signal });
  if (result.code) {
    const exists = await podman(['container', 'exists', containerName(id)], { allowFailure: true, signal });
    if (exists.code === 1) return null;
    throw Error(result.stderr.trim() || 'Could not inspect machine');
  }
  const value = JSON.parse(result.stdout)[0];
  if (value.Config?.Labels?.[machineLabel] !== id) throw Error('Container ownership label does not match; refusing to control it');
  return value;
}
async function checkRuntimeIdentity(id, info) {
  const p = locations(id);
  const mounts = info.Mounts || [];
  for (const [source,target] of [[p.rootfs,'/'],[p.exchange,'/run/taskarian-host'],[p.settings,'/run/taskarian-settings'],[p.guestDisplay,'/run/taskarian-display']]) {
    if (mounts.filter(item => item.Destination === target).length !== 1 || !mounts.some(item => item.Source === source && item.Destination === target)) throw Error('Podman changed the owned endpoint/rootfs mounts; stopped before launch');
  }
}
async function clearStoppedBrowserSocket(files, signal) {
  const directory = path.join(files.exchange, 'agent');
  const socketPath = path.join(directory, 'guest-browser.sock');
  const stat = async file => fs.lstat(file).catch(error => { if (error.code === 'ENOENT') return null; throw error; });
  const parent = await stat(directory);
  if (!parent) return;
  if (!parent.isDirectory() || parent.uid !== process.getuid() || parent.mode & 0o077) throw Error('Guest browser control directory is not private and owned');
  const original = await stat(socketPath);
  if (!original) return;
  if (!original.isSocket() || original.uid !== process.getuid()) throw Error('Guest browser control path is not an owned socket');
  // The serialized operation has verified its guest browser/display is stopped. Still
  // refuse a live listener, including one outside that guest, instead of taking
  // over its endpoint. A refused connection alone establishes a stale socket.
  const probe = AbortSignal.any([signal, AbortSignal.timeout(2000)]);
  await new Promise((resolve, reject) => {
    const client = net.createConnection({ path: socketPath, signal: probe });
    client.once('connect', () => { client.destroy(); reject(Error('Guest browser control is still listening; its socket was left unchanged')); });
    client.once('error', error => {
      client.destroy();
      if (['ECONNREFUSED', 'ENOENT'].includes(error.code)) resolve();
      else reject(Error('Could not verify the stale guest browser socket; it was left unchanged', { cause: error }));
    });
  });
  signal.throwIfAborted();
  const currentParent = await stat(directory), current = await stat(socketPath);
  if (!current) return;
  if (!currentParent || currentParent.dev !== parent.dev || currentParent.ino !== parent.ino ||
      currentParent.uid !== process.getuid() || currentParent.mode & 0o077 || !currentParent.isDirectory() ||
      current.dev !== original.dev || current.ino !== original.ino || !current.isSocket() || current.uid !== process.getuid()) {
    throw Error('Guest browser control path changed during startup; it was left unchanged');
  }
  await fs.unlink(socketPath);
}
async function operation(id) {
  const p = locations(id);
  return readJson(p.operation, null);
}
async function begin(id, kind, work) {
  if (closing) throw Error('The container service is stopping');
  if (active.has(id)) throw Error('This machine already has an operation in progress');
  // Reserve before the first await; starts/cancel/delete can arrive concurrently.
  const cancel = new AbortController();
  let completed;
  const completion = new Promise(resolve => { completed = resolve; });
  active.set(id, { id: null, cancel, completion });
  let current;
  try { current = await load(id); cancel.signal.throwIfAborted(); } catch (error) { active.delete(id); completed(); throw error; }
  const job = { id: randomUUID(), machineId: id, kind, state: 'running', phase: 'Preparing', startedAt: new Date().toISOString(), exitCode: null };
  const stream = createWriteStream(current.files.output, { flags: 'w', mode: 0o600 });
  try { await new Promise((resolve,reject) => { stream.once('open', resolve); stream.once('error',reject); }); } catch (error) { active.delete(id); completed(); throw error; }
  let streamError;
  stream.on('error', error => { streamError = error; cancel.abort(); });
  const context = { ...job, signal: cancel.signal, output: data => { if(stream.write(data)) return; return new Promise((resolve,reject) => { const done=error=>{stream.off('drain',drained);stream.off('error',failed);error?reject(error):resolve();}; const drained=()=>done();const failed=e=>done(e);stream.once('drain',drained);stream.once('error',failed); }); },
    phase: async phase => { job.phase = phase; await writeJson(current.files.operation, job); },
    check: () => { if (cancel.signal.aborted) throw Error('Operation cancelled'); if (streamError) throw streamError; },
  };
  active.set(id, { ...job, cancel, completion });
  try {
    await writeJson(current.files.operation, job);
    await saveState(id, { operation: job.id, lastError: null, state: kind === 'create' ? 'preparing' : kind === 'delete' ? 'deleting' : kind === 'stop' ? 'stopping' : 'starting' });
  } catch (error) { active.delete(id); completed(); stream.destroy(); throw error; }
  // The independent daemon owns this promise. Closing the native view never cancels it.
  void (async () => {
    try {
      context.check(); await work(context); context.check();
      job.state = 'complete'; job.exitCode = 0;
    } catch (error) {
      job.state = cancel.signal.aborted ? 'cancelled' : 'failed'; job.error = error.message; job.exitCode = 1;
      if (await fs.stat(current.files.directory).catch(() => null)) await saveState(id, { state: 'error', lastError: error.message });
      stream.write('\n' + error.message + '\n');
    } finally {
      job.finishedAt = new Date().toISOString();
      try {
        if (!stream.destroyed) await new Promise(resolve => stream.end(resolve));
        if (await fs.stat(current.files.directory).catch(() => null)) await writeJson(current.files.operation, job);
      } finally { active.delete(id); completed(); }
    }
  })().catch(error => console.error('Machine operation state could not be persisted:', error.message));
  return { machine: brief(current), operation: job };
}
async function prepareImage(config, job) {
  await installed(); job.check();
  const p = locations(config.id), context = path.join(p.directory, 'build-context');
  await job.phase('Preparing the pinned Debian image and matching guest packages');
  await fs.cp(path.join(packageRoot, 'image'), context, { recursive: true, force: true, dereference: false });
  await fs.copyFile(p.containerfile, path.join(context, 'Containerfile'));
  const manifest = await readJson(path.join(context, 'packages/manifest.json'));
  const packaged = await readJson(path.join(packageRoot,'../build-platform.json'));
  if (manifest.revision !== packaged.revision || !manifest.revision || manifest.architecture !== ({ x64:'amd64', arm64:'arm64' })[process.arch] || !manifest.packages?.length) throw Error('Matching guest package artifacts are missing for this architecture');
  const backend = manifest.packages.filter(item => item.package === 'taskarian-backend');
  if (!packaged.version || manifest.version !== packaged.version || backend.length !== 1 || backend[0].version !== packaged.version) {
    throw Error('The container guest backend must match this Taskarian app version');
  }
  const { createHash } = await import('node:crypto');
  for (const item of manifest.packages) {
    if (path.basename(item.file) !== item.file || !/^[a-f0-9]{64}$/.test(item.sha256)) throw Error('Invalid guest package manifest');
    const file = await fs.open(path.join(context, 'packages', item.file));
    const hash = createHash('sha256');
    try { for await (const bytes of file.createReadStream()) hash.update(bytes); } finally { await file.close(); }
    if (hash.digest('hex') !== item.sha256) throw Error('Guest package checksum mismatch: ' + item.file);
  }
  const image = 'localhost/taskarian-machine:' + config.id;
  await job.phase('Building Debian, XFCE and the Taskarian guest');
  await podman(['build','--layers','--tag',image,'--file',path.join(context,'Containerfile'),context], { output: job.output, signal: job.signal });
  job.check();
  await job.phase('Materializing the persistent rootfs with native UID mapping');
  await privateDir(p.rootfs);
  // Export a never-started image container, as in the donor. Archiving a running
  // helper's / would include live proc/sys mountpoint metadata and OCI injections.
  // Extract in the machine's namespace so numeric guest IDs map natively.
  const copy = 'set -e; chown 0:0 /run/taskarian-materialize; chmod 0755 /run/taskarian-materialize; exec tar --numeric-owner --xattrs --xattrs-include="*" --acls -xpf - -C /run/taskarian-materialize';
  const materializer = containerName(config.id) + '-materialize', source = containerName(config.id) + '-source';
  const archive = path.join(p.directory, 'materialize.tar');
  let input;
  try {
    await podman(['create','--name',source,'--label',`${machineLabel}=${config.id}`,'--network=none','--log-driver=none','--entrypoint=/usr/bin/true',image], { output: job.output, signal: job.signal });
    await podman(['export','--output',archive,source], { output: job.output, signal: job.signal });
    input = await fs.open(archive, 'r');
    await podman(['run','--rm','--interactive','--name',materializer,'--label',`${machineLabel}=${config.id}`,userNamespace,'--user=0','--network=none','--no-hosts','--read-only','--log-driver=none', '--entrypoint=/bin/bash',
      '--mount',mount(p.rootfs,'/run/taskarian-materialize'),image,'-c',copy], { input: input.fd, output: job.output, signal: job.signal });
  } finally {
    await input?.close();
    // Cancelling the CLI alone need not terminate its attached container.
    try { await podman(['rm','--force','--ignore',materializer,source],{output:job.output}); }
    finally { await fs.rm(archive, { force: true }); }
  }
  await saveState(config.id, { provisioned: true, packageRevision: manifest.revision, state: 'stopped', lastError: null });
}
async function guestCall(id, command, value = {}, timeout = 30000) {
  return socketRequest(path.join(locations(id).exchange,'agent/control.sock'), '/' + command, value, timeout);
}
async function startGateway(config, job) {
  const p = locations(config.id);
  const existing = await readJson(path.join(p.runtime,'gateway.json'), null);
  if (existing && existing.pid && await processStart(existing.pid) === existing.started) return existing;
  await stopGateway(config.id); job.check();
  const executable = path.join(packageRoot, '../display/taskarian-display');
  await fs.access(executable, fs.constants.X_OK);
  await fs.rm(path.join(p.display,'ready.json'), { force: true });
  const args = ['gateway','--directory',p.display,'--width','1280','--height','720'];
  if (config.display.mode === 'auto' && config.display.renderNode) {
    args.push('--drm-render-node',config.display.renderNode);
    if (config.display.formats.length) {
      const formats = path.join(p.runtime, 'display-formats.json');
      await writeJson(formats, { formats: config.display.formats });
      args.push('--formats-file',formats);
    }
  }
  const file = await fs.open(path.join(p.directory,'display.log'),'a',0o600);
  try { job.check(); } catch (error) { await file.close(); throw error; }
  const child = spawn(executable,args,{stdio:['ignore',file.fd,file.fd],detached:true});
  await Promise.all([file.close(), new Promise((resolve,reject) => { child.once('spawn',resolve); child.once('error',reject); })]);
  child.unref();
  const started = await processStart(child.pid);
  await writeJson(path.join(p.runtime,'gateway.json'),{pid:child.pid,started});
  for (const until = Date.now()+15000; Date.now()<until;) {
    job.check();
    if (await processStart(child.pid) !== started) throw Error('The display gateway stopped; see display.log');
    const ready = await readJson(path.join(p.display,'ready.json'),null);
    if (ready?.schema === 1 && ready.pid === child.pid) {
      if (ready.waylandSocket !== path.join(p.guestDisplay,'wayland-0')) throw Error('Display gateway returned an unexpected guest endpoint');
      // Persist native process identity separately; the donor handshake has no start tick.
      await writeJson(path.join(p.runtime,'gateway.json'),{pid:child.pid,started,...ready});
      return ready;
    }
    await sleep(50);
  }
  throw Error('The private display gateway did not become ready');
}
async function writeDisplayConfig(config, gateway) {
  const value = { machineId: config.id, generation: randomUUID(), enabled: Boolean(gateway), command: displayCommand(config),
    mode: gateway?.mode ?? config.display.mode, software: gateway?.mode === 'software', width: 1280, height: 720 };
  await writeJson(path.join(locations(config.id).settings, 'display.json'), value);
  return value;
}
async function failAfterCleanup(error, steps) {
  const failures = [];
  for (const [name, cleanup] of steps) {
    try { await cleanup(); }
    catch (failure) { failures.push(`${name}: ${failure.message}`); }
  }
  // Keep the operation's original failure first, and still attempt every owned
  // cleanup when an earlier step fails. Cleanup never turns a failed Start On.
  if (failures.length) throw Object.assign(new Error(`${error.message}; cleanup failed (${failures.join('; ')})`, { cause: error }), { name: error.name, code: error.code });
  throw error;
}
async function displayStatus(id, info) {
  const p = locations(id), gateway = await readJson(path.join(p.runtime, 'gateway.json'), null);
  const configured = await readJson(path.join(p.settings, 'display.json'), null);
  const status = await readJson(path.join(p.exchange, 'display-status.json'), null);
  const live = gateway && await processStart(gateway.pid) === gateway.started;
  const result = info?.State?.Running ? await podman(['exec', '--user', '0', info.Id, '/usr/bin/systemctl', 'show', 'taskarian-display.service', '--property=ActiveState', '--value']) : null;
  const unit = result?.stdout.trim();
  const current = status?.generation === configured?.generation ? status : null;
  const error = configured?.enabled !== false && (current?.error || (unit === 'failed' ? 'The Display service stopped. Check its launch command and service log.' : null));
  return { running: Boolean(live && unit === 'active' && current?.running),
    state: ['activating', 'deactivating'].includes(unit) ? (unit === 'activating' ? 'starting' : 'stopping') :
      live && unit === 'active' && current?.running ? 'running' : error ? 'error' : 'off',
    error: error || null,
    mode: gateway?.mode, fallbackReason: gateway?.fallbackReason || null };
}
async function changeDisplay(id, enabled) {
  if (closing) throw Error('The container service is stopping');
  if (active.has(id)) throw Error('Wait for the current container operation');
  const cancel = new AbortController();
  let completed;
  const completion = new Promise(resolve => { completed = resolve; });
  active.set(id, { cancel, completion, kind: enabled ? 'display-start' : 'display-stop' });
  try {
    const { config, files } = await load(id), info = await inspect(id, { signal: cancel.signal });
    validate(config,id);
    if (!info?.State?.Running) throw Error('Start this container before changing Display');
    const job = { signal: cancel.signal, check: () => cancel.signal.throwIfAborted(),
      output: data => fs.appendFile(path.join(files.directory, 'display.log'), data, { mode: 0o600 }) };
    const unit = action => podman(['exec', '--user', '0', info.Id, '/usr/bin/systemctl', action, 'taskarian-display.service'], { signal: cancel.signal, output: job.output });
    if (!enabled) {
      const file = path.join(files.settings, 'display.json');
      await writeJson(file, { ...await readJson(file), enabled: false });
      await unit('stop'); job.check();
      await stopMedia(id); await stopGateway(id);
    } else {
      const previous = await displayStatus(id, info);
      if (!previous.running) {
        // A failed/stopped display is explicitly restarted; this never restarts
        // the independent guest Agent, its Pi workers or the whole container.
        await unit('stop'); job.check();
        await stopMedia(id); await stopGateway(id);
        await clearStoppedBrowserSocket(files, cancel.signal);
        await fs.rm(path.join(files.exchange, 'agent/guest-browser-status.json'), { force: true });
        try {
          const gateway = await startGateway(config, job);
          const generation = (await writeDisplayConfig(config, gateway)).generation;
          await unit('start');
          let ready = false;
          for (const until = Date.now() + 60000; Date.now() < until;) {
            job.check();
            const status = await readJson(path.join(files.exchange, 'display-status.json'), null);
            if (status?.generation === generation) {
              if (status.error) throw Error(status.error);
              // The guest browser/control channel is independent of this
              // generation's desktop. Agent failure must not tear Display down.
              if (status.running && (await displayStatus(id, info)).running) { ready = true; break; }
            }
            await sleep(250);
          }
          if (!ready) throw Error('The Display command did not become ready; check its service log');
          await startMedia(config, info, job);
        } catch (error) {
          // No unattended retry and no software/headless substitute after a
          // failed explicit start. Keep the actual error and the Pi backend.
          await failAfterCleanup(error, [
            ['Display service', () => podman(['exec', '--user', '0', info.Id, '/usr/bin/systemctl', 'stop', 'taskarian-display.service'])],
            ['media', () => stopMedia(id)], ['display gateway', () => stopGateway(id)],
          ]);
        }
      }
    }
    return displayStatus(id, info);
  } finally { active.delete(id); completed(); }
}
async function start(config, job) {
  job.check();
  const id=config.id, p=await prepareRuntime(id), state=await readJson(p.state,{});
  watchActivation(id);
  if (!state.provisioned) throw Error('Finish creating this machine before starting it');
  await installed(); validate(config,id);
  await fs.access(config.launch.executable,fs.constants.X_OK);
  await fs.access(config.launch.cwd,fs.constants.X_OK);
  let info=await inspect(id);
  if (info?.State?.Running) {
    if (state.definition !== definition(config)) throw Error('Saved creation settings changed. Stop this machine before applying them');
    await saveState(id,{state:'running'}); return;
  }
  await installPendingAutostart(await load(id));
  if (info && state.definition !== definition(config)) { await podman(['rm',containerName(id)],{output:job.output,signal:job.signal}); info=null; }
  await saveState(id,{state:'starting',lastError:null});
  const startDisplay = displayAutostart(config);
  await job.phase(startDisplay ? 'Starting the private display and guest services' : 'Starting the guest Agent');
  let gateway;
  try {
    job.check();
    if (startDisplay) gateway=await startGateway(config,job);
    else { await stopMedia(id); await stopGateway(id); job.check(); }
    const displayConfig=await writeDisplayConfig(config,gateway);
    await writeJson(path.join(p.settings,'integration.json'),{schema:1,media:Object.fromEntries(Object.entries(config.integrations.media).map(([k,v])=>[k,v.enabled]))});
    if (!info) {
      await podmanLaunch(config.launch.executable,config.launch.arguments,{cwd:config.launch.cwd,env:config.launch.environment,output:job.output,signal:job.signal});
      info=await inspect(id); if (!info) throw Error('Podman did not create the machine');
      await saveState(id,{definition:definition(config)});
    }
    info = await inspect(id, { signal: job.signal });
    if (!info || info.State?.Running !== false) throw Error('The guest is not stopped; its browser socket was left unchanged');
    await checkRuntimeIdentity(id,info);
    await clearStoppedBrowserSocket(p, job.signal);
    await fs.rm(path.join(p.exchange,'agent/guest-browser-status.json'),{force:true});
    await fs.rm(path.join(p.exchange,'integration-status.json'),{force:true});
    job.check();
    await podman(['start',containerName(id)],{output:job.output,signal:job.signal});
    for (const until=Date.now()+60000;Date.now()<until;) {
      job.check();
      try {
        const status=await guestCall(id,'status');
        const display=startDisplay ? await readJson(path.join(p.exchange,'display-status.json'),null) : null;
        const currentDisplay=display?.generation===displayConfig.generation ? display : null;
        if (status.web?.error) throw Error(status.web.error);
        if (currentDisplay?.error) throw Error(currentDisplay.error);
        // Agent Off is an intentional ready state for its private controller.
        // Its browser channel cannot connect until Agent is On; Display has
        // its own generation-bound readiness and must not depend on that channel.
        const agentOff=status.web?.desired===false && status.web?.status==='stopped';
        const agentReady=!status.manager?.exiting && (agentOff || status.web?.status==='running');
        if (agentReady && (!startDisplay || currentDisplay?.running && (await displayStatus(id, await inspect(id,{signal:job.signal}))).running)) {
          if (startDisplay) await startMedia(config, await inspect(id),job);
          await saveState(id,{state:'running',lastError:null,display:gateway?.mode ?? null,displayFallbackReason:gateway?.fallbackReason||null}); return;
        }
      } catch (error) { if (!['ENOENT','ECONNREFUSED','ECONNRESET'].includes(error.code)) throw error; }
      const live=await inspect(id); if (!live?.State?.Running) throw Error('Guest stopped during startup; inspect the guest service log');
      await sleep(250);
    }
    throw Error(startDisplay ? 'Guest Taskarian Agent and Display did not become ready within 60 seconds' : 'Guest Taskarian Agent did not become ready within 60 seconds');
  } catch (error) {
    // A failed Start cannot leave a partially ready desktop appearing On.
    await failAfterCleanup(error, [
      ['media', () => stopMedia(id)],
      ['container', () => podman(['stop','--ignore',containerName(id)])],
      ['display gateway', () => stopGateway(id)],
    ]);
  }
}
async function stop(id,job,{deleting=false}={}) {
  await job.phase(deleting?'Stopping publishing, workers and machine services':'Stopping machine services');
  const failures = [];
  const attempt = async (name, work) => {
    try { await work(); }
    catch (error) { failures.push({ name, error }); }
  };
  if (deleting) await attempt('publishing', async () => {
    try { await guestCall(id,'share-publish',{enabled:false},2000); }
    catch (error) { if (!['ENOENT','ECONNREFUSED'].includes(error.code)) throw error; }
  });
  // A stalled guest cannot veto an explicit Stop. Keep every failure visible,
  // but finish the bounded, ownership-checked cleanup before returning it.
  await attempt('container', async () => {
    const info=await inspect(id,{signal:AbortSignal.timeout(2000)});
    if (info?.State?.Running || info?.State?.Paused) {
      await attempt('guest services', async () => {
        try { await guestCall(id,'shutdown',{},2000); }
        catch (error) { if (!['ENOENT','ECONNREFUSED','ECONNRESET'].includes(error.code)) throw error; }
      });
      await attempt('container stop', () => podman(['stop','--ignore','--time=6',containerName(id)],{output:job.output,signal:AbortSignal.timeout(8000)}));
      const remaining=await inspect(id,{signal:AbortSignal.timeout(2000)});
      if (remaining?.State?.Running || remaining?.State?.Paused) {
        await podman(['kill','--signal=KILL',containerName(id)],{output:job.output,signal:AbortSignal.timeout(3000)});
      }
      const final=await inspect(id,{signal:AbortSignal.timeout(2000)});
      if (final?.State?.Running || final?.State?.Paused) throw Error('The owned container has not stopped');
    }
  });
  await attempt('media', () => stopMedia(id));
  await attempt('display gateway', () => stopGateway(id));
  if (failures.length) {
    const first=failures[0].error;
    throw Object.assign(new Error(failures.map(({name,error}) => `${name}: ${error.message}`).join('; '), {cause:first}), {name:first.name,code:first.code});
  }
  await saveState(id,{state:'stopped',lastError:null});
}
async function remove(id,job) {
  const p=locations(id);
  await stop(id,job,{deleting:true});
  await podman(['rm','--ignore',containerName(id)],{output:job.output,signal:job.signal});
  await job.phase('Removing this machine rootfs and private Pi history');
  // Namespace-aware cleanup rejects live mountpoints and does not follow symlinks.
  await podman(['unshare','/usr/bin/python3',path.join(packageRoot,'libexec/remove-tree.py'),p.rootfs,p.directory],{output:job.output,signal:job.signal});
  activationWatchers.get(id)?.close(); activationWatchers.delete(id);
  await fs.rm(p.runtime,{recursive:true,force:true});
  // The catalogue record is the directory. Remove it only after rootfs cleanup succeeds.
  await fs.rm(p.directory,{recursive:true,force:true});
}
function applyCapabilities(config, capabilities) {
  if (!capabilities) return;
  if (!Array.isArray(capabilities.formats)) throw Error('Invalid native display capabilities');
  config.display.formats = capabilities.formats;
  // Preserve an explicit device choice. The native default is only for an unset one.
  if (!config.display.renderNode && capabilities.drmRenderNode) config.display.renderNode = capabilities.drmRenderNode;
}
async function saveCapabilities(value) {
  if (active.has(value.id)) throw Error('This machine already has an active operation');
  const r = await load(value.id), previous = integrationArguments(r.config);
  applyCapabilities(r.config, value.displayCapabilities);
  validate(r.config, value.id);
  const next = integrationArguments(r.config);
  if (JSON.stringify(previous) !== JSON.stringify(next)) {
    const args = r.config.launch.arguments;
    if (previous.length) {
      const index = args.findIndex((_, i) => previous.every((a,j) => args[i+j] === a));
      if(index < 0) throw Error('Reconcile the edited helper arguments in machine settings');
      args.splice(index,previous.length);
    }
    args.splice(args.indexOf('--rootfs'),0,...next);
  }
  await writeJson(r.files.config,r.config);
  return r.config;
}
async function create(value) {
  return catalogue(async()=>{
    if (typeof value.name!=='string') throw Error('Enter a machine name');
    await distinct(value.name);
    const id=randomUUID(),p=locations(id),config=await defaults(id,value.name.trim());
    applyCapabilities(config,value.displayCapabilities);
    validate(config,id);
    const additions=integrationArguments(config);
    config.launch.arguments.splice(config.launch.arguments.indexOf('--rootfs'),0,...additions);
    await privateDir(p.directory);
    await fs.copyFile(path.join(packageRoot,'image/Containerfile'),p.containerfile);
    await writeJson(p.config,config); await saveState(id,{state:'new',provisioned:false});
    return begin(id,'create',async job=>{await prepareImage(config,job); await start(config,job);});
  });
}
async function edit(value) {
  return catalogue(async()=>{
    const old=await load(value.id); if(active.has(value.id)) throw Error('Wait for this machine operation to finish before editing its settings');
    const config=validate(value.config,value.id); config.name=config.name.trim(); await distinct(config.name,value.id);
    // UI helpers own only their previous argument sequence; custom flags are untouched.
    const previous=integrationArguments(old.config),next=integrationArguments(config);
    if (JSON.stringify(previous)!==JSON.stringify(next)) {
      let args=[...config.launch.arguments];
      if (previous.length) {
        const start=args.findIndex((_,i)=>previous.every((a,j)=>args[i+j]===a));
        if(start<0) throw Error('Helper-managed arguments were edited; reconcile them before changing the integration controls');
        args.splice(start,previous.length);
      }
      args.splice(args.indexOf('--rootfs'),0,...next); config.launch.arguments=args;
    }
    validate(config,value.id);
    if(value.containerfileText!==undefined) {
      if(typeof value.containerfileText!=='string'||!value.containerfileText.trim()) throw Error('Containerfile cannot be empty');
      await fs.writeFile(old.files.containerfile,value.containerfileText,{mode:0o600});
    }
    await writeJson(old.files.config,config);
    return {machine:brief(await load(value.id)),config};
  });
}
async function action(name,value={},requestSignal) {
  if(name==='status') return {schema:3,pid:process.pid,started,runtime,updatePending,stopping:closing};
  if(name==='browser-shutdown') {
    if (!browserOwner || browserOwner.pid !== value.browserOwner?.pid || browserOwner.started !== value.browserOwner?.started) throw Error('This browser does not own these containers');
    shutdownReply = true; armGuard();
    return shutdown();
  }
  if(closing) throw Error('The container service is stopping');
  if(name==='upgrade') {
    if(value.pid!==process.pid || value.started!==started) throw Error('The container service changed while preparing its update');
    updatePending = true;
    if (upgradeCheck) return upgradeCheck;
    upgradeCheck = (async () => {
      if (retiring) return {restarting:true};
      if (active.size || mutationRequests) return {restarting:false};
      // Fence even Stop during the asynchronous idle check. Calls already
      // accepted are counted above, including work queued on the catalogue.
      retiring = true;
      const signal = AbortSignal.any([AbortSignal.timeout(10000), ...(requestSignal ? [requestSignal] : [])]);
      let idle = false;
      try {
        for (const r of await records()) {
          signal.throwIfAborted();
          // Only these read-only queries are cancelled on timeout/disconnect;
          // the guest and accepted lifecycle operations are never signalled.
          const info = await inspect(r.config.id, { signal });
          const gateway = await readJson(path.join(r.files.runtime,'gateway.json'),null);
          if (info?.State?.Running || info?.State?.Paused ||
              gateway && await processStart(gateway.pid) === gateway.started ||
              mediaStatus(r.config.id).some(channel => channel.running)) return {restarting:false};
        }
        signal.throwIfAborted();
        idle = true;
        return {restarting:true};
      } catch (error) {
        // An engine/metadata error must not take away read, Stop or Cancel.
        // It prevents replacement until the old owner can prove it is idle.
        return {restarting:false,error:'Could not confirm that the container service is idle: ' + error.message};
      } finally { if (!idle) retiring = false; }
    })();
    try { return await upgradeCheck; } finally { upgradeCheck = null; }
  }
  if (updatePending && (!duringUpdate.has(name) || retiring && mutations.has(name))) throw updateError(updateMessage);
  if(name==='list') {
    const list=await records();
    // Read each real container state asynchronously. An unavailable engine remains visible.
    await Promise.all(list.filter(r=>r.state.provisioned&&!active.has(r.config.id)).map(async r=>{
      try { const live=await inspect(r.config.id); if(r.state.state!=='error')r.state.state=live?.State?.Running?'running':'stopped'; }
      catch(error){r.state.state='error';r.state.lastError=error.message;}
    }));
    return {machines:list.map(brief)};
  }
  if(name==='activations') {
    if (value.ack) {
      const record = await load(value.ack.machineId);
      const pending = await readJson(path.join(record.files.exchange,'open.json'),null);
      if(pending?.id === value.ack.id) await saveState(value.ack.machineId,{activation:pending.id});
    }
    let activations = await pendingActivations();
    if (!activations.length && value.wait && !retiring && !closing) {
      await new Promise(resolve => { const finish=()=>{clearTimeout(timer);activationWaiters.delete(finish);resolve();}; const timer=setTimeout(finish,20000);activationWaiters.add(finish); });
      activations=await pendingActivations();
    }
    return {activations};
  }
  if(name==='create') return create(value);
  if(name==='save') return edit(value);
  if(name==='startup') return catalogue(async()=>{
    const jobs=[];
    for(const r of await records()) if(r.config.autostart&&!active.has(r.config.id)) {
      try {
        const info=await inspect(r.config.id);
        // Reopening the browser must not alter an already-running definition.
        if(info?.State?.Running) continue;
        const config=await saveCapabilities({id:r.config.id,displayCapabilities:value.displayCapabilities});
        jobs.push(await begin(r.config.id,'start',job=>start(config,job)));
      } catch(error){await saveState(r.config.id,{state:'error',lastError:error.message});}
    }
    return {operations:jobs};
  });
  if(name==='media-devices') return {devices:await mediaDevices()};
  if(name==='get') {const r=await load(value.id);return {machine:brief(r),config:r.config,containerfileText:await fs.readFile(r.files.containerfile,'utf8'),media:mediaStatus(value.id)};}
  if(name==='operation') {
    const p=locations(value.id),job=await operation(value.id),cursor=Number(value.cursor||0);
    if(!Number.isSafeInteger(cursor)||cursor<0) throw Error('Invalid output cursor');
    if(!job) return {state:'none',output:'',cursor};
    const file=await fs.open(p.output,'r');
    try {
      const bytes=Buffer.alloc(65536),{bytesRead}=await file.read(bytes,0,bytes.length,cursor);
      let end=bytesRead;
      if(end===bytes.length) {
        // Keep a UTF-8 character on one page; cursor always counts raw bytes.
        let lead=end-1;while(lead>=end-4&&(bytes[lead]&0xc0)===0x80)lead--;
        const first=bytes[lead],length=(first&0xf8)===0xf0?4:(first&0xf0)===0xe0?3:(first&0xe0)===0xc0?2:1;
        if(lead+length>end)end=lead;
      }
      return {...job,output:bytes.subarray(0,end).toString(),cursor:cursor+end,hasMore:(await file.stat()).size>cursor+end};
    }finally{await file.close();}
  }
  if(name==='cancel') {active.get(value.id)?.cancel.abort(); return {ok:true};}
  const r=await load(value.id);
  if(name==='endpoint') {
    const info=await inspect(value.id);if(!info?.State?.Running)throw Error('Start this machine before connecting');
    const session=await guestCall(value.id,'local-session');
    return {...session,socketPath:path.join(r.files.exchange,'agent-https.sock'),controlSocket:path.join(r.files.exchange,'agent/control.sock'),machineId:value.id};
  }
  if(name==='application-endpoint') {
    if(value.component!=='files')throw Error('Unsupported container application');
    const info=await inspect(value.id);if(!info?.State?.Running)throw Error('Start this machine before connecting');
    // The guest backend creates this fixed private endpoint on first app use.
    // No browser-supplied address or path can broaden the machine carrier.
    const socketPath=path.join(r.files.exchange,'agent/apps.sock');
    const endpoint=await fs.stat(socketPath).catch(()=>null);
    if(!endpoint?.isSocket())throw Error('Open Files in this container before connecting its application tab');
    return {socketPath,machineId:value.id,component:'files'};
  }
  if(name==='display') {
    const info=await inspect(value.id);
    if(!info?.State?.Running)throw Error('Start this container before opening Display');
    return {kind:'container',id:value.id,name:r.config.name,machineId:value.id,presenterSocket:path.join(r.files.display,'presenter.sock'),...await displayStatus(value.id,info)};
  }
  if(name==='display-start'||name==='display-stop') return changeDisplay(value.id,name==='display-start');
  if(name==='clipboard') {
    const info=await inspect(value.id);if(!info?.State?.Running)throw Error('Start this machine before using its clipboard');
    let type=value.type || 'text/plain';
    if(!['read','write'].includes(value.action)||!['text/plain','image/png'].includes(type))throw Error('Clipboard supports text/plain and image/png read/write');
    const signal=AbortSignal.any([AbortSignal.timeout(18000),...(requestSignal?[requestSignal]:[])]);
    const args=['exec',...(value.action==='write'?['--interactive']:[]),'--user','1000:1000','--env','DISPLAY=:0','--env','XAUTHORITY=/run/user/1000/taskarian-Xauthority',info.Id,'/usr/bin/timeout','--kill-after=2','15','/usr/bin/xclip','-selection','clipboard'];
    let target=type==='image/png'?'image/png':'UTF8_STRING';
    if(value.action==='read'&&!value.type) {
      const available=(await podman([...args,'-out','-target','TARGETS'],{binary:true,signal})).stdout.toString('utf8').split(/\r?\n/);
      if(available.includes('image/png')) {type='image/png';target='image/png';}
      else if(available.includes('UTF8_STRING')) target='UTF8_STRING';
      else if(available.includes('text/plain;charset=utf-8')) target='text/plain;charset=utf-8';
      else if(available.includes('text/plain')) target='text/plain';
      else throw Error('The desktop clipboard does not contain supported text or PNG content');
    }
    let input;
    if(value.action==='write') {
      if(type==='text/plain') {if(typeof value.text!=='string')throw Error('Expected clipboard text');input=value.text;}
      else {
        if(typeof value.dataBase64!=='string'||!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value.dataBase64))throw Error('Expected PNG bytes encoded as base64');
        input=Buffer.from(value.dataBase64,'base64');
        if(!input.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])))throw Error('Clipboard image is not PNG');
      }
    }
    const result=await podman([...args,value.action==='write'?'-in':'-out','-target',target],{input,binary:true,signal});
    if(value.action==='write')return {ok:true};
    if(type==='image/png') {
      if(!result.stdout.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])))throw Error('Clipboard image is not PNG');
      return {type,dataBase64:result.stdout.toString('base64')};
    }
    return {type,text:new TextDecoder('utf-8',{fatal:true}).decode(result.stdout)};
  }
  if(name==='autostart-status'||name==='autostart-save') {
    const cancel = new AbortController();
    let completed;
    const completion = new Promise(resolve => { completed = resolve; });
    // Serialize only admission with catalogue edits. The same active slot used
    // by Start/Delete/Display protects this rootfs while guest I/O runs without
    // holding up lifecycle requests for any other machine.
    await catalogue(() => {
      if(retiring)throw updateError(updateMessage);
      if(active.has(value.id))throw Error('Wait for the current machine operation before editing startup commands');
      active.set(value.id, { cancel, completion, kind: name });
    });
    try {
      const current=await load(value.id),info=await inspect(value.id,{signal:cancel.signal});
      cancel.signal.throwIfAborted();
      const valueToSave=name==='autostart-save'?{revision:value.revision,commands:value.commands}:undefined;
      return await (info?.State?.Running ? guestCall(value.id,name,valueToSave || {}) : stoppedAutostart(current,valueToSave));
    } finally { active.delete(value.id); completed(); }
  }
  if(name==='guest-control') {
    if(typeof value.command!=='string'||!['phone-status','share-status','share-setup','share-reissue','share-confirm','share-cancel','share-export','share-publish','share-startup','share-files','localai-share','service-status','service-save','service-remove','service-action','local-session','status','start','stop',
      'provider-login-describe','provider-login-status','provider-login-callback','provider-login-cancel',
      'performance-sample','performance-close','localai-first-run','localai-guided-select','localai-custom-router','localai-model-device','localai-status','localai-record','localai-browse','localai-router-models','localai-router-model-save',
      'localai-router-model-test','localai-router-model-test-status','localai-router-model-test-cancel','localai-save','localai-model-use',
      'localai-synthesize','localai-synthesis-cancel','localai-ini','localai-check','localai-install','localai-quantize','localai-cancel',
      'localai-action','localai-refresh','native-models-catalogue','native-models-preset','native-models-status','native-models-settings',
      'native-models-search','native-models-repository','native-models-download','native-models-action','project-root'].includes(value.command))throw Error('Unsupported guest settings operation');
    return guestCall(value.id,value.command,value.value||{},value.command==='start'?180000:30000);
  }
  if(name==='start')return catalogue(async()=>{const cfg=await saveCapabilities(value);return begin(value.id,'start',job=>start(cfg,job));});
  if(name==='stop')return catalogue(()=>begin(value.id,'stop',job=>stop(value.id,job)));
  if(name==='restart')return catalogue(async()=>{const cfg=await saveCapabilities(value);return begin(value.id,'restart',async job=>{await stop(value.id,job);await start(cfg,job);});});
  if(name==='retry')return catalogue(async()=>{
    const config=await saveCapabilities(value),r=await load(value.id);
    if(active.has(value.id))throw Error('An operation is already active');
    return begin(value.id,'create',async job=>{
      if(r.state.provisioned)return start(config,job);
      await podman(['rm','--force','--ignore',containerName(value.id)],{output:job.output});
      await podman(['unshare','/usr/bin/python3',path.join(packageRoot,'libexec/remove-tree.py'),r.files.rootfs,r.files.directory],{output:job.output});
      await prepareImage(config,job);await start(config,job);
    });
  });
  if(name==='delete') return catalogue(async()=>{
    const r=await load(value.id);
    if(value.confirmName!==r.config.name)throw Error('Confirm the machine name to delete its files, installed packages and private Pi history');
    if(active.has(value.id))throw Error('Cancel or finish the active machine operation before deleting');
    r.config.autostart=false;await writeJson(r.files.config,r.config);
    return begin(value.id,'delete',job=>remove(value.id,job));
  });
  throw Error('Unknown machine action');
}
// Bind the initial browser before recovering or starting any machine. A browser
// which died while systemd was starting us must not leave an unowned service.
try {
  await adoptBrowser({ pid: Number(process.env.TASKARIAN_BROWSER_PID), started: process.env.TASKARIAN_BROWSER_STARTED });
  await prepareStorage();
} catch (error) {
  console.error('Container browser ownership:', error.message);
  try { armGuard(); } catch {}
  await shutdown().catch(failure => console.error('Container shutdown:', failure.message));
  exitOwner(1);
}
// On daemon recovery preserve partial creations/errors instead of claiming Ready.
for(const r of await records()) {
  watchActivation(r.config.id);
  const job=await operation(r.config.id);
  if(job?.state==='running') {
    await writeJson(r.files.operation,{...job,state:'failed',exitCode:1,error:'Machine service stopped during this operation; inspect and retry',finishedAt:new Date().toISOString()});
    await saveState(r.config.id,{state:'error',lastError:'Previous machine operation was interrupted'});
  }
}
server=http.createServer(async(req,res)=>{
  const request=new AbortController();
  res.once('close',()=>{if(!res.writableEnded)request.abort();});
  const name=req.url.slice(1), mutation=mutations.has(name);
  if(mutation)mutationRequests++;
  try {
    if(req.url==='/status' && req.method==='GET')return json(res,await action('status'));
    if(req.method!=='POST')throw Error('Use POST for native machine actions');
    const value=await jsonBody(req);
    if(name!=='browser-shutdown' && value.browserOwner)await adoptBrowser(value.browserOwner);
    const result=await action(name,value,request.signal);
    if(name==='browser-shutdown') {
      if(res.destroyed)exitOwner();else {res.once('finish',()=>exitOwner());res.once('close',()=>exitOwner());}
    }
    if(name==='upgrade' && result.restarting) {
      // Idle update only: flush the reply before releasing the systemd owner.
      // Browser Quit has a separate cleanup path; updates preserve busy work.
      const retire=()=>{
        if(retirementStarted)return;
        retirementStarted=true;
        for(const wake of activationWaiters)wake();
        server.close(()=>{owner.close();process.exit(0);});
      };
      if(res.destroyed)retire();else {res.once('finish',retire);res.once('close',retire);}
    }
    if(!res.destroyed)json(res,result);
  }
  catch(error){
    if(name==='browser-shutdown' && shutdownReply) {
      if(res.destroyed)exitOwner(1);else {res.once('finish',()=>exitOwner(1));res.once('close',()=>exitOwner(1));}
    }
    if(!res.destroyed)json(res,{error:error.message,code:error.code},400);
  }
  finally {if(mutation)mutationRequests--;}
});
await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(daemonSocket,resolve);});
await fs.chmod(daemonSocket,0o600);
